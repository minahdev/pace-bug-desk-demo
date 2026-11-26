"""진짜 리뷰로 분류 확인 (4단계 마무리).

가민 커넥트·화웨이 헬스·삼성 헬스의 실제 구글 플레이 리뷰(영어·프랑스어, Zenodo 11066414)를
최종 분류 프롬프트(7회차)로 분류하고, 사람이 붙인 정답과 비교한다.
- 리뷰는 PACE가 아닌 다른 앱 이야기다. 그래서 7회차 프롬프트에서 PACE 기능 목록만 일반 안내로 바꾼
  cls_c7_real.md를 쓴다.
- 정답 맞추기: 버그 → 문제 보고, 개선 요청 → 기능 요청, 문의·판단 불가 → 무관.
  정답이 '둘 다'(문제 보고 + 기능 요청)인 리뷰는 둘 중 하나면 맞은 것으로 센다.
- 이 시험지로는 프롬프트를 고치지 않는다(확인만). 시험용(test)은 9단계에서 한 번만 쓴다.

실행 (프로젝트 폴더에서):
  점검:    .venv/bin/python pace_desk/eval/eval_real.py --limit 12
  연습용:  .venv/bin/python pace_desk/eval/eval_real.py
결과: pace_desk/eval/results/real/history.csv, pace_desk/eval/results/real/<시각>_<이름>/
"""

import argparse
import csv
import json
import random
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
from eval_cls import PROMPTS, SCHEMAS, predict  # noqa: E402  분류 실험과 같은 답 모양·답 읽기를 쓴다
from llm import ask_many, summarize  # noqa: E402

REAL_SET = HERE / "real" / "real_reviews_eval.jsonl"
RESULTS = HERE / "results" / "real"
HISTORY = RESULTS / "history.csv"
TO_REAL = {"버그": "문제 보고", "개선 요청": "기능 요청", "문의": "무관", "판단 불가": "무관"}
GOLD_KINDS = ("문제 보고", "기능 요청", "무관", "둘 다")
SAMPLE_SEED = 20261125
HISTORY_COLUMNS = ["timestamp", "name", "prompt", "split", "n_items", "accuracy",
                   *(f"recall_{k}" for k in GOLD_KINDS), "acc_en", "acc_fr", "format_errors",
                   "latency_p50_ms", "cost_usd", "output_dir"]


def gold_kind(labels):
    return "둘 다" if len(labels) > 1 else labels[0]


def load(split, limit):
    rows = [json.loads(line) for line in REAL_SET.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = [r for r in rows if r["split"] == split]
    if limit and limit < len(rows):
        rng = random.Random(SAMPLE_SEED)
        groups = defaultdict(list)
        for r in rows:
            groups[gold_kind(r["gold_labels"])].append(r)
        picked = []
        for kind in GOLD_KINDS:  # 정답 종류마다 고르게 뽑는다
            group = groups[kind]
            rng.shuffle(group)
            picked += group[: max(1, round(limit * len(group) / len(rows)))]
        rows = picked[:limit]
    return sorted(rows, key=lambda r: r["id"])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prompt", default="cls_c7_real.md", help="pace_desk/prompts 안의 프롬프트 파일")
    parser.add_argument("--output", default="json_cot", choices=sorted(SCHEMAS), help="답 모양")
    parser.add_argument("--effort", default="low")
    parser.add_argument("--name", default="7회차-실제리뷰", help="결과표에 적을 이름")
    parser.add_argument("--split", default="dev", choices=("dev", "test"))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--final", action="store_true", help="시험용(test)을 풀 때 꼭 붙인다. 9단계에서 한 번만")
    args = parser.parse_args()
    if args.split == "test" and not args.final:
        sys.exit("시험용 리뷰는 9단계에서 한 번만 풉니다. 정말 풀려면 --final을 붙이세요.")

    system = (PROMPTS / args.prompt).read_text(encoding="utf-8")
    rows = load(args.split, args.limit)
    jobs = [{"system": system, "user": f"<report>{r['report']}</report>", "schema": SCHEMAS[args.output],
             "effort": args.effort} for r in rows]
    print(f"== {args.name}: 진짜 리뷰 {args.split} {len(rows)}개")
    started = time.perf_counter()
    answers = ask_many(jobs, workers=args.workers, label=args.name)
    elapsed = time.perf_counter() - started

    out_rows = []
    for r, answer in zip(rows, answers):
        pred, format_ok = predict(answer, args.output)
        mapped = TO_REAL.get(pred, pred)
        out_rows.append({
            "id": r["id"], "app": r["app"], "lang": r["lang"], "stars": r["stars"],
            "gold": " + ".join(r["gold_labels"]), "gold_kind": gold_kind(r["gold_labels"]),
            "pred": pred, "pred_mapped": mapped, "correct": mapped in r["gold_labels"], "format_ok": format_ok,
            "latency_ms": answer.latency_ms if answer else "", "report": r["report"],
            "raw": answer.text[:600] if answer else "",
        })

    def acc(group):
        return sum(x["correct"] for x in group) / len(group) if group else None

    recall = {k: acc([x for x in out_rows if x["gold_kind"] == k]) for k in GOLD_KINDS}
    by_lang = {lang: acc([x for x in out_rows if x["lang"] == lang]) for lang in ("en", "fr")}
    by_app = {app: acc([x for x in out_rows if x["app"] == app]) for app in sorted({x["app"] for x in out_rows})}
    confusion = Counter((x["gold_kind"], x["pred"]) for x in out_rows)
    usage = summarize(answers)
    metrics = {"n": len(out_rows), "accuracy": acc(out_rows), "recall_by_gold": recall, "by_lang": by_lang,
               "by_app": by_app, "format_errors": sum(not x["format_ok"] for x in out_rows),
               "confusion": {f"{g} → {p}": n for (g, p), n in sorted(confusion.items())}}

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = RESULTS / f"{stamp}_{args.name}_{args.split}{len(rows)}"
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "predictions.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(out_rows[0]))
        writer.writeheader()
        writer.writerows(out_rows)
    (out_dir / "summary.json").write_text(json.dumps({"prompt": args.prompt, "metrics": metrics, "usage": usage},
                                                     ensure_ascii=False, indent=2), encoding="utf-8")
    new = not HISTORY.exists()
    with HISTORY.open("w" if new else "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_COLUMNS)
        if new:
            writer.writeheader()
        writer.writerow({
            "timestamp": stamp, "name": args.name, "prompt": args.prompt, "split": args.split,
            "n_items": len(rows), "accuracy": round(metrics["accuracy"], 4),
            **{f"recall_{k}": "" if recall[k] is None else round(recall[k], 4) for k in GOLD_KINDS},
            "acc_en": round(by_lang["en"], 4) if by_lang["en"] is not None else "",
            "acc_fr": round(by_lang["fr"], 4) if by_lang["fr"] is not None else "",
            "format_errors": metrics["format_errors"], "latency_p50_ms": usage["latency_ms_p50"],
            "cost_usd": usage["cost_usd"], "output_dir": str(out_dir.relative_to(HERE.parent)),
        })

    pct = lambda v: "-" if v is None else f"{v * 100:.1f}%"
    print(f"\n정확도 {pct(metrics['accuracy'])} ({sum(x['correct'] for x in out_rows)}/{len(out_rows)})"
          f" | 형식 오류 {metrics['format_errors']}개")
    print("정답 종류별: " + " · ".join(f"{k} {pct(v)}" for k, v in recall.items()))
    print("언어별: " + " · ".join(f"{k} {pct(v)}" for k, v in by_lang.items())
          + " | 앱별: " + " · ".join(f"{k} {pct(v)}" for k, v in by_app.items()))
    print("어떻게 갈렸나 (정답 → 우리 답): " + ", ".join(f"{k} {v}" for k, v in metrics["confusion"].items()))
    print(f"응답 시간 중간값 {usage['latency_ms_p50'] / 1000:.1f}초 | 전체 {elapsed:.0f}초 | 비용(정가 환산) ${usage['cost_usd']:.4f}")
    print(f"저장: {out_dir}")


if __name__ == "__main__":
    main()
