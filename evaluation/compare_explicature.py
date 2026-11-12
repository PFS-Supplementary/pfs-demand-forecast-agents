# compare_explicature.py — comparison with and without explicature
#
# What it does
# ─────────────────────────────────────────────────────────────────────────────
# Takes two alce_metrics.json files in which the same outputs were judged under two
# conditions, and tallies the change in recall and the sentence-level flips.
#
# base — sentences judged in isolation (without --explicature)
# exp  — judged with explicature applied
#
# Why it is needed
# ─────────────────────────────────────────────────────────────────────────────
# Part of the unsupported sentences are context-dependent sentences that begin with a
# referring expression. Recall therefore depends in part on the unit of judgment, and the
# size of that effect has to be measured to say what the indicator measures.
#
# Both directions are counted
# ─────────────────────────────────────────────────────────────────────────────
# Explicature is applied to every context-dependent sentence, not only to unsupported ones,
# so flips from supported to unsupported also occur. Both directions are counted and the net
# change is reported.
#
# Judgment failures are separated
# ─────────────────────────────────────────────────────────────────────────────
# Items whose failure is 'no_premise' (missing chunk) or 'judge_error' are not a matter of
# citation quality; they are excluded from the flip counts and reported separately.
#
# Usage:
#   python compare_explicature.py \
#       --base outputs/eval/alce_metrics.json \
#       --exp  outputs/eval_explicature/alce_metrics.json \
#       --out  outputs/eval_explicature

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

FAILURE_EXCLUDED = ("no_premise", "judge_error")


def key_of(case: str, rec: dict) -> tuple:
    """Key for matching flips. sentence is the original text without markers and is the same
        whether or not explicature is applied. run and section are included to tell apart the
        same sentence in different runs.
    """
    return (case, rec["run"], rec["section"], rec["sentence"])


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def unsup_map(data: dict) -> dict[tuple, dict]:
    out = {}
    for case, agg in data.get("aggregate", {}).items():
        for u in agg.get("unsupported", []):
            out[key_of(case, u)] = {**u, "case": case}
    return out


def recall_of(data: dict) -> dict[str, dict]:
    out = {}
    for case, agg in data.get("aggregate", {}).items():
        wm = agg.get("with_mixed", {})
        out[case] = {
            "recall": wm.get("recall"),
            "precision": wm.get("precision"),
            "recall_per_run": wm.get("recall_per_run"),
            "n_unsupported": len(agg.get("unsupported", [])),
        }
    return out


def fmt(x, d=1):
    return "—" if x is None else f"{x*100:.{d}f}%"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="문장 단독 판정 metrics")
    ap.add_argument("--exp", required=True, help="explicature 적용 metrics")
    ap.add_argument("--out", default=".")
    a = ap.parse_args()

    base, exp = load(Path(a.base)), load(Path(a.exp))
    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)

    # ── Integrity checks ──
    warn: list[str] = []
    bm, em = base.get("meta", {}), exp.get("meta", {})
    if bm.get("judge") != em.get("judge"):
        warn.append(f"판정기가 다르다 — base `{bm.get('judge')}` / "
                    f"exp `{em.get('judge')}`. 전환이 explicature 때문인지 "
                    f"판정기 때문인지 구분할 수 없다.")
    if bm.get("judge_prompt") and em.get("judge_prompt") \
            and bm["judge_prompt"] != em["judge_prompt"]:
        warn.append("판정 프롬프트가 다르다 — 대조가 성립하지 않는다.")
    if bm.get("explicature"):
        warn.append("base 가 이미 explicature 적용본이다 — --base/--exp 를 바꿨는가.")
    if not em.get("explicature"):
        warn.append("exp 가 explicature 미적용본이다 — --explicature 를 켜고 돌렸는가.")

    bu, eu = unsup_map(base), unsup_map(exp)

    def usable(rec: dict) -> bool:
        return rec.get("failure") not in FAILURE_EXCLUDED

    to_supported = {k: v for k, v in bu.items() if k not in eu}
    to_unsupported = {k: v for k, v in eu.items() if k not in bu}
    still = {k: v for k, v in eu.items() if k in bu}

    # Separate the judgment failures
    n_fail_base = sum(1 for v in bu.values() if not usable(v))
    n_fail_exp = sum(1 for v in eu.values() if not usable(v))

    conv = {k: v for k, v in to_supported.items() if usable(v)}
    rev = {k: v for k, v in to_unsupported.items() if usable(v)}

    # A resolved sentence disappears from the unsupported list of exp, so its explicature cannot
    # be read there. It is looked up by the original sentence in explicatures.json written by
    # alce_eval.py.
    expmap: dict[str, str] = {}
    mp = Path(a.exp).parent / "explicatures.json"
    if mp.exists():
        expmap = json.loads(mp.read_text(encoding="utf-8")).get("mapping", {})
    else:
        warn.append("explicatures.json 이 없다 — 해소 사례의 복원 내용을 확인할 수 "
                    "없다. alce_eval.py 수정본으로 다시 돌릴 것.")

    n_still_with_exp = sum(1 for v in still.values() if v.get("explicature"))

    # ── Report ──
    L: list[str] = ["# explicature 적용/미적용 대조\n"]
    if warn:
        L.append("\n> **경고**\n")
        for w in warn:
            L.append(f"> - {w}\n")

    L.append(f"\n판정기 `{em.get('judge','?')}` · "
             f"explicature 생성 `{em.get('explicature_converted','?')}`건 변환\n")

    L.append("\n## 1. 지표 변화\n")
    L.append("\n| 케이스 | recall(단독) | recall(복원) | 변화 | "
             "precision(단독) | precision(복원) | 미뒷받침(단독) | 미뒷받침(복원) |")
    L.append("|---|---|---|---|---|---|---|---|")
    rb, re_ = recall_of(base), recall_of(exp)
    for case in sorted(rb):
        b, e = rb[case], re_.get(case, {})
        d = (None if b["recall"] is None or e.get("recall") is None
             else e["recall"] - b["recall"])
        L.append(f"| {case} | {fmt(b['recall'])} | {fmt(e.get('recall'))} | "
                 f"{'—' if d is None else f'{d*100:+.1f}%p'} | "
                 f"{fmt(b['precision'])} | {fmt(e.get('precision'))} | "
                 f"{b['n_unsupported']} | {e.get('n_unsupported','—')} |")

    L.append("\n## 2. 문장 단위 전환\n")
    n_conv_restored = sum(1 for v in conv.values() if v["sentence"] in expmap)
    L.append(f"\n- 미뒷받침 → 뒷받침 (**복원으로 해소**): **{len(conv)}건** "
             f"(그중 실제로 explicature 가 생성된 것 {n_conv_restored}건)")
    L.append(f"- 뒷받침 → 미뒷받침 (**복원으로 역전**): **{len(rev)}건**")
    L.append(f"- 순변화: **{len(conv) - len(rev):+d}건**")
    L.append(f"- 여전히 미뒷받침: {len(still)}건 "
             f"(그중 explicature 가 붙은 것 {n_still_with_exp}건)")
    L.append(f"\n판정 실패 제외분 — base {n_fail_base}건 · exp {n_fail_exp}건 "
             f"(청크 결측·판정기 오류. 인용 품질 문제가 아니므로 전환 집계에서 뺐다).")

    for title, grp in (("해소", conv), ("역전", rev)):
        L.append(f"\n### {title} {len(grp)}건 — 소절 분포\n")
        c = Counter(v["section"] for v in grp.values())
        L.append("| 소절 | 건수 |")
        L.append("|---|---|")
        for k, n in c.most_common():
            L.append(f"| {k} | {n} |")
        cc = Counter(v["case"] for v in grp.values())
        L.append(f"\n케이스: " + " · ".join(f"{k} {n}" for k, n in cc.most_common()))

    L.append("\n## 3. 해석 시 주의\n")
    L.append("\n- `_RE_CONTEXT_DEP` 은 지시 표현으로 **시작하는** 문장만 잡는다. "
             "지시어 없이 앞 문장의 판단에 의존하는 문장(예: 이중진료권 분기가 "
             "앞에서 확정되어야 성립하는 서술)은 복원되지 않고 미뒷받침으로 남는다. "
             "따라서 '해소' 건수는 문맥 의존 문제의 하한이다.")
    L.append("- explicature 생성기는 생성 모델과 같은 계열이다. 다만 선행 문장만 "
             "받고 지침 청크를 보지 않으므로 정보 우위는 없다.")
    L.append("- '역전' 건수를 함께 보고할 것. 해소만 적으면 결과가 한쪽으로 기운다.")

    (outdir / "compare_explicature.md").write_text("\n".join(L), encoding="utf-8")

    # ── CSV of flips ──
    with (outdir / "explicature_flips.csv").open(
            "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["direction", "case", "run", "section", "chunk_ids",
                    "sentence", "explicature"])
        for v in conv.values():
            w.writerow(["해소(미뒷받침→뒷받침)", v["case"], v["run"], v["section"],
                        " | ".join(v["ids"]), v["sentence"],
                        expmap.get(v["sentence"], "")])
        for v in rev.values():
            w.writerow(["역전(뒷받침→미뒷받침)", v["case"], v["run"], v["section"],
                        " | ".join(v["ids"]), v["sentence"],
                        v.get("explicature") or expmap.get(v["sentence"], "")])

    print(f"[compare] 해소 {len(conv)} · 역전 {len(rev)} · "
          f"순변화 {len(conv)-len(rev):+d} · 잔존 {len(still)}")
    for w_ in warn:
        print("[compare] ⚠", w_)
    print(f"[compare] 저장: {outdir/'compare_explicature.md'} · "
          f"{outdir/'explicature_flips.csv'}")


if __name__ == "__main__":
    main()
