"""다시 푼 답(도우미 답안)의 모양 검사 (2단계, 정답 다시 확인).

도우미가 정답을 보지 않고 푼 답(qa/blind/*.jsonl)이 기준표의 모양 규칙을 지키는지 본다.
원래 정답(pace_eval.jsonl, batches/)은 열지 않는다. 그래서 도우미가 직접 돌려도 정답이 새지 않는다.

답 한 줄의 모양:
  {"id": "pe-001", "category": "버그",
   "extract": {"platform": ..., "env_detail": ..., "app_version": ..., "steps": ..., "steps_keywords": [...]},
   "link": {"issues": [...], "others_affected": false},
   "severity": 1, "completeness": 2, "sure": true, "note": "왜 이렇게 풀었는지 한 줄"}

실행: .venv/bin/python pace_desk/eval/qa/check_blind.py pace_desk/eval/qa/blind/part_1.jsonl --ids pe-001 pe-150
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

QA = Path(__file__).resolve().parent
sys.path.insert(0, str(QA.parent))
from build_eval_set import load_db, load_lines, validate  # noqa: E402  정답과 같은 검사 규칙을 쓴다

BLIND_INPUT = QA / "blind_input.jsonl"
LABEL_KEYS = {"id", "category", "extract", "link", "severity", "completeness", "sure", "note"}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", type=Path, help="검사할 답안 파일")
    parser.add_argument("--ids", nargs=2, metavar=("FIRST", "LAST"), help="이 범위의 문제를 빠짐없이 풀었는지도 본다")
    args = parser.parse_args()

    _, issues, _ = load_db()
    reports = {}
    for line in BLIND_INPUT.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            reports[row["id"]] = row["report"]

    rows, problems = load_lines(args.files)
    for problem in problems:
        print(f"  오류  {problem}")
    n_errors, n_warnings, seen = len(problems), 0, set()
    for where, label in rows:
        errors, warnings = [], []
        label_id = label.get("id")
        if set(label) != LABEL_KEYS:
            errors.append(f"키는 {sorted(LABEL_KEYS)} 이어야 함 (지금 {sorted(label)})")
        elif label_id not in reports:
            errors.append("문제지(blind_input.jsonl)에 없는 id")
        else:
            if not isinstance(label["sure"], bool):
                errors.append("sure는 true/false")
            # 정답 검사기에 넣으려고 문제 글과 빈칸(난이도·태그)을 채운다. 이 빈칸은 비교하지 않는다.
            item = {k: v for k, v in label.items() if k != "sure"}
            item.update(report=reports[label_id], difficulty="보통",
                        tags=["막연함"] if label["category"] == "판단 불가" else ["쉬운 기본"])
            more_errors, more_warnings = validate(item, issues)
            errors += more_errors
            warnings += more_warnings
        if label_id in seen:
            errors.append("같은 id를 두 번 풂")
        seen.add(label_id)
        for message in errors:
            print(f"  오류  {where} {label_id}: {message}")
        for message in warnings:
            print(f"  경고  {where} {label_id}: {message}")
        n_errors += len(errors)
        n_warnings += len(warnings)

    if args.ids:
        first, last = args.ids
        wanted = [i for i in sorted(reports) if first <= i <= last]
        missing = [i for i in wanted if i not in seen]
        extra = sorted(i for i in seen if i and not first <= i <= last)
        print(f"\n범위 {first}~{last}: {len(wanted)}문제 중 {len(wanted) - len(missing)}개 풂")
        if missing:
            print(f"  아직 안 푼 문제 {len(missing)}개: {' '.join(missing[:30])}{' …' if len(missing) > 30 else ''}")
        if extra:
            print(f"  범위 밖 문제 {len(extra)}개: {' '.join(extra[:30])}")

    labels = [label for _, label in rows]
    print(f"\n오류 {n_errors}개, 경고 {n_warnings}개")
    print("종류: " + ", ".join(f"{k} {v}" for k, v in Counter(l.get("category") for l in labels).most_common()))
    unsure = [l.get("id") for l in labels if l.get("sure") is False]
    print(f"확신 없음(sure=false) {len(unsure)}개: {' '.join(unsure)}")
    if n_errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
