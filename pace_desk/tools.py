"""기록부 도구 (6단계).

Haiku가 기록부(pace_desk/data/pace_db.json)를 찾아볼 때 쓰는 함수들이다.
- 7단계 실험과 웹 서비스가 그대로 import해서 쓴다 (코드가 미리 찾아 주는 방식).
- mcp_server.py가 같은 함수를 MCP 도구로 내놓는다 (Haiku가 직접 부르는 방식, Function calling).

도구 세 가지:
  search_known_issues(query, platform=None, limit=5)  제보 내용과 비슷한 알려진 이슈 찾기
  get_issue(issue_id)                                 이슈 하나의 자세한 정보 (상태, 고친 버전, 우회 방법 등)
  get_releases(platform=None)                         버전 기록 (어느 버전에서 무엇을 고쳤나)

시험: .venv/bin/python pace_desk/tools.py   (연습용 문제로 검색이 정답 이슈를 얼마나 찾는지 잰다)
"""

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = json.loads((HERE / "data" / "pace_db.json").read_text(encoding="utf-8"))
ISSUES = {issue["id"]: issue for issue in DB["issues"]}
RELEASES = {release["id"]: release for release in DB["releases"]}
PLATFORMS = ("Android 앱", "웹")


def _squeeze(text):
    return re.sub(r"\s+", "", str(text)).lower()


def _grams(text):
    """글자 두 개씩 끊은 조각들. 한국어는 띄어쓰기·조사가 달라도 조각이 겹치면 비슷한 글로 본다."""
    squeezed = _squeeze(text)
    return {squeezed[i:i + 2] for i in range(len(squeezed) - 1)}


ISSUE_GRAMS = {iid: _grams(" ".join([i["title"], i["symptom"], " ".join(i["keywords"])])) for iid, i in ISSUES.items()}


def _brief(issue, score=None):
    brief = {
        "id": issue["id"], "type": issue["type"], "title": issue["title"], "platforms": issue["platforms"],
        "status": issue["status"], "fixed_in": issue["fixed_in"], "report_count": issue["report_count"],
    }
    if score is not None:
        brief["score"] = round(score, 3)
    return brief


def search_known_issues(query, platform=None, limit=5):
    """제보 내용(query)과 비슷한 알려진 이슈를 점수 순으로 돌려준다.

    platform을 주면("Android 앱" 또는 "웹") 그 플랫폼에 해당하는 이슈만 본다. 둘 다에 해당하는 이슈도 포함된다.
    점수: 제보 글의 글자 조각이 이슈 제목·증상·키워드에 얼마나 들어 있나 + 이슈 키워드가 글에 그대로 나오면 덤.
    """
    if platform not in (None, *PLATFORMS):
        platform = None  # 모르는 값이면 거르지 않는다
    query_grams = _grams(query)
    squeezed = _squeeze(query)
    scored = []
    for iid, issue in ISSUES.items():
        if platform and platform not in issue["platforms"] and "전체" not in issue["platforms"]:
            continue
        overlap = len(query_grams & ISSUE_GRAMS[iid]) / (len(query_grams) or 1)
        keyword_hits = sum(1 for k in issue["keywords"] if len(_squeeze(k)) >= 2 and _squeeze(k) in squeezed)
        scored.append((overlap + 0.08 * keyword_hits, issue))
    scored.sort(key=lambda pair: -pair[0])
    return [_brief(issue, score) for score, issue in scored[: max(1, min(int(limit), 10))]]


def get_issue(issue_id):
    """이슈 하나의 자세한 정보. 고친 버전은 릴리스 정보(플랫폼·버전·날짜)를 붙여 준다."""
    issue = ISSUES.get(str(issue_id).strip().upper())
    if issue is None:
        return {"error": f"기록부에 없는 이슈입니다: {issue_id}"}
    detail = {k: issue[k] for k in ("id", "type", "area", "platforms", "title", "symptom", "status",
                                    "planned_in", "workaround", "first_reported", "report_count", "related")}
    detail["fixed_in"] = [{"release": r, "platform": RELEASES[r]["platform"], "version": RELEASES[r]["version"],
                           "date": RELEASES[r]["date"]} for r in issue["fixed_in"]]
    return detail


def get_releases(platform=None):
    """버전 기록. platform을 주면("Android 앱", "웹", "서버") 그 플랫폼 것만 준다."""
    releases = [r for r in DB["releases"] if platform in (None, r["platform"])]
    return {"latest": DB["meta"]["latest"],
            "releases": [{"id": r["id"], "platform": r["platform"], "version": r["version"], "date": r["date"],
                          "fixes": r.get("fixes", [])} for r in sorted(releases, key=lambda r: r["date"])]}


CALLS = {"search_known_issues": search_known_issues, "get_issue": get_issue, "get_releases": get_releases}


def call(name, arguments):
    """도구 이름과 인자로 함수를 부른다 (MCP 서버가 쓴다)."""
    if name not in CALLS:
        raise ValueError(f"모르는 도구: {name}")
    return CALLS[name](**(arguments or {}))


if __name__ == "__main__":
    # 시험: 연습용(dev) 문제 중 정답 이슈가 있는 글로, 검색이 정답 이슈를 몇 등 안에 찾는지 잰다
    items = [json.loads(line) for line in (HERE / "eval" / "pace_eval.jsonl").read_text(encoding="utf-8").splitlines()]
    linked = [i for i in items if i["split"] == "dev" and i["link"]["issues"]]
    for use_platform in (False, True):
        hits = {1: 0, 3: 0, 5: 0}
        for item in linked:
            found = [r["id"] for r in search_known_issues(item["report"],
                                                          item["extract"]["platform"] if use_platform else None, 5)]
            gold = item["link"]["issues"][0]
            for k in hits:
                hits[k] += gold in found[:k]
        label = "플랫폼으로 거름" if use_platform else "플랫폼 안 거름"
        print(f"[{label}] 정답 이슈가 있는 연습 문제 {len(linked)}개 — "
              + " · ".join(f"{k}등 안 {hits[k] / len(linked):.1%}" for k in hits))
