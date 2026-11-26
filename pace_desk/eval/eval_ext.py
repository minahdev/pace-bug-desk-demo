"""PACE 추출 실험 (5단계).

시험지 문제의 글에서 플랫폼·기기(env_detail)·앱 버전·재현 절차를 뽑게 하고, 정답과 칸마다 비교한다.
분류는 이미 끝났다고 치고 정답 종류를 함께 알려 준다(뽑기 실력만 따로 재려고). 분류와 잇는 건 7단계에서 한다.

실행 (프로젝트 폴더에서):
  점검 (연습용 20문제):  .venv/bin/python pace_desk/eval/eval_ext.py --exp E0 --limit 20
  연습용 전체 (240문제): .venv/bin/python pace_desk/eval/eval_ext.py --exp E0
  3번 반복:              .venv/bin/python pace_desk/eval/eval_ext.py --exp E0 --runs 3
결과:
  pace_desk/eval/results/ext/history.csv   회차마다 한 줄 (개선 히스토리, 엑셀로 열린다)
  pace_desk/eval/results/ext/<시각>_<회차>/ predictions.csv(문제별 답), summary.json
"""

import argparse
import csv
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent  # pace_desk/eval
PACE = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PACE))
from build_eval_set import bug_completeness  # noqa: E402  정보 완성도는 정답과 같은 계산 규칙으로
from eval_cls import CALL_ERROR, PROMPTS, load_items  # noqa: E402  같은 시험지·같은 문제 뽑기
from llm import ask_many, summarize  # noqa: E402

RESULTS = HERE / "results" / "ext"
HISTORY = RESULTS / "history.csv"
FIELDS = ("platform", "env_detail", "app_version")  # 지어내기·놓침을 세는 칸

# 추출 회차 목록. 한 회차는 바로 앞 회차에서 한 가지만 바꾼다.
EXPERIMENTS = {
    "E0": {"prompt": "ext_e0.md", "effort": "low",
           "change": "기본 프롬프트(Baseline): 뽑을 칸 다섯 개와 한 줄 뜻만, JSON 모양으로"},
    "E1": {"prompt": "ext_e1.md", "effort": "low",
           "change": "기준표 3장 추출 규칙: 플랫폼 단서·단서 아닌 말, 기기는 적힌 대로·일반 낱말 빼기, 버전 맞추기·최신, 절차는 버그에만"},
    "E2": {"prompt": "ext_e2.md", "effort": "low",
           "change": "Few-shot: 1회차에 새로 만든 예시 3개(아이폰 사파리 웹 버그, 버전 v1.1·가족 기기, 앱 문의)를 더함"},
    "E3": {"prompt": "ext_e3.md", "effort": "low", "schema": "evidence",
           "change": "근거 먼저 따오기: 2회차에 evidence 칸을 맨 앞에 두고, 각 칸의 근거가 되는 글의 표현을 그대로 따온 뒤 채우게 함"},
    "E4": {"prompt": "ext_e2.md", "effort": "medium",
           "change": "effort(생각 깊이): 2회차 그대로, low → medium"},
    "E5": {"prompt": "ext_e5.md", "effort": "low", "schema": "combined", "with_category": False,
           "change": "분류+추출 한 번에: 분류 7회차 프롬프트와 추출 2회차 프롬프트를 합쳐 한 번 부름(정답 종류를 알려 주지 않음)"},
}
TECHNIQUES = {
    "E0": "기준점(Baseline) · Structured Outputs (JSON Schema)",
    "E1": "Rubric(추출 규칙) · 정보 부족 시 비워 두기",
    "E2": "Few-shot (예시 3개)",
    "E3": "CoT 비슷한 근거 인용 (글에서 그대로 따오기)",
    "E4": "주요 파라미터 설정 (effort medium)",
    "E5": "Decomposition·Prompt Chaining 비교 (한 번에 묻기)",
}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["platform", "env_detail", "app_version", "steps", "steps_keywords"],
    "properties": {
        "platform": {"anyOf": [{"type": "string", "enum": ["Android 앱", "웹"]}, {"type": "null"}]},
        "env_detail": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "app_version": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "steps": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "steps_keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
    },
}

# 3회차: 근거(evidence)를 먼저 따오게 한다. 칸 순서대로 쓰므로 근거를 적은 뒤 칸을 채운다
SCHEMA_EVIDENCE = {
    "type": "object", "additionalProperties": False,
    "required": ["evidence", *SCHEMA["required"]],
    "properties": {"evidence": {"type": "array", "items": {"type": "string"}}, **SCHEMA["properties"]},
}
# 5회차: 분류와 추출을 한 번에 묻는다 (분류 이유 → 분류 → 뽑을 칸)
SCHEMA_COMBINED = {
    "type": "object", "additionalProperties": False,
    "required": ["reason", "category", *SCHEMA["required"]],
    "properties": {"reason": {"type": "string"},
                   "category": {"type": "string", "enum": ["버그", "문의", "개선 요청", "판단 불가"]},
                   **SCHEMA["properties"]},
}
SCHEMAS = {"basic": SCHEMA, "evidence": SCHEMA_EVIDENCE, "combined": SCHEMA_COMBINED}

HISTORY_COLUMNS = [
    "timestamp", "exp", "technique", "change", "split", "n_items", "runs", "effort",
    "cls_acc", "platform_acc", "version_acc", "env_acc", "steps_presence_acc", "steps_keyword_acc", "completeness_acc",
    "all_ok", "invented_rate", "missed_rate", "format_errors", "call_errors", "agreement",
    "latency_p50_ms", "latency_p95_ms", "input_tokens_avg", "output_tokens_avg", "cost_usd", "new_cost_usd",
    "elapsed_sec", "output_dir",
]
VERSION_OK = re.compile(r"\d+\.\d+\.\d+")
VERSION_ANY = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def squeeze(text):
    return re.sub(r"\s+", "", str(text)).lower()


def blank(value):
    """빈 문자열·'없음' 같은 값은 null로 본다."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text in ("", "null", "None", "없음", "-") else text


def norm_version(value):
    """버전 비교용: 'v1.2', '1.2버전' → '1.2.0'. 이상한 값은 그대로 둬서 틀리게 한다."""
    value = blank(value)
    if value is None or value == "최신":
        return value
    found = VERSION_ANY.search(value)
    return f"{found.group(1)}.{found.group(2)}.{found.group(3) or 0}" if found else value


def norm_env(value):
    """기기 비교용: ' · ', ',', '/'로 나눈 조각을 띄어쓰기·대소문자 없이 모은다."""
    value = blank(value)
    if value is None:
        return None
    return frozenset(squeeze(p) for p in re.split(r"\s*[·,/]\s*", value) if p.strip())


def grade(item, answer):
    """한 문제를 칸마다 채점한다."""
    gold = item["extract"]
    row = {"id": item["id"], "category": item["category"], "difficulty": item["difficulty"],
           "tags": ", ".join(item["tags"]), "report": item["report"],
           "gold_platform": gold["platform"], "gold_env": gold["env_detail"], "gold_version": gold["app_version"],
           "gold_steps": gold["steps"], "gold_keywords": ", ".join(gold["steps_keywords"])}
    if answer is None or not answer.ok or not isinstance(answer.data, dict):
        row.update(pred=CALL_ERROR, call_error=True, error=(answer.error if answer else "못 물음"))
        return row
    data = answer.data
    pred = {"platform": blank(data.get("platform")), "env_detail": blank(data.get("env_detail")),
            "app_version": blank(data.get("app_version")), "steps": blank(data.get("steps")),
            "steps_keywords": [k for k in (data.get("steps_keywords") or []) if blank(k)]}
    raw_version = pred["app_version"]
    if "category" in data:  # 5회차처럼 분류도 같이 답한 경우
        row.update(pred_category=data["category"], cls_ok=data["category"] == item["category"])
    row.update(
        call_error=False, error="",
        pred_platform=pred["platform"], pred_env=pred["env_detail"], pred_version=raw_version,
        pred_steps=pred["steps"], pred_keywords=", ".join(pred["steps_keywords"]),
        platform_ok=pred["platform"] == gold["platform"],
        version_ok=norm_version(raw_version) == gold["app_version"],
        version_format_ok=raw_version is None or raw_version == "최신" or bool(VERSION_OK.fullmatch(raw_version)),
        env_ok=norm_env(pred["env_detail"]) == norm_env(gold["env_detail"]),
        steps_presence_ok=(pred["steps"] is None) == (gold["steps"] is None),
    )
    if gold["steps"] and pred["steps"]:
        # 둘 다 절차가 있으면, 정답 핵심 낱말이 Haiku의 절차 문장이나 핵심 낱말 칸에 하나라도 들어 있나
        # (Haiku가 절차를 자기 말로 바꿔 써도, 핵심 낱말 칸에 글의 표현을 그대로 적었으면 같은 절차를 잡은 것으로 본다)
        mine = squeeze(pred["steps"] + " " + " ".join(pred["steps_keywords"]))
        row["steps_keyword_ok"] = any(squeeze(k) in mine for k in gold["steps_keywords"])
    if item["category"] == "버그":
        mine = bug_completeness({"platform": pred["platform"], "app_version": norm_version(raw_version),
                                 "steps": pred["steps"]})
        row.update(gold_completeness=item["completeness"], pred_completeness=mine,
                   completeness_ok=mine == item["completeness"])
    compare = {"platform": (pred["platform"], gold["platform"]),
               "env_detail": (pred["env_detail"], gold["env_detail"]),
               "app_version": (raw_version, gold["app_version"])}
    row["invented"] = ", ".join(f for f, (p, g) in compare.items() if g is None and p is not None)
    row["missed"] = ", ".join(f for f, (p, g) in compare.items() if g is not None and p is None)
    row["all_ok"] = row["platform_ok"] and row["version_ok"] and row["env_ok"] and row["steps_presence_ok"]
    return row


def rate(rows, key):
    values = [r[key] for r in rows if r.get(key) is not None and not r.get("call_error")]
    return sum(values) / len(values) if values else None


def score(rows, runs):
    graded = [r for r in rows if not r.get("call_error")]
    gold_none = sum(1 for r in graded for f, g in (("platform", r["gold_platform"]), ("env_detail", r["gold_env"]),
                                                   ("app_version", r["gold_version"])) if g is None)
    gold_some = len(graded) * len(FIELDS) - gold_none
    invented = sum(len([x for x in r["invented"].split(", ") if x]) for r in graded)
    missed = sum(len([x for x in r["missed"].split(", ") if x]) for r in graded)
    agreement = None
    if runs > 1:
        seen = defaultdict(set)
        for r in graded:
            seen[r["id"]].add((r["pred_platform"], norm_version(r["pred_version"]), norm_env(r["pred_env"]),
                               r["pred_steps"] is None))
        agreement = sum(len(v) == 1 for v in seen.values()) / len(seen)
    by_field = {f: {"invented": sum(f in r["invented"].split(", ") for r in graded),
                    "missed": sum(f in r["missed"].split(", ") for r in graded)} for f in FIELDS}
    return {
        "n": len(rows),
        "platform_acc": rate(rows, "platform_ok"), "version_acc": rate(rows, "version_ok"),
        "env_acc": rate(rows, "env_ok"), "steps_presence_acc": rate(rows, "steps_presence_ok"),
        "steps_keyword_acc": rate(rows, "steps_keyword_ok"), "completeness_acc": rate(rows, "completeness_ok"),
        "all_ok": rate(rows, "all_ok"), "cls_acc": rate(rows, "cls_ok"),
        "invented_rate": invented / gold_none if gold_none else 0.0, "invented": invented, "gold_none": gold_none,
        "missed_rate": missed / gold_some if gold_some else 0.0, "missed": missed, "gold_some": gold_some,
        "by_field": by_field,
        "format_errors": sum(not r["version_format_ok"] for r in graded),
        "call_errors": len(rows) - len(graded),
        "agreement": agreement,
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
    system = (PROMPTS / exp["prompt"]).read_text(encoding="utf-8")
    items = load_items(args.split, args.limit)
    with_category = exp.get("with_category", True)
    jobs = [{"system": system,
             "user": (f"<category>{item['category']}</category>\n" if with_category else "") + f"<report>{item['report']}</report>",
             "schema": SCHEMAS[exp.get("schema", "basic")], "effort": exp["effort"], "repeat": run}
            for run in range(args.runs) for item in items]
    label = f"{args.exp} {args.split}{len(items)}" + (f"×{args.runs}" if args.runs > 1 else "")
    print(f"== 추출 {label}: {exp['change']}")
    started = time.perf_counter()
    answers = ask_many(jobs, workers=args.workers, budget_usd=args.budget_usd, label=label)
    elapsed = time.perf_counter() - started

    rows = []
    for index, answer in enumerate(answers):
        run, position = divmod(index, len(items))
        row = grade(items[position], answer)
        row.update(run=run, latency_ms=answer.latency_ms if answer else "", cost_usd=answer.cost_usd if answer else "",
                   from_cache=answer.from_cache if answer else "", raw=answer.text[:600] if answer else "")
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
        ensure_ascii=False, indent=2, default=list), encoding="utf-8")
    old_rows = list(csv.DictReader(open(HISTORY, encoding="utf-8-sig"))) if HISTORY.exists() else []
    with HISTORY.open("w", encoding="utf-8-sig", newline="") as f:  # 칸이 늘어도 예전 줄이 어긋나지 않게 통째로 다시 쓴다
        writer = csv.DictWriter(f, fieldnames=HISTORY_COLUMNS)
        writer.writeheader()
        writer.writerows({k: r.get(k, "") for k in HISTORY_COLUMNS} for r in old_rows)
        round4 = lambda v: "" if v is None else round(v, 4)
        writer.writerow({
            "timestamp": stamp, "exp": args.exp, "technique": TECHNIQUES.get(args.exp, ""), "change": exp["change"],
            "split": args.split, "n_items": len(items), "runs": args.runs, "effort": exp["effort"],
            **{k: round4(metrics[k]) for k in ("cls_acc", "platform_acc", "version_acc", "env_acc", "steps_presence_acc",
                                               "steps_keyword_acc", "completeness_acc", "all_ok",
                                               "invented_rate", "missed_rate", "agreement")},
            "format_errors": metrics["format_errors"], "call_errors": metrics["call_errors"],
            "latency_p50_ms": usage["latency_ms_p50"], "latency_p95_ms": usage["latency_ms_p95"],
            "input_tokens_avg": usage["input_tokens_avg"], "output_tokens_avg": usage["output_tokens_avg"],
            "cost_usd": usage["cost_usd"], "new_cost_usd": usage["cost_usd_new"], "elapsed_sec": round(elapsed, 1),
            "output_dir": str(out_dir.relative_to(PACE)),
        })

    m = metrics
    print(f"\n플랫폼 {pct(m['platform_acc'])} · 앱 버전 {pct(m['version_acc'])} · 기기 {pct(m['env_acc'])}"
          f" · 재현 절차 있음/없음 {pct(m['steps_presence_acc'])} · 절차 핵심 낱말 {pct(m['steps_keyword_acc'])}")
    if m["cls_acc"] is not None:
        print(f"분류 정확도 {pct(m['cls_acc'])} (분류도 같이 답함)")
    print(f"정보 완성도(버그) {pct(m['completeness_acc'])} · 네 칸 모두 맞음 {pct(m['all_ok'])}"
          + (f" · 반복 일치율 {pct(m['agreement'])}" if m["agreement"] is not None else ""))
    print(f"지어낸 값 {m['invented']}개 / 정답이 빈칸인 {m['gold_none']}칸 = {pct(m['invented_rate'])}"
          f" | 놓친 값 {m['missed']}개 / 정답이 있는 {m['gold_some']}칸 = {pct(m['missed_rate'])}")
    print("  칸별 지어냄·놓침: " + " · ".join(f"{f} {v['invented']}·{v['missed']}" for f, v in m["by_field"].items()))
    print(f"버전 모양 어김 {m['format_errors']}개 · 호출 실패 {m['call_errors']}개")
    print(f"응답 시간 중간값 {usage['latency_ms_p50'] / 1000:.1f}초 · 느린 쪽 {usage['latency_ms_p95'] / 1000:.1f}초"
          f" | 전체 {elapsed:.0f}초 | 비용(정가 환산) ${usage['cost_usd']:.4f} (새로 쓴 것 ${usage['cost_usd_new']:.4f})")
    wrong = [r for r in rows if not r.get("call_error") and not r["all_ok"]]
    if wrong:
        print(f"\n네 칸 중 하나라도 틀린 문제 {len(wrong)}개 (앞 12개):")
        for r in wrong[:12]:
            bad = [name for name, key in (("플랫폼", "platform_ok"), ("버전", "version_ok"), ("기기", "env_ok"),
                                          ("절차", "steps_presence_ok")) if not r[key]]
            print(f"  {r['id']} [{r['category']}] 틀린 칸 {bad} | 정답 {r['gold_platform']}/{r['gold_version']}/"
                  f"{r['gold_env']}/{'절차O' if r['gold_steps'] else '절차X'} → 답 {r['pred_platform']}/"
                  f"{r['pred_version']}/{r['pred_env']}/{'절차O' if r['pred_steps'] else '절차X'} | {r['report'][:40]}")
    print(f"\n저장: {out_dir}\n기록: {HISTORY}")


if __name__ == "__main__":
    main()
