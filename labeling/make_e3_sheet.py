# make_e3_sheet.py — builds the sheet for manual labeling of judge agreement
#
# Purpose:
#   A labeling sheet for measuring how far the support decisions of the judge agree with
#   human judgment.
#
# Sampling design:
#   - Population = the unique (set of cited chunks, sentence) pairs the judge actually
#     decided. The judge decides each pair once (cache), so the unique pair is the natural
#     unit of agreement. Converting to the accuracy over all citing sentences (instances)
#     needs weighting by the instance frequency of each pair, which is kept in key.json.
#   - Strata = 50 supported / 50 unsupported (so that kappa can be interpreted).
#   - Pairs with a missing chunk (no_premise) are excluded: the judge made no decision, so
#     there is nothing to compare. Citations of nonexistent ids belong to a separate indicator.
#   - Overlap with the sample for cause classification is allowed: simple random sampling is
#     used, and the question asked is different (cause category vs. Y/N).
#   - A separate fixed seed is used, distinct from that of the cause-classification sample.
#
# Question (following the AIS of Rashkin et al.):
#   "Is this sentence supported by the cited chunks taken together?" Y/N + the supporting span.
#   The condition is the same as in the ALCE recall definition — with multiple citations the
#   chunks are concatenated into one premise. The human judges the same thing the judge did.
#
# Reconstruction:
#   Cache records carry no origin (case/run/section). All run files are traversed, the
#   guideline-citing sentences are classified again by the eval_common conventions, and the
#   cache keys are recomputed to recover the decisions.
#
# Splitting the preceding sentences:
#   The same rule as the evaluator is used (eval_common._RE_SENT_SPLIT: a boundary only when
#   the period follows a Hangul character, a parenthesis, or 」), so that the sentence unit
#   of the sheet matches the evaluator and a period inside a formula does not split a sentence.
#
# Avoiding anchoring (three separate files):
#   e3_sheet.md    the sheet — no judge results. Supported and unsupported pairs are mixed
#                  within each chunk group so that the order does not reveal them either.
#   e3_entry.csv   for entry — id, supported, evidence_span, note.
#                  No section column (spreadsheet software converts values such as '3-2' to dates).
#   e3_key.json    the key — judge result, stratum, origin, instance frequency. Not to be
#                  opened before labeling is finished.
#
# Usage:
#   python make_e3_sheet.py --cache outputs/eval/judge_cache.json \
#       --chunks chunks_dump.json --runs outputs/runs --out outputs/e3
#
# Back up e3_entry.csv once labeling starts; the manual labels cannot be regenerated.

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
from collections import defaultdict
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evaluation"))
import eval_common as E  # noqa: E402

SEED = 20260807  # fixed seed, distinct from that of the cause-classification sample
N_PER_STRATUM = 50  # for each of supported / unsupported
N_CONTEXT = 3  # number of preceding sentences shown


# ─────────────────────────────────────────────────────────────────────────────
# 1) Reconstructing the decisions — traverse all runs and recompute the cache keys
# ─────────────────────────────────────────────────────────────────────────────

def cache_key(label: str, premise: str, hypothesis: str) -> str:
    # same as alce_eval.CachedJudge._key
    return hashlib.sha1(
        f"{label}\x00{premise}\x00{hypothesis}".encode()).hexdigest()


def reconstruct(runs_dir: Path, cache_path: Path,
                chunks: dict[str, str]) -> tuple[dict, dict, dict]:
    """Recover the decision for each unique pair.

        Returns: (supported_pairs, unsupported_pairs, stats)
          pairs: {(sorted ids tuple, sentence): [instance dict, ...]}
          instance: {case, run, section, cls}
    """
    raw = json.loads(cache_path.read_text(encoding="utf-8"))
    label = raw["_judge"]
    entries = raw["entries"]

    pairs: dict[tuple, dict] = {}
    n_hit = n_miss = n_no_premise = 0

    run_files = E.find_runs(runs_dir)
    if not run_files:
        raise SystemExit("run 파일 없음:\n" + E.describe_runs_dir(runs_dir))

    for p in run_files:
        run = E.load_run(p)
        case = E.project_of(run, p)
        ridx = E.run_index_of(p)
        for sid, sec in E.iter_sections(run):
            for s in E.body_sentences(sec.get("text") or ""):
                mk = E.parse_markers(s)
                g = mk[E.MARKER_GUIDELINE]
                if not g:
                    continue
                numeric = mk[E.MARKER_CALC] or mk[E.MARKER_INPUT]
                cls = "guideline_mixed" if numeric else "guideline_pure"
                clean = E.strip_markers(s)
                joined = "\n\n".join(chunks[c] for c in g if chunks.get(c))
                if not joined:
                    n_no_premise += 1  # nonexistent id — excluded
                    continue
                k = cache_key(label, joined, clean)
                rec = entries.get(k)
                if rec is None:
                    n_miss += 1
                    continue
                n_hit += 1
                pk = (tuple(sorted(g)), clean)
                if pk not in pairs:
                    pairs[pk] = {"ids": list(g), "sentence": clean,
                                 "supported": rec["supports"], "instances": []}
                pairs[pk]["instances"].append(
                    {"case": case, "run": ridx, "section": sid, "cls": cls})

    sup = {k: v for k, v in pairs.items() if v["supported"]}
    uns = {k: v for k, v in pairs.items() if not v["supported"]}
    stats = {"judge_label": label, "n_instances": n_hit,
             "n_cache_miss": n_miss, "n_no_premise": n_no_premise,
             "n_pairs_supported": len(sup), "n_pairs_unsupported": len(uns)}
    return sup, uns, stats


# ─────────────────────────────────────────────────────────────────────────────
# 2) Preceding sentences — the same splitting rule as the evaluator
# ─────────────────────────────────────────────────────────────────────────────

_RE_MARKER = re.compile(r"\s*「[^」]*」")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", _RE_MARKER.sub("", s)).strip()


def _split_sents(text: str) -> list[tuple[int, str]]:
    """(paragraph number, sentence). Paragraphs are separated by blank lines and split with
        _RE_SENT_SPLIT of eval_common (a period after a Hangul character, a parenthesis, or 」).
    """
    out: list[tuple[int, str]] = []
    for pi, para in enumerate(re.split(r"\n\s*\n", text)):
        para = para.strip()
        if not para:
            continue
        flat = re.sub(r"\s+", " ", para.replace("\r", " "))
        for s in E._RE_SENT_SPLIT.split(flat):
            s = s.strip()
            if s:
                out.append((pi, s))
    return out


def attach_context(items: list[dict], runs_dir: Path) -> int:
    """Attach the preceding sentences, taken from the representative instance (the smallest in
        sort order) of each sample. Markers are kept — what the preceding sentences cite is
        itself useful for the judgment.
    """
    cache: dict[tuple, dict] = {}
    n_miss = 0
    for r in items:
        inst = sorted(r["instances"],
                      key=lambda x: (x["case"], x["run"], x["section"]))[0]
        r["ctx_instance"] = inst
        fkey = (inst["case"], inst["run"])
        if fkey not in cache:
            p = (runs_dir / inst["case"]
                 / f"chapter3_{inst['case']}_r{inst['run']}.json")
            cache[fkey] = json.loads(p.read_text(encoding="utf-8"))
        text = cache[fkey][inst["section"]]["text"]
        sents = _split_sents(text)
        target = _norm(r["sentence"])
        hit = [i for i, (_, s) in enumerate(sents)
               if _norm(s) == target or target in _norm(s)]
        if not hit:
            r["context"] = None
            n_miss += 1
            continue
        i = hit[0]
        pi_target = sents[i][0]
        prev = sents[max(0, i - N_CONTEXT):i]
        r["context"] = [("¶ " if pi != pi_target else "") + s for pi, s in prev]
    return n_miss


# ─────────────────────────────────────────────────────────────────────────────
# 3) Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="outputs/eval/judge_cache.json")
    ap.add_argument("--chunks", default="chunks_dump.json")
    ap.add_argument("--runs", default="outputs/runs")
    ap.add_argument("--out", default="outputs/e3")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--n", type=int, default=N_PER_STRATUM,
                    help="층당 표본 수 (뒷받침/미뒷받침 각각)")
    a = ap.parse_args()

    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)

    chunk_rows = json.loads(Path(a.chunks).read_text(encoding="utf-8"))
    chunks = {c["chunk_id"]: c["text"] for c in chunk_rows}
    chunk_meta = {c["chunk_id"]: c for c in chunk_rows}

    sup, uns, stats = reconstruct(Path(a.runs), Path(a.cache), chunks)
    print(f"재구성: 인스턴스 {stats['n_instances']:,} (캐시 실패 "
          f"{stats['n_cache_miss']} / 결측 제외 {stats['n_no_premise']})")
    print(f"고유 쌍: 뒷받침 {stats['n_pairs_supported']:,} / "
          f"미뒷받침 {stats['n_pairs_unsupported']:,}")
    if stats["n_cache_miss"]:
        raise SystemExit("⚠ 캐시 실패가 있다 — 캐시·청크·run 이 같은 배치의 "
                         "것인지 확인할 것. 표집을 진행하지 않는다.")

    rnd = random.Random(a.seed)
    pick_sup = rnd.sample(sorted(sup.keys()), min(a.n, len(sup)))
    pick_uns = rnd.sample(sorted(uns.keys()), min(a.n, len(uns)))
    sample = [dict(sup[k], stratum="S") for k in pick_sup] \
        + [dict(uns[k], stratum="U") for k in pick_uns]

    # Chunk groups (to avoid rereading) — shuffled within a group so that the order does not reveal the stratum.
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in sample:
        groups[tuple(sorted(r["ids"]))].append(r)
    ordered = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    sheet_rows: list[dict] = []
    for _, items in ordered:
        rnd.shuffle(items)
        sheet_rows.extend(items)
    for n, r in enumerate(sheet_rows, 1):
        r["id"] = f"E{n:03d}"

    n_ctx_miss = attach_context(sheet_rows, Path(a.runs))
    if n_ctx_miss:
        print(f"[경고] 선행 문장 추출 실패 {n_ctx_miss}건 — 시트에 표기됨")

    # ── e3_sheet.md ──
    md: list[str] = []
    md.append("# E3 인간 라벨링 시트 — 인용 뒷받침 판정\n")
    md.append(f"총 {len(sheet_rows)}건. 청크별로 묶여 있으므로 각 절의 **청크 "
              "원문을 한 번 읽고** 그 절의 문장을 연달아 판정하십시오.\n")
    md.append("## 판정 문항\n")
    md.append("**이 문장이 인용된 청크로 뒷받침되는가?** — 청크에 적힌 내용만으로 "
              "이 문장이 참이라고 결론지을 수 있으면 Y, 없으면 N.\n")
    md.append("- 인용이 여러 청크면 **전부 합쳐 하나의 근거**로 봅니다"
              "(개별 청크가 아니라 결합 전체가 뒷받침하는지).\n"
              "- 문장이 청크의 기준을 **사업에 적용**한 것이라면(사업 고유 수치·"
              "지명 등 청크에 없는 사실이 섞임) N 입니다 — 청크만으로는 그 적용 "
              "결과까지 결론지을 수 없기 때문입니다.\n"
              "- 세상 지식으로 참인지가 아니라, **청크가 함의하는지**만 봅니다.\n")
    md.append("## 기입 방법 (`e3_entry.csv`)\n")
    md.append("- `supported`: Y 또는 N\n"
              "- `evidence_span`: **Y 이면 필수** — 판단 근거가 된 청크의 대목을 "
              "짧게 옮겨 적으십시오(그 대목을 실제로 찾은 뒤 Y 를 찍는 절차). "
              "N 이면 가장 가까운 대목을 적거나 '없음'이라 적으십시오.\n"
              "- `note`: 판단이 어려웠던 이유 등 자유 기재(선택).\n")
    md.append("각 문장에는 **선행 문장(최대 3개)이 참고 맥락으로 병기**돼 있습니다. "
              "판정 단위는 대상 문장 하나입니다 — 선행 문장은 지시 표현의 "
              "지시대상 확인용이며, 선행 문장의 뒷받침 여부를 판정에 섞지 "
              "마십시오. `¶` 는 대상과 다른 문단임을 뜻합니다.\n")
    md.append("\n---\n")

    used_ids: list[str] = []
    for ids, _ in ordered:
        for cid in ids:
            if cid not in used_ids:
                used_ids.append(cid)

    used_chunk_chars = 0
    md.append("\n# 제1부. 청크 원문\n")
    md.append("판정 전에 통독하십시오. 제2부의 각 묶음은 여기를 참조합니다.\n")
    for cid in used_ids:
        c = chunk_meta.get(cid)
        used_chunk_chars += c["chars"]
        md.append(f"\n## `{cid}` — {c['heading']} ({c['chars']:,}자)\n")
        md.append("```\n" + c["text"].strip() + "\n```\n")

    md.append("\n---\n")
    md.append("\n# 제2부. 판정 대상\n")
    for ids, items in ordered:
        md.append(f"\n## 인용 청크: `{' + '.join(ids)}`  ({len(items)}건)\n")
        md.append("→ 원문은 제1부의 해당 항목 참조.\n")
        for r in items:
            inst = r["ctx_instance"]
            md.append(f"\n**{r['id']}**  (소절 {inst['section']})\n")
            ctx = r.get("context")
            if ctx is None:
                md.append("> *(선행 문장 추출 실패 — 원문에서 직접 확인할 것)*\n")
            elif ctx:
                for c in ctx:
                    md.append(f"> <sub>{c}</sub>\n")
            md.append(f"> **⟶ {r['sentence']}**\n")
        md.append("\n---\n")

    (outdir / "e3_sheet.md").write_text("\n".join(md), encoding="utf-8")

    # ── e3_entry.csv ──
    with (outdir / "e3_entry.csv").open("w", encoding="utf-8-sig",
                                        newline="") as f:
        w = csv.writer(f)
        # No section column (date conversion in spreadsheet software). Joined by id from key.json.
        w.writerow(["id", "supported", "evidence_span", "note"])
        for r in sheet_rows:
            w.writerow([r["id"], "", "", ""])

    # ── e3_key.json (the key — not to be opened before labeling is finished) ──
    key = [{"id": r["id"], "stratum": r["stratum"],
            "judge_supported": r["supported"],
            "ids": r["ids"], "sentence": r["sentence"],
            "n_instances": len(r["instances"]),
            "instances": r["instances"],
            "ctx_instance": r["ctx_instance"]}
           for r in sheet_rows]
    (outdir / "e3_key.json").write_text(
        json.dumps({"seed": a.seed, "n_per_stratum": a.n, **stats,
                    "design": {
                        "unit": "unique (sorted chunk ids, sentence) pair",
                        "no_premise_excluded": stats["n_no_premise"],
                        "overlap_with_cause_sample": "allowed",
                        "question": "sentence supported by joined chunks (Y/N) "
                                    "+ evidence span"},
                    "items": key},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")

    sheet_chars = sum(len(x) for x in md)
    print(f"표본: 뒷받침 {len(pick_sup)} / 미뒷받침 {len(pick_uns)} · "
          f"청크 묶음 {len(ordered)}개 · 동봉 청크 {len(used_ids)}개 "
          f"{used_chunk_chars:,}자")
    print(f"시트 총 분량 {sheet_chars:,}자")
    print(f"→ {outdir/'e3_sheet.md'}, {outdir/'e3_entry.csv'}, "
          f"{outdir/'e3_key.json'}")
    print("※ e3_entry.csv 백업 경로를 지금 정할 것")


if __name__ == "__main__":
    main()
