# load_survey5.py — loads the city/county/district tables of the 5th National Health Care Survey
# ─────────────────────────────────────────────────────────────────────────────
# What is loaded (four sheets of the city/county/district volume):
#   Ⅰ.B.1.     licensed beds            -> survey5_beds
#   Ⅱ.A.1. b)  total inpatient days     -> survey5_inpatient_days
#   Ⅱ.D.1.     emergency patients       -> survey5_emergency
#   Ⅰ.C.4. a)  physicians               -> survey5_physicians
#
# Why:
#   Bed utilization = total inpatient days / (licensed beds x days in the year). With both
#   series held by city/county/district, the numerator and denominator can be summed for
#   any combination of units, which gives the 'average bed utilization of the catchment'
#   named in section 3-4-1 of the guideline directly.
#   The published bed utilization by city/county/district is not loaded as it is, because a
#   unit without a facility of the type has the value 0.0, which cannot be told apart from
#   a utilization of 0%. Summing the source series makes such a unit contribute nothing to
#   the denominator.
#
# Safeguards:
#   - The default is a check-only mode. Without --commit the database is not touched.
#   - Rows whose region name cannot be mapped to a region_code are not loaded and are listed.
#   - The schema of region_master is read from sqlite_master, not assumed.
#
# Usage:
#   python load_survey5.py                 # check only (mapping rate, unmatched rows)
#   python load_survey5.py --commit        # load
# ─────────────────────────────────────────────────────────────────────────────

import argparse
import sqlite3
from collections import defaultdict
from pathlib import Path

import openpyxl

XLSX_DEFAULT = str(Path(__file__).resolve().parent.parent / "data" / "shared" / "stats" / "hospitals"
                   / "제5차_국민보건의료실태조사" / "2.+국민보건의료실태통계_(시군구편)_1209(수정).xlsx")
DB_DEFAULT = str(Path(__file__).resolve().parent.parent / "data" / "project_data.db")
SOURCE = "보건복지부 제5차 국민보건의료실태조사(2016-2020) 시군구편"

# Two-digit province prefix of the administrative code.
# Gangwon and Jeonbuk changed prefix when they became special self-governing provinces:
# Gangwon (June 2023) 42 -> 51; Jeonbuk (January 2024) 45 -> 52.
SIDO_PREFIX = {
    "서울": "11", "부산": "26", "대구": "27", "인천": "28", "광주": "29",
    "대전": "30", "울산": "31", "세종": "36", "경기": "41", "강원": "51",
    "충북": "43", "충남": "44", "전북": "52", "전남": "46", "경북": "47",
    "경남": "48", "제주": "50",
}

# Handling of a name that has more than one region_code.
# If left empty, the region is not loaded and is listed (no arbitrary choice). Use the
# output of --diagnose-ambiguous to see which code is in use in the other tables, then enter
# that code here.
AMBIGUOUS_RESOLUTION: dict[str, str] = {
    "경남 창원시": "48120",
    "세종 세종시": "36110",
}

# (sheet, table, value-column definition)
# cols: [(year, column number), ...]  / extra: sub-category when there is more than one value
SPECS = [
    {"sheet": "Ⅰ.B.1.", "table": "survey5_beds", "value": "licensed_beds",
     "cols": {2016: 4, 2017: 5, 2018: 6, 2019: 7, 2020: 8}, "sub": None},
    {"sheet": "Ⅱ.A.1. b)", "table": "survey5_inpatient_days", "value": "total_days",
     "cols": {2016: 4, 2017: 12, 2018: 20, 2019: 28, 2020: 36}, "sub": None},
    {"sheet": "Ⅱ.D.1.", "table": "survey5_emergency", "value": "patients",
     "cols": {2016: 4, 2017: 7, 2018: 10, 2019: 13, 2020: 16},
     "sub": {"전체": 0, "질병": 1, "손상중독": 2}},
    {"sheet": "Ⅰ.C.4. a)", "table": "survey5_physicians", "value": "physicians",
     "cols": {2016: 4, 2017: 5, 2018: 6, 2019: 7, 2020: 8}, "sub": None},
]


def parse_sheet(wb, spec):
    """sheet -> [(province, city/county/district, type, size, year, category, value), ...]"""
    ws = wb[spec["sheet"]]
    out = []
    sido = sgg = ftype = ""
    for r in ws.iter_rows(min_row=5, values_only=True):
        r = list(r)
        if r[0]:
            sido = str(r[0]).strip()
            sgg = ""  # reset the city/county/district when the province changes
        if r[1]:
            sgg = str(r[1]).strip()
        if r[2]:
            ftype = str(r[2]).strip()
        fsize = str(r[3]).strip() if r[3] else ""
        for year, c0 in spec["cols"].items():
            subs = spec["sub"] or {"전체": 0}
            for label, off in subs.items():
                v = r[c0 + off] if c0 + off < len(r) else None
                if isinstance(v, (int, float)):
                    out.append((sido, sgg, ftype, fsize, year, label, v))
    return out


def _name_candidates(sido: str, sgg: str):
    """Region name in the Excel file -> candidates for matching region_master, in order of priority.

        The survey prefixes the province name to district names of metropolitan cities to
        avoid duplicates (Busan -> '부산중구'), whereas short_name in region_master is the
        plain name. Removing the prefix is wrong in some cases (e.g. '부산진구'), so the
        original name is tried first and the prefix is removed only if that fails.
    """
    cands = [sgg]
    if sgg.startswith(sido) and len(sgg) - len(sido) >= 2:
        cands.append(sgg[len(sido):])  # '부산중구' -> '중구'

    # A province that is itself one row (Sejong): '세종시' in the Excel file <-> '세종' in region_master
    if sgg.endswith("시") and sgg[:-1] == sido:
        cands.append(sido)
    return cands


def region_lookup(conn):
    """{(province prefix, city/county/district name): region_code} from region_master."""
    cols = [x[1] for x in conn.execute("PRAGMA table_info('region_master')")]
    code_col = next((c for c in cols if "code" in c.lower()), None)
    name_cols = [c for c in cols
                 if c != code_col and any(k in c.lower()
                                          for k in ("name", "nm", "명"))]
    if not code_col or not name_cols:
        raise SystemExit(f"[!] region_master 에서 코드/이름 컬럼을 찾지 못했습니다. "
                         f"컬럼: {cols}")
    print(f"[i] region_master 사용 컬럼 — 코드: {code_col} · 이름 후보: {name_cols}")
    table = defaultdict(list)
    q = f"SELECT \"{code_col}\", " + ", ".join(f'"{c}"' for c in name_cols) + \
        " FROM region_master"
    for row in conn.execute(q):
        code = str(row[0]).strip()
        for nm in row[1:]:
            if nm:
                table[(code[:2], str(nm).strip())].append(code)
    return table, code_col, name_cols


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", default=XLSX_DEFAULT)
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()

    if not Path(a.xlsx).exists():
        raise SystemExit(f"[!] 엑셀을 찾지 못했습니다: {a.xlsx}\n"
                         f"    --xlsx 로 경로를 지정하십시오.")
    conn = sqlite3.connect(a.db)
    lookup, code_col, name_cols = region_lookup(conn)

    unmatched = defaultdict(int)
    ambiguous = defaultdict(set)
    parsed = {}
    wb = openpyxl.load_workbook(a.xlsx, read_only=True)

    for spec in SPECS:
        rows = parse_sheet(wb, spec)
        keep = []
        skip_agg = 0
        for sido, sgg, ftype, fsize, year, label, val in rows:
            if not sgg or sgg == "전체":  # provincial totals and the national row are not loaded
                skip_agg += 1
                continue
            pre = SIDO_PREFIX.get(sido)
            if pre is None:
                unmatched[f"시도명 미상: {sido}"] += 1
                continue
            codes = None
            for nm in _name_candidates(sido, sgg):
                codes = lookup.get((pre, nm))
                if codes:
                    break
            if not codes:
                unmatched[f"{sido} {sgg}"] += 1
                continue
            uniq = sorted(set(codes))
            if len(uniq) > 1:
                fixed = AMBIGUOUS_RESOLUTION.get(f"{sido} {sgg}")
                if fixed in uniq:
                    keep.append((fixed, year, ftype, fsize, label, val))
                else:
                    ambiguous[f"{sido} {sgg}"] |= set(uniq)
                continue
            keep.append((uniq[0], year, ftype, fsize, label, val))
        parsed[spec["table"]] = (spec, keep)
        print(f"\n[{spec['sheet']:<12}] 파싱 {len(rows):>7,}  "
              f"집계행 제외 {skip_agg:>6,}  적재대상 {len(keep):>7,}")
    wb.close()

    if unmatched:
        print(f"\n[!] region_code 로 매핑되지 않은 지역 {len(unmatched)}종 (전량)")
        for k, n in sorted(unmatched.items()):
            print(f"     {k}  ({n:,}행)")
    if ambiguous:
        print(f"\n[!] 한 이름에 코드가 둘 이상인 지역 {len(ambiguous)}종")
        for k, v in ambiguous.items():
            print(f"     {k} → {sorted(v)}")
            # Check in the other tables which code is actually in use
            for code in sorted(v):
                used = []
                for t, c in (("region_population_timeseries", "region_code"),
                             ("region_relevance_index", "region_code"),
                             ("catchment_scheme_assignment", "region_code"),
                             ("health_facility_inventory", "region_code")):
                    try:
                        n = conn.execute(
                            f"SELECT COUNT(*) FROM {t} WHERE {c}=?",
                            (code,)).fetchone()[0]
                        if n:
                            used.append(f"{t}={n}")
                    except sqlite3.Error:
                        pass
                print(f"        {code}: " + (", ".join(used) if used
                                             else "다른 테이블에서 사용 흔적 없음"))
        print("     → 사용 중인 코드를 AMBIGUOUS_RESOLUTION 에 적고 다시 실행하십시오.")
    if not unmatched and not ambiguous:
        print("\n[OK] 모든 지역이 region_code 로 매핑되었습니다.")

    if not a.commit:
        print("\n확인 모드입니다. DB 를 변경하지 않았습니다.")
        print("매핑 결과에 문제가 없으면 --commit 을 붙여 다시 실행하십시오.")
        conn.close()
        return

    for table, (spec, keep) in parsed.items():
        conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute(f"""CREATE TABLE {table} (
            region_code TEXT, reference_year INTEGER,
            facility_type TEXT, facility_size TEXT,
            category TEXT, {spec['value']} REAL, source TEXT,
            PRIMARY KEY (region_code, reference_year, facility_type,
                         facility_size, category))""")
        conn.executemany(
            f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?,?,?,?)",
            [(c, y, ft, fs, lb, v, SOURCE) for c, y, ft, fs, lb, v in keep])
        print(f"  적재: {table}  {len(keep):,}행")
    conn.commit()

    # Self-check after loading — whether the reconstructed bed utilization matches the published value
    print("\n■ 적재 검증: 재구성 병상이용률 vs 기존 bed_utilization(전국)")
    for y in (2016, 2018, 2020):
        row = conn.execute("""
            SELECT (SELECT SUM(total_days) FROM survey5_inpatient_days
                     WHERE reference_year=? AND facility_type='종합병원'
                       AND facility_size='500병상이상' AND category='전체'),
                   (SELECT SUM(licensed_beds) FROM survey5_beds
                     WHERE reference_year=? AND facility_type='종합병원'
                       AND facility_size='500병상이상' AND category='전체')
        """, (y, y)).fetchone()
        pub = conn.execute("""SELECT utilization_rate FROM bed_utilization
            WHERE catchment_scheme='전국' AND catchment_id='전국'
              AND reference_year=? AND facility_type='종합병원'
              AND facility_size='500병상이상'""", (y,)).fetchone()
        if row[0] and row[1]:
            calc = row[0] / (row[1] * 365) * 100
            print(f"   {y}  재구성(전 시군구 합) {calc:6.2f}%  "
                  f"공표 {pub[0]:6.2f}%" if pub else
                  f"   {y}  재구성 {calc:6.2f}%  공표 —")
    conn.close()
    print("\n완료.")


if __name__ == "__main__":
    main()