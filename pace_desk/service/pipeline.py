"""버그 제보 처리 흐름 (8단계).

제보 글 하나를 실험에서 고른 최종 프롬프트로 차례로 처리한다 (여러 LLM 태스크 조합, Prompt Chaining).
  ① 분류      cls_c7.md   (4단계 최종: 규칙 + 기능 목록 + 이유 먼저 쓰기)
  ② 추출      ext_e2.md   (5단계 최종: 규칙 + 예시 3개)          ┐ ①의 분류를 받아 둘을 동시에 부른다
  ③ 연결·점수  link_s3.md  (7단계 최종: Haiku가 기록부 도구로 직접 찾음) ┘
  ④ 코드 계산: 정보 완성도, 영향 범위·해결 상태·우선순위(기준표 5장), 부족한 정보 질문, 안내 문구

상태 관리: 제보마다 대화(처음 글 + 덧붙인 글)를 기억해 두었다가, 덧붙인 글이 오면 합쳐서 다시 처리한다.
"""

import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACE = HERE.parent
sys.path.insert(0, str(PACE))
sys.path.insert(0, str(PACE / "eval"))
from build_eval_set import bug_completeness, derive, load_db  # noqa: E402
from eval_cls import SCHEMA_COT as CLS_SCHEMA  # noqa: E402
from eval_ext import SCHEMA as EXT_SCHEMA, blank, norm_version  # noqa: E402
from eval_link import SCHEMA as LINK_SCHEMA  # noqa: E402
from llm import ask  # noqa: E402

PROMPTS = PACE / "prompts"
CLS_PROMPT = (PROMPTS / "cls_c7.md").read_text(encoding="utf-8")
EXT_PROMPT = (PROMPTS / "ext_e2.md").read_text(encoding="utf-8")
LINK_PROMPT = (PROMPTS / "link_s3.md").read_text(encoding="utf-8")
LINK_TOOLS = ["search_known_issues", "get_issue"]
DB, ISSUES, RELEASES = load_db()
LEVELS = ["낮음", "보통", "높음", "긴급"]


def _stat(answer, name):
    """화면과 기록에 보여 줄 호출 정보."""
    if answer is None:
        return None
    return {"step": name, "ok": answer.ok, "latency_ms": answer.latency_ms, "wall_ms": answer.wall_ms,
            "cost_usd": round(answer.cost_usd, 6), "from_cache": answer.from_cache,
            "tool_calls": answer.tool_calls or []}


def _clean_extract(data, category):
    data = data or {}
    extract = {"platform": blank(data.get("platform")), "env_detail": blank(data.get("env_detail")),
               "app_version": norm_version(data.get("app_version")), "steps": blank(data.get("steps")),
               "steps_keywords": [k for k in (data.get("steps_keywords") or []) if blank(k)]}
    if extract["platform"] not in ("Android 앱", "웹"):
        extract["platform"] = None
    if category != "버그":  # 재현 절차는 버그에만 (기준표 3장)
        extract["steps"], extract["steps_keywords"] = None, []
    return extract


def missing_questions(category, extract):
    """부족한 정보를 사용자에게 물어볼 문장으로 바꾼다 (정보 완성도에서 빠진 칸)."""
    if category == "판단 불가":
        return ["어떤 기능이나 화면에서, 무슨 일이 있었는지 조금만 더 알려 주세요."]
    if category != "버그":
        return []
    questions = []
    if not extract["platform"]:
        questions.append("앱과 웹 중 어디에서 겪으셨나요?")
    if extract["platform"] != "웹" and not extract["app_version"]:
        questions.append("앱 버전을 알려 주세요. 플레이스토어의 PACE 페이지에서 볼 수 있어요.")
    if not extract["steps"]:
        questions.append("어떻게 하면 다시 볼 수 있는지, 누른 순서대로 알려 주세요.")
    return questions


def guidance(category, issue_ids, scores, extract):
    """연결된 이슈와 해결 상태에 맞는 안내 문구."""
    if not issue_ids:
        return {"버그": "기록부에 없는 새 문제예요. 개발팀에 새 이슈로 전달할게요.",
                "개선 요청": "새로운 요청이에요. 요청 목록에 새로 올려 둘게요.",
                "문의": "기록부에 답이 없는 문의예요. 담당자가 직접 답해 드릴게요."}.get(category, "")
    main = ISSUES[issue_ids[0]]
    if main["status"] == "제한 사항":
        return f"고장이 아니라 지금 설계상 안 되는 부분이에요. {main.get('workaround') or ''}".strip()
    if scores["resolution"] == 1:
        plan = f" ({main['planned_in']}에 반영 예정)" if main.get("planned_in") else ""
        return f"이미 알고 있는 내용이에요. 지금 '{main['status']}' 상태예요{plan}."
    if scores["resolution"] == 0:
        if category == "버그" and extract["app_version"] is None:
            return "이미 고친 문제예요. 앱을 최신 버전으로 업데이트해 보시고, 그래도 생기면 앱 버전을 알려 주세요."
        return f"이미 해결된 내용이에요. {main.get('workaround') or ''}".strip()
    return "예전에 고친 문제가 다시 생겼어요(재발). 개발팀에 급히 전달할게요."


def analyze(text):
    """제보 글 하나를 끝까지 처리한다."""
    started = time.perf_counter()
    report = f"<report>{text}</report>"
    cls = ask(CLS_PROMPT, report, schema=CLS_SCHEMA)
    if not cls.ok:
        return {"ok": False, "error": f"분류 실패: {cls.error}", "calls": [_stat(cls, "분류")]}
    category, reason = cls.data["category"], cls.data.get("reason", "")
    user = f"<category>{category}</category>\n{report}"
    with ThreadPoolExecutor(max_workers=2) as pool:  # 추출과 연결은 서로 기다릴 필요가 없어 동시에 부른다
        ext_job = pool.submit(ask, EXT_PROMPT, user, schema=EXT_SCHEMA)
        link_job = (pool.submit(ask, LINK_PROMPT, user, schema=LINK_SCHEMA, tools=LINK_TOOLS)
                    if category != "판단 불가" else None)
        ext = ext_job.result()
        link = link_job.result() if link_job else None
    extract = _clean_extract(ext.data if ext.ok else None, category)

    issues, scores, completeness = [], None, None
    if category != "판단 불가" and link is not None and link.ok:
        issues = [i for i in (link.data.get("issues") or []) if i in ISSUES]
        severity = link.data.get("severity", 0) if category == "버그" else 0
        others = bool(link.data.get("others_affected"))
        derived = derive({"category": category, "link": {"issues": issues, "others_affected": others},
                          "severity": severity, "extract": extract}, DB, ISSUES, RELEASES)
        scores = {"severity": severity, "others_affected": others, **derived}
    if category == "버그":
        completeness = bug_completeness(extract)

    linked = [{"id": i, "title": ISSUES[i]["title"], "status": ISSUES[i]["status"],
               "fixed_in": ISSUES[i]["fixed_in"], "report_count": ISSUES[i]["report_count"]} for i in issues]
    calls = [c for c in (_stat(cls, "분류"), _stat(ext, "추출"), _stat(link, "연결·점수")) if c]
    return {
        "ok": True,
        "category": category, "reason": reason,
        "extract": extract,
        "linked_issues": linked,
        "scores": scores,
        "completeness": completeness,
        "missing": missing_questions(category, extract),
        "guidance": guidance(category, issues, scores, extract) if scores else "",
        "elapsed_sec": round(time.perf_counter() - started, 1),
        "cost_usd": round(sum(c["cost_usd"] for c in calls), 6),
        "calls": calls,
        "errors": [f"{c['step']} 실패" for c in calls if not c["ok"]],
    }


# ---------------------------------------------------------------- 상태 관리 (제보 대화)

_SESSIONS = {}
_LOCK = threading.Lock()


def _combined(messages):
    """처음 글과 덧붙인 글을 하나의 제보로 합친다. 시스템이 했던 답은 넣지 않는다 (필요한 맥락만)."""
    if len(messages) == 1:
        return messages[0]
    parts = [f"처음 글: {messages[0]}"] + [f"덧붙인 글 {n}: {m}" for n, m in enumerate(messages[1:], 1)]
    return "\n".join(parts)


def start_report(text):
    session_id = uuid.uuid4().hex[:10]
    result = analyze(text)
    with _LOCK:
        _SESSIONS[session_id] = {"messages": [text], "results": [result]}
    return session_id, result


def add_info(session_id, text):
    with _LOCK:
        session = _SESSIONS.get(session_id)
        if session is None:
            raise KeyError(session_id)
        session["messages"].append(text)
        messages = list(session["messages"])
    result = analyze(_combined(messages))
    with _LOCK:
        session["results"].append(result)
    return result


def get_session(session_id):
    with _LOCK:
        session = _SESSIONS.get(session_id)
        return None if session is None else {"messages": list(session["messages"]),
                                             "result": session["results"][-1]}
