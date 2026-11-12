# supplement_regions.py — adds rows missing because of administrative code changes
# ─────────────────────────────────────────────────────────────────────────────
# What it does — only adds rows that are missing because of administrative code changes or
# gaps in loading (no existing row is modified or deleted).
#   1) Sejong (36110): inpatient and outpatient visit days, 2013-2024
#      The Health Insurance Statistical Yearbook (Part 3, table by city/county/district)
#      lists them under '세종'. The values are extracted from the yearbook table with
#      extract_yearbook_district.py.
#   2) Incheon Michuhol-gu (28177): inpatient and outpatient visit days, 2013-2017
#      Nam-gu (28170) was renamed and recoded as Michuhol-gu in July 2018, and the rows up
#      to 2017 exist only under the old code. It is the same region, so the 28170 rows are
#      copied under 28177.
#   3) Daegu Gunwi-gun (27720): data up to 2022
#      Gunwi moved from Gyeongsangbuk-do (47720) to Daegu in July 2023, and the rows up to
#      2022 exist only under 47720, so they are copied.
#      Tables: inpatient and outpatient visit days, emergency deaths, supplied beds,
#      emergency beds (only tables queried by region). The survey beds and inpatient days
#      are summed over the nation and would be double counted, so they are not copied.
#   Not covered: emergency deaths (all years) and supplied beds (2016-2020) of Changwon
#   (48120), and emergency beds of Sejong, which would need the source data to be extracted
#   again. The pipeline leaves a region without data out of both the numerator and the
#   denominator and records it.
#
# Usage
#   python preprocessing/supplement_regions.py            # check only (prints the rows to be added)
#   python preprocessing/supplement_regions.py --apply    # back up the database, then add
# ─────────────────────────────────────────────────────────────────────────────
import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
from paths import DB_PATH                                            # noqa: E402

NOTE_SEJONG = " [보충: 연보 원표 '세종' 행 추출]"
NOTE_MICHU = " [보충: 남구 28170 → 미추홀구 28177 코드 이관]"
NOTE_GUNWI = " [보충: 경북 군위군 47720 → 대구 군위군 27720 코드 이관]"

# Visit days in the '-입 원' / '-외 래' rows of the '세종' block of the yearbook table
# (year: (inpatient, outpatient, sheet, row)) — as output by extract_yearbook_district.py
SEJONG = {
    2013: (348352, 2248665, "100", 69),
    2014: (391239, 2788681, "100", 69),
    2015: (502294, 3543558, "110", 69),
    2016: (575568, 4213613, "110", 69),
    2017: (606103, 4549583, "Ⅲ-2(계속3)", 69),
    2018: (675987, 5486759, "3-2(4)", 69),
    2019: (720362, 6018559, "3-2(4)", 69),
    2020: (694763, 5256536, "3-2(4)", 69),
    2021: (704706, 5700850, "3-2(4)", 69),
    2022: (702826, 6752432, "3-2(4)", 69),
    2023: (662276, 7105432, "3-2(4)", 75),
    2024: (667804, 7045594, "3-2(4)", 75),
}


def _plan(conn):
    plan = []  # (table, columns, row tuple, label)
    cols_i = ("region_code", "reference_year", "facility_type", "facility_size", "sex",
              "age_group", "metric", "value", "unit", "source")

    # 1) Sejong
    for y, (ip, op, sheet, row) in SEJONG.items():
        for metric, v, kind in (("INPATIENT_DAYS", ip, "입원"), ("OUTPATIENT_DAYS", op, "외래")):
            src = (f"국민건강보험공단 {y}년 건강보험통계연보 제3편 시·군·구별 {kind} 내원일수"
                   f"(주민등록주소지 기준){NOTE_SEJONG} 시트 {sheet} 행 {row}")
            plan.append(("inpatient_by_age_facility", cols_i,
                         ("36110", y, "의료기관전체", "N/A", "T", "TOTAL", metric, float(v), "일", src),
                         "세종 내원일수"))

    # 2) Michuhol (28170 -> 28177); 3-a) Gunwi visit days (47720 -> 27720, up to 2022)
    for old, new, ymax, note, label in (("28170", "28177", 2017, NOTE_MICHU, "미추홀 내원일수"),
                                        ("47720", "27720", 2022, NOTE_GUNWI, "군위 내원일수")):
        for r in conn.execute(
                f"SELECT {','.join(cols_i)} FROM inpatient_by_age_facility "
                "WHERE region_code=? AND reference_year<=?", (old, ymax)):
            r = list(r); r[0] = new; r[-1] = r[-1] + note
            plan.append(("inpatient_by_age_facility", cols_i, tuple(r), label))

    # 3-b) Gunwi demand_parameter (emergency deaths, supplied beds, emergency beds), up to 2022
    cols_d = ("scope", "region_code", "reference_year", "parameter", "facility_type",
              "value", "unit", "source")
    for r in conn.execute(
            f"SELECT {','.join(cols_d)} FROM demand_parameter WHERE region_code='47720' "
            "AND reference_year<=2022 AND parameter IN ('emergency_death','supply_beds','emergency_beds')"):
        r = list(r); r[1] = "27720"; r[-1] = r[-1] + NOTE_GUNWI
        plan.append(("demand_parameter", cols_d, tuple(r), f"군위 {r[3]}"))

    # 3-c) The survey beds and inpatient days (survey5_*) are not copied.
    # The national bed utilization is computed as the sum of these two tables over all units
    # (computation._bed_util_survey5, regions=None), so copying the 47720 rows to 27720 would
    # count Gunwi twice in the national total. If Gunwi is part of a catchment, it is left out
    # of the catchment's average bed utilization, and this is recorded.
    return plan


def _exists(conn, table, cols, row):
    # A row with the same primary key is skipped (values are not compared — existing rows are kept)
    pk = {
        "inpatient_by_age_facility": ("region_code", "reference_year", "facility_type",
                                      "facility_size", "sex", "age_group", "metric"),
        "demand_parameter": ("scope", "region_code", "reference_year", "parameter", "facility_type"),
    }[table]
    d = dict(zip(cols, row))
    q = f"SELECT 1 FROM {table} WHERE " + " AND ".join(f"{k}=?" for k in pk) + " LIMIT 1"
    return conn.execute(q, [d[k] for k in pk]).fetchone() is not None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    db = Path(a.db)
    conn = sqlite3.connect(db)
    plan = _plan(conn)
    todo = [p for p in plan if not _exists(conn, p[0], p[1], p[2])]
    from collections import Counter
    c_all, c_new = Counter(p[3] for p in plan), Counter(p[3] for p in todo)
    print(f"[supplement_regions] DB: {db}")
    for k in c_all:
        print(f"  {k:28s} 후보 {c_all[k]:4d} · 추가 예정 {c_new.get(k, 0):4d}")
    if not a.apply:
        print("[supplement_regions] 점검만 함(--apply 를 붙이면 백업 후 추가)")
        return
    conn.close()
    bak = db.with_name(db.name + f".backup_v4_{datetime.now():%Y%m%d_%H%M}")
    shutil.copy2(db, bak)
    print(f"[supplement_regions] 백업: {bak}")
    conn = sqlite3.connect(db)
    with conn:
        for table, cols, row, _ in todo:
            conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})", row)
    print(f"[supplement_regions] 추가 {len(todo)}행 완료")
    # Check after the run
    for code, nm in (("36110", "세종"), ("28177", "미추홀"), ("27720", "군위")):
        ys = [r[0] for r in conn.execute(
            "SELECT DISTINCT reference_year FROM inpatient_by_age_facility WHERE region_code=? "
            "AND metric='INPATIENT_DAYS' ORDER BY 1", (code,))]
        print(f"  {nm}({code}) 입원 내원일수 연도: {ys[0] if ys else '-'}~{ys[-1] if ys else '-'} ({len(ys)}개)")


if __name__ == "__main__":
    main()
