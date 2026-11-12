# agents/sub/catchment_resolver.py — converts the catchment stated in section 3-2 into region codes
#
# ─────────────────────────────────────────────────────────────────────────────
# Role: from the catchment that the Writer derived in section 3-2 by reading the guideline,
# extract the list of region codes that the following subsections use in calculation, and
# read the level of care of the new facility -> state["facility_level"].
#
# Design principles:
#   - The meaning of the catchment is the responsibility of the Writer in section 3-2, on the
#     basis of the guideline it cites. This node only structures that conclusion; no type
#     branching or conclusion is coded here.
#   - The Writer ends section 3-2 with a block in a fixed form ('[최종 진료권]' followed by
#     lines '- <category>: <place>, <place> …'). The block is parsed by rule; the LLM is
#     used only when the block departs from the form. The LLM identifies place names only
#     and never produces region codes; names are converted by matching against region_master.
#   - Matching pool = the cities/counties/districts in the project's distance data, in
#     statistical units, plus the nation (NATION '00000'). The guideline sets catchments by
#     administrative units that are close in distance and travel time, so the units in the
#     distance data are the candidate population. A non-autonomous district named by the
#     Writer is replaced by its city, and this is recorded.
#   - Homonymous districts (Buk-gu, Jung-gu, Nam-gu, Dong-gu, Seo-gu, Gangseo-gu,
#     Goseong-gun) are disambiguated by province: the province given as the category name
#     if there is one, otherwise the province of the project site.
#   - Expressions such as '○○ 전역/전체/일원' are reduced to the province or city name, a
#     province is expanded to its member units within the pool, and variants meaning the
#     whole country are mapped to the NATION code.
#   - Names that cannot be matched are recorded in unmatched.
#
# Input:  draft_text (the catchment statement of section 3-2), project_id
# Output: {scheme_label: {"regions":[codes], "names":[...], "unmatched":[...]}}
#   scheme_label is the category name written by the Writer, e.g. a local general catchment
#   and a special-purpose (nationwide) catchment. If the Writer does not derive a dual
#   catchment, a single scheme is returned.
# ─────────────────────────────────────────────────────────────────────────────

import re, sqlite3

from state import GraphState
from paths import DB_PATH, DIST_TABLE
from agents.sub.llm import call_json
from agents.sub.region_units import is_ilban_gu, to_stat_unit

NATION_CODE = "00000"  # the NATION record in region_master (the whole country)

# ─────────────────────────────────────────────────────────────────────────────
# Variants meaning the whole country
# ─────────────────────────────────────────────────────────────────────────────
# The Writer may write the nationwide catchment as '전국', '대한민국', '국내', and so on.
# All are mapped to the same code so that a dual catchment does not fail on notation alone.
#
# This gathers synonyms of a place name under one code; it is not a rule that decides which
# project has a nationwide catchment. Whether the catchment is nationwide is still judged by
# the Writer from the guideline chunks.
#
# Compared by exact match after removing spaces, parentheses, and middle dots. Partial
# matching would also catch expressions such as '전국 지방의료원 평균'.
_NATION_ALIASES = {
    "전국", "대한민국", "한국", "우리나라", "국내",
    "전국단위", "전국일원", "전국전역", "전국권", "전국범위",
    "대한민국전역", "대한민국전국", "국내전역",
    "전국대한민국", "대한민국전체", "전국전체",
}


def _normalize_place(name: str) -> str:
    """Normalization for comparing place names — removes spaces, parentheses, and middle dots."""
    return re.sub(r"[\s()（）·・]", "", name or "")


def is_nation_name(name: str) -> bool:
    """Whether the expression means the whole country."""
    return _normalize_place(name) in _NATION_ALIASES


def _get_conn():
    return sqlite3.connect(DB_PATH)


# ─────────────────────────────────────────────────────────────────────────────
# Matching pool — cities/counties/districts in the distance data + the nation
# ─────────────────────────────────────────────────────────────────────────────

def _build_candidate_pool(conn, project_id: str) -> list[dict]:
    """Whitelist for matching names to codes (not shown to the LLM).

        Purpose: a reference table for checking afterwards that a place name is a real unit and
                 for converting it to a code. It is not a list of candidates offered to the
                 LLM, which extracts place names only from the Writer's own statement.
        Contents: the administrative units in the distance data + the nation (NATION).
                 Each entry: {code, short_name, region_name, travel_min}.
    """
    rows = conn.execute(f"""
        SELECT d.region_code, m.short_name, m.region_name, d.travel_time_car_min
        FROM {DIST_TABLE} d
        JOIN region_master m ON d.region_code = m.region_code
        WHERE d.project_id=?
        ORDER BY (d.travel_time_car_min IS NULL), d.travel_time_car_min
    """, (project_id,)).fetchall()

    # Statistical units only (non-autonomous districts excluded). Trailing spaces in names are removed.
    pool = [{"code": rc, "short_name": (sn or "").strip(),
             "region_name": (rn or "").strip(), "travel_min": t}
            for rc, sn, rn, t in rows if not is_ilban_gu(rc)]

    # Add the nationwide record (NATION)
    nat = conn.execute(
        "SELECT region_code, short_name, region_name FROM region_master WHERE region_code=?",
        (NATION_CODE,)
    ).fetchone()
    if nat:
        pool.append({"code": nat[0], "short_name": nat[1],
                     "region_name": nat[2], "travel_min": None})
    return pool


# ─────────────────────────────────────────────────────────────────────────────
# Place-name extraction by the LLM
# ─────────────────────────────────────────────────────────────────────────────

def _extract_region_names(draft_text: str) -> dict:
    """Extract the catchment place names from the section 3-2 statement with the LLM.

        The LLM identifies only the place names set as the catchment in the statement the
        Writer has already written. No list of candidates is shown to it, since that would
        narrow the range of answers in advance. Whether an extracted name is a real unit is
        checked afterwards by _match_to_codes against the pool. The LLM does not produce
        region codes.

        Returns: {scheme_label: [region_name_str, ...]}  (LLM output, not codes)
    """
    prompt = f"""
다음은 KDI 예비타당성조사 보고서 제3장 진료권 설정 소절의 서술입니다.
이 서술에서 '진료권으로 설정된(또는 설정하기로 도출된) 지역'을 식별하십시오.

규칙:
1. 서술에서 진료권에 포함된다고 결론 내린 지역만 식별. '제외한다'·'포함하지
   않는다'고 한 지역은 제외.
2. 진료권이 둘 이상으로 구분되면(예: 지역주민 대상 일반진료 진료권 / 특수목적·
   전국 진료권) 각각 별도 그룹으로 나눌 것.
3. 지역명은 서술에 나온 표기 그대로 사용(예: "아산시", "충청남도 천안시 동남구").
   서술에 없는 지역을 임의로 추가하지 말 것.
4. '전국'으로 설정된 진료권이면 지역명에 "전국"을 사용.

## 진료권 서술
{draft_text}

JSON으로만 답하시오. 다른 텍스트 없이.
형식: {{"schemes": [{{"label": "<진료권 구분 명칭>", "regions": ["<지역명>", ...]}}, ...]}}
진료권이 하나면 schemes 배열에 항목 하나만.
""".strip()

    data, log = call_json(
        prompt, validate=lambda o: None if isinstance(o.get("schemes"), list)
        else "schemes 없음")
    _extract_region_names.last_log = log
    if data is None:
        print(f"[CatchmentResolver] LLM 추출 실패(시도 {log['attempts']}): {log['errors']}")
        return {}

    out = {}
    for i, sch in enumerate(data.get("schemes", [])):
        label = sch.get("label") or f"scheme_{i+1}"
        names = [n for n in sch.get("regions", []) if isinstance(n, str)]
        if names:
            out[label] = names
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Rule-based parsing of the [최종 진료권] block; extraction of the level of care
# The Writer writes its conclusion in the fixed form ('[최종 진료권]' followed by lines
# '- <category>: <place>, <place>…'). A block in that form is read by rule; the LLM is used
# only when the form is broken.
# ─────────────────────────────────────────────────────────────────────────────

_BLOCK_HEAD = re.compile(r"^\s*[>#*\-]*\s*\[\s*최종\s*진료권\s*\]\s*$")
_LEVEL_HEAD = re.compile(r"^\s*[>#*\-]*\s*\[\s*신규\s*시설\s*진료\s*수준\s*\]")


def parse_final_block_rule(draft_text: str) -> dict:
    """Returns: {label: [names]} ({} if the block does not follow the form)."""
    lines = draft_text.splitlines()
    start = next((i for i, ln in enumerate(lines) if _BLOCK_HEAD.match(ln)), None)
    if start is None:
        return {}
    out = {}
    for ln in lines[start + 1:]:
        st = ln.strip()
        if not st:
            continue
        if st.startswith("["):  # the next block ([신규 시설 진료 수준], etc.)
            break
        m = re.match(r"^[-•·*]\s*([^:：]+)[:：]\s*(.+)$", st)
        if m:
            label, body = m.group(1).strip(), m.group(2)
        else:
            # A line without a category name is read as a single catchment.
            m2 = re.match(r"^[-•·*]\s*(.+)$", st)
            if not m2:
                return {}  # no bullet either: not in the form -> LLM fallback
            label, body = "최종 진료권", m2.group(1)
        body = re.sub(r"「[^」]*」", "", body)  # remove any evidence marker attached
        body = re.sub(r"\((이하|외|총)[^)]*\)", "", body)
        body = re.sub(r"\(\s*\d{5}\s*\)", "", body)  # remove a code given in parentheses, e.g. '(41390)'
        names = [n.strip(" .") for n in re.split(r"[,，、]", body) if n.strip(" .")]
        if names:
            out.setdefault(label, []).extend(names)
    return out


def parse_facility_level(draft_text: str) -> str | None:
    """Read '상급종합병원급' / '종합병원급' from the [신규 시설 진료 수준] block."""
    lines = draft_text.splitlines()
    i = next((k for k, ln in enumerate(lines) if _LEVEL_HEAD.match(ln)), None)
    if i is None:
        return None
    tail = " ".join(lines[i:i + 3])
    if "상급종합병원급" in tail:
        return "상급종합병원급"
    if "종합병원급" in tail:
        return "종합병원급"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Name -> region_code conversion (matching only within the pool)
# ─────────────────────────────────────────────────────────────────────────────

def _build_sido_lookup(conn) -> dict[str, str]:
    """{province name: two-digit province code prefix}. Expansion happens only within the pool.

        The Writer sometimes states the catchment at province level (e.g. '[최종 진료권]
        - 일반진료권: 울산광역시'). The guideline defines the unit of a catchment as the
        city/county/district (or a smaller unit), so expanding a province name into its member
        units normalizes the statement to the unit the guideline prescribes. Expansion is
        limited to the pool, so no unit outside the distance data can appear.
    """
    out = {}
    for code, rname, sname in conn.execute(
            "SELECT region_code, region_name, short_name FROM region_master "
            "WHERE region_code LIKE '__000' AND region_code<>?", (NATION_CODE,)):
        for nm in (rname, sname):
            if nm:
                out[str(nm).strip()] = str(code)[:2]
    return out


def _build_all_names(conn) -> dict[str, list[str]]:
    """All names in region_master -> code (used to replace a non-autonomous district name with its city)."""
    out: dict[str, list[str]] = {}
    for code, rname, sname in conn.execute(
            "SELECT region_code, region_name, short_name FROM region_master "
            "WHERE valid_to IS NULL"):
        for nm in (rname, sname):
            if nm:
                out.setdefault(str(nm).strip(), []).append(str(code))
    return out


_WHOLE_SUFFIX = re.compile(r"\s*(전역|전체|일원|전 지역|전지역)$")


def _match_to_codes(names: list[str], pool: list[dict],
                    sido_map: dict[str, str] | None = None,
                    all_names: dict[str, list[str]] | None = None,
                    label: str | None = None,
                    home_prefix: str | None = None) -> dict:
    """Convert the place names to region codes within the pool.

        Matching rules:
          1) exact match on region_name first  (e.g. "충청남도 천안시 동남구")
          2) match on short_name: accepted if the pool has one such name; ambiguous if two or
             more -> unmatched
          3) not found anywhere -> unmatched
        No code outside the pool can be produced.
    """
    by_region_name = {}
    by_short = {}
    for c in pool:
        by_region_name.setdefault(c["region_name"], []).append(c["code"])
        by_short.setdefault(c["short_name"], []).append(c["code"])
    has_nation = any(c["code"] == NATION_CODE for c in pool)

    regions, matched_names, unmatched, expanded = [], [], [], []
    normalized = []
    # Order for disambiguating homonyms: the province given as the category name, then the province of the project site
    label_prefix = (sido_map or {}).get((label or "").strip())
    for nm in names:
        nm_s = nm.strip()
        # '울산광역시 전역' -> '울산광역시'
        base = _WHOLE_SUFFIX.sub("", nm_s).strip()
        if base and base != nm_s:
            normalized.append(f"{nm_s}→{base}")
            nm_s = base
        codes = None
        if nm_s in by_region_name and len(by_region_name[nm_s]) == 1:
            codes = [by_region_name[nm_s][0]]
        elif nm_s in by_short and len(by_short[nm_s]) == 1:
            codes = [by_short[nm_s][0]]
        elif nm_s in by_short and len(by_short[nm_s]) > 1:
            for pre, why in ((label_prefix, "구분명 시도"), (home_prefix, "사업 소재 시도")):
                if not pre:
                    continue
                hit = [c for c in by_short[nm_s] if c.startswith(pre)]
                if len(hit) == 1:
                    codes = hit
                    normalized.append(f"{nm_s}→{hit[0]}(동명 가림: {why})")
                    break
        elif has_nation and is_nation_name(nm_s):
            # Variants such as '대한민국' and '국내' are resolved to the NATION code.
            codes = [NATION_CODE]
            if _normalize_place(nm_s) != "전국":
                normalized.append(f"{nm_s}→전국")
        elif all_names and nm_s in all_names and len(all_names[nm_s]) == 1 \
                and to_stat_unit(all_names[nm_s][0]) in {c["code"] for c in pool}:
            # A non-autonomous district name (e.g. '경기도 안양시 만안구') -> the statistical unit of its city.
            # The pool has no non-autonomous districts, so such names are caught only here. The change is recorded.
            u = to_stat_unit(all_names[nm_s][0])
            codes = [u]
            normalized.append(f"{nm_s}→{u}(통계 단위)")
        elif sido_map and nm_s in sido_map:
            # A catchment stated at province level -> expanded to the member units within the pool
            pre = sido_map[nm_s]
            codes = [c["code"] for c in pool
                     if c["code"].startswith(pre) and c["code"] != NATION_CODE]
            if codes:
                expanded.append(f"{nm_s}→{len(codes)}개 시군구")
            else:
                codes = None
        if not codes:
            unmatched.append(nm_s)
            continue
        for code in codes:
            if code not in regions:
                regions.append(code)
        matched_names.append(nm_s)

    return {"regions": regions, "names": matched_names,
            "unmatched": unmatched, "expanded": expanded,
            "normalized": normalized}


# ─────────────────────────────────────────────────────────────────────────────
# Node body
# ─────────────────────────────────────────────────────────────────────────────

def _isolate_final_catchment_block(draft_text: str):
    """Return only the '[최종 진료권]' block at the end of the section 3-2 statement.

        Place names are extracted only from the conclusion the Writer declares, so that places
        mentioned in the body (distance, similar facilities, etc.) are not mixed in.
        Returns None if there is no such block; the caller then falls back to the whole text.
    """
    # From the first '최종 진료권' marker (bracket, markdown, and colon variants allowed) to the end
    m = re.search(r'(?m)^[\s>#*\-]*\[?\s*최종\s*진료권\s*[\]:]?', draft_text)
    if not m:
        return None
    block = draft_text[m.start():].strip()
    return block or None


def resolve_catchment(draft_text: str, project_id: str) -> dict:
    """Catchment statement -> {scheme_label: {regions, names, unmatched}}.

        A pure function. The graph node wrapper is catchment_resolver_node.
    """
    conn = _get_conn()
    try:
        pool = _build_candidate_pool(conn, project_id)
    finally:
        conn.close()

    if not pool:
        print(f"[CatchmentResolver] {project_id} 후보 풀 없음(거리 자료 부재)")
        return {}

    final_block = _isolate_final_catchment_block(draft_text)
    if final_block is not None:
        source_text = final_block
    else:
        source_text = draft_text
        print("[CatchmentResolver] '[최종 진료권]' 선언 블록 없음 — 본문 전체에서 추출(폴백)")

    conn = _get_conn()
    try:
        sido_map = _build_sido_lookup(conn)
        all_names = _build_all_names(conn)
    finally:
        conn.close()

    name_groups = parse_final_block_rule(draft_text)
    method, llm_log = "rule", None
    if not name_groups:
        method = "llm"
        name_groups = _extract_region_names(source_text)
        llm_log = getattr(_extract_region_names, "last_log", None)
        print("[CatchmentResolver] 블록 규칙 파싱 불가 → LLM 보조 추출")
    # Project site = the unit with the shortest travel time in the distance data (the site itself)
    home = next((c for c in pool if c["code"] != NATION_CODE), None)
    home_prefix = home["code"][:2] if home else None
    result = {}
    for label, names in name_groups.items():
        result[label] = _match_to_codes(names, pool, sido_map, all_names,
                                        label=label, home_prefix=home_prefix)
    resolve_catchment.last_parse = {"method": method, "llm_log": llm_log,
                                    "raw_groups": name_groups}

    # Log
    for label, r in result.items():
        extra = f" · 시도전개 {r['expanded']}" if r.get("expanded") else ""
        # Record how notation variants were resolved to codes, so that what was interpreted and how
        # remains in the output.
        if r.get("normalized"):
            extra += f" · 표기정규화 {r['normalized']}"
        print(f"[CatchmentResolver] [{label}] 코드 {r['regions']} "
              f"(매칭 {len(r['names'])} / 미매칭 {r['unmatched']}){extra}")
        # A failure to resolve is not passed over silently; it is recorded, because Computation
        # would otherwise fall back to the project site and the result would look like a normal
        # catchment output.
        if not r["regions"]:
            print(f"[CatchmentResolver] [!] [{label}] 진료권 해소 실패 — "
                  f"지역 코드 0개. 이후 소절은 사업 소재지로 되돌아갑니다. "
                  f"미매칭: {r['unmatched']}")
    return result


def catchment_resolver_node(state: GraphState) -> GraphState:
    """Graph node: acts only in the catchment-setting subsection (3-2).

        Takes the draft of section 3-2, extracts the catchment codes, and stores them in
        state["catchment_result"] for Computation in later subsections.
        Other subsections pass through unchanged.
    """
    section_id = state["section_id"]
    # Extraction is done only in the subsection that concludes the catchment setting.
    if not section_id.startswith("3-2"):
        return state

    draft = state.get("draft_text", "")
    if not draft.strip():
        return state

    result = resolve_catchment(draft, state["project_id"])
    level = parse_facility_level(draft)
    level_info = ({"level": level, "source": "§3-2 [신규 시설 진료 수준]"} if level
                  else {"level": None, "source": "§3-2 표기 없음"})
    print(f"[CatchmentResolver] 신규 시설 진료 수준: {level or '(표기 없음)'}")
    parse = getattr(resolve_catchment, "last_parse", {}) or {}
    new = {**state, "facility_level": level_info, "catchment_parse": parse}
    if result:
        new["catchment_result"] = result
    return new