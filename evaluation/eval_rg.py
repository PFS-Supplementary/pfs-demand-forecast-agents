# eval_rg.py — L2 attribution layer: RG Precision
#
# What is measured
# ─────────────────────────────────────────────────────────────────────────────
# Whether a figure written in the body agrees with its source, checked without a judge.
# L1 (eval_l1) measures whether a marker is attached; this file measures whether the value
# the marker points to is correct, by comparing the number written in the sentence with the
# number in the calculation log or the data records.
#
# Source of the definition
# ─────────────────────────────────────────────────────────────────────────────
# RG (Relation Generation) is defined in prose in section 3.2 of Wiseman et al. (2017): the
# precision and number of unique relations extracted from the generated text that also
# appear in the source records s. The formula RGP = |cp^r ∩ S| / |cp^r| is the formalization
# in section 3.2 of Wang (2019).
#
# Difference from those studies: Wiseman, Puduppully, and Wang pass the generated text
# through an IE model that extracts the relations again, and that step has its own error.
# This pipeline states (type, entity) at generation time with 「계산: key」, so there is no
# extraction step and only the value is compared.
#
# Unit of aggregation — occurrences
# ─────────────────────────────────────────────────────────────────────────────
# Earlier studies count unique relations. Here each written figure is a unit of
# verification, so the occurrence basis is the main indicator and the denominator on the
# unique basis is reported alongside for comparability. If the same value appears in two
# subsections, a reader has to check it twice, and an error in either is a real defect.
#
# What is compared against (S) — by marker type
# ─────────────────────────────────────────────────────────────────────────────
# 「계산: key」 -> verification_source.numbers[key]   (formula output)
# 「입력: key」 -> verification_source.inputs[key]    (value obtained from the database or the project plan)
# data label    -> one of three cases:
#    (a) equals data_records[].source           -> the record values of that source
#    (b) equals a value of input_sources/seed_sources -> the corresponding inputs[key]
#    (c) otherwise (the project plan, external yearbooks, etc.) -> out of scope
#        The project plan is not held as records, so there is nothing to compare against.
#        The number out of scope is always reported: otherwise the denominator would shrink
#        unnoticed and precision would be inflated (the issue noted by Wang 2019).
# 「KDI 세부지침 <표 …>」 -> reference to a guideline table. Not a source of figures; not a target.
#
# Outcomes
# ─────────────────────────────────────────────────────────────────────────────
# supported      : some figure in the sentence agrees with the source value
# value_mismatch : the source exists but no figure in the sentence agrees with it
# key_missing    : the key the marker points to is not in the source (a nonexistent key)
# no_number      : the sentence has no figure to compare (a marker without a value)
# out_of_scope   : nothing to compare against is defined (data case (c))
#
# Normalization rules (Wang 2019, section 4.1, applies corrections of the same kind)
# ─────────────────────────────────────────────────────────────────────────────
# - Thousands separators are removed.
# - Rounding: the source value is rounded to the number of decimals written in the body
#   (source 12.84 <-> body 12.8; source 621.45 <-> body 621).
# - Percentages: if the body token carries '%', 1/100 of its value is also a candidate
#   (source ri=1.0 <-> body '100%'). Without '%' no scaling is applied; allowing it
#   unconditionally would match 8544 to 0.8544 and inflate precision.
# - Sign: agreement in absolute value is accepted, and this is recorded in the outcome
#   (source -0.12917 <-> body '0.12917만큼 감소').
# - One number token is assigned to one marker only; otherwise precision would rise simply
#   because a sentence has several figures.
#
# Usage:
#   python eval_rg.py --runs outputs/runs --out outputs/eval

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

import eval_common as E

# ─────────────────────────────────────────────────────────────────────────────
# 1) Extracting figures from a sentence
# ─────────────────────────────────────────────────────────────────────────────
# Markers are removed first — a marker label may itself contain numbers, and without removal
# the sentence would be supported by the numbers in its own label.

_RE_NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?\s*%?")

# ── Numbers that are not value claims ──
# Cases in which the sentence contains a number that is not the value the marker points to;
# unless filtered out, they would be counted as value_mismatch.
#   - years and periods    '(2029~2058년)', '2019년'
#   - unit constants       '인구 1,000명당', '10만명당'  -> 1,000 is a unit, not a value
#   - type numbers         'KDI 세부지침 <표 Ⅲ-1>의 유형 2'
#   - section and chapter numbers  '제5차', '제2절'
# A source value can itself be a year (e.g. the year construction starts), so a year is
# excluded only when the source value is outside the range of years.
_RE_YEAR = re.compile(r"(?:19|20)\d{2}(?:\s*[~∼\-–]\s*(?:19|20)\d{2})?\s*년")
_RE_UNIT_CONST = re.compile(r"[\d,]+\s*(?:명당|천명당|만명당|인당)")
_RE_ORDINAL = re.compile(r"(?:유형|표|그림|제)\s*[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]*\s*-?\s*\d+")
_YEAR_MIN, _YEAR_MAX = 1900, 2100


def _spans(patterns, text) -> list[tuple[int, int]]:
    return [m.span() for p in patterns for m in p.finditer(text)]


def extract_numbers(sentence: str) -> list[dict]:
    """Extract the number tokens from a sentence with its markers removed.

        Element returned: {"raw", "value", "percent", "decimals", "start",
                           "is_year", "is_excluded"}
    """
    text = E.strip_markers(sentence)
    year_spans = _spans([_RE_YEAR], text)
    excl_spans = _spans([_RE_UNIT_CONST, _RE_ORDINAL], text)
    out = []
    for m in _RE_NUMBER.finditer(text):
        raw = m.group().strip()
        percent = raw.endswith("%")
        body = raw.rstrip("%").strip().replace(",", "")
        try:
            value = float(body)
        except ValueError:
            continue
        decimals = len(body.split(".")[1]) if "." in body else 0
        inside = lambda spans: any(a <= m.start() and m.end() <= b + 6
                                   for a, b in spans)
        out.append({"raw": raw, "value": value, "percent": percent,
                    "decimals": decimals, "start": m.start(),
                    "is_year": inside(year_spans),
                    "is_excluded": inside(excl_spans)})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 2) Value agreement
# ─────────────────────────────────────────────────────────────────────────────

def match_value(tok: dict, ref) -> str | None:
    """Whether a number token agrees with the source value ref. Returns the mode if so, otherwise None.

        Mode: 'exact' | 'rounded' | 'truncated' | 'percent' | 'abs'

        Truncation is accepted separately because the Writer sometimes truncates rather than
        rounds (e.g. source 3458.64255… written as '3,458.6425', whereas rounding to four
        decimals gives 3458.6426). Wang (2019, section 4.1) likewise corrects for differences
        in the rounding of percentages between summary and table. The modes are tallied
        separately, so the contribution of each rule is visible in the table.
    """
    try:
        r = float(ref)
    except (TypeError, ValueError):
        return None

    d = tok["decimals"]
    scale = 10 ** d
    cands = [(tok["value"], "exact")]
    if tok["percent"]:
        cands.append((tok["value"] / 100.0, "percent"))

    for v, mode in cands:
        if v == r:
            return mode
        if round(r, d) == round(v, d):
            return "rounded" if mode == "exact" else mode
        # truncation — digits below the written precision are dropped
        if math.floor(abs(r) * scale) / scale == round(abs(v), d) and \
                (r >= 0) == (v >= 0):
            return "truncated" if mode == "exact" else mode
        # the sign is expressed in words ('0.12917만큼 감소')
        if round(abs(r), d) == round(abs(v), d):
            return "abs"
        if math.floor(abs(r) * scale) / scale == round(abs(v), d):
            return "abs"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 3) Building what data markers are compared against
# ─────────────────────────────────────────────────────────────────────────────

def build_data_index(sec_vs: dict) -> tuple[dict, dict]:
    """(source label -> list of values, label of how an input was obtained -> (key, value))

        The first groups data_records by source.
        The second inverts the strings of input_sources/seed_sources into keys, because the
        Writer sometimes copies such a string as the marker
        (e.g. '시도추계(v2022) 2029 × 현재(2024) 인구비 안분' -> inputs['population']).
    """
    by_source: dict[str, list] = defaultdict(list)
    for r in sec_vs.get("data_records") or []:
        src = r.get("source")
        if src is not None:
            by_source[src].append(r.get("value"))

    inputs = sec_vs.get("inputs") or {}
    by_path: dict[str, tuple[str, object]] = {}
    for mapping in (sec_vs.get("input_sources") or {},
                    sec_vs.get("seed_sources") or {}):
        for k, path in mapping.items():
            if isinstance(path, str) and path not in by_path:
                by_path[path] = (k, inputs.get(k))
    return by_source, by_path


# ─────────────────────────────────────────────────────────────────────────────
# 4) Scoring one subsection
# ─────────────────────────────────────────────────────────────────────────────

STATUS_SUPPORTED = "supported"
STATUS_VALUE_MISMATCH = "value_mismatch"
STATUS_KEY_MISSING = "key_missing"
STATUS_NO_NUMBER = "no_number"
STATUS_OUT_OF_SCOPE = "out_of_scope"
# Wrong marker type — the key exists in the other bucket.
# An obtained input (inputs) sometimes carries a calculation marker, as in
# 「계산: population」 or 「계산: elasticity」. numbers and inputs are kept apart so that the
# scope of 'deterministically calculated' is not overstated, so these cases are counted
# separately. The value can be traced, but the claim that it is a formula output is wrong,
# so the case counts as a failure.
STATUS_WRONG_TYPE = "wrong_marker_type"


def _norm_label(x: str) -> str:
    return re.sub(r"\s+", "", re.sub(r"\([^)]*\)", "", x or ""))


def _canon_label(label: str, known: list[str]) -> str:
    if label in known:
        return label
    nl = _norm_label(label)
    exact = [k for k in known if _norm_label(k) == nl]
    if len(set(exact)) == 1:
        return exact[0]
    if len(nl) >= 8:
        hits = {k for k in known if nl in _norm_label(k)}
        if len(hits) == 1:
            return hits.pop()
    return label


def _judge(tokens: list[dict], used: dict[int, float],
           refs: list) -> tuple[str, dict | None]:
    """Find a number token that agrees with one of the values in refs.

        Assignment rules:
          - A token is assigned to one marker only; otherwise precision would rise with the
            number of figures in a sentence.
          - An already assigned token may be shared by a marker that points to the same value
            (e.g. one token '100%' supporting ri, ci, special_ri, and special_ci).
          - A year token is excluded when the source value is outside the range of years.
          - Unit-constant and type-number tokens (is_excluded) are never candidates.
    """
    usable = [t for t in tokens if not t["is_excluded"]]
    if not usable:
        return STATUS_NO_NUMBER, None
    for ref in refs:
        try:
            rv = float(ref)
        except (TypeError, ValueError):
            continue
        ref_is_year = _YEAR_MIN <= rv <= _YEAR_MAX
        for i, tok in enumerate(tokens):
            if tok["is_excluded"]:
                continue
            if tok["is_year"] and not ref_is_year:
                continue
            if i in used and used[i] != rv:
                continue
            mode = match_value(tok, ref)
            if mode:
                used[i] = rv
                return STATUS_SUPPORTED, {"token": tok["raw"], "ref": ref,
                                          "mode": mode}
    # If no token could be compared (all years/constants, or assignment conflicts): no_number
    real = any(not t["is_year"] and not t["is_excluded"] for t in tokens)
    return (STATUS_VALUE_MISMATCH if real else STATUS_NO_NUMBER), None


def _judge_scoped(tokens, used, key, sec_map, run_map,
                  other_sec=None, other_run=None):
    """Compare within the subsection first, then within the run.

        Within the subsection only, a marker such as 「계산: population」 placed in a subsection
        whose exposure list does not contain the key would be key_missing, although the value
        was produced in another subsection and can be traced within the run. The indicator is
        about traceability, so the run scope is the main indicator, and the subsection-scope
        result is reported alongside to show how often the Writer used a value not exposed in
        that subsection.
    """
    if key in sec_map:
        st, ev = _judge(tokens, used, [sec_map[key]])
        return st, ev, "section"
    if key in run_map:
        st, ev = _judge(tokens, used, [run_map[key]])
        return st, ev, "run"
    # If the key is in the other bucket (calculation <-> input), it is a wrong marker type, not a nonexistent key.
    for om in (other_sec or {}, other_run or {}):
        if key in om:
            return STATUS_WRONG_TYPE, None, "other_bucket"
    return STATUS_KEY_MISSING, None, None


def build_run_context(run: dict) -> dict:
    """Union of numbers/inputs over the whole run + the data index.

        A lookup table for a second check at run scope when the subsection-scope comparison
        fails. The same key does not take different values across subsections (the calculation
        is one catalog run per run, filtered by subsection). On a conflict, the first value is used.
    """
    numbers: dict = {}
    inputs: dict = {}
    by_source: dict[str, list] = defaultdict(list)
    by_path: dict[str, tuple[str, object]] = {}
    for _, sec in E.iter_sections(run):
        vs = sec.get("verification_source") or {}
        for k, v in (vs.get("numbers") or {}).items():
            numbers.setdefault(k, v)
        for k, v in (vs.get("inputs") or {}).items():
            inputs.setdefault(k, v)
        s_src, s_path = build_data_index(vs)
        for k, vals in s_src.items():
            by_source[k].extend(vals)
        for k, kv in s_path.items():
            by_path.setdefault(k, kv)
    return {"numbers": numbers, "inputs": inputs,
            "by_source": by_source, "by_path": by_path}


def score_section(sid: str, sec: dict, run_ctx: dict) -> dict:
    vs = sec.get("verification_source") or {}
    numbers = vs.get("numbers") or {}
    inputs = vs.get("inputs") or {}
    by_source, by_path = build_data_index(vs)

    records: list[dict] = []  # one marker = one outcome
    for s in E.body_sentences(sec.get("text") or ""):
        mk = E.parse_markers(s)
        if not (mk[E.MARKER_CALC] or mk[E.MARKER_INPUT] or mk[E.MARKER_DATA]):
            continue
        tokens = extract_numbers(s)
        used: dict[int, float] = {}
        clean = E.strip_markers(s)

        def add(kind, key, st, ev, scope):
            records.append({"kind": kind, "key": key, "status": st,
                            "evidence": ev, "scope": scope,
                            "section": sid, "sentence": clean})

        for key in mk[E.MARKER_CALC]:
            st, ev, scope = _judge_scoped(tokens, used, key, numbers,
                                          run_ctx["numbers"],
                                          inputs, run_ctx["inputs"])
            add("calc", key, st, ev, scope)

        for key in mk[E.MARKER_INPUT]:
            st, ev, scope = _judge_scoped(tokens, used, key, inputs,
                                          run_ctx["inputs"],
                                          numbers, run_ctx["numbers"])
            add("input", key, st, ev, scope)

        for label in mk[E.MARKER_DATA]:
            # A source label copied only in part by the Writer (parenthesized part omitted, only one
            # side of '+', etc.) is treated as the same source: exact match after normalization (spaces
            # and parenthesized content removed), or, if at least 8 characters long, containment in
            # exactly one existing label. Ambiguous cases are left as they are.
            label = _canon_label(label, list(by_source) + list(run_ctx["by_source"]))
            if label in by_source:
                st, ev = _judge(tokens, used, by_source[label])
                add("data_record", label, st, ev, "section")
            elif label in run_ctx["by_source"]:
                st, ev = _judge(tokens, used, run_ctx["by_source"][label])
                add("data_record", label, st, ev, "run")
            elif label in by_path or label in run_ctx["by_path"]:
                k, val = (by_path.get(label) or run_ctx["by_path"][label])
                scope = "section" if label in by_path else "run"
                if val is None:
                    add("data_input", label, STATUS_KEY_MISSING, None, scope)
                else:
                    st, ev = _judge(tokens, used, [val])
                    add("data_input", label, st, ev, scope)
            else:
                add("data_unindexed", label, STATUS_OUT_OF_SCOPE, None, None)

    return {"records": records,
            "n_numbers_available": len(numbers),
            "n_inputs_available": len(inputs)}


# ─────────────────────────────────────────────────────────────────────────────
# 5) Aggregation
# ─────────────────────────────────────────────────────────────────────────────

_CALC_KINDS = {"calc"}
_DATA_KINDS = {"input", "data_record", "data_input", "data_unindexed"}


def _rg_precision(records: list[dict], kinds: set[str]) -> dict:
    """Main indicator on the occurrence basis, with the unique basis alongside.

        out_of_scope is excluded from the denominator and its count is reported: a marker with
        nothing to compare against would count as a failure if included, and would inflate
        precision if dropped without notice.
    """
    sel = [r for r in records if r["kind"] in kinds]
    # Excluded from the denominator: markers with nothing to compare against (out_of_scope) and
    # qualitative statements without a figure (no_number). RG is the precision of
    # (type, entity, value) triples, so a sentence without a value forms no relation and is not
    # a target. Both counts are always reported.
    scoped = [r for r in sel
              if r["status"] not in (STATUS_OUT_OF_SCOPE, STATUS_NO_NUMBER)]
    hit = [r for r in scoped if r["status"] == STATUS_SUPPORTED]
    # The number of markers matched at subsection scope alone is reported alongside. The run
    # scope is the main indicator; the difference shows how often the Writer used a value that
    # was not exposed in the subsection.
    hit_section = [r for r in hit if r.get("scope") == "section"]
    hit_run = [r for r in hit if r.get("scope") == "run"]

    uniq_all = {(r["section"], r["key"]) for r in scoped}
    uniq_hit = {(r["section"], r["key"]) for r in hit}

    status = Counter(r["status"] for r in sel)
    modes = Counter(r["evidence"]["mode"] for r in hit if r["evidence"])
    return {
        "numerator": len(hit), "denominator": len(scoped),
        "ratio": (len(hit) / len(scoped)) if scoped else None,
        "unique_numerator": len(uniq_hit), "unique_denominator": len(uniq_all),
        "unique_ratio": (len(uniq_hit) / len(uniq_all)) if uniq_all else None,
        "numerator_section": len(hit_section),
        "n_cross_section": len(hit_run),
        "ratio_section": (len(hit_section) / len(scoped)) if scoped else None,
        "n_out_of_scope": status.get(STATUS_OUT_OF_SCOPE, 0),
        "n_no_number": status.get(STATUS_NO_NUMBER, 0),
        "status": dict(status), "match_modes": dict(modes),
    }


def _mean_sd(values: list[float]) -> dict:
    vals = [v for v in values if v is not None]
    if not vals:
        return {"n": 0, "mean": None, "sd": None}
    if len(vals) == 1:
        return {"n": 1, "mean": vals[0], "sd": None}
    return {"n": len(vals), "mean": statistics.fmean(vals),
            "sd": statistics.stdev(vals)}


def aggregate(per_run: list[dict]) -> dict:
    all_records: list[dict] = []
    calc_rates, data_rates = [], []
    failures: list[dict] = []

    for r in per_run:
        run_records = [rec for sc in r["sections"].values()
                       for rec in sc["records"]]
        all_records.extend(run_records)
        calc_rates.append(_rg_precision(run_records, _CALC_KINDS)["ratio"])
        data_rates.append(_rg_precision(run_records, _DATA_KINDS)["ratio"])
        for rec in run_records:
            if rec["status"] in (STATUS_VALUE_MISMATCH, STATUS_KEY_MISSING,
                                 STATUS_WRONG_TYPE):
                failures.append({**rec, "run": r["run"]})

    return {
        "n_runs": len(per_run),
        "rg_p_calc": {**_rg_precision(all_records, _CALC_KINDS),
                      "per_run": _mean_sd(calc_rates)},
        "rg_p_data": {**_rg_precision(all_records, _DATA_KINDS),
                      "per_run": _mean_sd(data_rates)},
        "failures": failures,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 6) Report
# ─────────────────────────────────────────────────────────────────────────────

def _pct(x, d=1):
    return "—" if x is None else f"{x*100:.{d}f}%"


def write_summary(agg_by_case: dict, out_path: Path) -> None:
    L = ["# L2 귀속 층 — RG Precision\n"]
    L.append("\n원분수는 등장(occurrence) 기준, 괄호는 고유(unique) 기준 분모.\n")

    for title, key in (("RG-P(계산)", "rg_p_calc"), ("RG-P(자료)", "rg_p_data")):
        L.append(f"\n## {title}\n")
        L.append("| 케이스 | 원분수(run 범위) | 비율 | 소절 범위만 | 소절 밖 조달 | "
                 "고유 기준 | 범위 밖 | 수치없음 | 실행 간 mean (sd) |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for case, a in agg_by_case.items():
            m = a[key]; p = m["per_run"]
            mean = "—" if p["mean"] is None else f"{p['mean']:.3f}"
            sd = "—" if p["sd"] is None else f"{p['sd']:.3f}"
            L.append(f"| {case} | {m['numerator']}/{m['denominator']} | "
                     f"{_pct(m['ratio'])} | "
                     f"{m['numerator_section']}/{m['denominator']} "
                     f"({_pct(m['ratio_section'])}) | {m['n_cross_section']} | "
                     f"{m['unique_numerator']}/{m['unique_denominator']} | "
                     f"{m['n_out_of_scope']} | {m['n_no_number']} | "
                     f"{mean} ({sd}) |")

    L.append("\n## 판정 내역\n")
    L.append("| 케이스 | 구분 | supported | value_mismatch | key_missing | "
             "wrong_marker_type | no_number | out_of_scope |")
    L.append("|---|---|---|---|---|---|---|---|")
    for case, a in agg_by_case.items():
        for lab, key in (("계산", "rg_p_calc"), ("자료", "rg_p_data")):
            s = a[key]["status"]
            L.append(f"| {case} | {lab} | {s.get('supported',0)} | "
                     f"{s.get('value_mismatch',0)} | {s.get('key_missing',0)} | "
                     f"{s.get('wrong_marker_type',0)} | "
                     f"{s.get('no_number',0)} | {s.get('out_of_scope',0)} |")

    L.append("\n## 일치 방식 분포\n")
    L.append("정규화 규칙이 실제로 얼마나 쓰였는지. exact 외의 비중이 크면 "
             "그 규칙이 결과를 좌우하므로 방법 장에 명시해야 한다.\n")
    L.append("| 케이스 | 구분 | exact | rounded | truncated | percent | abs |")
    L.append("|---|---|---|---|---|---|---|")
    for case, a in agg_by_case.items():
        for lab, key in (("계산", "rg_p_calc"), ("자료", "rg_p_data")):
            m = a[key]["match_modes"]
            L.append(f"| {case} | {lab} | {m.get('exact',0)} | "
                     f"{m.get('rounded',0)} | {m.get('truncated',0)} | "
                     f"{m.get('percent',0)} | {m.get('abs',0)} |")

    L.append("\n## 실패 사례 (value_mismatch · key_missing)\n")
    any_fail = False
    for case, a in agg_by_case.items():
        for f in a["failures"][:40]:
            any_fail = True
            L.append(f"- **{case} r{f['run']} [{f['section']}]** "
                     f"`{f['kind']}:{f['key']}` — {f['status']}\n"
                     f"  - {f['sentence'][:160]}")
    if not any_fail:
        L.append("없음.")

    out_path.write_text("\n".join(L), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# 7) Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="outputs/runs")
    ap.add_argument("--out", default="outputs/eval")
    args = ap.parse_args()

    paths = E.find_runs(args.runs)
    if not paths:
        print(f"[eval_rg] {args.runs} 에서 chapter3_*.json 을 찾지 못함")
        print(E.describe_runs_dir(args.runs))
        return

    by_case: dict[str, list[dict]] = defaultdict(list)
    detail: dict = {}
    for p in paths:
        run = E.load_run(p)
        case = E.project_of(run, p)
        rid = E.run_index_of(p)
        run_ctx = build_run_context(run)
        sections = {sid: score_section(sid, sec, run_ctx)
                    for sid, sec in E.iter_sections(run)}
        by_case[case].append({"run": rid, "sections": sections})
        detail[f"{case}/r{rid}"] = {
            sid: sc["records"] for sid, sc in sections.items() if sc["records"]}

    agg_by_case = {c: aggregate(rs) for c, rs in sorted(by_case.items())}

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "rg_metrics.json").write_text(
        json.dumps({"aggregate": agg_by_case, "per_marker": detail},
                   ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    write_summary(agg_by_case, out / "rg_summary.md")

    print(f"[eval_rg] 산출물 {len(paths)}개 · 케이스 {len(agg_by_case)}개 채점 완료")
    for case, a in agg_by_case.items():
        c, d = a["rg_p_calc"], a["rg_p_data"]
        print(f"  {case}: RG-P(계산) {c['numerator']}/{c['denominator']} "
              f"({_pct(c['ratio'])}, 고유 {c['unique_denominator']}) · "
              f"RG-P(자료) {d['numerator']}/{d['denominator']} "
              f"({_pct(d['ratio'])}, 범위밖 {d['n_out_of_scope']})")
    print(f"[eval_rg] 저장: {out/'rg_metrics.json'} / {out/'rg_summary.md'}")


if __name__ == "__main__":
    main()