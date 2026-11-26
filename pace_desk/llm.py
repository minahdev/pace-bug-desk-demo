"""Claude Haiku 5.5를 부르는 전화기 (3단계).

모든 실험과 서비스는 이 파일의 ask()·ask_many()로만 Haiku를 부른다.

부르는 방법은 Claude Code 헤드리스 모드(`claude -p`)다.
- API 키가 필요 없다. 대신 쓰는 사람의 Claude 요금제 사용량에서 빠진다.
  API 크레딧이 얼마 남지 않아 2026-11-25에 이 방법으로 바꿨다(CLAUDE.md).
- 모델은 claude-haiku-5-5로 고정한다.

헤드리스 모드에서 알아 둘 점 (2026-11-25에 직접 재 봄):
- Claude Code를 켜고 끄는 데 한 번에 약 4초가 더 든다. 그래서 여러 개를 동시에 부른다(ask_many).
- 개인 설정(CLAUDE.md·플러그인·훅·도구)은 다 끈다. 그래도 Claude Code가 약 650토큰을 덧붙인다
  (작업 폴더·날짜 같은 환경 안내와 계정 안내).
  - 모든 호출에 똑같이 붙으므로 실험끼리 비교하는 데는 지장이 없다.
  - 다만 토큰·비용 기록에는 들어간다.
- 구조화 출력(JSON Schema)은 Claude Code가 '도구 호출'로 처리해서 한 번 더 왕복한다(turns 2).
- 응답 시간(latency_ms)은 Claude Code 준비 시간을 뺀 API 시간이다. 준비 시간까지 넣은 값은 wall_ms다.
- 비용(cost_usd)은 API 정가로 환산한 값이다. 실제로는 요금제 사용량에서 빠진다.

같은 질문의 답은 llm_cache/에 저장해 두고 다시 쓴다.
- 사용량을 아낀다.
- 한도에 걸려 멈춰도 다시 돌리면 받은 답부터 이어서 한다.

시험: .venv/bin/python pace_desk/llm.py
"""

import atexit
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

MODEL = "claude-haiku-5-5"  # CLAUDE.md 규칙: 다른 모델로 바꾸지 않는다
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "low"  # 수업 연습 코드(llm_cls_v3.py)와 같은 기본값. 실험에서 바꿔 본다
TIMEOUT_SEC = 180
RETRY_WAITS = (10, 30, 60)  # 잠깐 막혔을 때 기다렸다가 다시 부르는 간격(초)
CACHE_VERSION = 1  # 부르는 방식을 바꾸면 올린다. 그러면 예전에 저장한 답을 쓰지 않는다

HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE / "llm_cache"
# 아무 파일도 없는 빈 폴더에서 실행한다. 프로젝트의 CLAUDE.md·메모리가 끼어들지 않게 하려는 것이다
EMPTY_DIR = Path(tempfile.mkdtemp(prefix="pace_headless_"))
atexit.register(shutil.rmtree, EMPTY_DIR, True)
# API 키가 환경에 있으면 Claude Code가 그 키로 부를 수 있다. 요금제로만 부르도록 빼고 실행한다
CHILD_ENV = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY")}
# 기록부 도구 서버 (6단계, Function calling). tools=[...]를 주면 Haiku가 이 도구들을 부를 수 있다
MCP_CONFIG = {"mcpServers": {"pace": {"command": sys.executable, "args": [str(HERE / "mcp_server.py")]}}}
TOOL_NAMES = ("search_known_issues", "get_issue", "get_releases")
# 잠깐 막힌 것(과부하·일시 오류·형식 실패)이라 다시 부르면 될 수 있는 오류의 표시
TRANSIENT_MARKS = ("429", "500", "502", "503", "504", "529", "overloaded", "rate", "timed out",
                   "초 안에 답이 없음", "결과를 읽지 못함", "구조화 출력", "structured_output",
                   "error_during_execution")


class UsageLimitError(RuntimeError):
    """요금제 사용 한도에 걸렸다. 한도가 풀린 뒤 다시 돌리면 저장된 답부터 이어진다."""


@dataclass
class Answer:
    """한 번 물어본 결과."""

    ok: bool
    text: str  # 모델이 쓴 답. 구조화 출력이면 JSON 글
    data: dict | None  # 구조화 출력(JSON Schema)으로 받은 값. 안 썼거나 실패하면 None
    error: str | None
    latency_ms: int  # API 응답 시간 (Claude Code 준비 시간을 뺌)
    wall_ms: int  # 실제로 기다린 시간 (준비 시간 포함)
    input_tokens: int
    cache_write_tokens: int  # 이번에 캐시에 새로 적은 토큰
    cache_read_tokens: int  # 캐시에서 다시 읽은 토큰 (싸고 빠르다)
    output_tokens: int
    thinking_tokens: int
    cost_usd: float  # API 정가로 환산
    turns: int
    attempts: int  # 몇 번 만에 받았나 (다시 부른 횟수 포함)
    from_cache: bool = False  # llm_cache/에 저장돼 있던 답을 다시 쓴 것인가
    tool_calls: list | None = None  # 도구를 주었을 때 Haiku가 부른 도구와 값 [{"name": ..., "input": {...}}, ...]


def _command(system, schema, effort, tools=None):
    cmd = [
        "claude", "-p",
        "--model", MODEL,
        "--system-prompt", system,  # 시스템 프롬프트를 통째로 우리 것으로 바꾼다 (Claude Code 기본 설명서 대신)
        "--effort", effort,
        "--tools", "",  # 파일·명령 도구를 하나도 주지 않는다. 글만 주고받는다
        "--output-format", "json",  # 답과 함께 시간·토큰·비용을 받는다
        "--safe-mode",  # CLAUDE.md·플러그인·훅 같은 개인 설정을 끈다
        "--no-session-persistence",  # 대화를 디스크에 남기지 않는다
        "--strict-mcp-config",  # 따로 준 MCP 서버만 쓴다 (지금은 없음)
        "--disable-slash-commands",
    ]
    if schema is not None:
        cmd += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
    if tools:
        # 우리 도구 서버만 켜고(--strict-mcp-config), 고른 도구만 허락한다. 어떤 도구를 불렀는지 보려고 사건을 한 줄씩 받는다
        # safe-mode는 MCP 서버까지 꺼 버린다. 그래서 도구를 쓸 때는 safe-mode 대신 개인 설정을 안 읽게 해서
        # (--setting-sources project, 빈 폴더라 실제로 읽을 설정이 없다) 개인 플러그인·훅이 끼어들지 않게 한다
        cmd.remove("--safe-mode")
        cmd[cmd.index("--output-format") + 1] = "stream-json"
        cmd += ["--setting-sources", "project",
                "--verbose", "--mcp-config", json.dumps(MCP_CONFIG, ensure_ascii=False),
                "--allowedTools", ",".join(f"mcp__pace__{name}" for name in tools)]
    return cmd


def _cache_path(system, user, schema, effort, repeat, tools=None):
    key = {"v": CACHE_VERSION, "model": MODEL, "system": system, "user": user,
           "schema": schema, "effort": effort, "repeat": repeat}
    if tools:  # 도구를 줄 때만 넣는다 (예전에 저장한 답의 이름표가 바뀌지 않게)
        key["tools"] = sorted(tools)
    raw = json.dumps(key, ensure_ascii=False, sort_keys=True)
    key = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return CACHE_DIR / key[:2] / f"{key}.json"


def _failed(message, started, attempts=1):
    wall_ms = int((time.perf_counter() - started) * 1000)
    return Answer(ok=False, text="", data=None, error=message, latency_ms=0, wall_ms=wall_ms,
                  input_tokens=0, cache_write_tokens=0, cache_read_tokens=0, output_tokens=0,
                  thinking_tokens=0, cost_usd=0.0, turns=0, attempts=attempts)


def _is_usage_limit(message):
    m = message.lower()
    return "limit" in m and any(word in m for word in ("hit your", "resets", "spend limit", "usage limit"))


def _read_stream(stdout):
    """stream-json 출력에서 마지막 결과와, Haiku가 부른 우리 도구 목록을 꺼낸다."""
    out, calls = None, []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use" and str(block.get("name", "")).startswith("mcp__pace__"):
                    calls.append({"name": block["name"].removeprefix("mcp__pace__"), "input": block.get("input")})
        elif event.get("type") == "result":
            out = event
    if out is None:
        raise json.JSONDecodeError("결과 줄이 없음", stdout[:200], 0)
    return out, calls


def _call_once(system, user, schema, effort, tools=None):
    started = time.perf_counter()
    try:
        proc = subprocess.run(_command(system, schema, effort, tools), input=user, capture_output=True,
                              text=True, cwd=EMPTY_DIR, env=CHILD_ENV, timeout=TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return _failed(f"{TIMEOUT_SEC}초 안에 답이 없음", started)
    wall_ms = int((time.perf_counter() - started) * 1000)
    calls = None
    try:
        if tools:
            out, calls = _read_stream(proc.stdout)
        else:
            out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        message = (proc.stderr or proc.stdout or "").strip()[:300]
        if _is_usage_limit(message):
            raise UsageLimitError(message)
        return _failed(f"결과를 읽지 못함: {message}", started)

    error = None
    if out.get("is_error") or out.get("subtype") != "success":
        error = " ".join(str(part) for part in (out.get("subtype"), out.get("api_error_status"),
                                                 str(out.get("result") or "")[:300]) if part)
        if _is_usage_limit(error):
            raise UsageLimitError(error)
    data = out.get("structured_output")
    if schema is not None and data is None and error is None:
        error = "구조화 출력이 비어 있음"
    usage = out.get("usage") or {}
    return Answer(
        ok=error is None, text=out.get("result") or "", data=data, error=error,
        latency_ms=int(out.get("duration_api_ms") or 0), wall_ms=wall_ms,
        input_tokens=usage.get("input_tokens", 0),
        cache_write_tokens=usage.get("cache_creation_input_tokens", 0),
        cache_read_tokens=usage.get("cache_read_input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        thinking_tokens=(usage.get("output_tokens_details") or {}).get("thinking_tokens", 0),
        cost_usd=float(out.get("total_cost_usd") or 0.0), turns=int(out.get("num_turns") or 0),
        attempts=1, tool_calls=calls,
    )


def cached(system, user, *, schema=None, effort=DEFAULT_EFFORT, repeat=0, tools=None):
    """저장된 답이 있으면 돌려주고, 없으면 None. Haiku를 부르지 않는다."""
    path = _cache_path(system, user, schema, effort, repeat, tools)
    if not path.exists():
        return None
    answer = Answer(**json.loads(path.read_text(encoding="utf-8")))
    answer.from_cache = True
    return answer


def ask(system, user, *, schema=None, effort=DEFAULT_EFFORT, repeat=0, use_cache=True, tools=None):
    """Haiku에게 한 번 묻는다.

    system: 시스템 프롬프트 (역할·규칙·예시처럼 매번 같은 내용)
    user:   이번에 처리할 글 (매번 바뀌는 내용)
    schema: 답의 모양(JSON Schema). 주면 그 모양대로 받아 Answer.data에 넣는다
    effort: 얼마나 깊이 생각할지 (low, medium, high, xhigh, max)
    repeat: 같은 질문을 여러 번 물어 답이 흔들리는지 볼 때 0, 1, 2…로 바꾼다. 번호마다 따로 저장한다
    tools:  Haiku가 부를 수 있는 기록부 도구 이름 목록 (예: ["search_known_issues", "get_issue"]). 6단계 Function calling
    """
    if effort not in EFFORTS:
        raise ValueError(f"effort는 {EFFORTS} 중 하나: {effort}")
    if tools and not set(tools) <= set(TOOL_NAMES):
        raise ValueError(f"도구는 {TOOL_NAMES} 중에서: {tools}")
    if use_cache:
        answer = cached(system, user, schema=schema, effort=effort, repeat=repeat, tools=tools)
        if answer is not None:
            return answer
    answer = None
    for attempt, wait in enumerate((0, *RETRY_WAITS), 1):
        if wait:
            time.sleep(wait)
        answer = _call_once(system, user, schema, effort, tools)
        answer.attempts = attempt
        if answer.ok or not any(mark in (answer.error or "") for mark in TRANSIENT_MARKS):
            break
    if answer.ok and use_cache:
        path = _cache_path(system, user, schema, effort, repeat, tools)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(f".{threading.get_ident()}.tmp")
        temp.write_text(json.dumps(asdict(answer), ensure_ascii=False), encoding="utf-8")
        temp.replace(path)  # 다 쓴 뒤에 이름을 바꿔서, 쓰다 만 파일이 남지 않게 한다
    return answer


def ask_many(jobs, *, workers=6, budget_usd=None, label="LLM"):
    """여러 질문을 동시에 묻는다.

    jobs: ask()에 넘길 값들의 목록. 예: [{"system": ..., "user": ..., "schema": ...}, ...]
    workers: 동시에 몇 개를 부를지. Claude Code 준비 시간(약 4초)을 겹쳐서 기다림을 줄인다
    budget_usd: 새로 부른 호출의 비용(API 정가 환산) 합이 이 값을 넘으면 더 부르지 않는다
    돌려주는 값: jobs와 같은 순서의 Answer 목록. 한도·예산 때문에 못 물은 자리는 None이다
    """
    results = [None] * len(jobs)
    stop, lock = threading.Event(), threading.Lock()
    reasons = []
    tally = {"done": 0, "cached": 0, "new": 0, "failed": 0, "cost": 0.0}
    started = time.perf_counter()

    def work(index, job):
        answer = cached(**{k: v for k, v in job.items() if k != "use_cache"})
        if answer is None:
            if stop.is_set():
                return
            try:
                answer = ask(**job)
            except UsageLimitError as exc:
                with lock:
                    if not stop.is_set():
                        reasons.append(f"요금제 사용 한도에 걸림 ({str(exc)[:120]})")
                    stop.set()
                return
        results[index] = answer
        with lock:
            tally["done"] += 1
            tally["cached" if answer.from_cache else "new"] += 1
            tally["failed"] += not answer.ok
            if not answer.from_cache:
                tally["cost"] += answer.cost_usd
            if budget_usd is not None and tally["cost"] > budget_usd and not stop.is_set():
                reasons.append(f"예산 ${budget_usd}를 넘음")
                stop.set()
            if tally["done"] % 20 == 0 or tally["done"] == len(jobs):
                minutes = (time.perf_counter() - started) / 60
                print(f"[{label}] {tally['done']}/{len(jobs)} — 저장된 답 {tally['cached']}, 새로 부름 {tally['new']}, "
                      f"실패 {tally['failed']}, 비용(정가 환산) ${tally['cost']:.4f}, {minutes:.1f}분", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(work, i, job) for i, job in enumerate(jobs)]:
            future.result()
    if stop.is_set():
        missing = sum(r is None for r in results)
        print(f"[{label}] 멈춤: {reasons[0]}. 못 물은 {missing}개는 None이다. "
              f"받은 답은 저장돼 있어서, 다시 돌리면 이어서 한다.", flush=True)
    return results


def summarize(answers):
    """여러 답의 요약: 성공·실패 수, 응답 시간, 토큰, 비용."""
    got = [a for a in answers if a is not None]
    fresh = [a for a in got if not a.from_cache]
    latencies = sorted(a.latency_ms for a in got if a.ok)

    def mean(values):
        values = list(values)
        return round(sum(values) / len(values), 1) if values else 0

    return {
        "total": len(answers),
        "ok": sum(a.ok for a in got),
        "failed": sum(not a.ok for a in got),
        "missing": len(answers) - len(got),
        "retried": sum(a.attempts > 1 for a in got),
        "from_cache": len(got) - len(fresh),
        "new_calls": len(fresh),
        "latency_ms_p50": int(statistics.median(latencies)) if latencies else 0,
        "latency_ms_p95": int(statistics.quantiles(latencies, n=20)[18]) if len(latencies) >= 2 else
                          (latencies[0] if latencies else 0),
        "input_tokens_avg": mean(a.input_tokens + a.cache_write_tokens + a.cache_read_tokens for a in got),
        "cache_read_tokens_avg": mean(a.cache_read_tokens for a in got),
        "output_tokens_avg": mean(a.output_tokens for a in got),
        "thinking_tokens_avg": mean(a.thinking_tokens for a in got),  # 출력 중 속으로 생각한 부분
        "cost_usd": round(sum(a.cost_usd for a in got), 6),  # 저장된 답도 처음 받을 때 든 비용으로 센다
        "cost_usd_new": round(sum(a.cost_usd for a in fresh), 6),  # 이번에 새로 쓴 비용
    }


if __name__ == "__main__":
    # 시험: 평가셋 연습용(dev) 문제 4개를 아주 짧은 설명서로 분류해 본다.
    # 두 번 돌려서, 두 번째에는 Haiku를 부르지 않고 저장된 답을 쓰는지 본다. 시험용(test) 문제는 쓰지 않는다.
    items = [json.loads(line) for line in (HERE / "eval" / "pace_eval.jsonl").read_text(encoding="utf-8").splitlines()]
    dev = [item for item in items if item["split"] == "dev"][:4]
    system = "PACE 앱 사용자가 보낸 글을 읽고 버그, 문의, 개선 요청, 판단 불가 중 하나로 분류한다."
    schema = {"type": "object", "additionalProperties": False, "required": ["category"],
              "properties": {"category": {"type": "string", "enum": ["버그", "문의", "개선 요청", "판단 불가"]}}}
    jobs = [{"system": system, "user": f"<report>{item['report']}</report>", "schema": schema} for item in dev]
    for round_no in (1, 2):
        print(f"\n== {round_no}번째로 돌리기 ==")
        started = time.perf_counter()
        answers = ask_many(jobs, workers=4, label=f"시험 {round_no}")
        print(f"전체 {time.perf_counter() - started:.1f}초")
        for item, answer in zip(dev, answers):
            if answer is None:
                print(f"  {item['id']}: 못 물음")
                continue
            got = answer.data["category"] if answer.ok else f"실패: {answer.error}"
            print(f"  {item['id']} 정답 {item['category']} / 답 {got} | API {answer.latency_ms}ms, "
                  f"기다림 {answer.wall_ms}ms, 저장된 답 {'예' if answer.from_cache else '아니오'}")
        print("  요약:", summarize(answers))
    plain = ask("짧게 답한다.", "'준비 완료'라고만 답해 주세요.")
    print(f"\n글로만 받기: {plain.text!r} (ok={plain.ok}, 저장된 답 {'예' if plain.from_cache else '아니오'})")
