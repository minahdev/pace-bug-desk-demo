"""PACE 분류 실험 (4단계).

시험지(pace_eval.jsonl)의 문제를 프롬프트와 함께 llm.py로 Haiku에게 보내 분류하게 하고, 정답과 비교해 채점한다.
실험(EXPERIMENTS)마다 바로 앞 실험에서 한 가지만 바꾼다.

실행 (프로젝트 폴더에서):
  점검 (연습용 20문제):      .venv/bin/python pace_desk/eval/eval_cls.py --exp C0 --limit 20
  연습용 전체 (240문제):     .venv/bin/python pace_desk/eval/eval_cls.py --exp C0
  3번 반복 (답이 흔들리나):  .venv/bin/python pace_desk/eval/eval_cls.py --exp C0 --runs 3
  시험용 (맨 마지막에 한 번): .venv/bin/python pace_desk/eval/eval_cls.py --exp C0 --split test --final

결과:
  pace_desk/eval/results/cls/history.csv   실험마다 한 줄씩 쌓인다 (개선 히스토리, 엑셀로 열린다)
  pace_desk/eval/results/cls/<시각>_<실험>/ predictions.csv(문제별 답), confusion.csv(혼동 행렬), summary.json
"""

import argparse
import csv
import json
import random
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent  # pace_desk/eval
PACE = HERE.parent  # pace_desk
sys.path.insert(0, str(PACE))
from llm import Answer, ask_many, summarize  # noqa: E402

EVAL_SET = HERE / "pace_eval.jsonl"
PROMPTS = PACE / "prompts"
RESULTS = HERE / "results" / "cls"
HISTORY = RESULTS / "history.csv"
CATEGORIES = ("버그", "문의", "개선 요청", "판단 불가")
FORMAT_ERROR = "형식 오류"  # 답에서 종류를 꺼내지 못함
CALL_ERROR = "호출 실패"  # Haiku에게서 답을 못 받음
SAMPLE_SEED = 20261125

# 실험 목록. 한 실험은 바로 앞 실험에서 한 가지만 바꾼다.
#   prompt: pace_desk/prompts/ 안의 시스템 프롬프트 파일
#   output: "text"는 자유 글로 받아 마지막 줄 '분류: …'에서 꺼낸다. "json"은 JSON Schema 모양으로 받는다
#   effort: Haiku의 생각 깊이 (low, medium, high, xhigh, max)
EXPERIMENTS = {
    "C0": {"prompt": "cls_c0.txt", "output": "text", "effort": "low",
           "change": "기본 프롬프트(Baseline): 역할과 네 종류의 한 줄 뜻만 주고, 자유 글로 답하게 함"},
    "C1": {"prompt": "cls_c1.txt", "output": "json", "effort": "low",
           "change": "답 모양을 JSON Schema로 고정(category만). 프롬프트는 출력 안내 문장 하나만 바꿈"},
    "C2": {"prompt": "cls_c2.md", "output": "json", "effort": "low",
           "change": "Markdown 템플릿: C1과 같은 내용을 제목(역할·PACE·할 일·분류·출력 형식)과 목록으로 정리"},
    "C3": {"prompt": "cls_c3.md", "output": "json", "effort": "low",
           "change": "Rubric·답변 불가 규칙: 기준표 2장의 '헷갈릴 때 고르는 순서' 5개 규칙(판단 불가를 쓰는 경우 포함)을 더함"},
    "C4": {"prompt": "cls_c4.md", "output": "json", "effort": "low",
           "change": "Context 선별: 짧은 PACE 소개를 기준표 1장의 기능 목록(있는 기능 전부, 없는 기능 예시, 목록에 없으면 없는 기능)으로 바꿈"},
    "C5": {"prompt": "cls_c5.md", "output": "json", "effort": "low",
           "change": "판단 불가 규칙 다듬기: 지시어+'안 돼요'도 판단 불가, 기기·버전·플랫폼만으로는 대상이 아님, 막연한 말은 증상이 아님"},
    "C6a": {"prompt": "cls_c6a.md", "output": "json", "effort": "low",
            "change": "Few-shot 3개: 시험지에 없는 새 예시(질문 말투 버그, 없는 기능 개선 요청, 지난 대화 판단 불가)를 더함"},
    "C6b": {"prompt": "cls_c6b.md", "output": "json", "effort": "low",
            "change": "Few-shot 6개: C6a에 문의, '언제 나와요' 개선 요청, 내 설정이 원인인 버그 예시 3개를 더함"},
    "C7": {"prompt": "cls_c7.md", "output": "json_cot", "effort": "low",
           "change": "CoT: C5(예시 없음)에 이유(reason) 칸을 종류보다 먼저 쓰게 함. 겪은 고장·기능 유무·해당 규칙을 짧게"},
    "C8a": {"prompt": "cls_c5.md", "output": "json", "effort": "medium",
            "change": "effort(생각 깊이): C5 그대로, low → medium"},
    "C8b": {"prompt": "cls_c5.md", "output": "json", "effort": "high",
            "change": "effort(생각 깊이): C5 그대로, low → high"},
    "C9": {"prompt": ("cls_c9_gate.md", "cls_c9_type.md"), "output": "chain", "effort": "low",
           "change": "Prompt Chaining: C5를 두 번 묻기로 나눔. ① 무엇에 대한 글인지 알 수 있나 → ② 알 수 있는 글만 버그·문의·개선 요청"},
    "C10a": {"prompt": "cls_c5.md", "output": "json", "effort": "low", "layout": "user_after",
             "change": "System·User 분리 안 함: 시스템은 역할 한 줄, 규칙 전부를 사용자 프롬프트에 넣고 제보 글을 맨 뒤에"},
    "C10b": {"prompt": "cls_c5.md", "output": "json", "effort": "low", "layout": "user_before",
             "change": "Prefix cache 배치 거꾸로: 시스템은 역할 한 줄, 사용자 프롬프트에 제보 글을 먼저 두고 규칙 전부를 뒤에"},
    "C11": {"prompt": "cls_c11.md", "output": "json_cot", "effort": "low",
            "change": "규칙 보완(진짜 리뷰에서 찾은 빈틈): 7회차에 '이미 해결된 문제는 버그 근거 아님', '대상 있는 막연한 바람은 개선 요청' 두 문장"},
    "C12": {"prompt": "cls_c12.md", "output": "json_cot", "effort": "low",
            "change": "11회차를 나눠 보기: 7회차에 '이미 해결된 문제는 버그 근거 아님' 한 문장만 더함"},
}
ROLE_LINE = "당신은 PACE 앱의 버그 제보 데스크 담당자입니다."

# 회차마다 시험한 수업 기법 (과제 공통 필수 조건의 이름). 결과표의 technique 칸에 적는다
TECHNIQUES = {
    "C0": "기준점(Baseline) · System·User 프롬프트 분리",
    "C1": "Structured Outputs (JSON Schema)",
    "C2": "Markdown 템플릿",
    "C3": "Rubric(판단 기준) · 정보 부족 시 답변 불가",
    "C4": "Context 선별",
    "C5": "정보 부족 시 답변 불가 (규칙 다듬기)",
    "C6a": "Few-shot (예시 3개)",
    "C6b": "Few-shot (예시 6개)",
    "C7": "CoT (생각 단계)",
    "C8a": "주요 파라미터 설정 (effort medium)",
    "C8b": "주요 파라미터 설정 (effort high)",
    "C9": "Decomposition · Prompt Chaining",
    "C10a": "System·User 프롬프트 분리 (안 했을 때와 비교)",
    "C10b": "Context 위치 배치 · Prefix cache 고려 배치 (거꾸로 했을 때와 비교)",
    "C11": "Rubric 보완 (진짜 리뷰 확인에서 찾은 빈틈 두 가지)",
    "C12": "Rubric 보완 (이미 해결된 문제 한 가지만)",
}


def place(prompt, report, layout):
    """프롬프트와 제보 글을 어디에 둘지 정한다. (시스템 프롬프트, 사용자 프롬프트)를 돌려준다."""
    tagged = f"<report>{report}</report>"
    if layout == "user_after":  # 규칙 → 글 (둘 다 사용자 프롬프트)
        return ROLE_LINE, f"{prompt}\n\n{tagged}"
    if layout == "user_before":  # 글 → 규칙 (둘 다 사용자 프롬프트)
        return ROLE_LINE, f"{tagged}\n\n{prompt}"
    return prompt, tagged  # 기본: 규칙은 시스템, 글은 사용자

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["category"],
    "properties": {"category": {"type": "string", "enum": list(CATEGORIES)}},
}
# CoT: 이유(reason)를 종류(category)보다 먼저 쓰게 한다. 칸 순서대로 쓰므로, 이유를 쓰고 나서 종류를 고른다
SCHEMA_COT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reason", "category"],
    "properties": {
        "reason": {"type": "string", "description": "판단 근거 한두 문장"},
        "category": {"type": "string", "enum": list(CATEGORIES)},
    },
}
SCHEMAS = {"text": None, "json": SCHEMA, "json_cot": SCHEMA_COT}
# Prompt Chaining: ① 무엇에 대한 글인지 알 수 있나(subject, answerable) → ② 알 수 있는 글만 세 종류 중 하나로
SCHEMA_GATE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["subject", "answerable"],
    "properties": {"subject": {"type": "string"}, "answerable": {"type": "boolean"}},
}
SCHEMA_TYPE3 = {
    "type": "object",
    "additionalProperties": False,
    "required": ["category"],
    "properties": {"category": {"type": "string", "enum": ["버그", "문의", "개선 요청"]}},
}

HISTORY_COLUMNS = [
    "timestamp", "exp", "technique", "change", "split", "n_items", "runs", "effort", "output",
    "accuracy", "macro_f1", *(f"recall_{c}" for c in CATEGORIES), "format_errors", "call_errors", "agreement",
    "latency_p50_ms", "latency_p95_ms", "input_tokens_avg", "output_tokens_avg", "cost_usd", "new_cost_usd",
    "elapsed_sec", "output_dir",
]
PREDICTION_COLUMNS = [
    "id", "run", "difficulty", "tags", "gold", "pred", "correct", "format_ok", "latency_ms", "wall_ms",
    "input_tokens", "output_tokens", "cost_usd", "from_cache", "error", "report", "raw",
]


def squeeze(text):
    return re.sub(r"\s+", "", text)


def load_items(split, limit):
    items = [json.loads(line) for line in EVAL_SET.read_text(encoding="utf-8").splitlines() if line.strip()]
    if split != "all":
        items = [i for i in items if i["split"] == split]
    if limit and limit < len(items):
        items = stratified_sample(items, limit)
    return sorted(items, key=lambda i: i["id"])


def stratified_sample(items, n):
    """종류 비율을 지키면서 n개를 고른다. 씨앗이 같아서 늘 같은 문제가 뽑힌다."""
    rng = random.Random(SAMPLE_SEED)
    groups = defaultdict(list)
    for item in sorted(items, key=lambda i: i["id"]):
        groups[item["category"]].append(item)
    for group in groups.values():
        rng.shuffle(group)
    shares = {cat: n * len(group) / len(items) for cat, group in groups.items()}
    take = {cat: int(share) for cat, share in shares.items()}
    for cat in sorted(shares, key=lambda c: shares[c] - take[c], reverse=True)[: n - sum(take.values())]:
        take[cat] += 1  # 반올림에서 남은 자리는 소수점이 큰 종류부터 채운다
    return [item for cat, group in groups.items() for item in group[: take[cat]]]


LINE_RE = re.compile(r"분류\s*[:：]\s*([^\n]+)")


def parse_text(text):
    """자유 글 답에서 종류를 꺼낸다. (종류, 형식을 지켰나)를 돌려준다."""
    found = LINE_RE.findall(text)
    if found:
        value = squeeze(found[-1]).strip("\"'`*.[]()")
        for cat in CATEGORIES:
            if value.startswith(squeeze(cat)):
                return cat, True
    # 마지막 줄 규칙을 안 지켰으면, 글에 종류 이름이 딱 하나만 나올 때만 그것을 답으로 본다
    named = [cat for cat in CATEGORIES if squeeze(cat) in squeeze(text)]
    return (named[0], False) if len(named) == 1 else (FORMAT_ERROR, False)


def predict(answer, output):
    if answer is None or not answer.ok:
        return CALL_ERROR, False
    if output.startswith("json"):
        value = (answer.data or {}).get("category")
        return (value, True) if value in CATEGORIES else (FORMAT_ERROR, False)
    return parse_text(answer.text)


def combine(first, second):
    """두 번 부른 답을 한 문제의 기록으로 합친다. 시간·토큰·비용은 더한다."""
    if second is None:
        return first
    return Answer(
        ok=first.ok and second.ok, text=f"{first.text} → {second.text}", data=second.data,
        error=first.error or second.error,
        latency_ms=first.latency_ms + second.latency_ms, wall_ms=first.wall_ms + second.wall_ms,
        input_tokens=first.input_tokens + second.input_tokens,
        cache_write_tokens=first.cache_write_tokens + second.cache_write_tokens,
        cache_read_tokens=first.cache_read_tokens + second.cache_read_tokens,
        output_tokens=first.output_tokens + second.output_tokens,
        thinking_tokens=first.thinking_tokens + second.thinking_tokens,
        cost_usd=first.cost_usd + second.cost_usd, turns=first.turns + second.turns,
        attempts=max(first.attempts, second.attempts), from_cache=first.from_cache and second.from_cache,
    )


def run_chain(exp, items, args, label):
    """두 번에 나눠 묻는다. ① 판단 가능 여부 → ② (가능한 글만) 버그·문의·개선 요청."""
    gate_system, type_system = ((PROMPTS / name).read_text(encoding="utf-8") for name in exp["prompt"])
    gate_jobs = [{"system": gate_system, "user": f"<report>{item['report']}</report>", "schema": SCHEMA_GATE,
                  "effort": exp["effort"], "repeat": run}
                 for run in range(args.runs) for item in items]
    gates = ask_many(gate_jobs, workers=args.workers, budget_usd=args.budget_usd, label=f"{label} ①")
    second = [i for i, g in enumerate(gates) if g and g.ok and (g.data or {}).get("answerable") is True]
    type_jobs = [{**gate_jobs[i], "system": type_system, "schema": SCHEMA_TYPE3} for i in second]
    types = dict(zip(second, ask_many(type_jobs, workers=args.workers, budget_usd=args.budget_usd,
                                      label=f"{label} ②")))
    answers, preds = [], []
    for i, gate in enumerate(gates):
        answerable = (gate.data or {}).get("answerable") if gate and gate.ok else None
        if gate is None or not gate.ok:
            answers.append(gate)
            preds.append((CALL_ERROR, False))
        elif answerable is False:
            answers.append(gate)
            preds.append(("판단 불가", True))
        elif answerable is not True:
            answers.append(gate)
            preds.append((FORMAT_ERROR, False))
        else:
            kind = types.get(i)
            answers.append(combine(gate, kind))
            value = (kind.data or {}).get("category") if kind and kind.ok else None
            preds.append((value, True) if value in CATEGORIES else
                         (CALL_ERROR if kind is None or not kind.ok else FORMAT_ERROR, False))
    return answers, preds, f"{gate_system}\n\n----- ② -----\n\n{type_system}"


def score(rows, runs):
    """채점: 정확도, 종류별 맞힌 비율(recall)·정밀도·F1, 혼동 행렬, 형식 오류, 3번 반복 일치율."""
    n = len(rows)
    per = {}
    for cat in CATEGORIES:
        tp = sum(r["gold"] == cat and r["pred"] == cat for r in rows)
        gold_n = sum(r["gold"] == cat for r in rows)
        pred_n = sum(r["pred"] == cat for r in rows)
        recall = tp / gold_n if gold_n else None
        precision = tp / pred_n if pred_n else (0.0 if gold_n else None)
        f1 = (2 * precision * recall / (precision + recall)) if precision and recall else 0.0
        per[cat] = {"gold": gold_n, "hit": tp, "predicted": pred_n, "recall": recall, "precision": precision, "f1": f1}
    used = [c for c in CATEGORIES if per[c]["gold"]]
    confusion = Counter((r["gold"], r["pred"]) for r in rows)
    by_difficulty = {}
    for level in ("쉬움", "보통", "어려움"):
        group = [r for r in rows if r["difficulty"] == level]
        if group:
            by_difficulty[level] = sum(r["correct"] for r in group) / len(group)
    agreement = None
    if runs > 1:
        answers = defaultdict(set)
        for r in rows:
            answers[r["id"]].add(r["pred"])
        agreement = sum(len(v) == 1 for v in answers.values()) / len(answers)
    return {
        "n": n,
        "accuracy": sum(r["correct"] for r in rows) / n,
        "macro_f1": sum(per[c]["f1"] for c in used) / len(used),
        "per_category": per,
        "by_difficulty": by_difficulty,
        "format_errors": sum(not r["format_ok"] and r["pred"] != CALL_ERROR for r in rows),
        "call_errors": sum(r["pred"] == CALL_ERROR for r in rows),
        "agreement": agreement,
        "confusion": {f"{g} → {p}": k for (g, p), k in sorted(confusion.items())},
    }


def pct(value):
    return "-" if value is None else f"{value * 100:.1f}%"


def write_outputs(out_dir, rows, metrics, usage, settings):
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "predictions.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=PREDICTION_COLUMNS)
        writer.writeheader()
        writer.writerows({k: r.get(k, "") for k in PREDICTION_COLUMNS} for r in rows)
    preds = list(CATEGORIES) + [p for p in (FORMAT_ERROR, CALL_ERROR) if any(r["pred"] == p for r in rows)]
    with (out_dir / "confusion.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["정답 \\ 답", *preds])
        for gold in CATEGORIES:
            writer.writerow([gold, *(sum(r["gold"] == gold and r["pred"] == p for r in rows) for p in preds)])
    (out_dir / "summary.json").write_text(json.dumps({"settings": settings, "metrics": metrics, "usage": usage},
                                                     ensure_ascii=False, indent=2), encoding="utf-8")


def append_history(row):
    new = not HISTORY.exists()
    # 처음 만들 때만 utf-8-sig(엑셀에서 한글이 안 깨지게)로 쓰고, 이어 쓸 때는 그냥 utf-8로 붙인다
    with HISTORY.open("w" if new else "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_COLUMNS)
        if new:
            writer.writeheader()
        writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--exp", required=True, choices=sorted(EXPERIMENTS), help="실험 이름")
    parser.add_argument("--split", default="dev", choices=("dev", "test", "all"), help="연습용(dev)·시험용(test)")
    parser.add_argument("--limit", type=int, help="종류 비율을 지켜 이 개수만 뽑아 점검한다")
    parser.add_argument("--runs", type=int, default=1, help="같은 문제를 몇 번 풀게 할지 (답이 흔들리는지 보기)")
    parser.add_argument("--workers", type=int, default=6, help="동시에 몇 개를 부를지")
    parser.add_argument("--budget-usd", type=float, help="새로 쓰는 비용(정가 환산)이 이 값을 넘으면 멈춘다")
    parser.add_argument("--final", action="store_true", help="시험용(test)을 풀 때 꼭 붙인다. 맨 마지막에 한 번만")
    args = parser.parse_args()
    if args.split != "dev" and not args.final:
        sys.exit("시험용 문제는 맨 마지막에 한 번만 풉니다. 정말 풀려면 --final을 붙이세요.")

    exp = EXPERIMENTS[args.exp]
    items = load_items(args.split, args.limit)
    label = f"{args.exp} {args.split}{len(items)}" + (f"×{args.runs}" if args.runs > 1 else "")
    print(f"== {label}: {exp['change']}")
    started = time.perf_counter()
    if exp["output"] == "chain":
        answers, preds, system = run_chain(exp, items, args, label)
    else:
        system = (PROMPTS / exp["prompt"]).read_text(encoding="utf-8")
        layout = exp.get("layout", "system")
        jobs = [dict(zip(("system", "user"), place(system, item["report"], layout)),
                     schema=SCHEMAS[exp["output"]], effort=exp["effort"], repeat=run)
                for run in range(args.runs) for item in items]
        answers = ask_many(jobs, workers=args.workers, budget_usd=args.budget_usd, label=label)
        preds = [predict(answer, exp["output"]) for answer in answers]
    elapsed = time.perf_counter() - started

    rows = []
    for index, answer in enumerate(answers):
        run, position = divmod(index, len(items))
        item = items[position]
        pred, format_ok = preds[index]
        rows.append({
            "id": item["id"], "run": run, "difficulty": item["difficulty"], "tags": ", ".join(item["tags"]),
            "gold": item["category"], "pred": pred, "correct": pred == item["category"], "format_ok": format_ok,
            "latency_ms": answer.latency_ms if answer else "", "wall_ms": answer.wall_ms if answer else "",
            "input_tokens": (answer.input_tokens + answer.cache_write_tokens + answer.cache_read_tokens) if answer else "",
            "output_tokens": answer.output_tokens if answer else "", "cost_usd": answer.cost_usd if answer else "",
            "from_cache": answer.from_cache if answer else "", "error": (answer.error or "") if answer else "못 물음",
            "report": item["report"], "raw": (answer.text[:600] if answer else ""),
        })
    metrics = score(rows, args.runs)
    usage = summarize(answers)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = RESULTS / (f"{stamp}_{args.exp}_{args.split}{len(items)}" + (f"x{args.runs}" if args.runs > 1 else ""))
    settings = {"exp": args.exp, **exp, "split": args.split, "n_items": len(items), "runs": args.runs,
                "system_prompt": system}
    write_outputs(out_dir, rows, metrics, usage, settings)
    append_history({
        "timestamp": stamp, "exp": args.exp, "technique": TECHNIQUES.get(args.exp, ""), "change": exp["change"],
        "split": args.split, "n_items": len(items),
        "runs": args.runs, "effort": exp["effort"], "output": exp["output"],
        "accuracy": round(metrics["accuracy"], 4), "macro_f1": round(metrics["macro_f1"], 4),
        **{f"recall_{c}": "" if metrics["per_category"][c]["recall"] is None
           else round(metrics["per_category"][c]["recall"], 4) for c in CATEGORIES},
        "format_errors": metrics["format_errors"], "call_errors": metrics["call_errors"],
        "agreement": "" if metrics["agreement"] is None else round(metrics["agreement"], 4),
        "latency_p50_ms": usage["latency_ms_p50"], "latency_p95_ms": usage["latency_ms_p95"],
        "input_tokens_avg": usage["input_tokens_avg"], "output_tokens_avg": usage["output_tokens_avg"],
        "cost_usd": usage["cost_usd"], "new_cost_usd": usage["cost_usd_new"], "elapsed_sec": round(elapsed, 1),
        "output_dir": str(out_dir.relative_to(PACE)),
    })

    per = metrics["per_category"]
    print(f"\n정확도 {pct(metrics['accuracy'])} ({sum(r['correct'] for r in rows)}/{len(rows)})"
          f" | 판단 불가 찾기 {pct(per['판단 불가']['recall'])}"
          f" | 형식 오류 {metrics['format_errors']}개 | 호출 실패 {metrics['call_errors']}개"
          + (f" | 반복 일치율 {pct(metrics['agreement'])}" if metrics["agreement"] is not None else ""))
    print("종류별 맞힌 비율: " + " · ".join(f"{c} {pct(per[c]['recall'])} ({per[c]['hit']}/{per[c]['gold']})"
                                       for c in CATEGORIES if per[c]["gold"]))
    print("난이도별 정확도: " + " · ".join(f"{k} {pct(v)}" for k, v in metrics["by_difficulty"].items()))
    print(f"응답 시간(API) 중간값 {usage['latency_ms_p50'] / 1000:.1f}초 · 느린 쪽(95%) {usage['latency_ms_p95'] / 1000:.1f}초"
          f" | 전체 걸린 시간 {elapsed:.0f}초")
    print(f"토큰 평균: 입력 {usage['input_tokens_avg']:,.0f} · 출력 {usage['output_tokens_avg']:,.0f}"
          f" | 비용(정가 환산) ${usage['cost_usd']:.4f} (이번에 새로 쓴 것 ${usage['cost_usd_new']:.4f})")
    wrong = [r for r in rows if not r["correct"]]
    if wrong:
        print(f"\n틀린 문제 {len(wrong)}개:")
        for r in wrong[:15]:
            print(f"  {r['id']} 정답 {r['gold']} → 답 {r['pred']} | {r['report'][:60]}")
    print(f"\n저장: {out_dir}\n기록: {HISTORY}")


if __name__ == "__main__":
    main()
