# agents/memory.py
# Memory agent: reads project facts and data from SQLite and assembles the Writer's context.
#
# What it provides:
#   - Project overview from project_config -> section_facts["project_context"]
#   - For section 3-2, the four kinds of catchment data named in the guideline (population,
#     self-sufficiency rate, distance and travel time, similar medical facilities), as raw
#     values only, with no interpretive wording and no layout that would steer the analysis.
#     Data (3) and (4) are given in statistical units (non-autonomous districts merged into
#     their city) so that population is not counted twice; data (4) covers the same
#     cities/counties/districts as data (3).
#   - Each loader returns, besides the text shown to the Writer, a list of (type, entity,
#     value) records for every figure in that text (Wiseman et al., 2017). The evaluation
#     checks the figures cited in the generated text against these records, so every figure
#     in the text must have a record.
#   - In section 3-2, a ruling on whether the special-purpose clause of the guideline applies
#     to the project (see the gate below). The ruling, its rationale, the raw response, and
#     the number of attempts are stored in section_facts.
#   - For subsections after 3-2, the fixed catchment (codes resolved to place names).
# The original report text is not loaded into the pipeline state.
# ─────────────────────────────────────────────────────────────────────────────
#
#
#
#
# The project context does not include a hospital-type label. Facts from the project plan
# (purpose, bed composition, etc.) are given, and the Writer determines the type and the
# catchment from Table III-1 of the guideline.

import re, sqlite3

from state import GraphState
from agents.sub.data_cap import data_year_cap
from paths import DB_PATH, DIST_TABLE, N_DISTANCE_SHOWN
from agents.sub.llm import call_json
from agents.sub.region_units import is_ilban_gu, children_of, region_name


def _get_conn():
    return sqlite3.connect(DB_PATH)


# ─────────────────────────────────────────────────────────────────────────────
# Source labels
# Managed in one place so that the [출처: …] labels in the Writer prompt and the source field
# of the records are always the same strings. The evaluation uses these labels to match the
# source declared in a sentence with the source where the value actually exists.
# ─────────────────────────────────────────────────────────────────────────────

SRC_POPULATION = "행정안전부 주민등록 인구통계 + NHIS 건강보험통계연보"
SRC_RI         = "보건복지부 제5차 국민보건의료실태조사(시군구편) Ⅱ.A.24"
# Short labels without parentheses, so that the Writer cites them unchanged. The reference
# date and the method of measurement are given on separate lines of the data block.
SRC_DISTANCE   = "Kakao Mobility 길찾기 API"
SRC_FACILITY   = "심사평가원 의료기관 신고자료"
FACILITY_REF   = "2025.12 신고 기준"
SRC_PLAN       = "사업 기본계획서"


def _rec(rtype: str, entity: str, value, source: str,
         year=None, unit: str | None = None, entity_code: str | None = None) -> dict:
    """A (type, entity, value) record; year, unit, and entity code are metadata.

        Follows the record form r = (r.t type, r.e entity, r.m value) of Wiseman et al. (2017).
        Where one more axis is needed, such as patient type, it is folded into the type name
        to keep three terms (e.g. ri_입원 / ri_외래).
    """
    return {"type": rtype, "entity": entity, "entity_code": entity_code,
            "year": year, "value": value, "unit": unit, "source": source}


# ─────────────────────────────────────────────────────────────────────────────
# Address parsing utilities (also used by computation.py)
# ─────────────────────────────────────────────────────────────────────────────

_METROPOLITAN_PREFIXES = (
    "서울특별시", "부산광역시", "대구광역시", "인천광역시",
    "광주광역시", "대전광역시", "울산광역시", "세종특별자치시",
)

_PROVINCE_MAP = {
    "서울특별시": "서울", "부산광역시": "부산", "대구광역시": "대구",
    "인천광역시": "인천", "광주광역시": "광주", "대전광역시": "대전",
    "울산광역시": "울산", "세종특별자치시": "세종",
    "경기도": "경기", "강원특별자치도": "강원", "강원도": "강원",
    "충청북도": "충북", "충청남도": "충남", "전북특별자치도": "전북",
    "전라북도": "전북", "전라남도": "전남", "경상북도": "경북",
    "경상남도": "경남", "제주특별자치도": "제주", "제주도": "제주",
}


def _parse_province(address: str) -> str | None:
    if not address:
        return None
    address = address.strip()
    for fullname in sorted(_PROVINCE_MAP.keys(), key=len, reverse=True):
        if address.startswith(fullname):
            return _PROVINCE_MAP[fullname]
    return None


def _parse_municipality(address: str) -> str | None:
    if not address:
        return None
    address = address.strip()
    for prefix in _METROPOLITAN_PREFIXES:
        if address.startswith(prefix):
            remaining = address[len(prefix):].strip()
            m = re.search(r"(\S+구|\S+군)", remaining)
            return m.group(1) if m else prefix
    m = re.search(r"(\S+시|\S+군|\S+구)", address)
    return m.group(1) if m else None


# ─────────────────────────────────────────────────────────────────────────────
# Loading the four kinds of data for section 3-2
# ─────────────────────────────────────────────────────────────────────────────
#
# Each function returns {"available": bool, "content": str, "note": str}.
# If the input is missing, available=False and note gives the reason.


def _get_location_code(conn, project_id: str, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM project_config WHERE project_id=? AND key=?",
        (project_id, key)
    ).fetchone()
    return row[0] if row else None


def _region_name(conn, region_code: str) -> str:
    row = conn.execute(
        "SELECT region_name FROM region_master WHERE region_code=?", (region_code,)
    ).fetchone()
    return row[0] if row else region_code


def _survey_year(conn, project_id: str):
    """Upper bound on data years (survey year - 1), the same bound used by Period and Computation
        (agents/sub/data_cap.py). Data for the survey year itself are published the following
        year and were not available to the original analysts.
    """
    return data_year_cap(conn, project_id)


def load_data_1_population(conn, project_id: str) -> dict:
    """Data (1): population (resident registration time series + health insurance coverage). Guideline section 3-2 ra., first item."""
    region_code = _get_location_code(conn, project_id, "location_sigungu_code")
    if not region_code:
        return {"available": False, "content": "", "records": [],
                "note": "자료①: location_sigungu_code 없음"}
    region_name = _region_name(conn, region_code)

    lines = [f"[출처: {SRC_POPULATION}]",
             f"[대상: {region_name} ({region_code})]"]
    recs: list[dict] = []  # source records for every figure shown in content

    # (a) total population, most recent five years
    sy = _survey_year(conn, project_id) or 9999
    rows = conn.execute("""
        SELECT year, population FROM region_population_timeseries
        WHERE region_code=? AND data_type='OBSERVED' AND sex='T' AND age_group='TOTAL'
          AND year<=?
        ORDER BY year DESC LIMIT 5
    """, (region_code, sy)).fetchall()
    if rows:
        lines.append("\n총인구(최근 5년):")
        for y, p in sorted(rows):
            lines.append(f"  - {y}년: {p:,}명")
            recs.append(_rec("population", region_name, p, SRC_POPULATION,
                             year=y, unit="명", entity_code=region_code))

    # (b) sex ratio, most recent year
    maxyr = conn.execute("""
        SELECT MAX(year) FROM region_population_timeseries
        WHERE region_code=? AND data_type='OBSERVED' AND sex='M' AND age_group='TOTAL'
          AND year<=?
    """, (region_code, sy)).fetchone()[0]
    if maxyr:
        sex_rows = conn.execute("""
            SELECT sex, population FROM region_population_timeseries
            WHERE region_code=? AND data_type='OBSERVED' AND age_group='TOTAL'
              AND sex IN ('M','F') AND year=?
        """, (region_code, maxyr)).fetchall()
        sd = {s: p for s, p in sex_rows}
        if 'M' in sd and 'F' in sd:
            lines.append(f"\n성비({maxyr}년): 남 {sd['M']:,}명 / 여 {sd['F']:,}명")
            recs.append(_rec("population_male", region_name, sd['M'], SRC_POPULATION,
                             year=maxyr, unit="명", entity_code=region_code))
            recs.append(_rec("population_female", region_name, sd['F'], SRC_POPULATION,
                             year=maxyr, unit="명", entity_code=region_code))

    # (c) share aged 65 and over (sum of age groups)
    ELDERLY = {'65~69세','70~74세','75~79세','80~84세','85~89세','90~94세','95~99세','100세 이상'}
    if maxyr:
        age_rows = conn.execute("""
            SELECT age_group, population FROM region_population_timeseries
            WHERE region_code=? AND data_type='OBSERVED' AND sex='T'
              AND age_group NOT IN ('TOTAL','AGE_RANGE_TOTAL') AND year=?
        """, (region_code, maxyr)).fetchall()
        if age_rows:
            total = sum(p for _, p in age_rows)
            eld = sum(p for ag, p in age_rows if ag in ELDERLY)
            if total and eld:
                lines.append(f"\n고령인구 비율(65세 이상, {maxyr}년): "
                             f"{eld/total*100:.1f}% ({eld:,}명 / 총 {total:,}명)")
                # The ratio is derived by this agent from the raw data, so it is also kept as a record
                # (otherwise a correct citation by the Writer would be judged unsupported).
                recs.append(_rec("elderly_ratio_pct", region_name,
                                 round(eld / total * 100, 1), SRC_POPULATION,
                                 year=maxyr, unit="%", entity_code=region_code))
                recs.append(_rec("elderly_population", region_name, eld, SRC_POPULATION,
                                 year=maxyr, unit="명", entity_code=region_code))
                recs.append(_rec("age_total_population", region_name, total,
                                 SRC_POPULATION, year=maxyr, unit="명",
                                 entity_code=region_code))

    # (d) health insurance coverage structure
    nhis = conn.execute("""
        SELECT reference_year, health_insured_total, medical_aid, pop_65_and_over
        FROM region_population_structure WHERE region_code=? AND reference_year<=?
        ORDER BY reference_year DESC LIMIT 1
    """, (region_code, sy)).fetchone()
    if nhis:
        y, hi, ma, e65 = nhis
        if hi:
            lines.append(f"\n건강보험 가입자({y}년): {hi:,}명")
            recs.append(_rec("health_insured", region_name, hi, SRC_POPULATION,
                             year=y, unit="명", entity_code=region_code))
        if ma:
            lines.append(f"의료급여 수급권자({y}년): {ma:,}명")
            recs.append(_rec("medical_aid", region_name, ma, SRC_POPULATION,
                             year=y, unit="명", entity_code=region_code))

    # (e) population projection (PROJECTED) — stated explicitly when not loaded
    # Restricted to the projection vintage fixed for the project.
    vint = _get_location_code(conn, project_id, "projection_vintage")
    proj = conn.execute("""
        SELECT year, population FROM region_population_timeseries
        WHERE region_code=? AND data_type='PROJECTED' AND sex='T' AND age_group='TOTAL'
          AND vintage=?
        ORDER BY year LIMIT 10
    """, (region_code, str(vint))).fetchall() if vint else []
    if proj:
        lines.append("\n장래인구 추계:")
        for y, p in proj:
            lines.append(f"  - {y}년: {p:,}명")
            recs.append(_rec("population_projected", region_name, p, SRC_POPULATION,
                             year=y, unit="명", entity_code=region_code))
    else:
        # Internal working notes are not shown to the Writer.
        lines.append("\n시·군·구 단위 장래인구추계: 보유 자료 없음")

    if len(lines) <= 2:
        return {"available": False, "content": "", "records": [],
                "note": f"자료①: {region_code} 인구 자료 없음"}
    return {"available": True, "content": "\n".join(lines), "records": recs,
            "note": f"{region_name}({region_code}) 인구"}


def load_data_2_relevance_index(conn, project_id: str) -> dict:
    """Data (2): regional relevance index RI (self-sufficiency rate). Guideline section 3-2 ra., second item."""
    region_code = _get_location_code(conn, project_id, "location_sigungu_code")
    if not region_code:
        return {"available": False, "content": "", "records": [],
                "note": "자료②: location_sigungu_code 없음"}
    region_name = _region_name(conn, region_code)

    rows = conn.execute("""
        SELECT reference_year, patient_type, value, unit FROM region_relevance_index
        WHERE region_code=? AND metric='SELF_SUFFICIENCY' AND facility_type='N/A'
          AND reference_year<=?
        ORDER BY reference_year DESC, patient_type
    """, (region_code, _survey_year(conn, project_id) or 9999)).fetchall()
    if not rows:
        return {"available": False, "content": "", "records": [],
                "note": f"자료②: {region_code} RI 자료 없음"}

    latest = max(r[0] for r in rows)
    lines = [f"[출처: {SRC_RI}]",
             f"[대상: {region_name} ({region_code})]",
             f"[지표: 자체충족률(관내 의료시설 이용 비율)]",
             f"", f"환자유형별 자체충족률({latest}년):"]
    recs: list[dict] = []
    for y, pt, val, unit in rows:
        if y == latest:
            lines.append(f"  - {pt}: {val:.2f}{unit}")
            # Patient type is folded into the type name instead of being a separate axis (ri_입원, etc.)
            recs.append(_rec(f"ri_{pt}", region_name, val, SRC_RI,
                             year=y, unit=unit, entity_code=region_code))
    return {"available": True, "content": "\n".join(lines), "records": recs,
            "note": f"{region_name}({region_code}) RI {latest}년"}


def _distance_rows(conn, project_id: str) -> list:
    """Rows of the distance table in statistical units (non-autonomous districts excluded), in ascending order of travel time."""
    rows = conn.execute(f"""
        SELECT d.region_code, m.region_name, d.straight_distance_km,
               d.road_distance_km, d.travel_time_car_min
        FROM {DIST_TABLE} d
        JOIN region_master m ON d.region_code = m.region_code
        WHERE d.project_id=?
        ORDER BY (d.travel_time_car_min IS NULL), d.travel_time_car_min ASC
    """, (project_id,)).fetchall()
    return [(rc, (rn or "").strip(), sk, rk, t) for rc, rn, sk, rk, t in rows
            if not is_ilban_gu(rc)]


def _distance_meta(conn, project_id: str) -> dict:
    row = conn.execute(f"""
        SELECT MIN(departure_time), MIN(dest_basis) FROM {DIST_TABLE}
        WHERE project_id=?""", (project_id,)).fetchone()
    return {"departure_time": row[0] if row else None,
            "dest_basis": row[1] if row else None}


def load_data_3_distance_time(conn, project_id: str) -> dict:
    """Data (3): distance and travel time. Guideline section 3-2 ra., third item.

        - Destination: the city/county/district office (the guideline names city halls,
          provincial offices, district offices, etc. as reference points).
        - Fixed departure time (directions with future traffic information).
        - Straight-line and road distances are both shown (the guideline considers
          straight-line distance together with travel time).
        - Rows for non-autonomous districts are dropped and only statistical units (the
          city) are shown, to avoid counting population twice (region_units).
        NULL values are shown as '-'.
    """
    rows = _distance_rows(conn, project_id)
    if not rows:
        return {"available": False, "content": "", "records": [], "shown_codes": [],
                "note": f"자료③: {project_id} 거리 자료 없음 ({DIST_TABLE} 미적재)"}
    meta = _distance_meta(conn, project_id)
    dep = meta.get("departure_time") or "-"
    lines = [f"[출처: {SRC_DISTANCE}]",
             f"[측정: 사업 예정지 ↔ 시·군·구 청사, 승용차, 출발 시각 {dep}]",
             f"[단위: 시·군·구(일반구는 소속 시로 통합)]",
             f"", f"이동시간 오름차순 상위 {N_DISTANCE_SHOWN}개 (직선거리 / 도로거리 / 이동시간):"]
    recs: list[dict] = []
    null_cnt = 0
    shown = rows[:N_DISTANCE_SHOWN]
    for rc, rn, sk, rk, t in shown:
        if t is None:
            null_cnt += 1
        ss = f"{sk:.1f}km" if sk is not None else "-"
        rs = f"{rk:.1f}km" if rk is not None else "-"
        ts = f"{t}분" if t is not None else "-(미측정)"
        lines.append(f"  - {rn}({rc}): 직선 {ss} / 도로 {rs} / {ts}")
        if sk is not None:
            recs.append(_rec("straight_distance_km", rn, sk, SRC_DISTANCE,
                             unit="km", entity_code=rc))
        if rk is not None:
            recs.append(_rec("distance_km", rn, rk, SRC_DISTANCE,
                             unit="km", entity_code=rc))
        if t is not None:
            recs.append(_rec("travel_time_min", rn, t, SRC_DISTANCE,
                             unit="분", entity_code=rc))
    if len(rows) > N_DISTANCE_SHOWN:
        lines.append(f"  (외 {len(rows)-N_DISTANCE_SHOWN}개 보유, 상위 {N_DISTANCE_SHOWN}개만 표시)")
        recs.append(_rec("meta_distance_rows_omitted", project_id,
                         len(rows) - N_DISTANCE_SHOWN, SRC_DISTANCE, unit="개"))
    recs.append(_rec("meta_distance_rows_shown", project_id,
                     len(shown), SRC_DISTANCE, unit="개"))
    note = f"{project_id} 거리 {len(rows)}건 중 상위 {len(shown)}"
    if null_cnt:
        note += f" (이동시간 미측정 {null_cnt}건 포함)"
    return {"available": True, "content": "\n".join(lines), "records": recs,
            "shown_codes": [r[0] for r in shown], "note": note}


def load_data_4_similar_facilities(conn, project_id: str, unit_codes: list[str]) -> dict:
    """Data (4): similar medical facilities (general hospitals and above). Guideline section 3-2 ra., fourth item.

        The range is the same cities/counties/districts as in data (3); no travel-time
        threshold is applied, since the guideline gives none. Facility data are stored by
        non-autonomous district, so for a city the facilities of its districts are gathered
        and shown under the city's name.
    """
    if not unit_codes:
        return {"available": False, "content": "", "records": [],
                "note": "자료④: 자료③ 제시 범위 없음"}
    scope_msg = f"(자료③에 제시된 시·군·구 {len(unit_codes)}개)"
    query_codes, owner = [], {}
    for u in unit_codes:
        for c in [u] + children_of(u):
            query_codes.append(c)
            owner[c] = u
    ph = ",".join("?" * len(query_codes))
    sy = _survey_year(conn, project_id)
    facilities = conn.execute(f"""
        SELECT f.region_code, f.facility_type, f.facility_name,
               f.total_beds, f.physician_count, f.establishment_date
        FROM health_facility_inventory f
        WHERE f.region_code IN ({ph})
        ORDER BY f.facility_type DESC, f.facility_name
    """, query_codes).fetchall()
    # Facilities opened after the upper bound on data years (survey year - 1) are excluded
    # (those with an unknown opening date are kept). Facilities closed after the survey are
    # not in the data (registry as of 2025.12) and cannot be restored.
    n_after = 0
    if sy:
        kept = []
        for f in facilities:
            est = str(f[5])[:4] if f[5] else ""
            if est.isdigit() and int(est) > sy:
                n_after += 1
                continue
            kept.append(f)
        facilities = kept

    if not facilities:
        return {"available": True,
                "content": (f"[출처: {SRC_FACILITY}]\n[기준: {FACILITY_REF}]\n"
                            f"[범위: {scope_msg}]\n\n해당 범위 내 종합병원급 없음."),
                "records": [_rec("meta_facility_count", project_id, 0,
                                 SRC_FACILITY, unit="건")],
                "note": f"범위 {len(unit_codes)}개 — 종합병원급 0건"}

    by_unit: dict[str, list] = {}
    for rc, ft, fn, beds, phys, est in facilities:
        by_unit.setdefault(owner[rc], []).append((ft, fn, beds, phys, est))

    lines = [f"[출처: {SRC_FACILITY}]",
             f"[기준: {FACILITY_REF}]",
             f"[범위: {scope_msg}]",
             f"[필터: 상급종합 / 종합병원"
             + (f", {sy}년 이전 개설" if sy else "") + "]"]
    recs: list[dict] = [
        _rec("meta_nearby_region_count", project_id, len(unit_codes),
             SRC_FACILITY, unit="개"),
        _rec("meta_facility_count", project_id, len(facilities),
             SRC_FACILITY, unit="건"),
    ]
    if all(f[3] is None for f in facilities):
        lines.append("[주의: 병상 수 자료 전체 미보유 — 시설별 규모 비교 불가, "
                     "시설명·종별·의사 수만 제시]")
    lines += ["", f"범위 내 종합병원급 시설({len(facilities)}건):"]
    for u in unit_codes:  # same order as data (3) (by travel time)
        items = by_unit.get(u)
        if not items:
            continue
        lines.append(f"\n  ▶ {region_name(u)}({u}):")
        for ft, fn, beds, phys, est in items:
            beds_s = f"{beds}병상" if beds else "병상 정보 없음"
            phys_s = f", 의사 {phys}명" if phys else ""
            est_s = f", 개설 {str(est)[:4]}" if est else ""
            lines.append(f"    - [{ft}] {fn} ({beds_s}{phys_s}{est_s})")
            if beds:
                recs.append(_rec("facility_beds", fn, beds, SRC_FACILITY,
                                 unit="병상", entity_code=u))
            if phys:
                recs.append(_rec("facility_physicians", fn, phys, SRC_FACILITY,
                                 unit="명", entity_code=u))
            if est:
                recs.append(_rec("facility_established_year", fn, str(est)[:4],
                                 SRC_FACILITY, entity_code=u))
    return {"available": True, "content": "\n".join(lines), "records": recs,
            "note": f"범위 {len(unit_codes)} → 시설 {len(facilities)}건"
                    + (f" (조사 연도 이후 개설 {n_after}건 제외)" if n_after else "")}


def build_catchment_data(conn, project_id: str) -> dict:
    """Assemble the block of the four kinds of data for section 3-2.

        Returns: {"block": str, "provided": [...], "omitted": {tag: note},
                  "records": [...]}
          block: data block injected into the Writer prompt
          provided: tags of the data provided
          omitted: tags of the data not held, with reasons
          records: source records for every figure shown in block
    """
    d3 = load_data_3_distance_time(conn, project_id)
    specs = [
        ("①인구",        load_data_1_population(conn, project_id),        "인구 자료"),
        ("②지역친화도",  load_data_2_relevance_index(conn, project_id),   "지역친화도(RI)"),
        ("③거리·이동시간", d3,                                             "거리·이동시간"),
        ("④유사의료시설", load_data_4_similar_facilities(
                             conn, project_id, d3.get("shown_codes", [])), "유사 의료시설"),
    ]
    lines = ["▪ 진료권 분석 자료 (KDI 지침 §3-2 라.에서 요구되는 자료):"]
    provided, omitted, records = [], {}, []
    for tag, data, label in specs:
        if data["available"]:
            lines.append(f"\n  [{tag}] {label}")
            lines.append(data["content"])
            provided.append(tag)
            for r in data.get("records", []):
                records.append({**r, "data_tag": tag})
        else:
            lines.append(f"\n  [{tag}] {label}: 보유 안 함 (사유: {data['note']})")
            omitted[tag] = data["note"]
    return {"block": "\n".join(lines), "provided": provided,
            "omitted": omitted, "records": records}


# ─────────────────────────────────────────────────────────────────────────────
# project_config -> natural-language context
# ─────────────────────────────────────────────────────────────────────────────

def _build_project_context(project_id: str, conn) -> str:
    rows = conn.execute(
        "SELECT key, value FROM project_config WHERE project_id=?", (project_id,)
    ).fetchall()
    cfg = {k: v for k, v in rows}
    lines = [
        f"[출처: {SRC_PLAN}]",
        f"사업명: {cfg.get('project_name', '')}",
        f"위치: {cfg.get('location_address', '')}",
        f"사업수행 주체: {cfg.get('project_executor', '')} / 운영 주체: {cfg.get('project_operator', '')}",
        f"총 병상: {cfg.get('total_beds', '')}병상 [{cfg.get('bed_breakdown', '')}]",
        f"연면적: {cfg.get('total_floor_area_m2', '')}㎡ / 부지면적: {cfg.get('site_area_m2', '')}㎡",
        f"개원 예정: {cfg.get('target_year', '')}년 / 분석 기간: {cfg.get('analysis_period_range', '')} ({cfg.get('analysis_period_years', '')}년)",
        f"공사 기간: {cfg.get('construction_start', '')}~{cfg.get('construction_end', '')}년",
        f"총 사업비: {cfg.get('total_project_cost', '')}",
        f"사업 목적: {cfg.get('project_purpose', '')}",
        f"사업 배경: {cfg.get('project_background', '')}",
    ]
    return "\n".join(line for line in lines if not line.endswith(": "))


# ─────────────────────────────────────────────────────────────────────────────
# Gate for the special-purpose clause
# If the chunks retrieved for section 3-2 include the special-purpose chunk, the language
# model rules once (called again on a format error) on whether the project falls under the
# guideline's definition. If it does not, the chunk is removed from the Writer's context and
# the Writer is told only the ruling and which clause does not apply.
# How the catchment is set (Types 1-3, including whether the facility is top-tier) is left
# to the Writer.
# ─────────────────────────────────────────────────────────────────────────────

_SPECIAL_PURPOSE_CHUNK_ID = "202212-III-2-특수목적"

_GATE_SYSTEM = (
    "당신은 KDI 예비타당성조사 지침의 적용 조건을 점검하는 판정자다. "
    "주어진 '지침 정의'에 비추어, '사업 정보'의 의료시설이 그 정의가 가리키는 "
    "대상에 실제로 해당하는지만 판정한다. 정의에 명시된 대상에 부합할 때만 '해당'으로 본다. "
    "사업 목적이 공익적이거나 일반적 의미에서 '특수'하다는 점만으로 확대 적용하지 않는다. "
    "추측하지 말고 지침 정의와 사업 정보에 근거해 판단한다."
)

SPECIAL_PURPOSE_NOT_APPLICABLE = (
    "[지침 적용 판정 — 특수목적 의료시설 해당 여부] 본 사업의 의료시설은 지침이 정의한 "
    "특수목적 의료시설(군인·경찰·소방관 등 특수 공공인력, 보훈대상자 및 그 가족을 "
    "대상으로 하는 의료시설)에 해당하지 않는 것으로 판정됨. 따라서 특수목적 의료시설에 "
    "관한 지침 조항과 KDI 세부지침 <표 Ⅲ-1>의 '특수한 목적을 갖는 경우' 기준은 본 사업에 "
    "적용되지 않음."
)


def _gate_validate(obj):
    if not isinstance(obj.get("applies"), bool):
        return "applies 가 true/false 가 아님"
    return None


def special_purpose_gate(chunk_text: str, project_context: str) -> dict:
    """Returns: {"applies": bool, "rationale": str, "decided": bool, "log": {...}}
        If the ruling fails (retries used up), the chunk is kept (applies=True, decided=False).
    """
    user = (
        f"## 지침 정의 (특수목적 의료시설)\n{chunk_text}\n\n"
        f"## 사업 정보\n{project_context}\n\n"
        "이 사업의 의료시설이 위 지침 정의의 '특수목적 의료시설'에 해당하는가?\n"
        "다음 JSON 으로만 답하라: "
        "{\"applies\": true 또는 false, \"rationale\": \"한 문장 근거\"}"
    )
    obj, log = call_json(user, system=_GATE_SYSTEM, validate=_gate_validate)
    if obj is None:
        return {"applies": True, "decided": False,
                "rationale": "판정 실패 — 청크 유지(보수적)", "log": log}
    return {"applies": obj["applies"], "decided": True,
            "rationale": str(obj.get("rationale", "")).strip(), "log": log}


def _confirmed_catchment_block(state: GraphState) -> str:
    """Block that spells out the catchment fixed in section 3-2 as place names (for subsections from 3-3 on)."""
    cr = state.get("catchment_result") or {}
    lines = []
    for label, body in cr.items():
        codes = (body or {}).get("regions") or []
        if not codes:
            continue
        names = ["전국" if c == "00000" else region_name(c) for c in codes]
        lines.append(f"- {label}: {', '.join(names)} (총 {len(codes)}개 단위)")
    if not lines:
        return ""
    return "[출처: 진료권 설정 결과]\n" + "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────

def memory_agent(state: GraphState) -> GraphState:
    section_id = state["section_id"]
    project_id = state["project_id"]
    conn = sqlite3.connect(DB_PATH)
    try:
        project_context = _build_project_context(project_id, conn)
        section_facts = {"project_context": project_context}

        gated_chunks = None
        if section_id.startswith("3-2"):
            catchment = build_catchment_data(conn, project_id)
            section_facts["catchment_data"] = catchment["block"]
            section_facts["catchment_records"] = catchment["records"]
            section_facts["catchment_omitted"] = catchment["omitted"]
            print(f"[Memory] §3-2 자료 주입 — 제공: {catchment['provided']} "
                  f"· 원천 레코드 {len(catchment['records'])}건")
            if catchment["omitted"]:
                print(f"[Memory] §3-2 자료 미보유: {list(catchment['omitted'].keys())}")

            chunks = state.get("guideline_chunks", [])
            sp = next((c for c in chunks
                       if c.get("chunk_id") == _SPECIAL_PURPOSE_CHUNK_ID), None)
            if sp is not None:
                gate = special_purpose_gate(sp["text"], project_context)
                section_facts["special_purpose_gate"] = gate
                if gate["applies"]:
                    print(f"[Memory] 특수목적 적용-조건: 해당 → 청크 유지 · {gate['rationale']}"
                          f" (시도 {gate['log']['attempts']})")
                else:
                    gated_chunks = [c for c in chunks
                                    if c.get("chunk_id") != _SPECIAL_PURPOSE_CHUNK_ID]
                    section_facts["special_purpose_ruling"] = SPECIAL_PURPOSE_NOT_APPLICABLE
                    print(f"[Memory] 특수목적 적용-조건: 미해당 → 청크 비공개 · "
                          f"{gate['rationale']} (시도 {gate['log']['attempts']})")
        else:
            block = _confirmed_catchment_block(state)
            if block and section_id != "3-1":
                section_facts["confirmed_catchment"] = block

        # Earlier subsections (same parent section, only those generated earlier in this run)
        parts = section_id.split("-")
        confirmed_facts = {}
        if len(parts) >= 3:
            parent_prefix = "-".join(parts[:2])
            for sid, val in (state.get("prior_sections") or {}).items():
                if sid != section_id and sid.startswith(parent_prefix + "-"):
                    confirmed_facts[sid] = val
            if confirmed_facts:
                print(f"[Memory] 이전 소절(파이프라인 생성) {len(confirmed_facts)}개 로드: "
                      f"{sorted(confirmed_facts)}")
    finally:
        conn.close()

    result = {**state, "section_facts": section_facts,
              "confirmed_facts": confirmed_facts}
    if gated_chunks is not None:
        result["guideline_chunks"] = gated_chunks
    return result
