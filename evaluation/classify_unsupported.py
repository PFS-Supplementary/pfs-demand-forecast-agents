# classify_unsupported.py — classifies unsupported sentences by cause
#
# What it does
# ─────────────────────────────────────────────────────────────────────────────
# Divides the sentences that alce_eval.py judged unsupported (recall misses) by type of
# cause. Not every sentence that is unsupported under the ALCE definition is a citation
# error; many are a matter of sentence type, and recall is read as worse than it is unless
# that is shown.
#
# Why a separate tool
# ─────────────────────────────────────────────────────────────────────────────
# The rules are fixed in code and the assignment of each sentence is written to a file, so
# that the classification can be reproduced.
#
# Rules — the first match in order of priority is taken
# ─────────────────────────────────────────────────────────────────────────────
# (2) Judgment that the sister guideline does not apply
#     The cited ids include '202105' (the detailed guideline for culture and tourism).
#     Typical: a sentence stating that the gravity-model guidance concerns demand estimation
#     in the culture and tourism sector and is not suitable for setting a medical catchment.
#     The chunk only explains the gravity model and does not say it is unsuitable for
#     medical facilities, so the sentence is unsupported by the ALCE criterion. A source
#     text cannot deny its own scope of application, so this category cannot in principle be
#     confirmed by citation verification.
#     -> The only category that can be identified deterministically.
#
# (1) Application of the guideline (contains project-specific facts)
#     The sentence contains a project-specific proper noun.
#     Typical: a sentence stating that the hospital of the case is a special-purpose
#     hospital, with a citation of the chunk on special-purpose facilities.
#     The chunk sets the criterion (what a special-purpose facility is) and the sentence
#     applies it to the project. The basis is split in two (criterion = guideline,
#     fact = project data).
#
# (3) Statement that data are absent
#     The sentence says a method could not be applied because data were not available.
#     The chunk contains no such statement, so the sentence is unsupported, but it is also
#     the kind of statement the system is meant to produce when identifying weak points.
#
# (4) Other
#     The remainder: citation of a wrong chunk, an operationalization not in the guideline,
#     judge errors, and so on. This is the category that needs review, and a sample must be
#     checked manually.
#
# Limitations — to be reported together with the results
# ─────────────────────────────────────────────────────────────────────────────
# (1) and (3) are approximate classifications that rely on keyword lists. The keywords were
# derived from the sentences of the three cases and do not carry over to other cases as they
# are. (4) is a residual category, so anything missed by (1) and (3) falls into it. The
# output of this tool is therefore for gauging scale and drawing samples; a classification
# reported in a paper should rest on a manually checked sample.
# Precedent: Wang (2019) had 30 samples classified manually into four categories (RP, WC, UG, IC).
#
# Usage:
#   python classify_unsupported.py --metrics outputs/eval/alce_metrics.json \
#       --out outputs/eval --sample 50

from __future__ import annotations

import argparse
import csv
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

CAT_APPLY = "① 지침 적용(사업 고유사실)"
CAT_SISTER = "② 자매지침 미적용 판단"
CAT_NODATA = "③ 자료 부재 서술"
CAT_OTHER = "④ 기타(청크 오귀속·조작화·판정기 오판)"
CATEGORIES = [CAT_APPLY, CAT_SISTER, CAT_NODATA, CAT_OTHER]

# Project-specific proper nouns, derived from the outputs of the three cases. They must be
# extended when cases are added, which is itself a limit on the generality of this classification.
PROJECT_TERMS = (
    "아산", "울산", "시흥", "경찰병원", "의료원", "서울대", "배곧", "충무병원",
    "본 사업", "본 분석",
)

# Markers of statements that data are absent or a method cannot be applied.
NODATA_TERMS = (
    "적용하지", "적용되지", "적용할 수 없", "적용 대상이 아", "적용하기 어렵",
    "해당하지 않", "부적절", "한계가 있",
    "제공되지 않", "확보되지 않", "미보유", "부재", "산출되지 않",
)

SISTER_VERSION = "202105"


def classify(sentence: str, ids: list) -> str:
    """Take the first match in order of priority. The order of the rules changes the result, so it is fixed."""
    if any(str(i).startswith(SISTER_VERSION) for i in (ids or [])):
        return CAT_SISTER
    if any(t in sentence for t in PROJECT_TERMS):
        return CAT_APPLY
    if any(t in sentence for t in NODATA_TERMS):
        return CAT_NODATA
    return CAT_OTHER


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--metrics", default="outputs/eval/alce_metrics.json")
    ap.add_argument("--out", default="outputs/eval")
    ap.add_argument("--sample", type=int, default=50,
                    help="사람 확인용 표본 크기(범주별 비례 배분)")
    ap.add_argument("--seed", type=int, default=20260805,
                    help="표본 추출 난수 시드 — 재현성을 위해 고정")
    args = ap.parse_args()

    mpath = Path(args.metrics)
    if not mpath.exists():
        print(f"[classify] {mpath} 없음")
        return
    data = json.loads(mpath.read_text(encoding="utf-8"))
    meta = data.get("meta", {})
    if str(meta.get("judge", "")).startswith("estimate"):
        print("[classify] ⚠ 추정(--estimate) 모드 산출물이다. 판정이 실제가 아니므로 "
              "분류에 의미가 없다. 본 실행 결과로 다시 돌릴 것.")
        return

    rows = []
    per_case: dict[str, Counter] = defaultdict(Counter)
    for case, a in data.get("aggregate", {}).items():
        for u in a.get("unsupported", []):
            cat = classify(u.get("sentence", ""), u.get("ids", []))
            per_case[case][cat] += 1
            rows.append({
                "project_id": case,
                "run": u.get("run", ""),
                "section": u.get("section", ""),
                "cls": u.get("cls", ""),
                "category": cat,
                "chunk_ids": " | ".join(map(str, u.get("ids", []))),
                "sentence": u.get("sentence", ""),
                "human_label": "",  # column to be filled in manually
                "human_note": "",
            })

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # Classification of all sentences
    with open(out / "unsupported_classified.csv", "w",
              encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # Sample for manual checking — allocated in proportion to the categories, fixed seed
    rng = random.Random(args.seed)
    by_cat: dict[str, list] = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)
    total = len(rows)
    sample: list = []
    for cat in CATEGORIES:
        pool = by_cat.get(cat, [])
        if not pool:
            continue
        k = max(1, round(args.sample * len(pool) / total))
        sample.extend(rng.sample(pool, min(k, len(pool))))
    rng.shuffle(sample)  # shuffled, since an ordering by category would reveal the automatic classification
    with open(out / "unsupported_sample.csv", "w",
              encoding="utf-8-sig", newline="") as f:
        # The automatic classification is hidden from the labeler (to avoid anchoring)
        cols = ["project_id", "run", "section", "chunk_ids", "sentence",
                "human_label", "human_note"]
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(sample)
    # The key (automatic classification) is written to a separate file
    with open(out / "unsupported_sample_key.csv", "w",
              encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["project_id", "run", "section",
                                          "category"], extrasaction="ignore")
        w.writeheader()
        w.writerows(sample)

    # Summary
    L = ["# 미뒷받침 문장 원인 분류 (자동)\n",
         f"\n판정기: `{meta.get('judge','?')}` · 총 {total}건\n",
         "\n> 이 분류는 키워드 기반 근사다. ④ 는 잔여 범주이므로 ①③ 의 누락이 "
         "모두 여기로 흘러든다. 논문에 싣는 분류는 사람이 확인한 표본에 근거할 것"
         "(Wang 2019 는 30개 표본을 사람이 분류했다).\n"]
    L.append("\n| 케이스 | " + " | ".join(c[0] for c in CATEGORIES) + " | 합계 |")
    L.append("|" + "---|" * (len(CATEGORIES) + 2))
    for case in sorted(per_case):
        c = per_case[case]
        L.append(f"| {case} | " + " | ".join(str(c.get(k, 0)) for k in CATEGORIES)
                 + f" | {sum(c.values())} |")
    tot = Counter()
    for c in per_case.values():
        tot.update(c)
    L.append("| **합계** | " + " | ".join(str(tot.get(k, 0)) for k in CATEGORIES)
             + f" | {total} |")
    L.append("\n## 범주 정의\n")
    for k in CATEGORIES:
        L.append(f"- **{k}** — {tot.get(k,0)}건 ({tot.get(k,0)/total:.1%})")
    L.append(f"\n## 표본\n\n`unsupported_sample.csv` {len(sample)}건 "
             f"(범주별 비례 배분, seed={args.seed}). 자동 분류 결과는 "
             f"`unsupported_sample_key.csv` 로 분리했다 — 확인자에게 주지 말 것"
             f"(앵커링 방지).")
    (out / "unsupported_classification.md").write_text("\n".join(L),
                                                       encoding="utf-8")

    print(f"[classify] 총 {total}건 분류")
    for k in CATEGORIES:
        print(f"  {k:34s} {tot.get(k,0):4d}  ({tot.get(k,0)/total:.1%})")
    print(f"[classify] 저장: {out/'unsupported_classified.csv'} · "
          f"{out/'unsupported_sample.csv'}({len(sample)}건) · "
          f"{out/'unsupported_classification.md'}")


if __name__ == "__main__":
    main()
