"""정답 고치기 도구 (2단계, 정답 다시 확인).

qa/label_fixes.jsonl에 적은 '고칠 점'을 묶음 파일(batches/*.jsonl)에 반영한다.
무엇을 왜 고쳤는지는 qa/label_changes.md에 남긴다. 이미 반영된 고칠 점은 건너뛰므로 여러 번 돌려도 된다.

고칠 점 한 줄의 모양:
  {"id": "pe-203", "set": {"link.issues": ["PACE-103"], "severity": 2}, "reason": "왜 고치는지"}
- set의 키에서 점(.)은 안쪽 칸을 가리킨다. 예: "extract.platform", "link.issues", "severity"
- 버그 글은 추출이 바뀌면 completeness(정보 완성도)를 계산 규칙에 맞게 자동으로 다시 맞춘다.

실행: .venv/bin/python pace_desk/eval/qa/fix_labels.py
"""

import json
import sys
from datetime import date
from pathlib import Path

QA = Path(__file__).resolve().parent
EVAL = QA.parent
sys.path.insert(0, str(EVAL))
from build_eval_set import bug_completeness  # noqa: E402  같은 계산 규칙을 쓴다

FIXES = QA / "label_fixes.jsonl"
LOG = QA / "label_changes.md"


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def get(item, field):
    for key in field.split("."):
        item = item[key]
    return item


def put(item, field, value):
    *parents, last = field.split(".")
    for key in parents:
        item = item[key]
    item[last] = value


def main():
    fixes = read_jsonl(FIXES)
    files = {path: read_jsonl(path) for path in sorted((EVAL / "batches").glob("*.jsonl"))}
    where = {item["id"]: (path, n) for path, items in files.items() for n, item in enumerate(items)}
    changes, touched = [], set()
    for fix in fixes:
        if fix["id"] not in where:
            sys.exit(f"없는 문제 id: {fix['id']}")
        path, n = where[fix["id"]]
        item = files[path][n]
        for field, value in fix["set"].items():
            before = get(item, field)
            if before != value:
                put(item, field, value)
                changes.append((fix["id"], field, before, value, fix["reason"]))
                touched.add(path)
        if item["category"] == "버그" and item["completeness"] != bug_completeness(item["extract"]):
            right = bug_completeness(item["extract"])
            changes.append((fix["id"], "completeness", item["completeness"], right, "추출이 바뀌어 계산 규칙에 맞춤"))
            item["completeness"] = right
            touched.add(path)

    for path in touched:
        path.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in files[path]), encoding="utf-8")
    if changes:
        first_time = not LOG.exists()
        with LOG.open("a", encoding="utf-8") as f:
            if first_time:
                f.write("# 정답 고친 기록\n\n| 날짜 | 문제 | 칸 | 전 | 후 | 이유 |\n|---|---|---|---|---|---|\n")
            for item_id, field, before, after, reason in changes:
                f.write(f"| {date.today()} | {item_id} | {field} | {json.dumps(before, ensure_ascii=False)} | "
                        f"{json.dumps(after, ensure_ascii=False)} | {reason} |\n")
    print(f"고친 칸 {len(changes)}개, 바뀐 파일 {len(touched)}개")
    for item_id, field, before, after, _ in changes:
        print(f"  {item_id} {field}: {json.dumps(before, ensure_ascii=False)} → {json.dumps(after, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
