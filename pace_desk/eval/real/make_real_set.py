"""실제 리뷰 시험지를 만든다 (2단계).

출처: Wei et al., "Zero-shot Bilingual App Reviews Mining with Large Language Models", ICTAI 2023.
      Zenodo 11066414, MIT 라이선스. 건강 앱 3개(Garmin Connect, Huawei Health, Samsung Health)의
      구글 플레이 리뷰 12,000개(영어·프랑스어)에 사람이 '문제 보고 / 기능 요청 / 무관' 정답을 붙였다.

하는 일
- 똑같은 글(중복)과 20자 미만의 짧은 글을 뺀다. 너무 쉬워서 시험이 쉬워지기 때문이다.
- 앱 3개 × 언어 2개마다 80개씩, 모두 480개를 고른다: 문제 보고만 30, 기능 요청만 25, 둘 다 5, 무관 20.
- 무관은 칭찬 한 줄처럼 쉬운 것보다 헷갈리는 것을 고른다. 절반은 별점 3 이하
  (불만은 있지만 구체적인 문제나 요청은 아닌 글)이고, 모두 40자 이상이다.
- 종류가 고르게 섞이도록 연습용(dev) 60%, 시험용(test) 40%로 나눈다.

실행: .venv/bin/python pace_desk/eval/real/make_real_set.py
"""

import csv
import json
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw" / "dataset" / "for_classification"
OUT_JSONL = HERE / "real_reviews_eval.jsonl"
OUT_CSV = HERE / "real_reviews_eval.csv"
SEED = 20261124
MIN_CHARS = 20
HARD_IRRELEVANT_MIN_CHARS = 40
PER_GROUP = {"문제 보고": 30, "기능 요청": 25, "둘 다": 5, "무관": 20}
TEST_SHARE = 0.4
SOURCE = "Wei et al. 2023, Zenodo 11066414 (MIT)"


def gold_labels(row):
    labels = []
    if row["problem_report"] == "1":
        labels.append("문제 보고")
    if row["feature_request"] == "1":
        labels.append("기능 요청")
    if row["irrelevant"] == "1":
        labels.append("무관")
    return labels


def load():
    """전체 리뷰를 읽고, 중복·짧은 글·라벨 없는 글을 뺀다."""
    rows, seen, dropped = [], set(), Counter()
    for path in sorted(RAW.glob("*.csv")):
        for row in csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines()):
            text = " ".join(row["data"].split())
            labels = gold_labels(row)
            if not labels:
                dropped["라벨 없음"] += 1
                continue
            if text.lower() in seen:
                dropped["중복"] += 1
                continue
            seen.add(text.lower())
            if len(text) < MIN_CHARS:
                dropped[f"{MIN_CHARS}자 미만"] += 1
                continue
            group = "둘 다" if labels == ["문제 보고", "기능 요청"] else labels[0] if len(labels) == 1 else None
            if group is None:
                dropped["라벨 조합 이상"] += 1
                continue
            rows.append({"app": row["app"], "lang": path.stem.split(".")[-1], "report": text, "gold_labels": labels,
                         "group": group, "stars": int(row["score"]), "thumbs_up": int(row["thumbsUpCount"] or 0)})
    return rows, dropped


def pick(rows, rng):
    """앱·언어·종류마다 정해진 수만큼 고른다."""
    pools = defaultdict(list)
    for row in rows:
        pools[(row["app"], row["lang"], row["group"])].append(row)
    chosen = []
    for app in sorted({r["app"] for r in rows}):
        for lang in ("en", "fr"):
            for group, n in PER_GROUP.items():
                pool = sorted(pools[(app, lang, group)], key=lambda r: r["report"])
                rng.shuffle(pool)
                if group == "무관":
                    long_enough = [r for r in pool if len(r["report"]) >= HARD_IRRELEVANT_MIN_CHARS]
                    unhappy = [r for r in long_enough if r["stars"] <= 3][: n // 2]
                    taken = {id(r) for r in unhappy}
                    others = [r for r in long_enough if id(r) not in taken][: n - len(unhappy)]
                    selected = unhappy + others
                else:
                    selected = pool[:n]
                if len(selected) < n:
                    print(f"주의: {app} {lang} {group} 후보가 {len(selected)}개뿐이에요")
                chosen += selected
    return chosen


def split(items, rng):
    """종류(문제 보고·기능 요청·둘 다·무관)마다 40%를 시험용으로 뗀다."""
    by_group = defaultdict(list)
    for item in items:
        by_group[item["group"]].append(item)
    for group in sorted(by_group):
        members = by_group[group]
        rng.shuffle(members)
        n_test = round(len(members) * TEST_SHARE)
        for index, item in enumerate(members):
            item["split"] = "test" if index < n_test else "dev"


def main():
    rng = random.Random(SEED)
    rows, dropped = load()
    chosen = pick(rows, rng)
    chosen.sort(key=lambda r: (r["app"], r["lang"], list(PER_GROUP).index(r["group"]), r["report"]))
    for number, item in enumerate(chosen, 1):
        item["id"] = f"rr-{number:03d}"
        item["source"] = SOURCE
    split(chosen, rng)

    keys = ["id", "split", "app", "lang", "report", "gold_labels", "stars", "thumbs_up", "source"]
    OUT_JSONL.write_text("".join(json.dumps({k: i[k] for k in keys}, ensure_ascii=False) + "\n" for i in chosen),
                         encoding="utf-8")
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:  # utf-8-sig: 엑셀에서 안 깨진다
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for i in chosen:
            writer.writerow({**{k: i[k] for k in keys}, "gold_labels": ", ".join(i["gold_labels"])})

    print(f"원본 리뷰 12,000개 중 쓸 수 있는 것 {len(rows)}개 (뺀 것: {dict(dropped)})")
    print(f"고른 리뷰 {len(chosen)}개")
    print("  종류:   " + ", ".join(f"{k} {v}" for k, v in Counter(i["group"] for i in chosen).items()))
    print("  앱·언어: " + ", ".join(f"{a} {l} {n}" for (a, l), n in sorted(Counter((i['app'], i['lang']) for i in chosen).items())))
    print("  나누기: " + ", ".join(f"{k} {v}" for k, v in Counter(i["split"] for i in chosen).items()))
    for group in PER_GROUP:
        members = [i for i in chosen if i["group"] == group]
        print(f"  {group}: 글 길이 중앙값 {statistics.median(len(i['report']) for i in members):.0f}자, "
              f"별점 평균 {statistics.mean(i['stars'] for i in members):.1f}")
    print("\n헷갈리는 '무관' 예시 (별점 3 이하, 영어)")
    for i in [i for i in chosen if i["group"] == "무관" and i["stars"] <= 3 and i["lang"] == "en"][:4]:
        print(f"  [{i['app']}, 별 {i['stars']}] {i['report'][:140]}")
    print("\n'둘 다' 예시 (영어)")
    for i in [i for i in chosen if i["group"] == "둘 다" and i["lang"] == "en"][:3]:
        print(f"  [{i['app']}] {i['report'][:160]}")
    print(f"\n저장: {OUT_JSONL}\n저장: {OUT_CSV}")


if __name__ == "__main__":
    main()
