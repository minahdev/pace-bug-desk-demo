"""PACE 연결·점수 실험 (7단계).

시험지 문제(판단 불가는 빼고)마다 Haiku가 세 가지를 답하게 하고 정답과 비교한다.
  - issues: 같은 문제인 알려진 이슈 (첫 번째가 주된 것, 없으면 새 문제)
  - others_affected: 다른 사람도 겪는다고 했나
  - severity: 심각도 0~2 (버그만, 나머지는 코드가 0으로 둔다)
그 답과 기록부 정보(제보 수, 상태, 고친 버전)로 코드가 영향 범위·해결 상태·우선순위를 계산한다(build_eval_set.derive).
분류와 추출은 정답을 쓴다(연결·점수 실력만 따로 재려고). 셋을 잇는 건 8·9단계에서 한다.

실행 (프로젝트 폴더에서):
  점검:   .venv/bin/python pace_desk/eval/eval_link.py --exp S0 --limit 20
  연습용: .venv/bin/python pace_desk/eval/eval_link.py --exp S0
결과: pace_desk/eval/results/link/history.csv, pace_desk/eval/results/link/<시각>_<회차>/
"""

import argparse
import csv
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACE = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PACE))
from build_eval_set import derive, load_db  # noqa: E402  우선순위는 정답과 같은 계산 규칙으로
from eval_cls import PROMPTS, load_items  # noqa: E402
from llm import ask_many, summarize  # noqa: E402
import tools  # noqa: E402

RESULTS = HERE / "results" / "link"
HISTORY = RESULTS / "history.csv"
LEVELS = ["낮음", "보통", "높음", "긴급"]
NONE = "(없음)"

EXPERIMENTS = {
    "S0": {"prompt": "link_s0.md", "effort": "low", "context": "list",
           "change": "기본 프롬프트(Baseline): 알려진 이슈 37개 목록을 통째로 보여 주고 issues·others_affected·severity를 답하게 함"},
    "S1": {"prompt": "link_s1.md", "effort": "low", "context": "list",
           "change": "Rubric: 0회차에 기준표 4장(같은 문제 가리기, 비슷한 짝, 문의·개선 요청 연결)과 5장(심각도) 규칙을 더함"},
    "S2": {"prompt": "link_s2.md", "effort": "low", "context": "candidates",
           "change": "Context 선별: 목록 대신 코드가 기록부를 검색한 후보 5개(증상·상태 포함)를 제보 뒤에 넣어 줌"},
    "S3": {"prompt": "link_s3.md", "effort": "low", "context": "tools",
           "change": "Function calling: 목록 없이 Haiku가 search_known_issues·get_issue 도구로 직접 찾게 함"},
}
TECHNIQUES = {
    "S0": "기준점(Baseline) · Structured Outputs",
    "S1": "Rubric(연결·심각도 규칙)",
    "S2": "Context 선별·위치 배치 (검색 후보 5개)",
    "S3": "Function calling (MCP 도구)",
}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["issues", "others_affected", "severity"],
    "properties": {
        "issues": {"type": "array", "items": {"type": "string", "pattern": "^PACE-[0-9]{3}$"}, "maxItems": 4},
        "others_affected": {"type": "boolean"},
        "severity": {"type": "integer", "enum": [0, 1, 2]},
    },
}
SCHEMA_COT = {
    "type": "object", "additionalProperties": False,
    "required": ["reason", *SCHEMA["required"]],
    "properties": {"reason": {"type": "string"}, **SCHEMA["properties"]},
}
SCHEMAS = {"basic": SCHEMA, "cot": SCHEMA_COT}

HISTORY_COLUMNS = [
    "timestamp", "exp", "technique", "change", "split", "n_items", "runs", "effort",
    "link_acc", "link_acc_known", "new_recall", "new_precision", "severity_acc", "others_acc",
    "priority_acc", "priority_within1", "candidate_recall", "tool_calls_avg", "agreement", "call_errors",
    "latency_p50_ms", "latency_p95_ms", "input_tokens_avg", "output_tokens_avg", "cost_usd", "new_cost_usd",
    "elapsed_sec", "output_dir",
]


def candidates_block(item, limit=5):
    """코드가 기록부를 검색한 후보 이슈 (2회차). 플랫폼은 정답 추출값으로 거른다."""
    found = tools.search_known_issues(item["report"], item["extract"]["platform"], limit)
    details = []
    for brief in found:
        full = tools.get_issue(brief["id"])
        details.append({k: full[k] for k in ("id", "type", "platforms", "status", "title", "symptom")}
                       | {"fixed_in": [f["release"] for f in full["fixed_in"]]})
    return details


def user_prompt(item, context):
    text = f"<category>{item['category']}</category>\n<report>{item['report']}</report>"
    if context == "candidates":
        lines = "\n".join(json.dumps(c, ensure_ascii=False) for c in candidates_block(item))
        text += f"\n<candidates>\n{lines}\n</candidates>"
    return text


def grade(item, answer, db, issues, releases):
    gold_issues = item["link"]["issues"]
    row = {"id": item["id"], "category": item["category"], "difficulty": item["difficulty"],
           "tags": ", ".join(item["tags"]), "report": item["report"],
           "gold_issues": ", ".join(gold_issues) or NONE, "gold_others": item["link"]["others_affected"],
           "gold_severity": item["severity"], "gold_priority": item["priority_label"]}
    if answer is None or not answer.ok or not isinstance(answer.data, dict):
        row.update(call_error=True, error=answer.error if answer else "못 물음")
        return row
    data = answer.data
    pred_issues = [i for i in data.get("issues", []) if i in issues]
    severity = data.get("severity", 0) if item["category"] == "버그" else 0
    others = bool(data.get("others_affected"))
    mine = derive({"category": item["category"], "link": {"issues": pred_issues, "others_affected": others},
                   "severity": severity, "extract": item["extract"]}, db, issues, releases)
    gold_first = gold_issues[0] if gold_issues else NONE
    pred_first = pred_issues[0] if pred_issues else NONE
    distance = abs(LEVELS.index(mine["priority_label"]) - LEVELS.index(item["priority_label"]))
    row.update(
        call_error=False, error="",
        pred_issues=", ".join(pred_issues) or NONE, pred_others=others, pred_severity=severity,
        pred_priority=mine["priority_label"], pred_score=mine["priority"],
        link_ok=pred_first == gold_first, gold_new=not gold_issues, pred_new=not pred_issues,
        others_ok=others == item["link"]["others_affected"], priority_ok=distance == 0, priority_within1=distance <= 1,
        tool_calls=len(answer.tool_calls or []), reason=data.get("reason", ""),
    )
    if item["category"] == "버그":
        row["severity_ok"] = severity == item["severity"]
    return row


def rate(rows, key, where=lambda r: True):
    values = [r[key] for r in rows if not r.get("call_error") and key in r and where(r)]
    return sum(values) / len(values) if values else None


def score(rows, runs):
    ok = [r for r in rows if not r.get("call_error")]
    new_pred = [r for r in ok if r["pred_new"]]
    agreement = None
    if runs > 1:
        seen = defaultdict(set)
        for r in ok:
            seen[r["id"]].add((r["pred_issues"].split(", ")[0], r["pred_severity"], r["pred_others"]))
        agreement = sum(len(v) == 1 for v in seen.values()) / len(seen)
    confusion = Counter((r["gold_priority"], r["pred_priority"]) for r in ok)
    return {
        "n": len(rows),
        "link_acc": rate(rows, "link_ok"),
        "link_acc_known": rate(rows, "link_ok", lambda r: not r["gold_new"]),
        "new_recall": rate(rows, "pred_new", lambda r: r["gold_new"]),
        "new_precision": (sum(r["gold_new"] for r in new_pred) / len(new_pred)) if new_pred else None,
        "severity_acc": rate(rows, "severity_ok"),
        "others_acc": rate(rows, "others_ok"),
        "priority_acc": rate(rows, "priority_ok"),
        "priority_within1": rate(rows, "priority_within1"),
        "candidate_recall": rate(rows, "candidate_hit") if any("candidate_hit" in r for r in rows) else None,
        "tool_calls_avg": (sum(r["tool_calls"] for r in ok) / len(ok)) if ok else None,
        "agreement": agreement,
        "call_errors": len(rows) - len(ok),
        "priority_confusion": {f"{g} → {p}": n for (g, p), n in sorted(confusion.items())},
    }


def pct(value):
    return "-" if value is None else f"{value * 100:.1f}%"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--exp", required=True, choices=sorted(EXPERIMENTS))
    parser.add_argument("--split", default="dev", choices=("dev", "test", "all"))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--budget-usd", type=float)
    parser.add_argument("--final", action="store_true", help="시험용(test)을 풀 때 꼭 붙인다. 9단계에서 한 번만")
    args = parser.parse_args()
    if args.split != "dev" and not args.final:
        sys.exit("시험용 문제는 9단계에서 한 번만 풉니다. 정말 풀려면 --final을 붙이세요.")

    exp = EXPERIMENTS[args.exp]
    db, issues, releases = load_db()
    system = (PROMPTS / exp["prompt"]).read_text(encoding="utf-8")
    items = [i for i in load_items(args.split, None) if i["category"] != "판단 불가"]
    if args.limit:
        items = items[: args.limit]
    tool_names = ["search_known_issues", "get_issue"] if exp["context"] == "tools" else None
    jobs = [{"system": system, "user": user_prompt(item, exp["context"]),
             "schema": SCHEMAS[exp.get("schema", "basic")], "effort": exp["effort"], "repeat": run,
             **({"tools": tool_names} if tool_names else {})}
            for run in range(args.runs) for item in items]
    label = f"{args.exp} {args.split}{len(items)}" + (f"×{args.runs}" if args.runs > 1 else "")
    print(f"== 연결·점수 {label}: {exp['change']}")
    started = time.perf_counter()
    answers = ask_many(jobs, workers=args.workers, budget_usd=args.budget_usd, label=label)
    elapsed = time.perf_counter() - started

    rows = []
    for index, answer in enumerate(answers):
        run, position = divmod(index, len(items))
        item = items[position]
        row = grade(item, answer, db, issues, releases)
        if exp["context"] == "candidates" and item["link"]["issues"]:
            row["candidate_hit"] = item["link"]["issues"][0] in jobs[index]["user"]
        row.update(run=run, latency_ms=answer.latency_ms if answer else "", cost_usd=answer.cost_usd if answer else "",
                   from_cache=answer.from_cache if answer else "",
                   tool_trace=json.dumps(answer.tool_calls, ensure_ascii=False) if answer and answer.tool_calls else "")
        rows.append(row)
    metrics = score(rows, args.runs)
    usage = summarize(answers)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = RESULTS / (f"{stamp}_{args.exp}_{args.split}{len(items)}" + (f"x{args.runs}" if args.runs > 1 else ""))
    out_dir.mkdir(parents=True, exist_ok=True)
    columns = sorted({k for r in rows for k in r}, key=lambda k: (k not in ("id", "run", "category"), k))
    with (out_dir / "predictions.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    (out_dir / "summary.json").write_text(json.dumps(
        {"settings": {"exp": args.exp, **exp, "split": args.split, "n_items": len(items), "runs": args.runs,
                      "system_prompt": system}, "metrics": metrics, "usage": usage},
        ensure_ascii=False, indent=2), encoding="utf-8")
    old_rows = list(csv.DictReader(open(HISTORY, encoding="utf-8-sig"))) if HISTORY.exists() else []
    with HISTORY.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_COLUMNS)
        writer.writeheader()
        writer.writerows({k: r.get(k, "") for k in HISTORY_COLUMNS} for r in old_rows)
        round4 = lambda v: "" if v is None else round(v, 4)
        writer.writerow({
            "timestamp": stamp, "exp": args.exp, "technique": TECHNIQUES.get(args.exp, ""), "change": exp["change"],
            "split": args.split, "n_items": len(items), "runs": args.runs, "effort": exp["effort"],
            **{k: round4(metrics[k]) for k in ("link_acc", "link_acc_known", "new_recall", "new_precision",
                                               "severity_acc", "others_acc", "priority_acc", "priority_within1",
                                               "candidate_recall", "tool_calls_avg", "agreement")},
            "call_errors": metrics["call_errors"],
            "latency_p50_ms": usage["latency_ms_p50"], "latency_p95_ms": usage["latency_ms_p95"],
            "input_tokens_avg": usage["input_tokens_avg"], "output_tokens_avg": usage["output_tokens_avg"],
            "cost_usd": usage["cost_usd"], "new_cost_usd": usage["cost_usd_new"], "elapsed_sec": round(elapsed, 1),
            "output_dir": str(out_dir.relative_to(PACE)),
        })

    m = metrics
    print(f"\n이슈 연결(1순위) {pct(m['link_acc'])} · 알려진 이슈 맞힘 {pct(m['link_acc_known'])}"
          f" · 새 문제 알아봄 {pct(m['new_recall'])} (새 문제라고 한 것 중 맞음 {pct(m['new_precision'])})")
    print(f"심각도(버그) {pct(m['severity_acc'])} · 다른 사람도 {pct(m['others_acc'])}"
          f" · 우선순위 {pct(m['priority_acc'])} · 한 칸 안 {pct(m['priority_within1'])}"
          + (f" · 반복 일치율 {pct(m['agreement'])}" if m["agreement"] is not None else ""))
    if m["candidate_recall"] is not None:
        print(f"후보 5개 안에 정답 이슈가 있었던 비율 {pct(m['candidate_recall'])}")
    if exp["context"] == "tools":
        print(f"문제 하나에 도구를 부른 횟수 평균 {m['tool_calls_avg']:.1f}번")
    print(f"응답 시간 중간값 {usage['latency_ms_p50'] / 1000:.1f}초 · 느린 쪽 {usage['latency_ms_p95'] / 1000:.1f}초"
          f" | 전체 {elapsed:.0f}초 | 비용(정가 환산) ${usage['cost_usd']:.4f} | 호출 실패 {m['call_errors']}개")
    wrong = [r for r in rows if not r.get("call_error") and not r["link_ok"]]
    if wrong:
        print(f"\n연결이 틀린 문제 {len(wrong)}개 (앞 10개):")
        for r in wrong[:10]:
            print(f"  {r['id']} [{r['category']}] 정답 {r['gold_issues']} → 답 {r['pred_issues']} | {r['report'][:45]}")
    print(f"\n저장: {out_dir}\n기록: {HISTORY}")


if __name__ == "__main__":
    main()
