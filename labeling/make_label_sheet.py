# make_label_sheet.py — builds the sheet for manual checking of a sample of unsupported sentences
#
# Sample design:
#   - B: all unsupported sentences that cite the sister guideline (202105)
#   - A: a random sample of the rest (fixed seed)
#
# Basis:
#   - Wang (2019, section 3) samples 30 summaries at random and categorizes them
#     afterwards, rather than sampling by category. The random part follows that precedent.
#   - B is examined in full rather than sampled, because it is small and the discussion of
#     the sister guideline otherwise rests on the automatic classification alone.
#
# Avoiding anchoring:
#   The sheet contains neither the judge results nor the automatic classification. The
#   information for comparison is stored only in key.json. Samples are grouped by chunk so
#   that they can be judged without rereading the chunks.
#
# Output:
#   label_sheet.md    the sheet to read and judge (grouped by chunk, with the chunk texts)
#   label_entry.csv   for entry (id, category, evidence_span, note)
#   label_key.json    for comparison (id -> case/run/section/ids/stratum)

from __future__ import annotations

import argparse
import csv
import json
import random
from collections import defaultdict
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evaluation"))

SEED = 20260805
N_RANDOM = 30
N_SUPP = 20
SISTER_PREFIX = "202105"

try:
    from classify_unsupported import classify as _classify
except Exception:  # without the classifier, proceed without labels
    def _classify(sentence, ids):
        return ""

CATEGORIES = """\
① 지침 적용   — 청크는 기준을 규정하고, 문장은 그 기준을 사업에 적용한다
                (근거가 지침+사업자료로 쪼개져 청크만으로는 함의되지 않음)
② 자매 지침 미적용 판단 — 청크가 자기 적용 범위를 부정할 수 없어 함의 불가
③ 자료 부재 서술 — 자료가 없다/미산출이다 라는 서술
④ 기타         — 청크 오귀속, 지침에 없는 조작화 등
⑤ 신규         — 위 넷에 안 들어가면 직접 이름을 붙일 것
"""


def load_unsupported(metrics_path: Path) -> list[dict]:
    d = json.loads(metrics_path.read_text(encoding="utf-8"))
    rows = []
    for pid, agg in d["aggregate"].items():
        for x in agg.get("unsupported", []):
            rows.append({
                "case": pid,
                "run": x["run"],
                "section": x["section"],
                "cls": x["cls"],
                "ids": list(x["ids"]),
                "sentence": x["sentence"],
            })
    return rows


def load_chunks(dump_path: Path) -> dict[str, dict]:
    return {r["chunk_id"]: r for r in json.loads(dump_path.read_text(encoding="utf-8"))}


# ── Preceding sentences ──
# Some sentences cannot be judged on their own because they begin with a referring
# expression. What is judged and the criterion (the set of sentences unsupported in
# isolation, ALCE definition) stay the same; the preceding sentences are shown as context to
# help the labeler. The original preceding sentences are shown rather than the explicature
# rewrites, since a rewrite may merge propositions or add content.

import re as _re

_RE_MARKER = _re.compile(r"\s*「[^」]*」")
N_CONTEXT = 3


def _norm(s: str) -> str:
    return _re.sub(r"\s+", " ", _RE_MARKER.sub("", s)).strip()


def _split_sents(text: str) -> list[tuple[int, str]]:
    """(paragraph number, sentence). Paragraphs are separated by blank lines and split with
        _RE_SENT_SPLIT of eval_common (a period after a Hangul character, a parenthesis, or 」),
        the same rule as in make_e3_sheet.
    """
    import eval_common as _E
    out: list[tuple[int, str]] = []
    for pi, para in enumerate(_re.split(r"\n\s*\n", text)):
        para = para.strip()
        if not para:
            continue
        flat = _re.sub(r"\s+", " ", para.replace("\r", " "))
        for s in _E._RE_SENT_SPLIT.split(flat):
            s = s.strip()
            if s:
                out.append((pi, s))
    return out


def attach_context(rows: list[dict], runs_dir: Path) -> None:
    """Attach the preceding sentences (at most N_CONTEXT) to each sample. Markers are kept —
        what the preceding sentences cite is itself useful for the judgment.
    """
    cache: dict[tuple, dict] = {}
    n_miss = 0
    for r in rows:
        fkey = (r["case"], r["run"])
        if fkey not in cache:
            p = runs_dir / r["case"] / f"chapter3_{r['case']}_r{r['run']}.json"
            cache[fkey] = json.loads(p.read_text(encoding="utf-8"))
        text = cache[fkey][r["section"]]["text"]
        sents = _split_sents(text)
        target = _norm(r["sentence"])
        hit = [i for i, (_, s) in enumerate(sents)
               if _norm(s) == target or target in _norm(s)]
        if not hit:
            r["context"] = None  # shown as 'extraction failed' in the sheet
            n_miss += 1
            continue
        i = hit[0]
        pi_target = sents[i][0]
        prev = sents[max(0, i - N_CONTEXT):i]
        # Paragraph boundary — ¶ before a sentence from a paragraph other than that of the target
        r["context"] = [("¶ " if pi != pi_target else "") + s for pi, s in prev]
    if n_miss:
        print(f"[경고] 선행 문장 추출 실패 {n_miss}건 — 시트에 표기됨")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--metrics", default="alce_metrics.json")
    ap.add_argument("--chunks", default="chunks_dump.json")
    ap.add_argument("--runs", default="outputs/runs",
                    help="run 산출물 디렉토리(선행 문장 추출용)")
    ap.add_argument("--out", default=".")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--n-random", type=int, default=N_RANDOM)
    ap.add_argument("--n-supp", type=int, default=N_SUPP,
                    help="④ 잔여 범주 보충 표본 크기(0이면 미사용)")
    a = ap.parse_args()

    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)

    rows = load_unsupported(Path(a.metrics))
    chunks = load_chunks(Path(a.chunks))

    sister = [r for r in rows if any(i.startswith(SISTER_PREFIX) for i in r["ids"])]
    rest = [r for r in rows if r not in sister]

    rnd = random.Random(a.seed)
    picked = rnd.sample(rest, min(a.n_random, len(rest)))

    for r in sister:
        r["stratum"] = "B"  # sister guideline, in full
    for r in picked:
        r["stratum"] = "A"  # random

    # Stratum C (optional, --n-supp) — a supplementary sample from the residual category.
    # A is not redrawn after the seed is fixed. C is a separate sample drawn only from the
    # residual category, without touching A.
    #   A — composition of all unsupported sentences (random; Wang 2019, section 3)
    #   C — composition within the residual category (random within that category)
    # C must not be combined with A to compute proportions.
    supp: list[dict] = []
    if a.n_supp > 0:
        from classify_unsupported import classify, CAT_OTHER
        taken = {id(r) for r in sister} | {id(r) for r in picked}
        pool = [r for r in rest
                if id(r) not in taken
                and classify(r["sentence"], r["ids"]) == CAT_OTHER]
        supp = rnd.sample(pool, min(a.n_supp, len(pool)))
        for r in supp:
            r["stratum"] = "C"

    sample = sister + picked + supp

    # Chunk groups — keyed by the set of cited ids, in descending order of frequency.
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in sample:
        groups[tuple(sorted(r["ids"]))].append(r)
    ordered = sorted(groups.items(), key=lambda kv: -len(kv[1]))

    # Shuffled within a group so that the stratum is not revealed in the sheet.
    sheet_rows: list[dict] = []
    for _, items in ordered:
        rnd.shuffle(items)
        sheet_rows.extend(items)
    for n, r in enumerate(sheet_rows, 1):
        r["id"] = f"S{n:03d}"

    attach_context(sheet_rows, Path(a.runs))

    # ── label_sheet.md ──
    md: list[str] = []
    md.append("# 미뒷받침 문장 표본 검증 시트\n")
    md.append(f"총 {len(sheet_rows)}건. 청크별로 묶여 있으므로 각 절의 **청크 원문을 "
              "한 번 읽고** 그 절의 문장을 연달아 판정하십시오.\n")
    md.append("판정할 것: 이 문장이 **왜** 해당 청크로 뒷받침되지 않는가.\n")
    md.append("```\n" + CATEGORIES + "```\n")
    md.append("**`context_dependent` 를 먼저 적으십시오**(Y/N). 이 문장이 앞 문장 "
              "없이 그 자체로 해석되는가를 묻는 것이며, 범주와는 별개의 축입니다. "
              "Y 이면 앞 문장을 복원했다고 가정하고 범주를 판정하십시오.\n")
    md.append("각 문장에는 **선행 문장(최대 3개)이 참고 맥락으로 병기**돼 있습니다. "
              "판정 단위는 여전히 대상 문장 하나입니다 — 선행 문장은 지시 표현의 "
              "지시대상을 확인하는 용도로만 쓰고, 선행 문장 자체의 뒷받침 여부를 "
              "판정에 섞지 마십시오. `¶` 는 판정 대상과 다른 문단임을 뜻합니다.\n")
    md.append("범주와 함께 **근거 대목**을 적으십시오. 청크에 관련 대목이 있는데도 "
              "함의가 안 되는 경우(①·②)와, 청크에 아예 없는 경우(④)를 가르는 것이 "
              "이 작업의 핵심입니다.\n")
    md.append("기입은 `label_entry.csv` 에 하십시오.\n")
    md.append("\n---\n")

    # Each chunk text is included once (repeating it per group would multiply the length).
    used_ids: list[str] = []
    for ids, _ in ordered:
        for cid in ids:
            if cid not in used_ids:
                used_ids.append(cid)

    used_chunk_chars = 0
    md.append("\n# 제1부. 청크 원문\n")
    md.append("판정 전에 통독하십시오. 제2부의 각 묶음은 여기를 참조합니다.\n")
    for cid in used_ids:
        c = chunks.get(cid)
        if c is None:
            md.append(f"\n## `{cid}` — **컬렉션에 없음**\n")
            md.append("> 실재하지 않는 id 입니다(오탈자). 청크 원문 대조가 불가능하므로 "
                      "판정 시 그 사실을 적으십시오.\n")
            continue
        used_chunk_chars += c["chars"]
        md.append(f"\n## `{cid}` — {c['heading']} ({c['chars']:,}자)\n")
        md.append("```\n" + c["text"].strip() + "\n```\n")

    md.append("\n---\n")
    md.append("\n# 제2부. 판정 대상\n")

    for ids, items in ordered:
        md.append(f"\n## 인용 청크: `{' + '.join(ids)}`  ({len(items)}건)\n")
        md.append("→ 원문은 제1부의 해당 항목 참조.\n")
        md.append("\n#### 판정 대상 문장\n")
        for r in items:
            rid = r["id"]
            md.append(f"\n**{rid}**  (소절 {r['section']})\n")
            ctx = r.get("context")
            if ctx is None:
                md.append("> *(선행 문장 추출 실패 — 원문에서 직접 확인할 것)*\n")
            elif ctx:
                for c in ctx:
                    md.append(f"> <sub>{c}</sub>\n")
            md.append(f"> **⟶ {r['sentence']}**\n")
        md.append("\n---\n")

    (outdir / "label_sheet.md").write_text("\n".join(md), encoding="utf-8")

    # ── label_entry.csv ──
    with (outdir / "label_entry.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        # section is not included, because spreadsheet software converts '3-2' to a date; it is in
        # label_key.json and can be joined by id.
        # context_dependent — a sentence that depends on the judgment of the preceding sentence
        # without a referring expression is not caught by the explicature pattern and stays
        # unsupported. It is recorded on an axis separate from the category, since it is not
        # exclusive of the categories.
        w.writerow(["id", "context_dependent", "category",
                    "evidence_span", "note"])
        for r in sheet_rows:
            w.writerow([r["id"], "", "", "", ""])

    # ── label_key.json ──
    key = [{"id": r["id"], "stratum": r["stratum"], "case": r["case"],
            "run": r["run"], "section": r["section"], "cls": r["cls"],
            "ids": r["ids"], "sentence": r["sentence"],
            "auto_category": _classify(r["sentence"], r["ids"])}
           for r in sheet_rows]
    (outdir / "label_key.json").write_text(
        json.dumps({"seed": a.seed, "n_random": a.n_random,
                    "n_total_unsupported": len(rows),
                    "n_supp": a.n_supp, "n_sister": len(sister), "n_rest": len(rest),
                    "items": key}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    print(f"미뒷받침 전체 {len(rows)}건 · A 무작위 {len(picked)} · "
          f"B 자매지침 전수 {len(sister)} · C ④보충 {len(supp)}")
    print(f"시트 {len(sheet_rows)}건 · 청크 묶음 {len(ordered)}개 · "
          f"동봉 청크 분량 {used_chunk_chars:,}자")
    print(f"→ {outdir/'label_sheet.md'}, {outdir/'label_entry.csv'}, "
          f"{outdir/'label_key.json'}")


if __name__ == "__main__":
    main()