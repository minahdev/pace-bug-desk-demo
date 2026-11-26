"""처음부터 끝까지 시험 (9단계).

웹 서비스와 똑같은 흐름(service/pipeline.py의 analyze)으로 시험지 문제를 처리하고 정답과 비교한다.
분류 → 추출·연결·점수를 모두 Haiku가 예측한 값으로 이어서 계산하므로, 실제 서비스의 실력이다.
(앞 단계 실험은 한 태스크만 따로 재려고 정답 분류·정답 추출을 넣어 줬다.)

실행: .venv/bin/python pace_desk/eval/eval_pipeline.py --split test --final
결과: pace_desk/eval/results/pipeline/<시각>_<범위>/predictions.csv, summary.json
"""

import argparse
import csv
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACE = HERE.parent
sys.path.insert(0, str(PACE / "service"))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PACE))
from pipeline import analyze  # noqa: E402
from eval_cls import CATEGORIES, load_items  # noqa: E402
from eval_ext import norm_env  # noqa: E402

RESULTS = HERE / "results" / "pipeline"
LEVELS = ["낮음", "보통", "높음", "긴급"]
NONE = "(없음)"


def grade(item, r):
    gold = item["extract"]
    row = {"id": item["id"], "category": item["category"], "difficulty": item["difficulty"], "report": item["report"]}
    if not r.get("ok"):
        row.update(error=r.get("error", "실패"))
        return row
    e = r["extract"]
    row.update(
        error="", pred_category=r["category"], cls_ok=r["category"] == item["category"],
        platform_ok=e["platform"] == gold["platform"], version_ok=e["app_version"] == gold["app_version"],
        env_ok=norm_env(e["env_detail"]) == norm_env(gold["env_detail"]),
        steps_presence_ok=(e["steps"] is None) == (gold["steps"] is None),
        invented=sum(1 for f in ("platform", "env_detail", "app_version") if gold[f] is None and e[f] is not None),
        gold_none=sum(1 for f in ("platform", "env_detail", "app_version") if gold[f] is None),
        elapsed_sec=r["elapsed_sec"], cost_usd=r["cost_usd"],
        pred_issues=", ".join(i["id"] for i in r["linked_issues"]) or NONE,
        gold_issues=", ".join(item["link"]["issues"]) or NONE,
        pred_priority=r["scores"]["priority_label"] if r["scores"] else "", gold_priority=item["priority_label"] or "",
    )
    if item["category"] == "판단 불가":
        return row
    gold_first = item["link"]["issues"][0] if item["link"]["issues"] else NONE
    pred_first = r["linked_issues"][0]["id"] if r["linked_issues"] else NONE
    row["link_ok"] = pred_first == gold_first if r["scores"] else False
    if r["scores"]:
        distance = abs(LEVELS.index(r["scores"]["priority_label"]) - LEVELS.index(item["priority_label"]))
        row.update(priority_ok=distance == 0, priority_within1=distance <= 1)
    else:  # 점수 매길 글을 판단 불가로 잘못 보낸 경우
        row.update(priority_ok=False, priority_within1=False)
    if item["category"] == "버그":
        row["completeness_ok"] = r["completeness"] == item["completeness"]
        if r["scores"] and r["category"] == "버그":
            row["severity_ok"] = r["scores"]["severity"] == item["severity"]
    return row


def rate(rows, key):
    values = [r[key] for r in rows if key in r and not r.get("error")]
    return sum(values) / len(values) if values else None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--split", default="dev", choices=("dev", "test", "all"))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=4, help="동시에 처리할 제보 수 (제보 하나가 Haiku를 2~3번 부른다)")
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()
    if args.split != "dev" and not args.final:
        sys.exit("시험용 문제는 9단계에서 한 번만 풉니다. 정말 풀려면 --final을 붙이세요.")
    items = load_items(args.split, args.limit)
    print(f"== 처음부터 끝까지: {args.split} {len(items)}문제 (웹 서비스와 같은 흐름)")
    started = time.perf_counter()
    done = []

    def work(item):
        result = analyze(item["report"])
        done.append(1)
        if len(done) % 20 == 0 or len(done) == len(items):
            print(f"  {len(done)}/{len(items)} — {(time.perf_counter() - started) / 60:.1f}분", flush=True)
        return result

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(work, items))
    elapsed = time.perf_counter() - started
    rows = [grade(item, r) for item, r in zip(items, results)]

    scored = [r for r in rows if r["category"] != "판단 불가"]
    recall = {c: (sum(r.get("cls_ok", False) for r in rows if r["category"] == c) /
                  max(1, sum(r["category"] == c for r in rows))) for c in CATEGORIES}
    times = [r["elapsed_sec"] for r in rows if not r.get("error")]
    invented = sum(r.get("invented", 0) for r in rows)
    gold_none = sum(r.get("gold_none", 0) for r in rows)
    metrics = {
        "n": len(rows), "errors": sum(bool(r.get("error")) for r in rows),
        "cls_acc": rate(rows, "cls_ok"), "recall_by_category": recall,
        "platform_acc": rate(rows, "platform_ok"), "version_acc": rate(rows, "version_ok"),
        "env_acc": rate(rows, "env_ok"), "steps_presence_acc": rate(rows, "steps_presence_ok"),
        "invented_rate": invented / gold_none if gold_none else 0.0,
        "link_acc": rate(scored, "link_ok"), "severity_acc": rate(scored, "severity_ok"),
        "priority_acc": rate(scored, "priority_ok"), "priority_within1": rate(scored, "priority_within1"),
        "completeness_acc": rate(scored, "completeness_ok"),
        "time_p50_sec": statistics.median(times) if times else None,
        "time_p95_sec": statistics.quantiles(times, n=20)[18] if len(times) >= 2 else None,
        "cost_per_report_usd": (sum(r.get("cost_usd", 0) for r in rows) / len(rows)) if rows else None,
        "elapsed_sec": round(elapsed, 1),
    }
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = RESULTS / f"{stamp}_{args.split}{len(items)}"
    out_dir.mkdir(parents=True, exist_ok=True)
    columns = sorted({k for r in rows for k in r}, key=lambda k: (k not in ("id", "category", "pred_category"), k))
    with (out_dir / "predictions.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    (out_dir / "summary.json").write_text(json.dumps({"split": args.split, "metrics": metrics}, ensure_ascii=False,
                                                     indent=2), encoding="utf-8")
    pct = lambda v: "-" if v is None else f"{v * 100:.1f}%"
    m = metrics
    print(f"\n분류 {pct(m['cls_acc'])} (" + " · ".join(f"{c} {pct(v)}" for c, v in recall.items()) + ")")
    print(f"추출: 플랫폼 {pct(m['platform_acc'])} · 앱 버전 {pct(m['version_acc'])} · 기기 {pct(m['env_acc'])}"
          f" · 재현 절차 있음/없음 {pct(m['steps_presence_acc'])} · 지어낸 값 {pct(m['invented_rate'])}")
    print(f"연결·점수: 이슈 연결 {pct(m['link_acc'])} · 심각도 {pct(m['severity_acc'])} · 우선순위 {pct(m['priority_acc'])}"
          f" · 한 칸 안 {pct(m['priority_within1'])} · 정보 완성도(버그) {pct(m['completeness_acc'])}")
    print(f"제보 1건 처리 시간 중간값 {m['time_p50_sec']}초 · 느린 쪽 {m['time_p95_sec']}초"
          f" · 1건 비용(정가 환산) 약 ${m['cost_per_report_usd']:.4f} · 실패 {m['errors']}건 · 전체 {elapsed:.0f}초")
    print(f"저장: {out_dir}")


if __name__ == "__main__":
    main()
