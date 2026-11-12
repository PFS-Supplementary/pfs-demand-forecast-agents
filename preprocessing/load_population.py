"""load_population.py

Converts the Ministry of the Interior and Safety resident-registration population
statistics (population by age, annual) CSV into INSERTs for the table
region_population_timeseries.

Rules:
  - The source is kept in full as a single source (raw sex x age_group).
  - Thirteen branch offices are skipped (they are already included in their city or
    district, so keeping them would double count).
  - Jeonbuk: old codes are mapped to the new codes (45*** -> 52***). After mapping, the
    primary key (region_code, year, data_type, source, sex, age_group) has no collision.
  - Empty cells are not inserted (the 2024-2025 cells of the 45*** rows are empty and
    drop out).

Modes:
  1. sql: CSV -> an SQL file of INSERTs in batches of 500 rows
  2. db:  CSV -> INSERT directly into the SQLite database

Usage:
  python load_population.py --csv <population csv> --output insert_population.sql --mode sql
  python load_population.py --csv <population csv> --db /path/to/project_data.db --mode db
"""

import argparse
import csv
import re
import sys
import sqlite3
from collections import defaultdict
from pathlib import Path


# ─────────────────────────────────────────────────────────────────
# 0. Mapping rules
# ─────────────────────────────────────────────────────────────────

# Thirteen branch offices skipped (already included in their city or district; would double count)
SKIP_CODES_5DIGIT = {
    "28114",  # Incheon Jung-gu Yeongjong branch office
    "28118",  # Incheon Jung-gu Yongyu branch office
    "28265",  # Incheon Seo-gu Geomdan branch office
    "41105",  # Bukbu branch office (Gyeonggi)
    "41222",  # Gyeonggi Songtan branch office
    "41224",  # Gyeonggi Anjung branch office
    "41362",  # Gyeonggi Pungyang branch office
    "41592",  # Gyeonggi Hwaseong Dongbu branch office
    "41594",  # Gyeonggi Hwaseong Dongtan branch office
    "48245",  # Gyeongnam Sacheon Namyang branch office
    "48252",  # Gyeongnam Jangyu branch office
    "48332",  # Gyeongnam Yangsan Ungsang branch office
    "51105",  # Gangwon Donghae branch office
}

# Jeollabuk-do (45***) -> Jeonbuk Special Self-Governing Province (52***)
# Renamed on 18 January 2024. In the CSV the 2023 data are on the 45*** rows and the
# 2024-2025 data on the 52*** rows. Mapping both to the new codes (52***) causes no primary
# key collision, since the years are separate.
JEONBUK_MAP = {
    "45000": "52000", "45110": "52110", "45111": "52111", "45113": "52113",
    "45130": "52130", "45140": "52140", "45180": "52180", "45190": "52190",
    "45210": "52210", "45710": "52710", "45720": "52720", "45730": "52730",
    "45740": "52740", "45750": "52750", "45770": "52770", "45790": "52790",
    "45800": "52800",
}

# Incheon Nam-gu 28170 -> Michuhol-gu 28177 (1 July 2018): already the new code in the CSV; no mapping needed.
# Gunwi, Gyeongbuk 47720 -> Daegu 27720 (1 July 2023): already the new code in the CSV; no mapping needed.

# Sex encoding
SEX_MAP = {"계": "T", "남": "M", "여": "F"}

# data_type: all values of this source are OBSERVED
DATA_TYPE = "OBSERVED"

# source label
SOURCE_LABEL = "행정안전부 주민등록인구통계 (연령별인구현황 연간)"

# reference_date from the CSV: headers have the form "2023년_..."
# The data refer to the end of December.
REFERENCE_DATE_FMT = "{year}-12-31"


# ─────────────────────────────────────────────────────────────────
# 1. Header parsing
# ─────────────────────────────────────────────────────────────────

def parse_header_column(col_name):
    """Header column name -> (year, sex, age_group).
        e.g. "2023년_계_총인구수" -> (2023, "T", "TOTAL")
             "2024년_남_0~4세" -> (2024, "M", "0~4세")
             "2025년_여_연령구간인구수" -> (2025, "F", "AGE_RANGE_TOTAL")
             "2023년_계_100세 이상" -> (2023, "T", "100세 이상")

        Returns: dict {year, sex, age_group}, or None if the header is "행정구역"
    """
    if col_name == "행정구역":
        return None
    parts = col_name.split("_")
    if len(parts) < 3:
        return None
    year_str = parts[0]  # "2023년"
    sex_str = parts[1]  # "계", "남", "여"
    age_str = "_".join(parts[2:])  # the rest — an age group, or "총인구수" / "연령구간인구수"

    # year
    m = re.match(r"(\d{4})년", year_str)
    if not m:
        return None
    year = int(m.group(1))

    # sex
    if sex_str not in SEX_MAP:
        return None
    sex = SEX_MAP[sex_str]

    # age_group encoding
    if age_str == "총인구수":
        age_group = "TOTAL"
    elif age_str == "연령구간인구수":
        age_group = "AGE_RANGE_TOTAL"
    else:
        # the age group as it is (the Korean label is kept)
        age_group = age_str

    return {"year": year, "sex": sex, "age_group": age_group}


def parse_region_cell(cell):
    """"서울특별시  (1100000000)" -> "11000"
        "서울특별시 종로구 (1111000000)" -> "11110"

        Returns: 5-digit region_code or None
    """
    m = re.search(r"\((\d+)\)", cell)
    if not m:
        return None
    code_10 = m.group(1)
    if len(code_10) < 5:
        return None
    return code_10[:5]


def apply_region_mapping(code_5):
    """Apply the mapping rules. Returns None for a region to skip."""
    if code_5 in SKIP_CODES_5DIGIT:
        return None
    return JEONBUK_MAP.get(code_5, code_5)


# ─────────────────────────────────────────────────────────────────
# 2. CSV parsing -> rows to insert
# ─────────────────────────────────────────────────────────────────

def parse_csv_to_rows(csv_path):
    """Population CSV -> generator of rows to insert.

        Yields: dict {region_code, year, data_type, sex, age_group, population, source, reference_date}
    """
    with open(csv_path, encoding="cp949") as f:
        reader = csv.reader(f)
        header = next(reader)

        # Parse the header: column index -> (year, sex, age_group)
        col_meta = {}
        for i, h in enumerate(header):
            meta = parse_header_column(h)
            if meta:
                col_meta[i] = meta

        stats = {"total_rows": 0, "skipped_region": 0, "unmapped_region": 0,
                 "skipped_empty_cell": 0, "emitted": 0}

        for row in reader:
            stats["total_rows"] += 1
            region_cell = row[0]
            code_5 = parse_region_cell(region_cell)
            if code_5 is None:
                stats["unmapped_region"] += 1
                print(f"  [WARN] 코드 추출 실패: {region_cell!r}", file=sys.stderr)
                continue

            mapped_code = apply_region_mapping(code_5)
            if mapped_code is None:
                stats["skipped_region"] += 1
                continue

            # Each data column -> one row
            for col_idx, meta in col_meta.items():
                if col_idx >= len(row):
                    continue
                cell = row[col_idx].strip().replace(",", "")
                if not cell:
                    stats["skipped_empty_cell"] += 1
                    continue
                try:
                    population = int(cell)
                except ValueError:
                    print(f"  [WARN] 정수 변환 실패: region={mapped_code} col={col_idx} value={cell!r}",
                          file=sys.stderr)
                    continue

                yield {
                    "region_code": mapped_code,
                    "year": meta["year"],
                    "data_type": DATA_TYPE,
                    "sex": meta["sex"],
                    "age_group": meta["age_group"],
                    "population": population,
                    "source": SOURCE_LABEL,
                    "reference_date": REFERENCE_DATE_FMT.format(year=meta["year"]),
                }
                stats["emitted"] += 1

        # Print statistics at the end
        print(f"\n[로더 통계]", file=sys.stderr)
        print(f"  CSV 전체 행: {stats['total_rows']}", file=sys.stderr)
        print(f"  SKIP된 region (출장소 등): {stats['skipped_region']}", file=sys.stderr)
        print(f"  코드 추출 실패: {stats['unmapped_region']}", file=sys.stderr)
        print(f"  빈 셀 (예: 45***의 2024·2025년): {stats['skipped_empty_cell']:,}", file=sys.stderr)
        print(f"  최종 INSERT 행: {stats['emitted']:,}", file=sys.stderr)


# ─────────────────────────────────────────────────────────────────
# 3. SQL mode (batches of 500 rows)
# ─────────────────────────────────────────────────────────────────

BATCH_SIZE = 500

INSERT_PREFIX = """INSERT INTO region_population_timeseries
    (region_code, year, data_type, sex, age_group, population, source, reference_date)
VALUES
"""


def escape_sql(v):
    """Escape an SQL string: ' -> ''"""
    if v is None:
        return "NULL"
    if isinstance(v, int):
        return str(v)
    s = str(v).replace("'", "''")
    return f"'{s}'"


def row_to_values_clause(row):
    return (
        f"({escape_sql(row['region_code'])}, "
        f"{escape_sql(row['year'])}, "
        f"{escape_sql(row['data_type'])}, "
        f"{escape_sql(row['sex'])}, "
        f"{escape_sql(row['age_group'])}, "
        f"{escape_sql(row['population'])}, "
        f"{escape_sql(row['source'])}, "
        f"{escape_sql(row['reference_date'])})"
    )


def generate_sql(csv_path, output_path):
    """CSV -> SQL file of INSERTs in batches of 500 rows."""
    rows = list(parse_csv_to_rows(csv_path))
    n_total = len(rows)
    n_batches = (n_total + BATCH_SIZE - 1) // BATCH_SIZE

    with open(output_path, "w", encoding="utf-8") as f:
        # Header comment
        f.write(f"-- ===================================================================\n")
        f.write(f"-- insert_pop_timeseries_{Path(csv_path).stem}.sql\n")
        f.write(f"-- 자료원: {SOURCE_LABEL}\n")
        f.write(f"-- 원본 CSV: {Path(csv_path).name}\n")
        f.write(f"-- 총 INSERT 행: {n_total:,}\n")
        f.write(f"-- 분할 블록 수: {n_batches} (블록당 최대 {BATCH_SIZE}행)\n")
        f.write(f"-- 적용 방식: DBeaver Alt+X 전체 실행\n")
        f.write(f"-- ===================================================================\n\n")

        for batch_idx in range(n_batches):
            start = batch_idx * BATCH_SIZE
            end = min(start + BATCH_SIZE, n_total)
            f.write(f"-- Block {batch_idx + 1}/{n_batches} (rows {start+1}-{end})\n")
            f.write(INSERT_PREFIX)
            values = [row_to_values_clause(rows[i]) for i in range(start, end)]
            f.write(",\n".join(values))
            f.write(";\n\n")

    print(f"\n✓ SQL 파일 생성 완료: {output_path}", file=sys.stderr)
    print(f"  총 {n_total:,}행, {n_batches} 블록", file=sys.stderr)


# ─────────────────────────────────────────────────────────────────
# 4. Direct database mode
# ─────────────────────────────────────────────────────────────────

def apply_to_db(csv_path, db_path):
    """CSV -> INSERT directly into the SQLite database."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Enable foreign keys
    cur.execute("PRAGMA foreign_keys = ON")

    insert_sql = """
    INSERT INTO region_population_timeseries
        (region_code, year, data_type, sex, age_group, population, source, reference_date)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """

    rows = list(parse_csv_to_rows(csv_path))
    data = [
        (r["region_code"], r["year"], r["data_type"], r["sex"], r["age_group"],
         r["population"], r["source"], r["reference_date"])
        for r in rows
    ]

    try:
        cur.executemany(insert_sql, data)
        conn.commit()
        print(f"\n✓ DB 적용 완료: {len(data):,}행 INSERT", file=sys.stderr)
    except sqlite3.IntegrityError as e:
        conn.rollback()
        print(f"\n[ERROR] IntegrityError: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────
# 5. CLI
# ─────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True, help="행안부 인구현황 CSV 경로 (CP949)")
    ap.add_argument("--mode", choices=["sql", "db"], default="sql",
                    help="sql: SQL 파일 생성 / db: DB 직접 적용")
    ap.add_argument("--output", help="SQL 모드: 출력 SQL 파일 경로")
    ap.add_argument("--db", help="DB 모드: project_data.db 경로")
    args = ap.parse_args()

    if args.mode == "sql":
        if not args.output:
            ap.error("--mode sql 사용 시 --output 필수")
        generate_sql(args.csv, args.output)
    elif args.mode == "db":
        if not args.db:
            ap.error("--mode db 사용 시 --db 필수")
        apply_to_db(args.csv, args.db)


if __name__ == "__main__":
    main()
