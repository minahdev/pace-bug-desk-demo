"""정답지 다시 확인(이중 채점) 도구 (2단계).

다른 사람(도우미)이 정답을 보지 않고 같은 글을 다시 풀게 한 뒤, 원래 정답과 다른 곳만 골라낸다.
다른 곳은 사람이 기준표를 보고 누가 맞는지 정해서 정답을 고치거나 글을 다시 쓴다.

실행:
  1) 문제지 만들기:  .venv/bin/python pace_desk/eval/qa/compare_blind.py --make-input
     → qa/blind_input.jsonl (id와 글만 들어 있다)
  2) 도우미가 qa/blind/*.jsonl 에 답을 적는다 (모양은 check_blind.py 참고). 여러 명이 나눠 풀면 파일을 나눠 적는다
  3) 비교:          .venv/bin/python pace_desk/eval/qa/compare_blind.py [답안 파일 …]
     → 항목별 일치율을 보여 주고, 다른 곳을 qa/disagreements.csv 로 저장한다
       답안 파일을 주면 그 파일만, 안 주면 qa/blind/ 의 답안 전부를 비교한다
"""

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

QA = Path(__file__).resolve().parent
sys.path.insert(0, str(QA.parent))
from build_eval_set import derive, load_db  # noqa: E402  우선순위는 정답과 같은 계산 규칙으로 구한다

GOLD = QA.parent / "pace_eval.jsonl"
BLIND_INPUT = QA / "blind_input.jsonl"
BLIND_DIR = QA / "blind"
OUT_CSV = QA / "disagreements.csv"
SCORE_FIELDS = ("심각도", "정보 완성도", "우선순위")


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def fields(item, db, issues, releases):
    """비교할 값들. 판단 불가가 아닐 때만 점수를 비교한다."""
    ex, link = item["extract"], item["link"]
    scored = item["category"] != "판단 불가"
    return {
        "분류": item["category"],
        "플랫폼": ex["platform"],
        "앱 버전": ex["app_version"],
        "재현 절차 있음": ex["steps"] is not None,
        "첫 연결 이슈": link["issues"][0] if link["issues"] else "(없음)",
        "다른 사람도": link["others_affected"],
        "심각도": item["severity"] if scored else None,
        "정보 완성도": item["completeness"] if scored else None,
        "우선순위": derive(item, db, issues, releases)["priority_label"] if scored else None,
    }


def make_input():
    gold = read_jsonl(GOLD)
    BLIND_INPUT.write_text("".join(json.dumps({"id": g["id"], "report": g["report"]}, ensure_ascii=False) + "\n"
                                   for g in gold), encoding="utf-8")
    print(f"문제지 {len(gold)}개 저장: {BLIND_INPUT}")


def compare(paths=None):
    db, issues, releases = load_db()
    gold = {g["id"]: g for g in read_jsonl(GOLD)}
    blind = {}
    for path in paths or sorted(BLIND_DIR.glob("*.jsonl")):
        for b in read_jsonl(path):
            blind[b["id"]] = b
    missing = sorted(set(gold) - set(blind))
    if paths:  # 고른 답안 파일만 비교할 때는 그 파일이 푼 문제만 본다
        gold = {k: v for k, v in gold.items() if k in blind}
        missing = []
    if missing:
        print(f"도우미 답이 없는 문제 {len(missing)}개: {' '.join(missing[:20])}{' …' if len(missing) > 20 else ''}")

    rows, agree, total, flagged = [], Counter(), Counter(), set()
    category_pairs = Counter()
    for item_id in sorted(set(gold) & set(blind)):
        g, b = fields(gold[item_id], db, issues, releases), fields(blind[item_id], db, issues, releases)
        if g["분류"] != b["분류"]:
            category_pairs[(g["분류"], b["분류"])] += 1
        for name in g:
            if name in SCORE_FIELDS and (g[name] is None or b[name] is None):
                continue  # 어느 한쪽이 판단 불가면 점수는 비교하지 않는다 (분류에서 이미 갈림)
            total[name] += 1
            if g[name] == b[name]:
                agree[name] += 1
            else:
                flagged.add(item_id)
                rows.append({"id": item_id, "항목": name, "원래 정답": g[name], "다시 푼 답": b[name],
                             "글": gold[item_id]["report"], "정답 메모": gold[item_id]["note"],
                             "다시 푼 메모": blind[item_id].get("note", ""),
                             "확신": "" if blind[item_id].get("sure", True) else "없음"})

    print(f"\n비교한 문제 {len(set(gold) & set(blind))}개 — 항목별 일치율 (원래 정답 vs 다시 푼 답)")
    for name in total:
        print(f"  {name:<8} {agree[name]}/{total[name]} = {agree[name] / total[name]:.1%}")
    if category_pairs:
        print("\n분류가 갈린 경우 (원래 정답 → 다시 푼 답)")
        for (g_cat, b_cat), n in category_pairs.most_common():
            print(f"  {g_cat} → {b_cat}: {n}")
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "항목", "원래 정답", "다시 푼 답", "글", "정답 메모",
                                               "다시 푼 메모", "확신"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n다른 곳 {len(rows)}개 ({len(flagged)}문제) 저장: {OUT_CSV}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--make-input", action="store_true", help="정답을 뺀 문제지를 만든다")
    parser.add_argument("files", nargs="*", type=Path, help="이 답안 파일만 비교한다 (안 주면 qa/blind/ 전부)")
    args = parser.parse_args()
    make_input() if args.make_input else compare(args.files)


if __name__ == "__main__":
    main()
