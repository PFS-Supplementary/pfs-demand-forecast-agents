# driver.py — runs the subsections of Chapter 3 in order for one case
#
#
# What is stored for each subsection (verification_source):
#   - calc_results (numbers, executed, skipped, seed_sources): the values computed by
#     Computation and the inputs it consumed. `skipped` records which formula was not run
#     for lack of which input.
#   - the data records shown to the Writer in section 3-2
#   - guideline_chunk_ids: the ids of the guideline chunks actually passed to the Writer,
#     after the special-purpose gate in memory_agent. Chunk texts are not stored.
# These are the reference sets against which the evaluation checks the generated sentences.
# The run-level base_period is stored under `_run`.
# ─────────────────────────────────────────────────────────────────────────────
# Role:
#   Feeds the Chapter 3 subsections from section_registry, one at a time in table-of-contents
#   order, to the single-subsection processor (orchestrator.graph), and passes and collects
#   results between subsections.
#
# Scope: Chapter 3 (demand forecast) only.
#
# Passed between subsections:
#   The catchment_result produced in section 3-2 is kept and placed in the initial state of
#   later subsections. catchment_resolver_node does nothing outside section 3-2, so the value
#   is preserved.
#
#   base_period (decided once by period_decider in the first section 3-3 subsection) is passed
#   to the subsections of sections 3-3, 3-4, and 3-5, so that the number of outpatients in
#   section 3-4 and the calculations in section 3-5 use the same years and the same catchment
#   as section 3-3. period_decider_node decides only in section 3-3 and respects an injected
#   value, so later subsections use the carried value without deciding again. The projection
#   vintage in base_period is used for apportioning population and for the police workforce
#   projection.
#
# Usage:
#   python driver.py             # default: HOS_ASAN
#   python driver.py HOS_ULSAN
# ─────────────────────────────────────────────────────────────────────────────

import sys, json, hashlib
from pathlib import Path

from agents.orchestrator import graph
from agents.sub.section_registry import get_section_ids, get_section_title
from paths import OUT_DIR, GEN_MODEL, DIST_TABLE, N_DISTANCE_SHOWN, DB_PATH, PIPELINE_VERSION, DATA_YEAR_LAG
from agents.sub.chunk_store import provenance as chunk_provenance
from agents.sub.llm import reset_served, served_models


def code_fingerprint() -> str:
    """Hash of the code used in the run (all .py files under this folder). Recorded in every output."""
    root = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for p in sorted(root.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        h.update(p.relative_to(root).as_posix().encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]

# Prefixes of the subsections that receive base_period
PERIOD_SECTIONS = ("3-3", "3-4", "3-5")


def _init_state(section_id: str, project_id: str,
                carried_catchment: dict, carried_prior: dict,
                carried_period: dict, carried_level: dict | None = None) -> dict:
    """Initial state for running one subsection.

        carried_catchment: the catchment result fixed so far (normally from section 3-2).
          Section 3-2 itself receives an empty dict so that it derives the catchment anew.
        carried_prior: summaries of the subsections generated earlier in this run.
        carried_period: the base_period decided once per case. The value set in the first
          section 3-3 subsection is passed to sections 3-3, 3-4, and 3-5.
          period_decider_node respects an injected value.
    """
    bp = carried_period if section_id.startswith(PERIOD_SECTIONS) else {}
    return {
        "section_id":       section_id,
        "project_id":       project_id,
        "catchment_result": carried_catchment,
        "prior_sections":   carried_prior,
        "base_period":      bp,
        "facility_level":   carried_level or {},
        "retry_count":      0,
        "critique_history": [],
    }


def run_chapter3(project_id: str) -> dict:
    section_ids = get_section_ids()
    reset_served()  # count only the models that responded in this run
    print(f"=== 제3장 수요추정 순회: {project_id} ===")
    print(f"소절 {len(section_ids)}개: {section_ids}\n")

    carried_catchment: dict = {}
    carried_prior: dict = {}
    carried_period: dict = {}
    carried_level: dict = {}
    results: dict = {}

    for sid in section_ids:
        title = get_section_title(sid) or ""
        print(f"\n{'─'*72}\n▶ [{sid}] {title}\n{'─'*72}")

        final = graph.invoke(_init_state(sid, project_id,
                                         carried_catchment, carried_prior,
                                         carried_period, carried_level))

        cr = final.get("catchment_result") or {}
        if sid == "3-2":
            carried_level = final.get("facility_level") or {}
        if sid == "3-2" and cr:
            carried_catchment = cr
            print(f"  → catchment_result 확정, 이후 소절로 전달: "
                  f"{ {k: v.get('regions') for k, v in cr.items()} }")

        # The base_period set in the first section 3-3 subsection is fixed for the case and passed on
        # (later subsections of sections 3-3, 3-4, and 3-5 use the same value)
        bp = final.get("base_period") or {}
        if sid.startswith("3-3") and bp and not carried_period:
            carried_period = bp
            print(f"  → base_period 확정(케이스 1회), 이후 §3-3·§3-4·§3-5 소절로 전달")

        critique = final.get("critique") or {}
        passed   = critique.get("pass")
        text     = final.get("final_text") or final.get("draft_text") or ""

        if text and len(text) > 50:
            carried_prior[sid] = {"title": title, "summary": text[:300]}

        # ── Reference data for verification ───────────────────────────────
        # calc_results = deterministic results computed by Computation + the input seeds consumed.
        # Reference set for sentences with a 「계산」 marker.
        calc = final.get("calc_results") or {}
        sf   = final.get("section_facts") or {}
        # ── Reference set for guideline citations ─────────────────────────
        # Only the ids of the chunks actually passed to the Writer after the gate are stored.
        # memory_agent hides the special-purpose chunk conditionally, so the set the Writer saw is a
        # subset of SECTION_CHUNK_IDS; citing a hidden chunk counts as citing an id outside the set.
        # Chunk texts are not stored; the evaluation fetches them by id.
        gchunks = final.get("guideline_chunks") or []
        guideline_chunk_ids = [c.get("chunk_id") for c in gchunks
                               if isinstance(c, dict) and c.get("chunk_id")]
        verification_source = {
            "numbers":        calc.get("numbers") or {},
            # Retrieved inputs are stored separately from formula results. They are the reference set
            # for sentences citing data and must not be mixed with the deterministic calculations.
            "inputs":         calc.get("inputs") or {},
            "input_sources":  calc.get("input_sources") or {},
            "unresolved":     calc.get("unresolved") or [],
            "executed":       calc.get("executed") or [],
            "skipped":        calc.get("skipped_formulas") or calc.get("skipped") or {},
            "seed_sources":   calc.get("seed_sources") or {},
            # Ids of the guideline chunks passed to the Writer after the gate.
            # Reference set for 「202212-…」 markers; an id outside this set is an invalid citation.
            "guideline_chunk_ids": guideline_chunk_ids,
            # Filled only in section 3-2. Reference set for sentences citing data.
            "data_records":   sf.get("catchment_records") or [],
            "data_omitted":   sf.get("catchment_omitted") or {},
            # Catchment codes actually used in the calculation and what was normalized
            "regions":        calc.get("regions") or {},
            "region_normalization": calc.get("region_normalization") or [],
        }
        # Record of judgments made during the run
        decision_record = {
            "critique_history": final.get("critique_history") or [],
        }
        if sid == "3-2":
            decision_record["special_purpose_gate"] = sf.get("special_purpose_gate")
            decision_record["special_purpose_ruling_given"] = \
                bool(sf.get("special_purpose_ruling"))
            decision_record["catchment_parse"] = final.get("catchment_parse") or {}
            decision_record["facility_level"] = final.get("facility_level") or {}

        results[sid] = {
            "title":            title,
            "passed":           passed,
            "text":             text,
            "catchment_result": cr,
            "critique":         critique,
            "retries":          final.get("retry_count", 0),
            "decision_record":  decision_record,
            "verification_source": verification_source,
        }
        n_in  = len(verification_source["inputs"])
        n_un  = len(verification_source["unresolved"])
        n_num = len(verification_source["numbers"])
        n_rec = len(verification_source["data_records"])
        n_skip = len(verification_source["skipped"])
        n_chunk = len(verification_source["guideline_chunk_ids"])
        print(f"  결과: {'PASS' if passed else 'FAIL'} · 서술 {len(text)}자 "
              f"· 계산수치 {n_num} · 입력 {n_in} · 미산출 {n_un} "
              f"· 자료레코드 {n_rec} · 미실행공식 {n_skip} · 회수청크 {n_chunk}")

    # Run-level metadata. The key starts with '_' so that it is not mixed with the subsections
    # (code that iterates over subsections skips it because it has no text).
    results["_run"] = {
        "project_id":  project_id,
        "base_period": carried_period,
        "facility_level": carried_level,
        "section_ids": section_ids,
        "pipeline_version": PIPELINE_VERSION,
        "data_year_lag": DATA_YEAR_LAG,
        "code_sha256_16": code_fingerprint(),
        "model": GEN_MODEL,
        "served_models": served_models(),  # number of calls by response model_version
        "generation_config": "default (not specified)",
        "distance_table": DIST_TABLE,
        "n_distance_shown": N_DISTANCE_SHOWN,
        "db_path": str(DB_PATH),
        # Whether guideline chunks came from ChromaDB or the JSON dump, and the text hash of each chunk
        "chunk_store": chunk_provenance(),
    }
    return results


def _write_outputs(project_id: str, results: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"chapter3_{project_id}.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    lines = [f"# 제3장 수요 추정 — {project_id}\n"]
    for sid, r in results.items():
        if sid.startswith("_"):  # run metadata is not written to the md file
            continue
        status = "PASS" if r["passed"] else "FAIL"
        lines.append(f"\n## [{sid}] {r['title']}  ({status})\n")
        if r["catchment_result"]:
            cr = {k: v.get("regions") for k, v in r["catchment_result"].items()}
            lines.append(f"_진료권: {cr}_\n")
        lines.append(r["text"] or "(서술 없음)")
    (OUT_DIR / f"chapter3_{project_id}.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n저장: chapter3_{project_id}.md / .json  →  {OUT_DIR}")


def _summary(results: dict) -> None:
    print(f"\n{'='*72}\n[요약]\n{'='*72}")
    for sid, r in results.items():
        if sid.startswith("_"):
            continue
        status = "PASS" if r["passed"] else "FAIL"
        cr = ""
        if r["catchment_result"]:
            cr = "  진료권=" + str({k: v.get("regions")
                                   for k, v in r["catchment_result"].items()})
        print(f"  [{sid}] {status:4} · {len(r['text']):>5}자{cr}")


if __name__ == "__main__":
    project_id = sys.argv[1] if len(sys.argv) > 1 else "HOS_ASAN"
    results = run_chapter3(project_id)
    _write_outputs(project_id, results)
    _summary(results)