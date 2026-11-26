"""PACE 제보 평가셋을 검사하고 합친다 (2단계).

- 묶음 파일(batches/*.jsonl)의 정답이 기준표(labeling_guide.md)대로 적혔는지 검사한다.
- 코드로 계산하는 정답(영향 범위·해결 상태·우선순위)을 붙인다.
- 연습용(dev)과 시험용(test)으로 나눠 pace_eval.jsonl과 pace_eval.csv(엑셀용)로 저장한다.

실행:
  묶음 하나 검사:  .venv/bin/python pace_desk/eval/build_eval_set.py --check pace_desk/eval/batches/batch_a.jsonl
  전부 합치기:     .venv/bin/python pace_desk/eval/build_eval_set.py
"""

import argparse
import csv
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = HERE.parent / "data" / "pace_db.json"
BATCH_DIR = HERE / "batches"
OUT_JSONL = HERE / "pace_eval.jsonl"
OUT_CSV = HERE / "pace_eval.csv"
TEST_SHARE = 0.4  # 40%(약 120개)는 마지막에 한 번만 보는 시험용. 판단 불가를 빼도 채점 시험이 100개를 넘게
MIN_PER_TASK = 100  # 태스크마다 연습용·시험용 각각 최소 이만큼은 있어야 한다
MIN_TOTAL = 300  # 태스크마다 전체 평가셋은 최소 이만큼 (과제 추가 조건)
SEED = 20261124

CATEGORIES = ("버그", "문의", "개선 요청", "판단 불가")
PLATFORMS = ("Android 앱", "웹")
DIFFICULTIES = ("쉬움", "보통", "어려움")
TAGS = {
    "질문형 버그", "부탁형 버그", "불평형 개선 요청", "버그처럼 보이는 문의", "키워드 함정", "여러 문제",
    "지시어만", "이전 대화 참조", "첨부 참조", "인사·감탄만", "막연함",
    "비슷한 짝", "새 문제", "제한 사항", "없는 기능",
    "옛 버전", "재발", "버전 표기 변형", "플랫폼 혼동", "정보 부족",
    "보안", "데이터 손실",
    "오타·줄임말", "긴 글", "감정 과다", "영어 섞임", "쉬운 기본",
}
ABSTAIN_TAGS = {"지시어만", "이전 대화 참조", "첨부 참조", "인사·감탄만", "막연함"}
OPEN_STATUSES = {"확인 중", "수정 중", "검토 중", "계획됨", "보류"}
ITEM_KEYS = ["id", "report", "category", "extract", "link", "severity", "completeness", "difficulty", "tags", "note"]
EXTRACT_KEYS = {"platform", "env_detail", "app_version", "steps", "steps_keywords"}
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


def load_db():
    db = json.loads(DB_PATH.read_text(encoding="utf-8"))
    return db, {i["id"]: i for i in db["issues"]}, {r["id"]: r for r in db["releases"]}


def ver(text):
    return tuple(int(part) for part in text.split("."))


def squeeze(text):
    return re.sub(r"\s+", "", text)


def bug_completeness(extract):
    """버그의 정보 완성도. 웹은 [플랫폼, 재현 절차], 앱이나 모름은 [플랫폼, 앱 버전, 재현 절차]."""
    have_platform = extract["platform"] is not None
    have_steps = extract["steps"] is not None
    if extract["platform"] == "웹":
        parts = [have_platform, have_steps]
    else:
        parts = [have_platform, extract["app_version"] is not None, have_steps]
    filled = sum(parts)
    if filled == len(parts):
        return 2
    return 1 if filled else 0


def derive(item, db, issues, releases):
    """코드가 계산하는 정답: 영향 범위, 해결 상태, 우선순위 (기준표 5.3~5.5)."""
    if item["category"] == "판단 불가":
        return {"impact": None, "resolution": None, "priority": None, "priority_label": None}
    linked = item["link"]["issues"]
    main = issues[linked[0]] if linked else None

    people = main["report_count"] + 1 if main else 1
    if item["link"]["others_affected"]:
        people = max(people, 3)
    impact = 2 if people >= 10 else 1 if people >= 3 else 0

    if main is None:
        resolution = 2  # 새 문제·새 요청·기록부에 답이 없는 문의
    elif main["status"] == "제한 사항":
        resolution = 0
    elif main["status"] in OPEN_STATUSES:
        resolution = 1
    elif item["category"] != "버그":
        resolution = 0  # 이미 고쳤거나 이미 있는 기능이라고 안내
    else:
        app_fixes = [releases[r]["version"] for r in main["fixed_in"] if releases[r]["platform"] == "Android 앱"]
        if not app_fixes:
            resolution = 2  # 웹·서버 수정은 모두에게 이미 적용됨 → 지금 생기면 재발
        else:
            version = item["extract"]["app_version"]
            if version is None:
                resolution = 0  # 버전을 모르면 업데이트 안내부터
            else:
                if version == "최신":
                    version = db["meta"]["latest"]["Android 앱"]
                resolution = 0 if ver(version) < ver(min(app_fixes, key=ver)) else 2

    total = item["severity"] + impact + resolution
    label = "긴급" if total >= 5 else "높음" if total == 4 else "보통" if total >= 2 else "낮음"
    return {"impact": impact, "resolution": resolution, "priority": total, "priority_label": label}


def validate(item, issues):
    """(오류 목록, 경고 목록). 오류가 하나라도 있으면 합치지 않는다. 경고는 사람이 한 번 더 본다."""
    errors, warnings = [], []
    missing = [k for k in ITEM_KEYS if k not in item]
    if missing:
        return [f"빠진 키 {missing}"], []
    extra = sorted(set(item) - set(ITEM_KEYS))
    if extra:
        errors.append(f"모르는 키 {extra}")

    if not re.fullmatch(r"pe-\d{3}", str(item["id"])):
        errors.append("id는 'pe-숫자3자리'")
    report = item["report"]
    if not isinstance(report, str) or len(report.strip()) < 2:
        errors.append("report가 비어 있음")
        report = ""
    category = item["category"]
    if category not in CATEGORIES:
        errors.append(f"category 값 '{category}'")

    extract = item["extract"]
    extract_ok = isinstance(extract, dict) and set(extract) == EXTRACT_KEYS
    if not extract_ok:
        errors.append(f"extract 키는 {sorted(EXTRACT_KEYS)}")
    else:
        if extract["platform"] not in (*PLATFORMS, None):
            errors.append(f"platform 값 '{extract['platform']}'")
        version = extract["app_version"]
        if version is not None and version != "최신" and not VERSION_RE.fullmatch(str(version)):
            errors.append(f"app_version 모양 '{version}' (주.부.수, '최신', null 중 하나)")
        detail = extract["env_detail"]
        if detail is not None:
            if not str(detail).strip():
                errors.append("env_detail이 빈 문자열 (없으면 null)")
            for part in str(detail).split(" · "):
                if squeeze(part) not in squeeze(report):
                    warnings.append(f"env_detail '{part}'가 글에 그대로 없음")
        keywords = extract["steps_keywords"]
        if extract["steps"] is None:
            if keywords:
                errors.append("steps가 null이면 steps_keywords는 []")
        elif not isinstance(keywords, list) or not 1 <= len(keywords) <= 3:
            errors.append("steps가 있으면 steps_keywords는 1~3개")
        else:
            for keyword in keywords:
                if squeeze(keyword) not in squeeze(report):
                    warnings.append(f"steps_keywords '{keyword}'가 글에 그대로 없음")
        if category != "버그" and extract["steps"] is not None:
            errors.append("재현 절차(steps)는 버그에만 적는다 (기준표 3장)")

    link = item["link"]
    if not isinstance(link, dict) or set(link) != {"issues", "others_affected"}:
        errors.append("link 키는 issues, others_affected")
    else:
        unknown = [i for i in link["issues"] if i not in issues]
        if unknown:
            errors.append(f"기록부에 없는 이슈 {unknown}")
        if len(set(link["issues"])) != len(link["issues"]):
            errors.append("issues에 같은 ID가 두 번")
        if not isinstance(link["others_affected"], bool):
            errors.append("others_affected는 true/false")
        if category == "판단 불가" and link["issues"]:
            errors.append("판단 불가는 issues가 []")
        known = [issues[i] for i in link["issues"] if i in issues]
        if known:
            first = known[0]
            if category == "버그" and first["type"] != "버그":
                errors.append(f"버그 글의 첫 이슈({first['id']})가 개선 요청")
            if category == "개선 요청" and first["type"] != "개선 요청" and first["status"] != "제한 사항":
                warnings.append(f"개선 요청 글의 첫 이슈({first['id']})가 버그 (제한 사항을 풀어 달라는 요청만 버그 이슈에 잇는다)")
            platform = extract["platform"] if extract_ok else None
            if platform and platform not in first["platforms"] and "전체" not in first["platforms"]:
                warnings.append(f"글의 플랫폼({platform})이 첫 이슈({first['id']}) 플랫폼 {first['platforms']}에 없음")

    severity, completeness = item["severity"], item["completeness"]
    if category == "판단 불가":
        if severity is not None or completeness is not None:
            errors.append("판단 불가는 severity·completeness가 null")
    elif category == "버그":
        if severity not in (0, 1, 2):
            errors.append("버그 severity는 0~2")
        if extract_ok and completeness != bug_completeness(extract):
            errors.append(f"버그 completeness는 계산 규칙상 {bug_completeness(extract)}인데 {completeness}로 적힘")
    elif category in ("문의", "개선 요청"):
        if severity != 0:
            errors.append(f"{category} severity는 0")
        if completeness not in (1, 2):
            errors.append(f"{category} completeness는 1 또는 2")

    if item["difficulty"] not in DIFFICULTIES:
        errors.append(f"difficulty 값 '{item['difficulty']}'")
    tags = item["tags"]
    if not isinstance(tags, list) or not tags:
        errors.append("tags는 하나 이상")
    else:
        unknown_tags = [t for t in tags if t not in TAGS]
        if unknown_tags:
            errors.append(f"목록에 없는 태그 {unknown_tags}")
        if category == "판단 불가" and not ABSTAIN_TAGS & set(tags):
            warnings.append("판단 불가인데 판단 불가 태그(지시어만 등)가 없음")
    if not isinstance(item["note"], str) or not item["note"].strip():
        errors.append("note가 비어 있음")
    return errors, warnings


def load_lines(paths):
    rows, problems = [], []
    for path in paths:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rows.append((f"{path.name}:{number}", json.loads(line)))
            except json.JSONDecodeError as exc:
                problems.append(f"{path.name}:{number}  JSON 문법 오류: {exc}")
    return rows, problems


def check_rows(rows, issues):
    """모든 줄을 검사한다. (오류 수, 경고 수)를 돌려주고 내용을 출력한다."""
    n_errors = n_warnings = 0
    seen_ids, seen_reports = {}, {}
    for where, item in rows:
        errors, warnings = validate(item, issues)
        item_id = item.get("id")
        if item_id in seen_ids:
            errors.append(f"id가 {seen_ids[item_id]}와 겹침")
        seen_ids.setdefault(item_id, where)
        key = squeeze(str(item.get("report", "")))
        if key in seen_reports:
            errors.append(f"글이 {seen_reports[key]}와 똑같음")
        seen_reports.setdefault(key, where)
        for message in errors:
            print(f"  오류  {where} {item_id}: {message}")
        for message in warnings:
            print(f"  경고  {where} {item_id}: {message}")
        n_errors += len(errors)
        n_warnings += len(warnings)
    return n_errors, n_warnings


def summarize(items, issues):
    print(f"\n문제 {len(items)}개")
    print("  종류:   " + ", ".join(f"{k} {v}" for k, v in Counter(i["category"] for i in items).most_common()))
    print("  난이도: " + ", ".join(f"{k} {v}" for k, v in Counter(i["difficulty"] for i in items).most_common()))
    print("  플랫폼: " + ", ".join(f"{k or '모름'} {v}" for k, v in
                                 Counter(i["extract"]["platform"] for i in items).most_common()))
    scored = [i for i in items if i.get("priority") is not None]
    if scored:
        print("  해결 상태: " + ", ".join(f"{k}점 {v}" for k, v in sorted(Counter(i["resolution"] for i in scored).items())))
        print("  우선순위: " + ", ".join(f"{k} {v}" for k, v in Counter(i["priority_label"] for i in scored).most_common()))
    tag_counts = Counter(t for i in items for t in i["tags"])
    print("  태그:   " + ", ".join(f"{t} {n}" for t, n in tag_counts.most_common()))
    first_links = Counter(i["link"]["issues"][0] for i in items if i["link"]["issues"])
    print(f"  연결 없음(새 문제 등): {sum(1 for i in items if not i['link']['issues'] and i['category'] != '판단 불가')}개")
    unused = [iid for iid in issues if iid not in first_links]
    print(f"  한 번도 첫 연결로 안 쓰인 이슈: {' '.join(unused) if unused else '없음'}")
    if "split" in items[0]:
        print("  나누기: " + ", ".join(f"{k} {v}" for k, v in Counter(i["split"] for i in items).most_common()))


def assign_split(items):
    """종류·난이도가 고르게 섞이도록 무리마다 40%를 시험용으로 뗀다. 씨앗이 같아 늘 같게 나뉜다."""
    rng = random.Random(SEED)
    groups = defaultdict(list)
    for item in items:
        groups[(item["category"], item["difficulty"])].append(item)
    for key in sorted(groups):
        group = sorted(groups[key], key=lambda i: i["id"])
        rng.shuffle(group)
        n_test = round(len(group) * TEST_SHARE)
        for index, item in enumerate(group):
            item["split"] = "test" if index < n_test else "dev"


def ordered(item):
    keys = ["id", "split", "report", "category", "extract", "link", "severity", "completeness",
            "impact", "resolution", "priority", "priority_label", "difficulty", "tags", "note"]
    return {k: item[k] for k in keys if k in item}


def write_csv(items):
    columns = ["split", "id", "category", "difficulty", "tags", "report", "platform", "env_detail", "app_version",
               "steps", "steps_keywords", "linked_issues", "others_affected", "severity", "completeness",
               "impact", "resolution", "priority", "priority_label", "note"]
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:  # utf-8-sig: 엑셀에서 한글이 안 깨진다
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for i in items:
            ex = i["extract"]
            writer.writerow({
                "split": i["split"], "id": i["id"], "category": i["category"], "difficulty": i["difficulty"],
                "tags": ", ".join(i["tags"]), "report": i["report"], "platform": ex["platform"] or "",
                "env_detail": ex["env_detail"] or "", "app_version": ex["app_version"] or "",
                "steps": ex["steps"] or "", "steps_keywords": ", ".join(ex["steps_keywords"]),
                "linked_issues": ", ".join(i["link"]["issues"]), "others_affected": i["link"]["others_affected"],
                "severity": "" if i["severity"] is None else i["severity"],
                "completeness": "" if i["completeness"] is None else i["completeness"],
                "impact": "" if i["impact"] is None else i["impact"],
                "resolution": "" if i["resolution"] is None else i["resolution"],
                "priority": "" if i["priority"] is None else i["priority"],
                "priority_label": i["priority_label"] or "", "note": i["note"],
            })


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", nargs="+", type=Path, help="이 묶음 파일들만 검사하고 저장하지 않는다")
    args = parser.parse_args()

    db, issues, releases = load_db()
    paths = args.check or sorted(BATCH_DIR.glob("*.jsonl"))
    if not paths:
        sys.exit(f"묶음 파일이 없어요: {BATCH_DIR}")
    rows, problems = load_lines(paths)
    for problem in problems:
        print(f"  오류  {problem}")
    print(f"검사: {', '.join(p.name for p in paths)}")
    n_errors, n_warnings = check_rows(rows, issues)
    n_errors += len(problems)
    print(f"\n오류 {n_errors}개, 경고 {n_warnings}개")

    items = [item for _, item in rows]
    clean = n_errors == 0
    if clean:
        for item in items:
            item.update(derive(item, db, issues, releases))
    summarize(items, issues) if clean else None
    if not clean:
        sys.exit(1)
    if args.check:
        return

    items.sort(key=lambda i: i["id"])
    assign_split(items)
    for split in ("dev", "test"):
        part = [i for i in items if i["split"] == split]
        scored = [i for i in part if i["category"] != "판단 불가"]
        if len(part) < MIN_PER_TASK or len(scored) < MIN_PER_TASK:
            print(f"\n주의: {split} {len(part)}개 중 점수 매기는 글 {len(scored)}개 — 태스크마다 {MIN_PER_TASK}개 이상이 되도록 늘려야 해요")
    scored_all = [i for i in items if i["category"] != "판단 불가"]
    if len(items) < MIN_TOTAL or len(scored_all) < MIN_TOTAL:
        print(f"\n주의: 전체 {len(items)}개 중 점수 매기는 글 {len(scored_all)}개 — 태스크마다 {MIN_TOTAL}개 이상이 되도록 늘려야 해요")
    OUT_JSONL.write_text("".join(json.dumps(ordered(i), ensure_ascii=False) + "\n" for i in items), encoding="utf-8")
    write_csv(items)
    summarize(items, issues)
    print(f"\n저장: {OUT_JSONL}\n저장: {OUT_CSV}")


if __name__ == "__main__":
    main()
