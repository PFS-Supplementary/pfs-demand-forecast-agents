# agents/sub/period_decider.py — decides the base period for section 3-3 and the population projection vintage
# ─────────────────────────────────────────────────────────────────────────────
# Role: before the demand calculation in section 3-3, decide on the basis of the guideline
# which year(s) of data to use for the inpatient utilization rate and for bed utilization,
# and which vintage of the population projection to use, and structure the result as the
# base_period object used by Computation.
#
# Projection vintage: the vintage is the latest projection available at the time of the PFS.
# The vintage column holds the base year (2017/2020/2022), and publication comes about two
# years later, so the vintage cannot be inferred from the survey year. The vintage fixed for
# each case is read from project_config['projection_vintage'] and only checked against the
# vintages held in the database. The reference year for the current population share is
# computed in one place by Computation.
#
# Why a separate node:
#   Choosing years and a vintage is neither calculation nor data retrieval; it is a judgment
#   about interpreting the guideline and choosing sources. It has its own node, like
#   catchment_resolver.
#
# Output: state["base_period"]
#   {"inpatient_days":  {"years":[...], "agg":"single|mean", "basis":"..."},
#    "bed_utilization": {"years":[...], "agg":..., "facility_type":...,
#                        "facility_size":..., "basis":...},
#    "projection":      {"vintage":"2022", "target_year":2029, "basis":"..."}}
# Whether to use the catchment or the national bed utilization is not decided here:
# Computation produces both. The guideline lists the bases for bed utilization without
# ranking them.
#
# ─────────────────────────────────────────────────────────────────────────────

import re, sqlite3

from state import GraphState
from paths import DB_PATH
from agents.sub.llm import call_json
from agents.sub.data_cap import data_year_cap, survey_year as _survey_year

CATCHMENT_SCHEME = "중진료권70"

# Guideline chunks given as the basis for the base-period judgment: the chunk on data to
# apply (2-2ra, which mentions "the most recent 3-5 years" for population), Method 1 of
# Section 3 (3-1), which defines the inpatient utilization rate and bed utilization, and
# Section 4 (4-1), which lists the bases for bed utilization. All are original guideline
# chunks; no interpretation is added.
GUIDELINE_PERIOD_CHUNK_IDS = [
    "202212-III-2-2라",
    "202212-III-3-1",
    "202212-III-4-1",
]

# Level of care judged by the Writer in section 3-2 -> institution type for bed utilization (values of survey5_beds)
LEVEL_TO_FTYPE = {"상급종합병원급": "상급종합", "종합병원급": "종합병원"}
DEFAULT_LEVEL = "종합병원급"  # guideline 2-2na: a general hospital in principle


def _get_conn():
    return sqlite3.connect(DB_PATH)


def _load_config(conn, project_id: str, key: str):
    row = conn.execute("SELECT value FROM project_config WHERE project_id=? AND key=?",
                       (project_id, key)).fetchone()
    return row[0] if row else None


# ─────────────────────────────────────────────────────────────────────────────
# Evidence source 1: guideline chunks (deterministic retrieval)
# ─────────────────────────────────────────────────────────────────────────────

def _load_guideline_period_text() -> str:
    """Fetch the chunks that bear on the base period directly by id."""
    try:
        from agents.sub.chunk_store import get_chunks
        by_id = {c["chunk_id"]: c["text"] for c in get_chunks(GUIDELINE_PERIOD_CHUNK_IDS)}
        missing = [i for i in GUIDELINE_PERIOD_CHUNK_IDS if i not in by_id]
        if missing:
            print(f"[PeriodDecider] 경고: 근거 청크 없음 {missing}")
        return "\n\n".join(f"[지침 청크 id={i}]\n{by_id[i]}"
                            for i in GUIDELINE_PERIOD_CHUNK_IDS if i in by_id)
    except Exception as e:
        print(f"[PeriodDecider] 가이드라인 청크 회수 실패: {e}")
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Evidence source 2: precedents in past reports (semantic search) — currently a stub
# ─────────────────────────────────────────────────────────────────────────────

def _search_precedent(query: str) -> list[dict]:
    """Semantic search for base-period precedents in a corpus of past reports. (The corpus is not indexed; returns nothing.)"""
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Database: years with data; deterministic choice of institution type and size
# ─────────────────────────────────────────────────────────────────────────────

def _available_years_inpatient(conn, regions: list[str]) -> list[int]:
    if not regions:
        return []
    q = ("SELECT DISTINCT reference_year FROM inpatient_by_age_facility "
         "WHERE metric='INPATIENT_DAYS' AND region_code IN (%s) ORDER BY reference_year"
         % ",".join("?" * len(regions)))
    return [r[0] for r in conn.execute(q, regions)]


def _catchment_id_for_regions(conn, regions: list[str]):
    ids = set()
    for r in regions:
        for (c,) in conn.execute(
            "SELECT DISTINCT catchment_id FROM catchment_scheme_assignment "
            "WHERE scheme=? AND region_code=?", (CATCHMENT_SCHEME, r)):
            ids.add(c)
    return next(iter(ids)) if len(ids) == 1 else None


def _facility_type_from_level(level_info: dict | None) -> tuple[str, dict]:
    """Convert the judgment written by the Writer under [신규 시설 진료 수준] in section 3-2 into an institution type.

        Using the Writer's own judgment keeps the type determination in section 3-2 and the
        bed utilization calculation consistent within a run.
        If the line is missing, the guideline's default (general hospital) is used and recorded.
    """
    lv = (level_info or {}).get("level")
    if lv in LEVEL_TO_FTYPE:
        return LEVEL_TO_FTYPE[lv], {"level": lv, "source": "§3-2 [신규 시설 진료 수준]"}
    return LEVEL_TO_FTYPE[DEFAULT_LEVEL], {
        "level": DEFAULT_LEVEL,
        "source": "§3-2 표기 없음 → 지침 원칙(종합병원급) 적용",
    }


def _facility_size_for_beds(conn, project_id: str, ftype: str, catchment_id: str | None):
    """Match the size band that contains total_beds, deterministically from the data."""
    if ftype == "상급종합":
        return ""  # the size value for tertiary general hospitals in survey5_beds is ''
    row = conn.execute("SELECT value FROM project_config WHERE project_id=? AND key='total_beds'",
                       (project_id,)).fetchone()
    if not row:
        return None
    try:
        beds = int(row[0])
    except (ValueError, TypeError):
        return None
    sizes = [s for (s,) in conn.execute(
        "SELECT DISTINCT facility_size FROM bed_utilization WHERE facility_type=?", (ftype,))]
    for s in sizes:
        m = re.match(r'(\d+)~(\d+)', s)
        if m and int(m.group(1)) <= beds <= int(m.group(2)):
            return s
        m2 = re.match(r'(\d+)병상이상', s)
        if m2 and beds >= int(m2.group(1)):
            return s
    return None


def _bedutil_years_survey5(conn, regions: list[str], ftype: str,
                           fsize: str | None) -> list[int]:
    """Years that can be used for bed utilization.

        Bed utilization is computed as survey5_inpatient_days / (survey5_beds x days in the
        year), so the available years are those in which beds of the given type and size exist
        in the catchment's cities/counties/districts. National data always exist and only the
        catchment side depends on whether facilities are present, so the narrower side (the
        catchment) is used as the basis; this is safe for both variants.
    """
    if not regions:
        return []
    ph = ",".join("?" * len(regions))
    q = (f"SELECT DISTINCT reference_year FROM survey5_beds "
         f"WHERE facility_type=? AND category='전체' AND licensed_beds>0 "
         f"AND region_code IN ({ph})")
    params = [ftype] + list(regions)
    if fsize:
        q += " AND facility_size=?"
        params.append(fsize)
    q += " ORDER BY reference_year"
    return [r[0] for r in conn.execute(q, params)]


def _bedutil_years_national(conn, ftype: str, fsize: str | None) -> list[int]:
    q = ("SELECT DISTINCT reference_year FROM survey5_beds "
         "WHERE facility_type=? AND category='전체' AND licensed_beds>0")
    params = [ftype]
    if fsize:
        q += " AND facility_size=?"
        params.append(fsize)
    q += " ORDER BY reference_year"
    return [r[0] for r in conn.execute(q, params)]


def _bedutil_years(conn, catchment_id: str | None, ftype: str, fsize: str | None,
                   scope: str) -> list[int]:
    """Available years based on the published table. Not used; kept for reference."""
    if scope == "national":
        q = ("SELECT DISTINCT reference_year FROM bed_utilization "
             "WHERE catchment_scheme='전국' AND facility_type=?")
        params = [ftype]
    else:
        if not catchment_id:
            return []
        q = ("SELECT DISTINCT reference_year FROM bed_utilization "
             "WHERE catchment_scheme=? AND catchment_id=? AND facility_type=?")
        params = [CATCHMENT_SCHEME, catchment_id, ftype]
    if fsize:
        q += " AND facility_size=?"; params.append(fsize)
    q += " ORDER BY reference_year"
    return [r[0] for r in conn.execute(q, params)]


# ─────────────────────────────────────────────────────────────────────────────
# Projection vintage — read the fixed value from config and check it against the database (no inference)
# ─────────────────────────────────────────────────────────────────────────────

def _available_vintages(conn) -> list[str]:
    return [str(r[0]) for r in conn.execute(
        "SELECT DISTINCT vintage FROM region_population_timeseries "
        "WHERE data_type='PROJECTED' ORDER BY vintage")]


def _decide_projection(conn, project_id: str) -> dict:
    """Decide the Statistics Korea projection vintage used to apportion the catchment's future population.

        The vintage is the latest projection available at the time of the PFS. The value fixed
        for each case (project_config['projection_vintage']) is read and checked against the
        vintages held in the database, without inferring publication years.
        target_year comes from config. ratio_ref_year is computed by Computation (not here).
    """
    vintage = _load_config(conn, project_id, "projection_vintage")
    if not vintage:
        print("[PeriodDecider] projection 미결정: project_config.projection_vintage 없음")
        return {}
    avail = _available_vintages(conn)
    if str(vintage) not in avail:
        print(f"[PeriodDecider] projection 미결정: vintage {vintage} 가 DB 보유분 {avail} 에 없음")
        return {}

    target_year = _load_config(conn, project_id, "target_year")
    try:
        target_year = int(target_year) if target_year is not None else None
    except (ValueError, TypeError):
        target_year = None
    if target_year is None:
        print("[PeriodDecider] projection 미결정: target_year 없음")
        return {}

    survey = _load_config(conn, project_id, "survey_year")
    basis = (f"예타 {survey} 시점 가용 최신 통계청 시도편 장래인구추계({vintage}년 기준); "
             f"진료권 추계 = 시도 추계 × 현재 인구비 안분(가이드라인 §3-2)")
    return {"vintage": str(vintage), "target_year": target_year, "basis": basis}


# ─────────────────────────────────────────────────────────────────────────────
# Year judgment by the LLM
# ─────────────────────────────────────────────────────────────────────────────

def _period_validate(obj):
    for k in ("inpatient_days", "bed_utilization"):
        v = obj.get(k)
        if not isinstance(v, dict) or not isinstance(v.get("years"), list):
            return f"{k}.years 없음"
    return None


def _llm_decide(guideline_text: str, precedent: list[dict],
                inpatient_years: list[int], bedutil_years_catchment: list[int],
                bedutil_years_national: list[int], ftype: str, fsize: str | None):
    """Returns: (decision dict | {}, log)

        The instructions do not raise considerations that are not in the guideline. The model
        must state the basis for its choice.
    """
    precedent_block = "\n".join(f"- {p}" for p in precedent) or "(선례 자료 없음)"
    prompt = f"""
당신은 KDI 예비타당성조사 수요추정의 기준연도를 결정하는 역할입니다.
가이드라인 근거와 자료 가용연도를 보고, 입원의료이용률(1인당 입원일수)과 병상이용률을
'어느 연도(들)' 자료로 산정할지 판단하십시오.

## 가이드라인 (근거로 인용할 것)
{guideline_text}

## 과거 보고서 선례
{precedent_block}

## 자료 가용연도 (이 안에서만 선택 가능)
- 입원일수(입원의료이용률 산정용): {inpatient_years}
- 병상이용률(종별 '{ftype}'{'/'+fsize if fsize else ''}): {bedutil_years_catchment or '없음'}

## 판단 규칙
1. 가이드라인이 기준기간을 명시하면 그에 맞춰 가용연도 중에서 고르고,
   단일연도(single)인지 평균(mean)인지 정한다.
2. 병상이용률은 '해당 진료권의 평균값'과 '전국 종별 값'을 모두 산출한다.
   둘 중 하나를 고르지 말 것. 연도와 집계 방식만 정하면 된다.
3. 각 결정에 'basis'(가이드라인 문구 또는 선례)를 반드시 적는다. 근거 없는 선택 금지.
4. 가용연도 목록에 없는 연도를 만들지 말 것.

JSON으로만 답하시오:
{{"inpatient_days": {{"years":[...], "agg":"single|mean", "basis":"..."}},
  "bed_utilization": {{"years":[...], "agg":"single|mean", "basis":"..."}}}}
""".strip()
    obj, log = call_json(prompt, validate=_period_validate)
    if obj is None:
        print(f"[PeriodDecider] LLM 판단 실패(시도 {log['attempts']}): {log['errors']}")
        return {}, log
    return obj, log


# ─────────────────────────────────────────────────────────────────────────────
# validator: intersect the LLM output with the years that have data (removes years that do not exist)
# ─────────────────────────────────────────────────────────────────────────────

def _validate_years(picked: list, available: list[int]) -> tuple[list[int], list[int]]:
    avail = set(available)
    keep = sorted({int(y) for y in (picked or [])
                   if isinstance(y, (int, str)) and str(y).isdigit() and int(y) in avail})
    dropped = [y for y in (picked or []) if not (str(y).isdigit() and int(y) in avail)]
    return keep, dropped  # sorted, so that 'single' aggregation uses the latest year


# ─────────────────────────────────────────────────────────────────────────────
# Node body
# ─────────────────────────────────────────────────────────────────────────────

def decide_period(project_id: str, regions: list[str],
                  level_info: dict | None = None) -> dict:
    """Guideline + precedents + data availability -> base_period. A pure function."""
    conn = _get_conn()
    try:
        inpatient_years = _available_years_inpatient(conn, regions)
        cid = _catchment_id_for_regions(conn, regions)  # kept for reference
        ftype, ftype_src = _facility_type_from_level(level_info)
        fsize = _facility_size_for_beds(conn, project_id, ftype, cid)
        print(f"[PeriodDecider] 병상이용률 종별: {ftype}/{fsize or '-'} ({ftype_src['source']})")
        # Available years are computed from the survey source data: the years in which beds of the
        # given type and size exist in the catchment's cities/counties/districts.
        bedutil_catch = _bedutil_years_survey5(conn, regions, ftype, fsize)
        bedutil_nat = _bedutil_years_national(conn, ftype, fsize)
        if not bedutil_catch:
            # If the catchment has no facility of the type, the national years are offered (the national variant is still computed)
            bedutil_catch = bedutil_nat

        # Data after the time of the PFS are excluded (a factual constraint, not an analytical choice).
        # Upper bound = survey year - 1: data for the survey year itself are published the following
        # year and were not available to the original analysts.
        cap = data_year_cap(conn, project_id)
        if cap is not None:
            pre = (len(inpatient_years), len(bedutil_catch), len(bedutil_nat))
            inpatient_years = [y for y in inpatient_years if y <= cap]
            bedutil_catch = [y for y in bedutil_catch if y <= cap]
            bedutil_nat = [y for y in bedutil_nat if y <= cap]
            print(f"[PeriodDecider] 자료 연도 상한 {cap}(조사 연도 {_survey_year(conn, project_id)} − 1) 적용 "
                  f"— 입원일수 {pre[0]}→{len(inpatient_years)}, "
                  f"병상이용률 catch {pre[1]}→{len(bedutil_catch)} / nat {pre[2]}→{len(bedutil_nat)}")
        else:
            print("[PeriodDecider] survey_year 없음 — 후보연도 상한 미적용")

        guideline_text = _load_guideline_period_text()
        precedent = _search_precedent(f"{ftype} 기준연도 입원일수 병상이용률")

        decision, llm_log = _llm_decide(guideline_text, precedent, inpatient_years,
                                        bedutil_catch, bedutil_nat, ftype, fsize)

        out = {}
        # Decision record: the inputs (available years, institution type) are kept together with the raw response and the number of attempts.
        out["decision_log"] = {
            "guideline_chunk_ids": GUIDELINE_PERIOD_CHUNK_IDS,
            "available": {"inpatient_days": inpatient_years,
                          "bed_utilization_catchment": bedutil_catch,
                          "bed_utilization_national": bedutil_nat},
            "facility": {"type": ftype, "size": fsize, **ftype_src},
            "attempts": llm_log["attempts"], "errors": llm_log["errors"],
            "raw": llm_log["raw"], "decided": bool(decision),
        }

        if decision:
            # Inpatient days
            idp = decision.get("inpatient_days") or {}
            keep, dropped = _validate_years(idp.get("years"), inpatient_years)
            if dropped:
                print(f"[PeriodDecider] 입원일수 환각/미가용 연도 제거: {dropped}")
            if keep:
                out["inpatient_days"] = {
                    "years": keep, "agg": idp.get("agg", "single"),
                    "basis": idp.get("basis", ""),
                }

            # Bed utilization
            bdp = decision.get("bed_utilization") or {}
            # Both variants are computed, so the available years are the union.
            avail = sorted(set(bedutil_catch) | set(bedutil_nat))
            keep, dropped = _validate_years(bdp.get("years"), avail)
            if dropped:
                print(f"[PeriodDecider] 병상이용률 환각/미가용 연도 제거: {dropped}")
            if keep and fsize is not None:
                out["bed_utilization"] = {
                    "years": keep, "agg": bdp.get("agg", "single"),
                    "facility_type": ftype, "facility_size": fsize,
                    "basis": bdp.get("basis", ""),
                }
            elif fsize is None:
                print(f"[PeriodDecider] {ftype} 규모 구간 매칭 실패(사업병상수 포함 구간 없음) "
                      f"→ 병상이용률 미결정 기록")
        else:
            print("[PeriodDecider] LLM 결정 없음 → 연도 의존 입력 skip(projection 은 별도 결정)")

        # Projection vintage — independent of the LLM decision (fixed value in config + database check)
        projection = _decide_projection(conn, project_id)
        if projection:
            out["projection"] = projection

        for k, v in out.items():
            if k != "decision_log":
                print(f"[PeriodDecider] {k}: {v}")
        return out
    finally:
        conn.close()


def _general_regions(state: GraphState) -> list[str]:
    """catchment_result (general catchment) -> regions. Same rule as computation._extract_regions."""
    cr = state.get("catchment_result") or {}
    SPECIAL = ("특수목적", "전국", "특수", "national", "special")
    general = []
    if isinstance(cr, dict) and cr:
        items = list(cr.items())
        for label, body in items:
            regs = [str(x) for x in (body.get("regions") or [])] if isinstance(body, dict) else []
            if not regs:
                continue
            is_special = any(k in str(label) for k in SPECIAL) or set(regs) == {"00000"}
            if not (is_special and len(items) > 1):
                general.extend(r for r in regs if r not in general)
    if general:
        from agents.sub.region_units import normalize
        general, _ = normalize(general)
    if not general:
        conn = _get_conn()
        try:
            row = conn.execute("SELECT value FROM project_config WHERE project_id=? AND key='location_sigungu_code'",
                               (state["project_id"],)).fetchone()
        finally:
            conn.close()
        if row:
            general = [str(row[0])]
    return general


def period_decider_node(state: GraphState) -> GraphState:
    """Graph node: acts only in the subsections of section 3-3; otherwise passes the state through.

        Wired as memory -> period_decider -> computation. catchment_result is already in the
        state (produced in section 3-2 and passed by the driver), so the catchment regions are
        available.
    """
    section_id = state["section_id"]
    if not section_id.startswith("3-3"):
        return state
    # A value already injected from outside (driver) takes precedence
    if state.get("base_period"):
        return state

    regions = _general_regions(state)
    base_period = decide_period(state["project_id"], regions,
                                state.get("facility_level"))
    return {**state, "base_period": base_period}