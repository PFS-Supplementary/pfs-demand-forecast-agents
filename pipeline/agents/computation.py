# agents/computation.py — Computation Agent
#
# Obtains the raw inputs from the database and runs the deterministic calculation with the
# formula catalog (agents/sub/formula_catalog.py).
#
# Inputs obtained here:
#   population (future) <- provincial projection (vintage) x the catchment's current share
#                          of the provincial population (the method stated in the guideline
#                          for section 3-2, used because city/county/district projections
#                          cover a limited time range). vintage, target_year, and the
#                          reference year come from base_period["projection"]
#                          (period_decider), otherwise from project_config.
#   inpatient utilization rate <- inpatient days / observed population, same set of regions
#   bed utilization  <- survey inpatient days / (licensed beds x days in the year), computed
#                       for the catchment and for the nation
#   local_per_1000   <- demand_parameter (supply_beds, general hospital level and above) / observed population
#   type_share       <- demand_parameter (inpatient_days_by_type, national)
#   outpatient_ratio <- inpatient_by_age_facility (OUTPATIENT/INPATIENT)
#   death_rate       <- demand_parameter (emergency_death, 7 items) / observed population
#   elasticity       <- kdi_reference_values
# Rules:
#   - The upper bound on data years is the survey year - 1 (agents/sub/data_cap.py).
#   - The number of days in a year follows the survey's definition of bed utilization
#     (366 for 2016 and 2020, otherwise 365).
#   - When summing over the catchment, a region without data is left out of both the
#     numerator and the denominator, and is recorded in notes. For emergency beds (the
#     denominator of the rate of increase) the denominator cannot be adjusted, so the
#     missing regions are only recorded.
#   - Catchment codes are normalized to statistical units, and the codes used are stored in
#     calc_results["regions"]; the executed formulas are stored in calc_results["executed"].
#   - An input that cannot be obtained is not injected, and the formulas that need it are
#     skipped; there is no fallback value.
#
# Exposure to the Writer:
#   - Of the seed inputs, only those consumed by an executed formula are exposed (_consumed_inputs).
#   - No calculated figures are injected into section 3-2 (setting the catchment), which is
#     where the catchment is derived.

import calendar
import json
import sqlite3
import hashlib
from collections import defaultdict
from pathlib import Path

from state import GraphState
from paths import DB_PATH
from agents.sub.formula_catalog import compute_with_report, OUTPUT_ALIAS, CATALOG
from agents.sub.region_units import normalize
from agents.sub.data_cap import data_year_cap

# Catchment-level data not in region_master (bed utilization) are linked through this mapping scheme.
CATCHMENT_SCHEME = "중진료권70"


# ─────────────────────────────────────────────────────────────────────────────
# Database helpers — common
# ─────────────────────────────────────────────────────────────────────────────

def _get_conn():
    return sqlite3.connect(DB_PATH)


def _normalize_value(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (ValueError, TypeError):
        return v
    return int(f) if f.is_integer() else f


def _load_config(conn, project_id: str, key: str):
    row = conn.execute(
        "SELECT value FROM project_config WHERE project_id=? AND key=?",
        (project_id, key),
    ).fetchone()
    return row[0] if row else None


def _load_config_int(conn, project_id: str, key: str):
    v = _load_config(conn, project_id, key)
    try:
        return int(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def _get_target_year(conn, project_id: str) -> int | None:
    return _load_config_int(conn, project_id, "target_year")


def _sido_of(region_code: str) -> str:
    """City/county/district region_code -> code of its province (first two digits + '000')."""
    return region_code[:2] + "000"


def _is_aggregate_unit(region_code: str) -> bool:
    """Whether region_code is itself a province or the nation (ends in '000'): the projection is used directly, without apportioning."""
    return region_code.endswith("000")


# ─────────────────────────────────────────────────────────────────────────────
# Database helpers — deterministic readers of the normalized tables
# ─────────────────────────────────────────────────────────────────────────────

def _observed_pop(conn, region_code: str, year: int):
    """Observed population (T/TOTAL) of one region. None if absent."""
    row = conn.execute(
        """SELECT population FROM region_population_timeseries
           WHERE region_code=? AND year=? AND data_type='OBSERVED'
             AND sex='T' AND age_group='TOTAL'""",
        (region_code, year),
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _sum_observed_pop(conn, regions: list[str], year: int):
    """Sum of the observed population over the catchment's region codes. (None, all) if nothing contributes."""
    total = 0.0
    got = False
    missing = []
    for r in regions:
        p = _observed_pop(conn, r, year)
        if p is not None:
            total += p
            got = True
        else:
            missing.append(r)
    return (total if got else None), missing


def _projected_pop_unit(conn, unit_code: str, year: int, vintage: str):
    """Projected population (T/TOTAL, given vintage) of a province or the nation. None if absent."""
    row = conn.execute(
        """SELECT population FROM region_population_timeseries
           WHERE region_code=? AND year=? AND data_type='PROJECTED'
             AND vintage=? AND sex='T' AND age_group='TOTAL'""",
        (unit_code, year, str(vintage)),
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _projected_pop_anbun(conn, regions: list[str], target_year: int,
                         vintage: str, ratio_ref_year: int):
    """Future population of the catchment = sum over provinces of [ provincial projection
        (vintage, target_year) x (current population of the catchment's units in that
        province / current population of the province) ].

        Guideline, section 3-2: when city/county/district projections do not cover the period,
        use the provincial projection x the (current/recent) population share.
        The reference year of the share is ratio_ref_year (the latest observed year not after
        the upper bound). If region_code is a province or the nation (ends in '000'), its
        projection is used directly.
        Returns: (sum or None, notes).
    """
    notes: list[str] = []
    # Group by province (a province/nation code is its own group key)
    by_sido: dict[str, list[str]] = defaultdict(list)
    for r in regions:
        sido = r if _is_aggregate_unit(r) else _sido_of(r)
        by_sido[sido].append(r)

    total = 0.0
    got = False
    for sido, members in by_sido.items():
        sido_future = _projected_pop_unit(conn, sido, target_year, vintage)
        if sido_future is None:
            notes.append(f"population: 시도 {sido} {target_year}(v{vintage}) 추계 없음 — 해당 시도 제외")
            continue

        # A catchment that is itself a province or the nation is used directly (share = 1)
        if len(members) == 1 and _is_aggregate_unit(members[0]) and members[0] == sido:
            total += sido_future
            got = True
            continue

        sido_now = _observed_pop(conn, sido, ratio_ref_year)
        part_now, miss = _sum_observed_pop(conn, members, ratio_ref_year)
        if sido_now and part_now is not None:
            total += sido_future * (part_now / sido_now)
            got = True
            if miss:
                notes.append(f"population 안분: 시도 {sido} 내 현재인구 누락 {miss}(부분 비율)")
        else:
            notes.append(f"population 안분 skip: 시도 {sido} 현재인구비({ratio_ref_year}) 산정 불가")
    return (total if got else None), notes


def _raw_visit_days(conn, regions: list[str], year: int, metric: str):
    """Sum of visit days over the catchment's region codes (metric=INPATIENT_DAYS|OUTPATIENT_DAYS).

        Returns (sum or None, regions with data), so that the caller can use the same set of
        regions for the denominator (population).
    """
    total = 0.0
    covered = []
    for r in regions:
        row = conn.execute(
            """SELECT value FROM inpatient_by_age_facility
               WHERE region_code=? AND reference_year=?
                 AND age_group='TOTAL' AND metric=? AND sex='T'""",
            (r, year, metric),
        ).fetchone()
        if row and row[0] is not None:
            total += float(row[0])
            covered.append(r)
    return (total if covered else None), covered


def _raw_inpatient_days(conn, regions: list[str], year: int):
    """Sum of inpatient days. Returns (sum or None, regions with data)."""
    return _raw_visit_days(conn, regions, year, "INPATIENT_DAYS")


def _days_in_year(year: int) -> int:
    """Days in the year as defined in the survey's bed utilization formula (366 in leap years)."""
    return 366 if calendar.isleap(int(year)) else 365


def _catchment_id_for_regions(conn, regions: list[str], scheme: str = CATCHMENT_SCHEME):
    """catchment_id of the catchment the regions belong to. The value if unique; None and a reason if none or several."""
    ids = set()
    for r in regions:
        for (c,) in conn.execute(
            "SELECT DISTINCT catchment_id FROM catchment_scheme_assignment "
            "WHERE scheme=? AND region_code=?",
            (scheme, r),
        ):
            ids.add(c)
    if len(ids) == 1:
        return next(iter(ids)), None
    if not ids:
        return None, f"{scheme} 매핑에 region {regions} 없음"
    return None, f"region이 복수 진료권에 걸침({sorted(ids)}) — 집계 단위 결정 필요"


def _bed_util(conn, catchment_id: str, year: int, ftype: str, fsize: str,
              scheme: str = CATCHMENT_SCHEME):
    """Bed utilization (%) by catchment, year, institution type, and size. None if absent."""
    row = conn.execute(
        """SELECT utilization_rate FROM bed_utilization
           WHERE catchment_scheme=? AND catchment_id=? AND reference_year=?
             AND facility_type=? AND facility_size=?""",
        (scheme, catchment_id, year, ftype, fsize),
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _bed_util_survey5(conn, regions: list[str] | None, year: int,
                      ftype: str, fsize: str):
    """Bed utilization (fraction) = sum of inpatient days / (sum of licensed beds x days in the year). None if absent.

        The published bed utilization is the ratio of total inpatient days to licensed beds,
        with the days in the year following the survey's definition (366 for 2016 and 2020).
        With both source series held by city/county/district, the numerator and denominator
        can be summed for any combination of units, which gives the 'average of the catchment'
        named in section 3-4-1 of the guideline directly. A unit without a facility of the
        type has zero beds and contributes nothing to the denominator, so it is excluded
        automatically.

        regions=None sums over all units (= the nation).
    """
    def _sum(table, col):
        q = (f"SELECT SUM({col}) FROM {table} WHERE reference_year=? "
             f"AND facility_type=? AND facility_size=? AND category='전체'")
        params = [year, ftype, fsize]
        if regions:
            ph = ",".join("?" * len(regions))
            q += f" AND region_code IN ({ph})"
            params += list(regions)
        row = conn.execute(q, params).fetchone()
        return row[0] if row and row[0] is not None else None

    days = _sum("survey5_inpatient_days", "total_days")
    beds = _sum("survey5_beds", "licensed_beds")
    if not days or not beds:
        return None
    return days / (beds * _days_in_year(year))


def _reference_value(conn, metric: str):
    """A single indicator value from kdi_reference_values (a value stated in the guideline)."""
    row = conn.execute(
        "SELECT value FROM kdi_reference_values WHERE metric=?", (metric,)
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


# ─────────────────────────────────────────────────────────────────────────────
# Database helpers — raw data for the special-purpose (police) catchment (police_demand_input)
# Internal data that are not public, loaded by the user. Only the raw values are read here
# and made available as seed inputs; judging the validity and limits of the method is left
# to the Writer (LLM).
# ─────────────────────────────────────────────────────────────────────────────

def _police_raw(conn, project_id: str):
    """Index the rows of police_demand_input by item. None if there are none."""
    try:
        rows = conn.execute(
            "SELECT source_table, reference_year, item, value FROM "
            "police_demand_input WHERE project_id=?", (project_id,)).fetchall()
    except Exception:
        return None
    if not rows:
        return None
    by_item: dict[str, list] = {}
    for st, yr, item, val in rows:
        if val is None:
            continue
        by_item.setdefault(item, []).append((yr, float(val), st))
    return by_item


def _pget(praw: dict, item: str):
    """Value of an average/single item (first value, regardless of year). None if absent."""
    rec = praw.get(item)
    return rec[0][1] if rec else None


def _pget_at(praw: dict, item: str, target_year: int):
    """Item by year: the latest value not after target_year; otherwise the latest value; None if absent."""
    rec = sorted([r for r in praw.get(item, []) if r[0] is not None],
                 key=lambda x: x[0])
    if not rec:
        rec0 = praw.get(item)
        return rec0[0][1] if rec0 else None
    le = [r for r in rec if r[0] <= target_year]
    return (le[-1] if le else rec[-1])[1]


def _pget_latest(praw: dict, item: str):
    """Latest value of an item by year (the value itself if the year is None). None if absent."""
    rec = praw.get(item)
    if not rec:
        return None
    with_yr = [r for r in rec if r[0] is not None]
    if with_yr:
        return sorted(with_yr, key=lambda x: x[0])[-1][1]
    return rec[0][1]


def _police_table_of(praw: dict, item: str) -> str:
    rec = praw.get(item)
    return rec[0][2] if rec else ""


# ─────────────────────────────────────────────────────────────────────────────
# Database helpers — seed inputs for the five derived quantities (latest/recent years not after the upper bound)
# ─────────────────────────────────────────────────────────────────────────────

def _supply_beds_latest(conn, regions: list[str], upper_year: int | None):
    """Sum of supplied beds at general hospital level and above (tertiary + general) in the
        catchment, the year used, and the regions with data.
        Latest year not after the upper bound. Returns (beds_sum, used_year, covered) or (None, None, []).

        'With data' = the region has at least one supply_beds row for that year (regions
        without a general hospital have a zero row, so no row at all means missing data).
    """
    if not regions:
        return None, None, []
    ph = ",".join("?" * len(regions))
    params = list(regions)
    yq = (f"SELECT reference_year, SUM(value) FROM demand_parameter "
          f"WHERE parameter='supply_beds' AND facility_type IN ('상급종합','종합병원') "
          f"AND region_code IN ({ph})")
    if upper_year is not None:
        yq += " AND reference_year<=?"; params.append(upper_year)
    yq += " GROUP BY reference_year ORDER BY reference_year DESC LIMIT 1"
    row = conn.execute(yq, params).fetchone()
    if not row or row[1] is None:
        return None, None, []
    y = int(row[0])
    covered = [r for r in regions if conn.execute(
        "SELECT 1 FROM demand_parameter WHERE parameter='supply_beds' "
        "AND region_code=? AND reference_year=? LIMIT 1", (r, y)).fetchone()]
    return float(row[1]), y, covered


def _type_share_national(conn, upper_year: int | None):
    """National share of visit days at (tertiary + general hospitals) in all medical and health
        institutions, and the year used.
        Latest year not after the upper bound. Returns (share, used_year) or (None, None).
    """
    yq = ("SELECT reference_year FROM demand_parameter "
          "WHERE parameter='inpatient_days_by_type' AND region_code='00000'")
    params = []
    if upper_year is not None:
        yq += " AND reference_year<=?"; params.append(upper_year)
    yq += " ORDER BY reference_year DESC LIMIT 1"
    row = conn.execute(yq, params).fetchone()
    if not row:
        return None, None
    y = int(row[0])
    num = conn.execute(
        "SELECT SUM(value) FROM demand_parameter WHERE parameter='inpatient_days_by_type' "
        "AND region_code='00000' AND reference_year=? "
        "AND facility_type IN ('상급종합병원','종합병원')", (y,)).fetchone()
    den = conn.execute(
        "SELECT value FROM demand_parameter WHERE parameter='inpatient_days_by_type' "
        "AND region_code='00000' AND reference_year=? "
        "AND facility_type='의료기관및보건기관'", (y,)).fetchone()
    if not num or num[0] is None or not den or not den[0]:
        return None, None
    return float(num[0]) / float(den[0]), y


def _outpatient_ratio(conn, regions: list[str], years: list[int]):
    """Sum of outpatient / sum of inpatient visit days in the catchment (summed over the year window). Returns (ratio, used_years) or (None, None)."""
    if not regions or not years:
        return None, None
    out_tot = inp_tot = 0.0
    used = []
    for y in years:
        # Only regions with both outpatient and inpatient data (the ratio uses the same set of regions)
        _, cov_o = _raw_visit_days(conn, regions, y, "OUTPATIENT_DAYS")
        _, cov_i = _raw_visit_days(conn, regions, y, "INPATIENT_DAYS")
        both = [r for r in regions if r in cov_o and r in cov_i]
        o, _ = _raw_visit_days(conn, both, y, "OUTPATIENT_DAYS")
        i, _ = _raw_visit_days(conn, both, y, "INPATIENT_DAYS")
        if o is not None and i:
            out_tot += o; inp_tot += i; used.append(y)
    if inp_tot <= 0 or not used:
        return None, None
    return out_tot / inp_tot, used


def _emergency_death_rate(conn, regions: list[str], upper_year: int | None, window: int = 5):
    """Per-capita mortality from the three major emergency conditions (7 items) in the catchment
        = sum of deaths / sum of observed population.
        Most recent `window` years not after the upper bound. Returns (rate, used_years) or (None, None).
    """
    _emergency_death_rate.last_missing = {}  # so that values from an earlier call do not remain
    if not regions:
        return None, None
    ph = ",".join("?" * len(regions))
    params = list(regions)
    yq = (f"SELECT DISTINCT reference_year FROM demand_parameter "
          f"WHERE parameter='emergency_death' AND region_code IN ({ph})")
    if upper_year is not None:
        yq += " AND reference_year<=?"; params.append(upper_year)
    yq += " ORDER BY reference_year DESC LIMIT ?"; params.append(window)
    years = [int(r[0]) for r in conn.execute(yq, params)]
    if not years:
        return None, None
    deaths = 0.0
    pop = 0.0
    used = []
    missing: dict[int, list[str]] = {}
    for y in years:
        # Only regions with death data enter the numerator and the denominator
        covered = [r for r in regions if conn.execute(
            "SELECT 1 FROM demand_parameter WHERE parameter='emergency_death' "
            "AND region_code=? AND reference_year=? LIMIT 1", (r, y)).fetchone()]
        if len(covered) < len(regions):
            missing[y] = [r for r in regions if r not in covered]
        if not covered:
            continue
        dp = ",".join("?" * len(covered))
        d = conn.execute(
            f"SELECT SUM(value) FROM demand_parameter WHERE parameter='emergency_death' "
            f"AND region_code IN ({dp}) AND reference_year=?",
            list(covered) + [y]).fetchone()
        p, _ = _sum_observed_pop(conn, covered, y)
        if d and d[0] is not None and p:
            deaths += float(d[0]); pop += p; used.append(y)
    _emergency_death_rate.last_missing = missing
    if pop <= 0 or not used:
        return None, None
    return deaths / pop, sorted(used)


def _emergency_beds_existing(conn, regions: list[str], base_period: dict):
    """Existing emergency beds in the catchment (denominator in section 3-5-3). Read from the
        emergency_beds table, reusing the type/size/year/aggregation of
        base_period['bed_utilization']. The facility_type key is composed as '종합병원_{size}'.
        Returns (value, basis text).
    """
    bp = (base_period or {}).get("bed_utilization") or {}
    ftype = bp.get("facility_type")
    fsize = bp.get("facility_size")
    years = bp.get("years")
    agg = bp.get("agg", "single")
    if not (ftype and years):
        return None, "base_period(bed_utilization) 미결정"
    # type/size -> facility_type label in emergency_beds
    if ftype == "상급종합":
        key = "상급종합"
    elif ftype == "종합병원" and fsize and fsize != "N/A":
        key = f"종합병원_{fsize}"
    elif ftype == "종합병원":
        return None, "종합병원 규모(facility_size) 미결정"
    else:
        key = ftype
    ph = ",".join("?" * len(regions))
    vals = []
    missing = set()
    for y in sorted(years):
        row = conn.execute(
            f"SELECT SUM(value) FROM demand_parameter WHERE parameter='emergency_beds' "
            f"AND region_code IN ({ph}) AND facility_type=? AND reference_year=?",
            list(regions) + [key, y]).fetchone()
        if row and row[0] is not None:
            vals.append(float(row[0]))
        # Record the regions with no emergency-bed row at all for that year (missing data)
        for r in regions:
            if not conn.execute(
                    "SELECT 1 FROM demand_parameter WHERE parameter='emergency_beds' "
                    "AND region_code=? AND reference_year=? LIMIT 1", (r, y)).fetchone():
                missing.add(r)
    if not vals:
        return None, f"emergency_beds[{key}] 자료 없음"
    val = _agg(vals, agg)
    tail = f" · 자료 누락 지역 {sorted(missing)}(기존 병상에 미포함)" if missing else ""
    return val, f"{key}/{years}({agg}){tail}"


# ─────────────────────────────────────────────────────────────────────────────
# Loader for tables of the original reports (not called; see computation_node)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_tables_by_section(conn, project_id: str, section_id: str) -> list[dict]:
    try:
        rows = conn.execute(
            """SELECT table_label, title, page_num, rows_json
               FROM report_tables WHERE project_id=? AND section_id=?
               ORDER BY page_num""",
            (project_id, section_id),
        ).fetchall()
    except Exception as e:
        print(f"[Computation] 표 DB 조회 실패: {e}")
        return []
    result = []
    for label, title, page_num, rows_json in rows:
        try:
            rows_data = json.loads(rows_json) if rows_json else []
        except json.JSONDecodeError:
            rows_data = []
        result.append({"label": label, "title": title or "",
                       "page_num": page_num, "rows": rows_data})
    return result


def _format_tables(tables: list[dict]) -> str:
    if not tables:
        return ""
    lines = []
    for tbl in tables:
        lines.append(f"[{tbl['label']}] {tbl['title']} (p.{tbl['page_num']})")
        for i, row in enumerate(tbl["rows"]):
            cells = [str(c).replace("\n", " ") for c in row]
            lines.append("| " + " | ".join(cells) + " |")
            if i == 0:
                lines.append("| " + " | ".join(["---"] * len(cells)) + " |")
        lines.append("")
    return "\n".join(lines).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation helpers
# ─────────────────────────────────────────────────────────────────────────────

def _agg(values: list[float], mode: str):
    """Aggregate over the base years. mode='single' (one year) | 'mean' (window average)."""
    if not values:
        return None
    if mode == "mean":
        return sum(values) / len(values)
    # single: assumes one year. If several are given, the latest is used (with a warning).
    if len(values) > 1:
        print(f"[Computation] agg='single'인데 연도 {len(values)}개 — 최신값 사용")
    return values[-1]


# ─────────────────────────────────────────────────────────────────────────────
# Projection parameters (base_period["projection"] first, otherwise config)
# ─────────────────────────────────────────────────────────────────────────────

def _projection_params(conn, project_id: str, base_period: dict,
                       regions: list[str]) -> tuple[dict | None, list[str]]:
    """Decide vintage / target_year / ratio_ref_year.

        Priority: base_period["projection"] (from period_decider) -> project_config.
        ratio_ref_year (reference year of the current population share) = the latest observed
          year not after the upper bound for which both the catchment's units and their
          provinces have observations.
    """
    notes: list[str] = []
    proj = (base_period or {}).get("projection") or {}

    vintage = proj.get("vintage") or _load_config(conn, project_id, "projection_vintage")
    target_year = proj.get("target_year") or _get_target_year(conn, project_id)
    ratio_ref_year = proj.get("ratio_ref_year")

    if not vintage:
        notes.append("projection skip: projection_vintage 미설정(config/period_decider)")
        return None, notes
    if not target_year:
        notes.append("projection skip: target_year 미설정")
        return None, notes

    if ratio_ref_year is None:
        ratio_ref_year = _resolve_ratio_ref_year(conn, project_id, regions)
        if ratio_ref_year is None:
            notes.append("projection skip: 현재 인구비 기준연도(OBSERVED) 산정 불가")
            return None, notes

    return {"vintage": str(vintage), "target_year": int(target_year),
            "ratio_ref_year": int(ratio_ref_year)}, notes


def _resolve_ratio_ref_year(conn, project_id: str, regions: list[str]) -> int | None:
    """Latest observed year not after the upper bound (observations for both the catchment's units and their provinces)."""
    survey = data_year_cap(conn, project_id)  # survey year - 1
    sido_codes = {r if _is_aggregate_unit(r) else _sido_of(r) for r in regions}
    units = [r for r in regions if not _is_aggregate_unit(r)] + list(sido_codes)
    if not units:
        return None
    ph = ",".join("?" * len(units))
    params = list(units)
    q = (f"SELECT year, COUNT(DISTINCT region_code) FROM region_population_timeseries "
         f"WHERE data_type='OBSERVED' AND sex='T' AND age_group='TOTAL' "
         f"AND region_code IN ({ph})")
    if survey is not None:
        q += " AND year<=?"; params.append(survey)
    q += " GROUP BY year HAVING COUNT(DISTINCT region_code)=? ORDER BY year DESC LIMIT 1"
    params.append(len(set(units)))
    row = conn.execute(q, params).fetchone()
    return int(row[0]) if row else None


# ─────────────────────────────────────────────────────────────────────────────
# Seed inputs — based on the base_period decision object. No fallback.
# ─────────────────────────────────────────────────────────────────────────────

def _sources_from_notes(seed: dict, notes: list[str]) -> dict:
    """Derive the source of each seed value from notes.

        When a seed is obtained, a note of the form 'key: <how it was obtained>' is left.
        That note is the record of where the value came from, so it is used as the source as
        it stands rather than building a separate dictionary of sources. Seeds without a note
        (guideline assumptions, reference-table values) are left without a source.
    """
    src: dict = {}
    for n in notes:
        if ":" not in n:
            continue
        k, rest = n.split(":", 1)
        k = k.strip()
        if k in seed and " " not in k:
            src.setdefault(k, rest.strip())
    return src


def _build_general_seed(conn, project_id: str, regions: list[str],
                        base_period: dict) -> tuple[dict, list[str], dict]:
    """Seed for the general catchment. regions = the units of the catchment. notes = warnings and records."""
    seed: dict = {}
    notes: list[str] = []
    survey = data_year_cap(conn, project_id)  # upper bound on data years (survey year - 1)

    # ── Population (future; multiplied in the formulas): provincial projection x current share ──
    pp, pnotes = _projection_params(conn, project_id, base_period, regions)
    notes.extend(pnotes)
    if pp:
        pop, anotes = _projected_pop_anbun(
            conn, regions, pp["target_year"], pp["vintage"], pp["ratio_ref_year"])
        notes.extend(anotes)
        if pop is not None:
            seed["population"] = int(round(pop))
            notes.append(f"population: 시도추계(v{pp['vintage']}) {pp['target_year']} × "
                         f"현재({pp['ratio_ref_year']}) 인구비 안분")
        else:
            notes.append("population skip: 안분 결과 없음")

    # ── Per-capita inpatient utilization rate: inpatient days[base] / observed population[base] ──
    bp = (base_period or {}).get("inpatient_days")
    inp_years = bp.get("years") if bp else None
    if bp and inp_years:
        if not bp.get("basis"):
            notes.append("inpatient_days: base_period.basis 비어있음 — auditability 미충족")
        mode = bp.get("agg", "single")
        rates = []
        for y in sorted(inp_years):
            days, covered = _raw_inpatient_days(conn, regions, y)
            if covered and len(covered) < len(regions):
                notes.append(f"inpatient_days {y} 자료 누락 지역 "
                             f"{[r for r in regions if r not in covered]} — 분자·분모에서 함께 제외")
            base_pop, _ = _sum_observed_pop(conn, covered, y)  # same set of regions
            if days is not None and base_pop:
                rates.append(days / base_pop)
            else:
                notes.append(f"inpatient_days {y}: 입원일수/OBSERVED인구 자료 부족 — 제외")
        val = _agg(rates, mode)
        if val is not None:
            seed["inpatient_days"] = round(val, 4)
    else:
        notes.append("inpatient_days skip: base_period 미결정")

    # ── Bed utilization: by type/size, aggregated over the base years ──
    bp = (base_period or {}).get("bed_utilization")
    if bp and bp.get("years"):
        if not bp.get("basis"):
            notes.append("bed_utilization: base_period.basis 비어있음 — auditability 미충족")
        # Both variants are computed. Section 3-4-1 of the guideline lists 'the average of the
        # catchment', 'the bed utilization for the type of the facility', and 'the bed utilization
        # of similar facilities' as bases without ranking them, so the catchment value and the
        # national value are both produced and the choice is left to the user.
        ftype = bp.get("facility_type")
        fsize = bp.get("facility_size", "")
        mode = bp.get("agg", "single")
        for variant, regs in (("catchment", regions), ("national", None)):
            vals = []
            for y in sorted(bp["years"]):
                v = _bed_util_survey5(conn, regs, y, ftype, fsize)
                if v is not None:
                    vals.append(v)
                else:
                    notes.append(f"bed_utilization_{variant} {y} {ftype}/{fsize}: "
                                 f"해당 병상 없음 — 제외")
            val = _agg(vals, mode)
            if val is not None:
                seed[f"bed_utilization_{variant}"] = round(val, 4)
                notes.append(
                    f"bed_utilization_{variant}: Σ재원일수÷(Σ허가병상×연간일수, 윤년 366) "
                    f"[{ftype}/{fsize}/{bp['years']}({mode})"
                    f"{'/진료권 '+str(len(regions))+'개 시군구' if regs else '/전국'}]")
            else:
                notes.append(f"bed_utilization_{variant} skip: "
                             f"{ftype}/{fsize} 병상 자료 없음")
    else:
        notes.append("bed_utilization skip: base_period 미결정")

    # ── Beds per 1,000 local population (local_per_1000): supplied beds[base] / (population[base]/1000) ──
    # Like inpatient days, the ratio is computed here. This avoids a clash on the population
    # key: the denominator is the base-year population, whereas the population multiplied in
    # delta_beds is the future population.
    beds_sum, beds_year, beds_cov = _supply_beds_latest(conn, regions, survey)
    if beds_sum is not None:
        if len(beds_cov) < len(regions):
            notes.append(f"local_per_1000 {beds_year} 자료 누락 지역 "
                         f"{[r for r in regions if r not in beds_cov]} — 분자·분모에서 함께 제외")
        base_pop, _ = _sum_observed_pop(conn, beds_cov, beds_year)  # same set of regions
        if base_pop:
            seed["local_per_1000"] = round(beds_sum / (base_pop / 1000.0), 4)
            notes.append(f"local_per_1000: 종합병원급이상 공급병상[{beds_year}]÷OBSERVED인구[{beds_year}]")
        else:
            notes.append(f"local_per_1000 skip: {beds_year} OBSERVED인구 없음")
    else:
        notes.append("local_per_1000 skip: 공급병상(종합병원급이상) 자료 없음")

    # ── Share of inpatients of the specific type (type_share): national visit days ──
    ts, ts_year = _type_share_national(conn, survey)
    if ts is not None:
        seed["type_share"] = round(ts, 4)
        notes.append(f"type_share: 전국 (상급종합+종합)÷의료기관및보건기관 내원일수[{ts_year}]")
    else:
        notes.append("type_share skip: 전국 종별 내원일수 자료 없음")

    # ── Outpatient-to-inpatient ratio (outpatient_ratio): catchment sum of outpatient / inpatient ──
    if inp_years:
        orr, or_years = _outpatient_ratio(conn, regions, inp_years)
        if orr is not None:
            seed["outpatient_ratio"] = round(orr, 4)
            notes.append(f"outpatient_ratio: 진료권 Σ외래÷Σ입원 내원일수{or_years}")
        else:
            notes.append("outpatient_ratio skip: 진료권 외래/입원 내원일수 부족")
    else:
        notes.append("outpatient_ratio skip: 기준연도창(inpatient_days) 미결정")

    # ── Emergency mortality rate (death_rate): per-capita, three major conditions, recent 5 years ──
    dr, dr_years = _emergency_death_rate(conn, regions, survey, window=5)
    for y, miss in sorted(getattr(_emergency_death_rate, "last_missing", {}).items()):
        notes.append(f"death_rate {y} 자료 누락 지역 {miss} — 분자·분모에서 함께 제외")
    if dr is not None:
        seed["death_rate"] = dr  # per-capita rate (fraction); multiplied by the future population -> emergency deaths
        notes.append(f"death_rate: 진료권 Σ3대응급(7항목)사망자÷Σ OBSERVED인구{dr_years}")
    else:
        notes.append("death_rate skip: 3대응급 사망자/인구 자료 부족")

    # ── Guideline assumptions / stated values ──
    seed["ri"] = 1.0
    seed["ci"] = 1.0
    nat = _reference_value(conn, "인구천명당병상수")
    if nat is not None:
        seed["national_per_1000"] = nat
    el = _reference_value(conn, "응급사망률_응급병상_탄력성")
    if el is not None:
        seed["elasticity"] = el  # for death_rate_reduction

    # ── Beds of the facility (section 3-4-1) ──
    # Guideline, section 3-4-1: annual inpatients = beds x bed utilization x 365. Beds here are
    # the beds the facility will hold within its service capacity, i.e. the planned beds of the
    # new facility -> project_config['total_beds'] (a fact from the project plan) is used as is.
    #
    # The guideline assumes one number of beds for one facility and has no rule for splitting
    # beds between a dual catchment (general / special-purpose). beds is therefore not put in
    # the special seed: assigning the bed composition of the project plan (bed_breakdown) to the
    # general and special calculations would be a choice made by the pipeline, not by the
    # guideline, so it is not done and the value is left uncomputed.
    total_beds = _load_config_int(conn, project_id, "total_beds")
    if total_beds is not None:
        seed["beds"] = total_beds
        notes.append(f"beds: 사업계획서 총 병상 {total_beds}병상 "
                     f"(지침 §3-4-1 '보유하게 되는 병상 수')")
    else:
        notes.append("beds skip: project_config['total_beds'] 없음 "
                     "→ §3-4 연간 입원·외래환자 수 자동 skip")

    # ── Rate of increase in emergency beds (section 3-5-3): new / existing emergency beds in the catchment ──
    # new:      project_config['new_emergency_beds'] (project plan)
    # existing: emergency_beds[catchment, type/size of the facility, base_period years] — reuses
    #           the bed_utilization decision (type, size, years) made by period_decider.
    new_beds = _load_config_int(conn, project_id, "new_emergency_beds")
    if new_beds is not None:
        existing, eb_note = _emergency_beds_existing(conn, regions, base_period)
        if existing and existing > 0:
            seed["bed_increase_rate_pct"] = round(new_beds / existing * 100.0, 4)
            notes.append(f"bed_increase_rate_pct: 신규 {new_beds} ÷ 기존 응급병상 {existing} ({eb_note})")
        else:
            notes.append(f"bed_increase_rate_pct skip: 진료권 기존 응급병상 조달 불가 ({eb_note})")
    else:
        notes.append("bed_increase_rate_pct skip: project_config['new_emergency_beds'] 없음 "
                     "→ death_rate_reduction·preventable_deaths 자동 skip")

    return seed, notes, _sources_from_notes(seed, notes)


def _build_special_seed(conn, project_id: str, special_regions: list[str],
                        base_period: dict, shared_bed_util: dict | None = None
                        ) -> tuple[dict | None, list[str], dict]:
    """Seed for the special-purpose (police) catchment — the general Method 1, Method 2, and
        outpatient formulas applied to police values.

        Design:
          - No separate formula. 'Population' is replaced by the police workforce and the
            'inpatient utilization rate' by inpatient days per police officer, and the same
            formula catalog is run.
          - The raw values (workforce, utilization days, beds of the existing hospital) are
            read from police_demand_input and made available as seeds. For Method 2 only the
            raw supply_beds (licensed beds of the existing hospital) are given and the formula
            computes the per-1,000 value. For the police the denominator and the multiplied
            population are the same workforce, so the formula holds as it is.
          - The code states no conclusion. Only the source context (sources) is exposed to the
            Writer, which describes the validity and limits of the method.
          - death_rate (section 3-5) is not injected -> emergency_deaths is skipped and the
            Writer states why it is not applied.

        Returns: (seed | None, notes, sources). Without police_raw: population only / None.
    """
    notes: list[str] = []
    sources: dict = {}
    if not special_regions:
        return None, notes, sources

    target_year = _get_target_year(conn, project_id)
    praw = _police_raw(conn, project_id)

    # ── No police raw data -> only the national projected population; everything else is skipped ──
    if praw is None:
        pp, pnotes = _projection_params(conn, project_id, base_period, special_regions)
        notes.extend(pnotes)
        seed = {"ri": 1.0, "ci": 1.0}
        if pp:
            pop, anotes = _projected_pop_anbun(
                conn, special_regions, pp["target_year"], pp["vintage"], pp["ratio_ref_year"])
            notes.extend(anotes)
            if pop is not None:
                seed["population"] = int(round(pop))
        if "population" not in seed:
            return None, notes, sources
        return seed, notes, sources

    # ── Police track ──
    if target_year is None:
        notes.append("special(경찰) skip: target_year 없음")
        return None, notes, sources

    seed = {"ri": 1.0, "ci": 1.0}

    # population <- projected police workforce (target_year) + coast guard
    force = _pget_at(praw, "경찰공무원 인력", target_year)
    marine = _pget(praw, "해양경찰 인력") or 0.0
    if force is None:
        notes.append("special(경찰) skip: 경찰 인력 추계 raw 없음")
        return None, notes, sources
    seed["population"] = int(round(force + marine))
    sources["population"] = (f"경찰공무원 인력 추계({target_year}) {int(force):,}명 + "
                             f"해양경찰 {int(marine):,}명 [출처: "
                             f"{_police_table_of(praw, '경찰공무원 인력')}]")

    # inpatient utilization rate <- total inpatient days of the police / police workforce (average)
    inp = _pget(praw, "입원 입내원일수")
    favg = _pget(praw, "경찰 인력(평균)")
    if inp and favg:
        seed["inpatient_days"] = round(inp / favg, 4)
        sources["inpatient_days"] = (f"경찰 입원 입내원일수÷경찰 인력(평균) "
                                     f"[출처: {_police_table_of(praw, '입원 입내원일수')}]")
    else:
        notes.append("special(경찰) inpatient_days skip: 입원일수/인력 raw 부족")

    # type share <- police inpatient days at general hospital level and above / all inpatient days
    gen_inp = _pget(praw, "종합병원급이상 입원 입내원일수")
    if gen_inp and inp:
        seed["type_share"] = round(gen_inp / inp, 4)
        sources["type_share"] = ("경찰 종합병원급이상 입원÷전체 입원 입내원일수"
                                 f"(자료가 종합병원급이상 단위로만 제공) "
                                 f"[출처: {_police_table_of(praw, '종합병원급이상 입원 입내원일수')}]")
    else:
        notes.append("special(경찰) type_share skip: 종합병원급이상/전체 입원 raw 부족")

    # outpatient-to-inpatient ratio <- police outpatient / inpatient visit days
    out = _pget(praw, "외래 입내원일수")
    if out and inp:
        seed["outpatient_ratio"] = round(out / inp, 4)
        sources["outpatient_ratio"] = (f"경찰 외래÷입원 입내원일수 "
                                       f"[출처: {_police_table_of(praw, '외래 입내원일수')}]")
    else:
        notes.append("special(경찰) outpatient_ratio skip: 외래/입원 raw 부족")

    # Method 2 numerator <- raw licensed beds of the existing hospital only -> the formula computes local_per_1000
    beds = _pget_latest(praw, "허가병상수")
    if beds:
        seed["supply_beds"] = beds
        sources["supply_beds"] = (f"국립경찰병원 본원 허가병상수 {int(beds):,}병상 "
                                  f"[출처: {_police_table_of(praw, '허가병상수')}]")
    else:
        notes.append("special(경찰) supply_beds 없음: 방법2 자동 skip")

    # ── Bed utilization ──
    # The value of the general catchment is not copied. The guideline treats the catchment of a
    # special-purpose facility as the whole country, so its 'average of the catchment' is by
    # definition the national sum, and it is computed directly from the units of the special
    # catchment. When that catchment is the nation ('00000' or other aggregate units), the two
    # variants give the same value; if they differ, an input was obtained incorrectly.
    bp_bu = (base_period or {}).get("bed_utilization")
    if bp_bu and bp_bu.get("years"):
        sp_ftype = bp_bu.get("facility_type")
        sp_fsize = bp_bu.get("facility_size", "")
        sp_mode = bp_bu.get("agg", "single")
        # If the special catchment consists only of aggregate units (nation/provinces), use the national sum
        sp_regs = None if all(_is_aggregate_unit(r) for r in special_regions) \
            else special_regions
        for variant, regs in (("catchment", sp_regs), ("national", None)):
            vals = [v for v in
                    (_bed_util_survey5(conn, regs, y, sp_ftype, sp_fsize)
                     for y in sorted(bp_bu["years"])) if v is not None]
            val = _agg(vals, sp_mode)
            if val is not None:
                seed[f"bed_utilization_{variant}"] = round(val, 4)
                sources[f"bed_utilization_{variant}"] = (
                    f"Σ재원일수÷(Σ허가병상×연간일수, 윤년 366) [{sp_ftype}/{sp_fsize}/"
                    f"{bp_bu['years']}({sp_mode})/"
                    f"{'전국(지침상 특수목적 진료권=전국)' if regs is None else str(len(regs))+'개 시군구'}]")
            else:
                notes.append(f"special bed_utilization_{variant} skip: "
                             f"{sp_ftype}/{sp_fsize} 병상 자료 없음")
    else:
        notes.append("special(경찰) bed_utilization skip: base_period 미결정")

    # stated national value per 1,000 (the benchmark in Method 2)
    nat = _reference_value(conn, "인구천명당병상수")
    if nat is not None:
        seed["national_per_1000"] = nat

    # death_rate (section 3-5) and beds (facility basis) are not injected -> those formulas are skipped and the Writer states why
    return seed, notes, sources


# ─────────────────────────────────────────────────────────────────────────────
# catchment_result -> regions
# ─────────────────────────────────────────────────────────────────────────────

SPECIAL_LABEL_KEYWORDS = ("특수목적", "전국", "특수", "national", "special")


def _extract_regions(conn, state: GraphState):
    """Extract (general regions, special regions) from state.catchment_result."""
    cr = state.get("catchment_result") or {}
    general, special = [], []

    if isinstance(cr, dict) and cr:
        items = list(cr.items())  # [(label, {regions,...}), ...]
        for label, body in items:
            regions = [str(x) for x in (body.get("regions") or [])] \
                if isinstance(body, dict) else []
            if not regions:
                continue
            is_special = (
                any(k in str(label) for k in SPECIAL_LABEL_KEYWORDS)
                or set(regions) == {"00000"}
            )
            # With a single scheme, it is treated as general (the target of the demand calculation) even if it carries a special-purpose keyword.
            if is_special and len(items) > 1:
                special.extend(r for r in regions if r not in special)
            else:
                general.extend(r for r in regions if r not in general)

    # Normalize to statistical units and remove duplicates (non-autonomous district -> its city).
    # The resolver in section 3-2 already normalizes, so this normally changes nothing; any
    # change is recorded.
    general, gch = normalize(general)
    special_n, sch = normalize([r for r in special if r != "00000"])
    special = (["00000"] if "00000" in special else []) + special_n
    _extract_regions.last_changes = gch + sch
    if gch or sch:
        print(f"[Computation] 진료권 코드 정규화: {gch + sch}")

    if not general:
        # The fallback to the project site is logged. If catchment resolution in section 3-2 fails
        # and the calculation proceeds with the city/county/district of the project site, the
        # output would otherwise look like a normal single-unit catchment.
        sig = _load_config(conn, state["project_id"], "location_sigungu_code")
        if sig:
            general = [str(sig)]
            print(f"[Computation] [!] §3-2 진료권 해소 결과가 비어 사업 소재지"
                  f"({sig}) 단독으로 되돌림 — 이 실행의 진료권은 도출된 것이 "
                  f"아니라 대체된 것임")
        else:
            print("[Computation] [!] §3-2 진료권 해소 실패 + 사업 소재지 코드 없음 "
                  "— 진료권 의존 계산 전부 skip")
    return general, special


# ─────────────────────────────────────────────────────────────────────────────
# Catalog execution
# compute_with_report normalizes the output keys to the semantic names in OUTPUT_ALIAS, so
# the vocabulary of _SECTION_NUMBER_KEYS below and the keys of numbers agree.
# ─────────────────────────────────────────────────────────────────────────────

def _consumed_inputs(executed: list[str]) -> set:
    """Set of input keys actually consumed by the executed formulas (the seed exposure filter)."""
    consumed = set()
    for fid in executed:
        f = CATALOG.get(fid)
        if f:
            consumed.update(f.required_inputs)
    return consumed


def _run_catalog(conn, project_id: str, regions: list[str],
                 special_regions: list[str], base_period: dict) -> dict:
    general_seed, gnotes, gsources = _build_general_seed(
        conn, project_id, regions, base_period)
    for n in gnotes:
        print(f"[Computation] (general) {n}")
    gen = compute_with_report(general_seed)

    # Values produced by formulas (numbers) are kept separate from input values obtained from
    # the database and the project plan (inputs). An obtained input is a fact of its source, not
    # a calculation result, so it is attributed to that source and does not carry a 「계산」 marker.
    numbers = dict(gen["numbers"])  # formula outputs
    executed = list(gen["executed"])
    skipped = dict(gen["skipped"])
    # Only the seeds consumed by an executed formula are exposed (unused constants are withheld).
    consumed = _consumed_inputs(executed)
    inputs: dict = {}
    input_sources: dict = {}
    for k, v in general_seed.items():
        if k in consumed and k not in numbers:
            inputs[k] = v
            if k in gsources:
                input_sources[k] = gsources[k]

    special_seed, snotes, ssources = _build_special_seed(
        conn, project_id, special_regions, base_period,
        shared_bed_util=None)  # computed directly from the units of the special catchment, not copied
    for n in snotes:
        print(f"[Computation] (special) {n}")
    seed_sources: dict = {}
    if special_seed is not None:
        sp = compute_with_report(special_seed)
        for k, v in sp["numbers"].items():
            numbers[f"special_{k}"] = v
        # For the special seed as well, only what the executed formulas consumed is exposed
        sp_consumed = _consumed_inputs(sp["executed"])
        for k, v in special_seed.items():
            if k in sp_consumed and f"special_{k}" not in numbers:
                inputs[f"special_{k}"] = v
        # Expose the source context of the consumed special seeds to the Writer (the judgment is left to the LLM)
        for k, src in ssources.items():
            if k in sp_consumed:
                seed_sources[f"special_{k}"] = src
                input_sources.setdefault(f"special_{k}", src)
        executed += [f"special_{e}" for e in sp["executed"]]
        for k, v in sp["skipped"].items():
            skipped[f"special_{k}"] = v
        print(f"[Computation] 특수목적(경찰) 진료권 계산: {len(sp['executed'])}개 공식 실행")
    else:
        print("[Computation] 특수목적 입력 없음 → 단일(일반) 진료권")

    return {"numbers": numbers, "inputs": inputs,
            "input_sources": input_sources,
            "executed": executed, "skipped": skipped,
            "seed": general_seed, "seed_sources": seed_sources}


# ─────────────────────────────────────────────────────────────────────────────
# Computation Agent
# ─────────────────────────────────────────────────────────────────────────────

_ch3_cache: dict[str, dict] = {}


def _cache_key(project_id, regions, special_regions, base_period) -> str:
    sig = json.dumps({"r": regions, "s": special_regions, "bp": base_period},
                     sort_keys=True, ensure_ascii=False, default=str)
    return project_id + ":" + hashlib.md5(sig.encode("utf-8")).hexdigest()[:10]


# Per-subsection exposure filter.
# The catalog computes the whole case once, but the Writer of each subsection is shown only
# the outputs and inputs that the subsection deals with. The mapping follows the guideline's
# table of contents (3-3 beds / 3-4 inpatients and outpatients / 3-5 emergency), which
# determines which formula belongs to which subsection. This keeps, for example, the bed
# values of section 3-3 out of section 3-5.
# stem = the key without the 'special_' prefix. Keys not in the subsection's set are not shown to the Writer.
_SECTION_NUMBER_KEYS = {
    # Values that use bed utilization are produced as _catchment/_national variants, so the
    # vocabulary holds both. Values that do not (annual_inpatient, delta_beds, etc.) have one entry.
    "3-3-1": {"population", "inpatient_days", "ri", "ci", "type_share",
              "annual_inpatient",
              "bed_utilization_catchment", "bed_utilization_national",
              "capacity_per_bed_catchment", "capacity_per_bed_national",
              "required_beds_catchment", "required_beds_national",
              "specific_type_beds_catchment", "specific_type_beds_national"},
    "3-3-2": {"population", "supply_beds", "local_per_1000",
              "national_per_1000", "delta_beds"},
    # The term for section 3-4 is 'annual_inpatient_facility', not 'annual_inpatient'.
    # Method 1 of section 3-3 (potential demand of the catchment = population x inpatient
    # utilization rate x RI) and section 3-4 (facility basis = beds x bed utilization x 365) are
    # different quantities. After alias normalization the name 'annual_inpatient' belongs to the
    # output of Method 1, so using it here would make section 3-4 report the section 3-3 value
    # as its own. If the facility formula is skipped, section 3-4 states that the value was not
    # computed.
    "3-4-1": {"population", "beds",
              "bed_utilization_catchment", "bed_utilization_national",
              "annual_inpatient_facility_catchment",
              "annual_inpatient_facility_national"},
    "3-4-2": {"population", "outpatient_ratio",
              "annual_inpatient_facility_catchment",
              "annual_inpatient_facility_national",
              "annual_outpatient_catchment", "annual_outpatient_national"},
    "3-4-3": set(),  # induced demand: descriptive (no figures)
    "3-5-1": set(),  # basic data: descriptive
    "3-5-2": {"population", "death_rate", "emergency_deaths"},
    "3-5-3": {"population", "death_rate", "emergency_deaths", "elasticity",
              "bed_increase_rate_pct", "death_rate_reduction",
              "preventable_deaths"},
}


def _readable_skipped_for_section(skipped: dict, section_id: str) -> list[str]:
    """Items of this subsection that were not computed, in a readable form.

        The Writer is told which items were skipped so that the draft can state what is
        missing instead of passing over it.
        Only formulas in the subsection's vocabulary are kept.
    """
    allowed = _SECTION_NUMBER_KEYS.get(section_id)
    if not allowed:
        return []
    out = []
    for fid, missing in skipped.items():
        if not missing:
            continue
        is_special = fid.startswith("special_")
        base = fid[len("special_"):] if is_special else fid
        f = CATALOG.get(base)
        if f is None:
            continue
        outname = OUTPUT_ALIAS.get(base, base)
        if outname not in allowed:
            continue
        scope = "특수목적 진료권" if is_special else "일반 진료권"
        out.append(f"[{scope}] {f.description} → 산출되지 않음 "
                   f"(확보되지 않은 입력: {', '.join(missing)})")
    return sorted(out)


def _filter_numbers_for_section(numbers: dict, section_id: str) -> dict:
    """Keep only the figures the current subsection deals with. Undefined subsections (3-1, etc.) get none.

        The filter logs both directions:
          (a) keys that were computed but dropped because they are not in the vocabulary
              -> possible vocabulary mismatch
          (b) keys in the vocabulary that were not computed -> inputs not satisfied (may be normal)
    """
    allowed = _SECTION_NUMBER_KEYS.get(section_id)
    if allowed is None:
        return {}

    def _stem(k: str) -> str:
        return k[len("special_"):] if k.startswith("special_") else k

    out = {k: v for k, v in numbers.items() if _stem(k) in allowed}
    return out


def _report_section_filter(numbers: dict, inputs: dict, section_id: str) -> None:
    """Log the decisions of the per-subsection exposure filter.

        The decision is made once, on the union of numbers (formula outputs) and inputs
        (obtained inputs). Filtering the two dicts separately would report keys held by the
        other as 'not computed'.
    """
    allowed = _SECTION_NUMBER_KEYS.get(section_id)
    if not allowed:
        return

    def _stem(k: str) -> str:
        return k[len("special_"):] if k.startswith("special_") else k

    available = {**numbers, **inputs}
    dropped = sorted(k for k in available if _stem(k) not in allowed)
    absent = sorted(a for a in allowed
                    if not any(_stem(k) == a for k in available))
    if dropped:
        print(f"[Computation] {section_id} — 소절 어휘 밖이라 미노출: {dropped}")
    if absent:
        print(f"[Computation] {section_id} — 소절 어휘에 있으나 미산출: {absent}")


def computation_agent(state: GraphState) -> GraphState:
    section_id = state["section_id"]
    project_id = state["project_id"]

    # Section 3-2 (setting the catchment) is where the catchment is derived -> no calculated figures or tables are injected.
    if section_id == "3-2":
        print(f"[Computation] {section_id} 진료권 설정 — 계산 수치 미주입(도출 소절)")
        return {**state, "calc_results": {}}
    # Section 3-1 (overview) has no vocabulary and the catchment is not yet set, so the calculation is skipped.
    if section_id == "3-1":
        return {**state, "calc_results": {}}

    conn = _get_conn()
    try:
        # Tables of the original reports (report_tables) are not given to the Writer; the original
        # reports are used only for comparison. The loader is kept but not called.
        tables, formatted = [], ""

        if section_id.startswith("3-"):
            regions, special_regions = _extract_regions(conn, state)
            base_period = state.get("base_period") or {}
            key = _cache_key(project_id, regions, special_regions, base_period)

            if key not in _ch3_cache:
                print(f"[Computation] 카탈로그 계산 실행 (project={project_id}, "
                      f"regions={regions}, special={special_regions})")
                if not base_period:
                    print("[Computation] base_period 미주입 — 기준연도 의존 입력은 "
                          "skip됨(period_decider 미배선 상태에서는 driver가 주입 필요)")
                _ch3_cache[key] = _run_catalog(conn, project_id, regions,
                                               special_regions, base_period)
            else:
                print(f"[Computation] 카탈로그 캐시 사용 (key={key})")

            result = _ch3_cache[key]
            # Per-subsection exposure filter
            numbers = _filter_numbers_for_section(result["numbers"], section_id)
            inputs  = _filter_numbers_for_section(result.get("inputs", {}), section_id)
            _report_section_filter(result["numbers"], result.get("inputs", {}),
                                   section_id)
            skipped = {k: v for k, v in result["skipped"].items() if v}
            unresolved = _readable_skipped_for_section(skipped, section_id)

            print(f"[Computation] {section_id} — 표 {len(tables)}개 + "
                  f"수치 {len(numbers)}개 + 입력 {len(inputs)}개"
                  f"(소절필터 적용, 전체 {len(result['numbers'])}) "
                  f"(실행 {len(result['executed'])}공식, 미충족 {len(skipped)}공식)")
            if skipped:
                print("[Computation] 입력 미충족(skip)된 공식:")
                for fid, miss in skipped.items():
                    print(f"  - {fid}: 부족 입력 {miss}")

            calc_results = {"numbers": numbers, "inputs": inputs,
                            "regions": {"general": regions, "special": special_regions},
                            "region_normalization":
                                getattr(_extract_regions, "last_changes", [])}
            if formatted:
                calc_results["tables"] = formatted
            calc_results["skipped_formulas"] = skipped
            # Keep the list of executed formulas in the output.
            # numbers is stored after per-subsection filtering, so a value that was computed but exposed
            # in no subsection would leave no trace. With executed stored, the number of executed
            # formulas can be compared with the number exposed for each run. executed keeps the
            # formula_id (alias normalization applies only to numbers).
            calc_results["executed"] = list(result.get("executed", []))
            if unresolved:
                calc_results["unresolved"] = unresolved
                print(f"[Computation] {section_id} — 이 소절 미산출 항목 "
                      f"{len(unresolved)}건(Writer 에 노출)")
            # Sources of the obtained inputs (only for the inputs exposed)
            isrc = {k: v for k, v in result.get("input_sources", {}).items()
                    if k in inputs}
            if isrc:
                calc_results["input_sources"] = isrc
            if result.get("seed_sources"):
                src = {k: v for k, v in result["seed_sources"].items()
                       if k in numbers or k in inputs}
                if src:
                    calc_results["seed_sources"] = src
            return {**state, "calc_results": calc_results}

        # Subsections outside Chapter 3: tables only
        if not tables:
            print(f"[Computation] {section_id} — 표 없음")
            return {**state, "calc_results": {}}
        print(f"[Computation] {section_id} — 표 {len(tables)}개 로드")
        return {**state, "calc_results": {"tables": formatted}}
    finally:
        conn.close()