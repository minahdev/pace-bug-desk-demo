"""PACE 더미 DB(pace_db.json)가 규칙대로 만들어졌는지 검사한다 (1단계).

기록부(pace_db.json)를 손으로 고친 뒤에는 이 파일을 다시 돌려 앞뒤가 맞는지 확인한다.

실행: .venv/bin/python pace_desk/data/check_pace_db.py
"""

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

DB_PATH = Path(__file__).with_name("pace_db.json")
ISSUE_PLATFORMS = {"Android 앱", "웹", "전체"}
RELEASE_PREFIX = {"Android 앱": "앱", "웹": "웹", "서버": "서버"}
DONE_STATUS = {"버그": "해결됨", "개선 요청": "출시됨"}  # 이 상태일 때만 '고친 버전'이 있다


def version_key(release):
    """'1.0.2' → (1, 0, 2), '2026.08.10' → (2026, 8, 10). 버전 크기 비교용."""
    return tuple(int(part) for part in release["version"].split("."))


def check(db):
    """(검사 이름, 통과 여부) 목록을 돌려준다."""
    results = []

    def expect(name, condition, bad=None):
        results.append((name + (f"  → 문제: {bad}" if bad else ""), bool(condition)))

    today = date.fromisoformat(db["meta"]["today"])
    statuses = db["meta"]["statuses"]
    issues = {i["id"]: i for i in db["issues"]}
    releases = {r["id"]: r for r in db["releases"]}

    expect("이슈 ID가 겹치지 않고 'PACE-숫자3자리' 모양",
           len(issues) == len(db["issues"]) and all(re.fullmatch(r"PACE-\d{3}", i) for i in issues))
    expect("릴리스 ID가 겹치지 않고 '앱/웹/서버 + 버전' 모양",
           len(releases) == len(db["releases"])
           and all(r["platform"] in RELEASE_PREFIX and r["id"] == f'{RELEASE_PREFIX[r["platform"]]} {r["version"]}'
                   for r in db["releases"]))

    bad = [i["id"] for i in db["issues"] if i["type"] not in statuses or i["status"] not in statuses[i["type"]]]
    expect("이슈 종류와 상태는 meta.statuses에 적힌 값만", not bad, bad)

    bad = [i["id"] for i in db["issues"] if not i["platforms"] or not set(i["platforms"]) <= ISSUE_PLATFORMS]
    expect("이슈 플랫폼은 Android 앱·웹·전체 중에서", not bad, bad)

    bad = [i["id"] for i in db["issues"] if not i["title"] or not i["symptom"] or len(i["keywords"]) < 3]
    expect("모든 이슈에 제목·증상과 검색어 3개 이상", not bad, bad)

    bad = [i["id"] for i in db["issues"] if (i["status"] == DONE_STATUS[i["type"]]) != bool(i["fixed_in"])]
    expect("해결됨·출시됨이면 고친 버전이 있고, 그 밖의 상태면 비어 있음", not bad, bad)

    bad = []
    for i in db["issues"]:
        for rid in i["fixed_in"]:
            r = releases.get(rid)
            if r is None or i["id"] not in r["fixes"] or r["date"] < i["first_reported"]:
                bad.append(f'{i["id"]}→{rid}')
    for r in db["releases"]:
        for iid in r["fixes"]:
            if iid not in issues or r["id"] not in issues[iid]["fixed_in"]:
                bad.append(f'{r["id"]}→{iid}')
    expect("이슈의 '고친 버전'과 릴리스의 '고친 이슈'가 서로 맞고, 제보된 뒤에 고침", not bad, bad)

    bad = [i["id"] for i in db["issues"] if i["status"] == "제한 사항" and not i["workaround"]]
    expect("제한 사항에는 사용자 안내 문구가 있음", not bad, bad)

    bad = [i["id"] for i in db["issues"] if i["status"] == "계획됨" and not i["planned_in"]]
    expect("계획됨에는 예정 버전이 있음", not bad, bad)

    bad = [i["id"] for i in db["issues"] if any(r not in issues or r == i["id"] for r in i["related"])]
    expect("related(헷갈리기 쉬운 짝)는 실제로 있는 다른 이슈만", not bad, bad)

    bad = [i["id"] for i in db["issues"] if date.fromisoformat(i["first_reported"]) > today or i["report_count"] < 1]
    bad += [r["id"] for r in db["releases"] if date.fromisoformat(r["date"]) > today]
    expect("날짜는 모두 가상 세계의 오늘 이전이고 제보 수는 1 이상", not bad, bad)

    bad = []
    for platform in RELEASE_PREFIX:
        shipped = sorted((r for r in db["releases"] if r["platform"] == platform), key=lambda r: r["date"])
        if [version_key(r) for r in shipped] != sorted(version_key(r) for r in shipped):
            bad.append(f"{platform} 버전 순서")
        if shipped and db["meta"]["latest"].get(platform) != shipped[-1]["version"]:
            bad.append(f"{platform} 최신 버전")
    expect("플랫폼마다 버전이 날짜 순으로 올라가고 meta.latest가 마지막 버전과 같음", not bad, bad)

    accounts = db["accounts"]
    expect("계정 ID가 겹치지 않고 개발자 계정은 하나",
           len({a["id"] for a in accounts}) == len(accounts) and sum(a["role"] == "개발자" for a in accounts) == 1)
    return results


def main():
    db = json.loads(DB_PATH.read_text(encoding="utf-8"))
    results = check(db)
    print("검사 결과")
    for name, passed in results:
        print(f"  {'통과' if passed else '실패'}  {name}")
    failed = sum(not passed for _, passed in results)
    print(f"\n{len(results) - failed}/{len(results)}개 통과")
    if failed:
        sys.exit(1)

    issues = db["issues"]
    print(f"\n이슈 {len(issues)}개 · 릴리스 {len(db['releases'])}개 · 계정 {len(db['accounts'])}개")
    for kind in ("버그", "개선 요청"):
        counts = Counter(i["status"] for i in issues if i["type"] == kind)
        print(f"  {kind} {sum(counts.values())}개: " + ", ".join(f"{s} {n}" for s, n in counts.items()))
    from_log = sum(i["source"] != "가상" for i in issues)
    print(f"  출처: 개발 로그 {from_log}개, 가상 {len(issues) - from_log}개")

    print("\n영역별")
    by_area = defaultdict(list)
    for i in issues:
        by_area[i["area"]].append(i["id"])
    for area, ids in by_area.items():
        print(f"  {area} {len(ids)}개: {' '.join(ids)}")

    print("\n헷갈리기 쉬운 짝 (겉으로 비슷하지만 다른 이슈)")
    by_id = {i["id"]: i for i in issues}
    seen = set()
    for i in issues:
        for other in i["related"]:
            pair = tuple(sorted((i["id"], other)))
            if pair in seen:
                continue
            seen.add(pair)
            a, b = by_id[pair[0]], by_id[pair[1]]
            print(f"  {a['id']} {a['title']} [{a['status']}]")
            print(f"    ↔ {b['id']} {b['title']} [{b['status']}]")


if __name__ == "__main__":
    main()
