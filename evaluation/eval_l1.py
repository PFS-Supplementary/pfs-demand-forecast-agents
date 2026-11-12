# eval_l1.py — evaluator for the L1 notation layer
#
# Position: L1 of the three-layer evaluation. No judge (LLM/NLI) is needed; everything is deterministic.
#   L1 notation layer    <- this file
#   L2 attribution layer : RG-P (calculation, data) = eval_rg.py / citation recall and precision = alce_eval.py
#   L3 content layer     : coverage of the items required by the guideline, classification of causes (manual)
#
# Indicators
# ─────────────────────────────────────────────────────────────────────────────
# (1) Marker rate           body sentences with a marker / body sentences
#     Rashkin et al. (2023) define AIS in two stages and use the first (interpretability) as
#     a filter before the second (attribution). The share of sentences that enter the
#     judgment is reported separately so that it is clear what the later indicators are
#     computed on. Wang (2019) points to the same issue: no record is extracted from
#     unsupported content, so a factuality metric does not penalize hallucination.
#     Reported as mean (sd) per case together with the raw fraction.
#
# (2) Citation id consistency, in two steps
#     (2a) nonexistent id      : an id that does not exist anywhere in the corpus
#     (2b) outside the set     : an id that exists in the corpus but is outside the set
#                                retrieved for the subsection
#     The two differ in kind (a fabricated basis vs. a basis attributed to the wrong place)
#     and are not merged. The distinction is defined only when the identifier space is
#     closed; in ALCE (Gao et al. 2023) the passages provided are cited by number, so it
#     does not arise.
#     Reference set: verification_source["guideline_chunk_ids"] (the set the Writer actually
#     saw, after gating). If absent, SECTION_CHUNK_IDS is used as a fallback and the number
#     of fallbacks is reported. The fallback is less exact: memory_agent hides the
#     special-purpose chunks conditionally, so the actual set is a subset of SECTION_CHUNK_IDS.
#
# (3) Distribution of marker types    guideline (by version) / calculation / input / data
#     The versions (202212 medical / 202105 culture and tourism) are separated to show how
#     often the sister guideline was cited.
#
# (4) Rate of stated uncomputed items   uncomputed items stated in the body / unique unresolved items
#     The denominator is unresolved, not skipped: skipped includes inputs missing in the
#     context of section 3-1 (before the catchment is set), which differ from items the
#     guideline requires but that could not be obtained. unresolved is filtered by
#     subsection and is in readable form. The numerator uses approximate matching, so all
#     matches are printed for manual checking.
#
# (5) Execution-exposure consistency    values exposed in a subsection / executed formulas
#     Detects a value that was computed but dropped by the subsection filter without a
#     warning. Runs without an executed field are skipped.
#
# (6) Subheading compliance     '[소제목]' lines / all heading lines
#     Measures whether headings are marked at generation time so that the evaluators do not
#     have to infer them.
#
# (7) Share of context-dependent sentences (auxiliary): sentences that begin with a
#     demonstrative or a conjunctive adverb and cannot be interpreted without the preceding
#     context. This is a prior measurement for the explicature procedure of Rashkin et al.,
#     not an indicator.
#
# Unit of aggregation
# ─────────────────────────────────────────────────────────────────────────────
# No average is taken across cases. The three cases were chosen to differ in type, not
# sampled at random from a superpopulation, so a spread attached to a cross-case mean would
# have no clear interpretation (the assumption required by the framework of Miller 2024
# does not hold).
#   - deterministic indicators whose rate sits at the upper bound -> summed raw fractions
#     (a full comparison, not an estimate)
#   - indicators that vary materially across runs -> mean (sd) per case, n = number of runs
#   - no confidence intervals are used.
#
# Usage:
#   python eval_l1.py --runs outputs/runs --out outputs/eval
#   (--retrieval and --catalog default to the files under pipeline/agents/)

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

import eval_common as E

# ─────────────────────────────────────────────────────────────────────────────
# SECTION_CHUNK_IDS (fallback path)
# ─────────────────────────────────────────────────────────────────────────────
# The reference set is normally guideline_chunk_ids in the output. For outputs without that
# field, the mapping is read from retrieval.py. Since retrieval.py imports chromadb, it is
# not imported as a module; only the constant definitions are extracted from the source and
# executed, so that the evaluator does not need a database connection.

_RE_SCI = re.compile(r"^SECTION_CHUNK_IDS.*?^\}", re.S | re.M)


def load_section_chunk_ids(retrieval_path: str | Path | None) -> dict[str, list[str]]:
    if not retrieval_path:
        return {}
    p = Path(retrieval_path)
    if not p.exists():
        print(f"[eval_l1] 경고: {retrieval_path} 없음 — SECTION_CHUNK_IDS 후퇴 생략")
        return {}
    src = p.read_text(encoding="utf-8")
    m = _RE_SCI.search(src)
    if not m:
        print(f"[eval_l1] 경고: {retrieval_path} 에서 SECTION_CHUNK_IDS 를 찾지 못함")
        return {}
    ns: dict = {}
    exec(m.group().replace(": dict[str, list[str]]", "", 1), ns)
    return ns.get("SECTION_CHUNK_IDS", {})


# ─────────────────────────────────────────────────────────────────────────────
# (4) Whether an uncomputed item is stated
# ─────────────────────────────────────────────────────────────────────────────
# Form of an unresolved string (from computation._readable_skipped_for_section):
#   "[특수목적 진료권] 연간 입원환자 수(시설) = 병상수 × 병상이용률 × 365
#    [대상 의료시설 종별에 따른 전국 병상이용률 기준] → 산출되지 않음
#    (확보되지 않은 입력: beds)"
# Rule: body sentences containing a 'not computed' signal word are candidates, and an item
# counts as stated if a candidate contains one of the item's Korean keywords.
# Matching is approximate, so every match and non-match is kept (the denominator is small
# enough for manual review).

_UNRESOLVED_SIGNALS = ("산출되지 않", "산출하지 못", "산출 불가", "미산출",
                       "확보되지 않", "확보하지 못", "제시하지 않", "제시되지 않")

# Keywords taken from the item name — tokens of two or more Hangul characters. The stopwords
# below do not discriminate between items and are excluded; with words such as '연간',
# '가능', or '사망자' left in, different items would attach to the same sentence.
_KEYWORD_STOP = {"진료권", "기준", "적용", "경우", "대상", "의료", "시설", "해당",
                 "따른", "지역", "평균", "수치", "항목", "특수", "목적", "일반",
                 "연간", "가능", "사망자", "추계", "규모", "이용률", "병상"}
_RE_HANGUL_TOKEN = re.compile(r"[가-힣]{2,}")

# Labels that tell the two variants (_catchment/_national) apart, found in the bracketed label of the item string.
#   e.g. "[대상 의료시설 종별에 따른 전국 병상이용률 기준]" -> national
#        "[해당 진료권의 평균 병상이용률 기준]"             -> catchment
_VARIANT_PATTERNS = {
    "national": ("전국",),
    "catchment": ("해당 진료권", "진료권의 평균", "진료권 평균"),
}


def _unresolved_variant(item: str) -> str | None:
    """Which of the two variants the item belongs to. None if it has no variant."""
    labels = " ".join(re.findall(r"\[[^\]]*\]", item))
    for var, pats in _VARIANT_PATTERNS.items():
        if any(p in labels for p in pats):
            return var
    return None


def _unresolved_keywords(item: str) -> tuple[str | None, set[str]]:
    """(required keyword, set of auxiliary keywords).

        The required keyword is the longest token taken from the item name ('입원환자',
        '외래환자', '응급사망자', '감소율', etc.). Without it in the sentence there is no match.
        The auxiliary keywords include the missing input keys (in English), since the body
        sometimes writes the keys as they are.
    """
    head = item.split("=")[0]  # the right-hand side of the formula is excluded
    head = re.sub(r"\[[^\]]*\]", " ", head)  # remove the bracketed labels (the variant label is used separately)
    toks = [t for t in _RE_HANGUL_TOKEN.findall(head) if t not in _KEYWORD_STOP]
    required = max(toks, key=len) if toks else None

    aux = set(toks)
    m = re.search(r"확보되지 않은 입력:\s*([^)]*)", item)
    if m:
        aux |= {k.strip() for k in m.group(1).split(",") if k.strip()}
    return required, aux


def score_unresolved(text: str, unresolved: list[str]) -> dict:
    """Whether uncomputed items are stated — conservative one-to-one matching.

        Rule (all must hold for a hit):
          (a) the sentence contains a 'not computed' signal word;
          (b) the sentence contains the item's required keyword;
          (c) if the item is one of the two variants, the sentence contains the wording of that variant;
          (d) a sentence is assigned to one item only (an assigned sentence is not reused).

        Since (a)-(d) are all required, the indicator is a lower bound. Items that are stated
        in different wording may go unmatched; the list of non-matches is printed for manual
        checking.
    """
    items = []
    seen = set()
    for u in unresolved:  # unique items only (the same item is reported repeatedly)
        key = u.strip()
        if key and key not in seen:
            seen.add(key)
            items.append(key)

    sents = E.body_sentences(text)
    cand = [E.strip_markers(s) for s in sents
            if any(sig in s for sig in _UNRESOLVED_SIGNALS)]

    used: set[int] = set()
    detail = []
    hit = 0
    # Items with a longer required keyword are assigned first — the more discriminating one takes the sentence first.
    order = sorted(range(len(items)),
                   key=lambda i: -len(_unresolved_keywords(items[i])[0] or ""))
    results: dict[int, dict] = {}
    for i in order:
        it = items[i]
        required, aux = _unresolved_keywords(it)
        variant = _unresolved_variant(it)
        matched, matched_idx = None, None
        for j, c in enumerate(cand):
            if j in used:
                continue
            if required and required not in c:
                continue
            if variant and not any(p in c for p in _VARIANT_PATTERNS[variant]):
                continue
            matched, matched_idx = c, j
            break
        if matched is not None:
            used.add(matched_idx)
            hit += 1
        results[i] = {"item": it, "required": required,
                      "variant": variant, "keywords": sorted(aux),
                      "matched": matched}
    detail = [results[i] for i in range(len(items))]

    return {"n_items": len(items), "n_stated": hit,
            "n_signal_sentences": len(cand), "detail": detail}


# ─────────────────────────────────────────────────────────────────────────────
# (7) Context-dependent sentences
# ─────────────────────────────────────────────────────────────────────────────
# Sentences that begin with an expression referring to the preceding sentence, so that the
# referent is not determined when the sentence stands alone. Rashkin et al. (2023) prescribe
# resolving such sentences into explicatures before judging attribution. The share measured
# here bounds the effect of applying the explicature procedure.

_RE_CONTEXT_DEP = re.compile(
    r"^(이|그|해당|동|본|위|앞|상기)(는|를|에|의|와|과|로|으로|것|러한|와 같|와같)"
    r"|^(이는|이를|이에|이와|이러한|그러나|그런데|따라서|또한|한편|아울러|다만|반면|"
    r"이후|이때|여기서|위와|앞서)"
)


def is_context_dependent(sentence: str) -> bool:
    return bool(_RE_CONTEXT_DEP.match(E.strip_markers(sentence)))


# ─────────────────────────────────────────────────────────────────────────────
# Scoring one subsection
# ─────────────────────────────────────────────────────────────────────────────

def score_section(sid: str, sec: dict,
                  section_chunk_ids: dict[str, list[str]],
                  corpus_ids: set[str]) -> dict:
    text = sec.get("text") or ""
    vs = sec.get("verification_source") or {}

    # ── Line classification; subheading compliance ──
    line_kinds = Counter(kind for _, kind in E.iter_lines(text))
    n_head_ok = line_kinds[E.LINE_SUBHEAD_OK]
    n_head_bad = line_kinds[E.LINE_SUBHEAD_MD] + line_kinds[E.LINE_SUBHEAD_NUM]

    # ── Body sentences; markers ──
    sents = E.body_sentences(text)
    n_sent = len(sents)
    n_marked = 0
    n_ctx_dep = 0
    types = Counter()
    gver = Counter()
    cited_ids = Counter()
    for s in sents:
        mk = E.parse_markers(s)
        if mk["has_marker"]:
            n_marked += 1
        if is_context_dependent(s):
            n_ctx_dep += 1
        for t in (E.MARKER_GUIDELINE, E.MARKER_CALC, E.MARKER_INPUT, E.MARKER_DATA):
            types[t] += len(mk[t])
        for cid in mk[E.MARKER_GUIDELINE]:
            cited_ids[cid] += 1
            gver[E.guideline_version(cid)] += 1

    # ── Citation id consistency ──
    stored = vs.get("guideline_chunk_ids")
    if stored:
        allow, ref_source = set(stored), "stored"
    else:
        allow, ref_source = set(section_chunk_ids.get(sid, [])), "fallback"
    n_cite = sum(cited_ids.values())
    halluc, out_of_scope = Counter(), Counter()
    for cid, c in cited_ids.items():
        if cid in allow:
            continue
        if cid in corpus_ids:
            out_of_scope[cid] += c
        else:
            halluc[cid] += c

    # ── Stated uncomputed items ──
    unres = score_unresolved(text, vs.get("unresolved") or [])

    # ── Execution-exposure consistency ──
    # A subsection run before the catchment is set (section 3-1) is reported as a separate row.
    # Section 3-1 runs before section 3-2, so there is no catchment. Merging its executed and
    # unsatisfied formulas with those of the other subsections would distort both counts.
    # The test is whether catchment_result is empty; no subsection id is hard-coded.
    pre_catchment = not (sec.get("catchment_result") or {})
    executed = vs.get("executed") or []
    exposed = set((vs.get("numbers") or {}).keys())
    exec_info = {"executed": list(executed), "exposed": sorted(exposed),
                 "n_executed": len(executed), "n_exposed": len(exposed),
                 "available": bool(executed), "pre_catchment": pre_catchment}

    return {
        "n_sentences": n_sent,
        "n_marked": n_marked,
        "marker_rate": (n_marked / n_sent) if n_sent else None,
        "n_context_dependent": n_ctx_dep,
        "marker_types": dict(types),
        "guideline_versions": dict(gver),
        "n_citations": n_cite,
        "citation_ref_source": ref_source,
        "n_id_hallucination": sum(halluc.values()),
        "id_hallucination": dict(halluc),
        "n_id_out_of_scope": sum(out_of_scope.values()),
        "id_out_of_scope": dict(out_of_scope),
        "n_subhead_ok": n_head_ok,
        "n_subhead_violation": n_head_bad,
        "subhead_violations": {
            "markdown": line_kinds[E.LINE_SUBHEAD_MD],
            "section_label": line_kinds[E.LINE_SUBHEAD_NUM],
        },
        "unresolved": unres,
        "execution": exec_info,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# (5) Subsections excluded from execution-exposure consistency
# ─────────────────────────────────────────────────────────────────────────────
# Subsections run before the catchment is set (empty catchment_result) are excluded from the
# aggregate. Their calculation context differs from the other subsections, and such a
# subsection is an overview that does not write calculated values in the body, so it is not
# a target of this indicator. The number excluded is kept in the report. The other
# indicators (marker rate, citation consistency, etc.) include these subsections.


_RE_ALIAS = re.compile(r"^OUTPUT_ALIAS.*?^\}", re.S | re.M)


def load_output_alias(catalog_path: str | Path | None) -> dict[str, str]:
    """Read OUTPUT_ALIAS (formula id -> exposed semantic name) from formula_catalog.py.

        Needed for execution-exposure consistency: executed keeps the formula_id while numbers
        is normalized to aliases, so without matching the names every aliased formula would be
        reported as missing. If it cannot be read, only the counts are compared.
        The automatically generated aliases (_catchment/_national variants) are not in the
        dictionary literal of the source, so for an id with a variant suffix the suffix is
        removed for the lookup and then attached again.
    """
    if not catalog_path:
        return {}
    p = Path(catalog_path)
    if not p.exists():
        print(f"[eval_l1] 경고: {catalog_path} 없음 — 실행–노출 이름 대응 생략")
        return {}
    m = _RE_ALIAS.search(p.read_text(encoding="utf-8"))
    if not m:
        return {}
    ns: dict = {}
    exec(m.group().replace(": dict[str, str]", "", 1), ns)
    return ns.get("OUTPUT_ALIAS", {})


_BU_SUFFIXES = ("_catchment", "_national")


def exposed_name(fid: str, alias: dict[str, str]) -> str:
    """The key under which an executed formula id is exposed in numbers."""
    if fid in alias:
        return alias[fid]
    for suf in _BU_SUFFIXES:  # variants are looked up without the suffix
        if fid.endswith(suf):
            base = fid[: -len(suf)]
            if base in alias:
                return alias[base] + suf
            # the 'special_' prefix together with a variant suffix
            if base.startswith("special_") and base[len("special_"):] in alias:
                return "special_" + alias[base[len("special_"):]] + suf
    if fid.startswith("special_") and fid[len("special_"):] in alias:
        return "special_" + alias[fid[len("special_"):]]
    return fid


def _mean_sd(values: list[float]) -> dict:
    vals = [v for v in values if v is not None]
    if not vals:
        return {"n": 0, "mean": None, "sd": None}
    if len(vals) == 1:
        return {"n": 1, "mean": vals[0], "sd": None}
    return {"n": len(vals), "mean": statistics.fmean(vals),
            "sd": statistics.stdev(vals)}


_SUM_KEYS = ("n_sentences", "n_marked", "n_context_dependent", "n_citations",
             "n_id_hallucination", "n_id_out_of_scope",
             "n_subhead_ok", "n_subhead_violation")


def aggregate(per_run: list[dict], alias: dict[str, str] | None = None) -> dict:
    """List of runs for one case -> summed raw fractions + mean (sd) across runs.

        Element of per_run: {"run": "01", "sections": {sid: score, ...}}
    """
    totals = Counter()
    types = Counter()
    gver = Counter()
    halluc_detail = Counter()
    scope_detail = Counter()
    unres_items = unres_stated = 0
    exec_runs = []  # per run: (executed set, exposed set, missing list)
    exec_pre = []  # subsections before the catchment is set — excluded from the aggregate
    run_marker_rates = []

    for r in per_run:
        run_sent = run_marked = 0
        run_executed: set[str] = set()
        run_exposed: set[str] = set()
        for sid, sc in r["sections"].items():
            for k in _SUM_KEYS:
                totals[k] += sc.get(k) or 0
            for t, c in (sc.get("marker_types") or {}).items():
                types[t] += c
            for v, c in (sc.get("guideline_versions") or {}).items():
                gver[v] += c
            for cid, c in (sc.get("id_hallucination") or {}).items():
                halluc_detail[cid] += c
            for cid, c in (sc.get("id_out_of_scope") or {}).items():
                scope_detail[f"{sid}:{cid}"] += c
            u = sc.get("unresolved") or {}
            unres_items += u.get("n_items", 0)
            unres_stated += u.get("n_stated", 0)
            ex = sc.get("execution") or {}
            if ex.get("available"):
                if ex.get("pre_catchment"):
                    exec_pre.append(sid)
                else:
                    run_executed.update(ex.get("executed") or [])
                    run_exposed.update(ex.get("exposed") or [])
            run_sent += sc.get("n_sentences") or 0
            run_marked += sc.get("n_marked") or 0
        run_marker_rates.append((run_marked / run_sent) if run_sent else None)
        # Whether an executed formula was exposed in no subsection. Without the alias map, names
        # cannot be matched, so only counts are compared and the missing list is left empty.
        if run_executed:
            if alias:
                missing = sorted(f for f in run_executed
                                 if exposed_name(f, alias) not in run_exposed)
            else:
                missing = []
            exec_runs.append({"run": r["run"],
                              "n_executed": len(run_executed),
                              "n_exposed": len(run_exposed),
                              "missing": missing,
                              "alias_available": bool(alias)})

    n_head = totals["n_subhead_ok"] + totals["n_subhead_violation"]
    return {
        "n_runs": len(per_run),
        # (1) Marker rate — raw fraction + mean (sd) across runs
        "marker_rate": {
            "numerator": totals["n_marked"],
            "denominator": totals["n_sentences"],
            "ratio": (totals["n_marked"] / totals["n_sentences"])
                     if totals["n_sentences"] else None,
            "per_run": _mean_sd(run_marker_rates),
        },
        # (2) Citation id consistency — raw fraction
        "citation_ids": {
            "n_citations": totals["n_citations"],
            "n_hallucination": totals["n_id_hallucination"],
            "n_out_of_scope": totals["n_id_out_of_scope"],
            "hallucinated_ids": dict(halluc_detail),
            "out_of_scope_ids": dict(scope_detail),
        },
        # (3) Distribution of marker types
        "marker_types": dict(types),
        "guideline_versions": dict(gver),
        # (4) Rate of stated uncomputed items — raw fraction
        "unresolved_disclosure": {
            "numerator": unres_stated, "denominator": unres_items,
            "ratio": (unres_stated / unres_items) if unres_items else None,
        },
        # (5) Execution-exposure consistency — compared on the union within a run.
        # The executed list of each subsection is the result of the same catalog run stored
        # repeatedly, so adding them would multiply the count by the number of subsections. The
        # union within a run is taken and 'formulas computed in this run' is compared with 'values
        # exposed in some subsection'.
        "execution_exposure": {
            "available": bool(exec_runs),
            "alias_available": bool(alias),
            "n_runs": len(exec_runs),
            "n_executed_total": sum(e["n_executed"] for e in exec_runs),
            "n_exposed_total": sum(e["n_exposed"] for e in exec_runs),
            "n_missing_total": sum(len(e["missing"]) for e in exec_runs),
            "runs_with_missing": [e for e in exec_runs if e["missing"]],
            "excluded_pre_catchment_sections": len(exec_pre),
        },
        # (6) Subheading compliance
        "subhead_compliance": {
            "numerator": totals["n_subhead_ok"], "denominator": n_head,
            "ratio": (totals["n_subhead_ok"] / n_head) if n_head else None,
        },
        # (7) Share of context-dependent sentences (auxiliary)
        "context_dependent": {
            "numerator": totals["n_context_dependent"],
            "denominator": totals["n_sentences"],
            "ratio": (totals["n_context_dependent"] / totals["n_sentences"])
                     if totals["n_sentences"] else None,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────

def _pct(x, digits=1):
    return "—" if x is None else f"{x*100:.{digits}f}%"


def write_summary(agg_by_case: dict, out_path: Path,
                  fallback_sections: list[str]) -> None:
    L = ["# L1 표기 층 평가 결과\n"]

    L.append("\n## (1) 마커 보유율\n")
    L.append("| 케이스 | 원분수 | 비율 | 실행 간 mean (sd) | n |")
    L.append("|---|---|---|---|---|")
    for case, a in agg_by_case.items():
        m = a["marker_rate"]; p = m["per_run"]
        sd = "—" if p["sd"] is None else f"{p['sd']:.3f}"
        mean = "—" if p["mean"] is None else f"{p['mean']:.3f}"
        L.append(f"| {case} | {m['numerator']}/{m['denominator']} | "
                 f"{_pct(m['ratio'])} | {mean} ({sd}) | {p['n']} |")

    L.append("\n## (2) 인용 id 정합\n")
    L.append("| 케이스 | 지침 마커 | id 환각 | 회수범위 이탈 |")
    L.append("|---|---|---|---|")
    for case, a in agg_by_case.items():
        c = a["citation_ids"]
        L.append(f"| {case} | {c['n_citations']} | {c['n_hallucination']} | "
                 f"{c['n_out_of_scope']} |")
    for case, a in agg_by_case.items():
        c = a["citation_ids"]
        if c["hallucinated_ids"]:
            L.append(f"\n- {case} 환각 id: {c['hallucinated_ids']}")
        if c["out_of_scope_ids"]:
            L.append(f"\n- {case} 범위 이탈(소절:id): {c['out_of_scope_ids']}")

    L.append("\n## (3) 마커 유형 분포\n")
    L.append("| 케이스 | 지침 | 계산 | 입력 | 자료 | 202212 | 202105 |")
    L.append("|---|---|---|---|---|---|---|")
    for case, a in agg_by_case.items():
        t = a["marker_types"]; g = a["guideline_versions"]
        L.append(f"| {case} | {t.get('guideline',0)} | {t.get('calc',0)} | "
                 f"{t.get('input',0)} | {t.get('data',0)} | "
                 f"{g.get('202212',0)} | {g.get('202105',0)} |")

    L.append("\n## (4) 미산출 항목 명시율\n")
    L.append("| 케이스 | 원분수 | 비율 |")
    L.append("|---|---|---|")
    for case, a in agg_by_case.items():
        u = a["unresolved_disclosure"]
        L.append(f"| {case} | {u['numerator']}/{u['denominator']} | "
                 f"{_pct(u['ratio'])} |")

    L.append("\n## (5) 실행–노출 정합\n")
    L.append("run 단위 합집합 비교. 진료권 확정 전 소절(catchment_result 공백 — §3-1)은 "
             "사업 소재지 단독으로 되돌려 계산되므로 집계에서 제외한다.\n")
    L.append("| 케이스 | 실행 공식(합) | 노출 값(합) | 미노출 | 제외 소절 |")
    L.append("|---|---|---|---|---|")
    for case, a in agg_by_case.items():
        e = a["execution_exposure"]
        if not e["available"]:
            L.append(f"| {case} | executed 미저장 | 측정 불가 | — | — |")
            continue
        miss = (str(e["n_missing_total"]) if e["alias_available"]
                else "판정 불가(별칭 맵 없음)")
        L.append(f"| {case} | {e['n_executed_total']} | {e['n_exposed_total']} | "
                 f"{miss} | {e['excluded_pre_catchment_sections']} |")
    for case, a in agg_by_case.items():
        e = a["execution_exposure"]
        for r in e.get("runs_with_missing", []):
            L.append(f"\n- {case} r{r['run']} 미노출 공식: {r['missing']}")

    L.append("\n## (6) 소제목 규약 준수율\n")
    L.append("| 케이스 | 원분수 | 비율 |")
    L.append("|---|---|---|")
    for case, a in agg_by_case.items():
        s = a["subhead_compliance"]
        L.append(f"| {case} | {s['numerator']}/{s['denominator']} | "
                 f"{_pct(s['ratio'])} |")

    L.append("\n## (7) 문맥 의존 문장 비율 (보조)\n")
    L.append("| 케이스 | 원분수 | 비율 |")
    L.append("|---|---|---|")
    for case, a in agg_by_case.items():
        c = a["context_dependent"]
        L.append(f"| {case} | {c['numerator']}/{c['denominator']} | "
                 f"{_pct(c['ratio'])} |")

    if fallback_sections:
        L.append("\n## 주의 — 인용 기준 집합 후퇴\n")
        L.append("아래 소절은 산출물에 guideline_chunk_ids 가 없어 "
                 "SECTION_CHUNK_IDS 로 후퇴했다. 게이트(특수목적 청크 비공개)가 "
                 "반영되지 않으므로 회수범위 이탈이 과소 계상될 수 있다.\n")
        L.append(f"- 해당 (케이스/회차/소절) {len(fallback_sections)}건")

    out_path.write_text("\n".join(L), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    _here = Path(__file__).resolve().parent  # resolved from this file's location, independent of the working directory
    ap.add_argument("--runs", default="outputs/runs")
    ap.add_argument("--out", default="outputs/eval")
    ap.add_argument("--retrieval", default=str(_here.parent / "pipeline" / "agents" / "retrieval.py"),
                    help="SECTION_CHUNK_IDS 후퇴 경로")
    ap.add_argument("--catalog", default=str(_here.parent / "pipeline" / "agents" / "sub" / "formula_catalog.py"),
                    help="OUTPUT_ALIAS 경로(실행–노출 이름 대응)")
    args = ap.parse_args()

    sci = load_section_chunk_ids(args.retrieval)
    alias = load_output_alias(args.catalog)
    corpus_ids = {cid for ids in sci.values() for cid in ids}
    if not corpus_ids:
        print("[eval_l1] 경고: 코퍼스 id 집합이 비었음 → id 환각/범위이탈 구분 불가")

    paths = E.find_runs(args.runs)
    if not paths:
        print(f"[eval_l1] {args.runs} 에서 chapter3_*.json 을 찾지 못함")
        print(E.describe_runs_dir(args.runs))
        return

    by_case: dict[str, list[dict]] = defaultdict(list)
    fallback: list[str] = []
    detail: dict = {}

    for p in paths:
        run = E.load_run(p)
        case = E.project_of(run, p)
        rid = E.run_index_of(p)
        sections = {}
        for sid, sec in E.iter_sections(run):
            sc = score_section(sid, sec, sci, corpus_ids)
            sections[sid] = sc
            if sc["citation_ref_source"] == "fallback" and sc["n_citations"]:
                fallback.append(f"{case}/r{rid}/{sid}")
        by_case[case].append({"run": rid, "sections": sections})
        detail[f"{case}/r{rid}"] = sections

    agg_by_case = {case: aggregate(runs, alias)
                   for case, runs in sorted(by_case.items())}

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "l1_metrics.json").write_text(
        json.dumps({"aggregate": agg_by_case, "per_section": detail},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")
    write_summary(agg_by_case, out / "l1_summary.md", fallback)

    print(f"[eval_l1] 산출물 {len(paths)}개 · 케이스 {len(agg_by_case)}개 채점 완료")
    for case, a in agg_by_case.items():
        m = a["marker_rate"]; c = a["citation_ids"]; s = a["subhead_compliance"]
        print(f"  {case}: 마커보유 {m['numerator']}/{m['denominator']} "
              f"({_pct(m['ratio'])}) · 지침마커 {c['n_citations']} "
              f"(환각 {c['n_hallucination']} / 범위이탈 {c['n_out_of_scope']}) "
              f"· 소제목규약 {_pct(s['ratio'])}")
    if fallback:
        print(f"[eval_l1] 인용 기준 집합 후퇴 {len(fallback)}건 "
              f"(guideline_chunk_ids 미저장 산출물)")
    print(f"[eval_l1] 저장: {out/'l1_metrics.json'} / {out/'l1_summary.md'}")


if __name__ == "__main__":
    main()