"""load_nhis_structure.py

Loads seven years (2013-2019) of the NHIS Health Insurance Statistical Yearbook
(Part 1 General Status, Chapter 1 Covered Population) into the table
region_population_structure.

Rules:
  - Primary key (region_code, reference_year, source): one row per source.
  - Old region codes are mapped to the new codes (the same policy as for the
    resident-registration population series).
  - medical_aid is NULL for cities/counties/districts; provinces only, from table I-3-4.
  - Provinces come entirely from tables I-3-1 and I-3-4; the provincial parent rows in
    table I-5 are skipped.

Mapping:
  I-3-1 provinces -> health_insured_total
  I-3-4 provinces -> medical_aid
  I-5 cities/counties/districts -> health_insured_total / health_insured_workplace /
                                   workplace_insured / workplace_dependent /
                                   region_insured / region_households

Sheet names (by year):
  2013-2016: p.10 (I-3-1), p.16 (I-3-4), p.20/22/24/26 (I-5)
  2017:      Ⅰ-3-1, Ⅰ-3-4, Ⅰ-5 and its three continuation sheets
  2018-2019: 1-3-1, 1-3-4, 1-5 ①-④

Usage:
  python load_nhis_structure.py
  python load_nhis_structure.py --dry-run    # statistics only, nothing written
"""

import argparse
import re
import sys
import sqlite3
import time
from pathlib import Path
from collections import defaultdict

# Dependencies: xlrd (xls), openpyxl (xlsx)
try:
    import xlrd
    from openpyxl import load_workbook
except ImportError as e:
    sys.exit(f"필수 패키지 누락: {e}. pip install xlrd openpyxl --break-system-packages")


# =================================================================
# 0. Paths
# =================================================================

DEFAULT_BASE_PATH = Path(__file__).resolve().parent.parent
# Base directory of the NHIS files (one subdirectory per year)
NHIS_BASE_DIR = DEFAULT_BASE_PATH / "data" / "shared" / "stats" / "hospitals"
DEFAULT_DB_PATH = DEFAULT_BASE_PATH / "data" / "project_data.db"

# Years to process
NHIS_YEARS = list(range(2013, 2020))  # 2013-2019


def find_nhis_file(nhis_base_dir, year):
    """Find the NHIS covered-population file of a year.
        Pattern: {year}년_건강보험통계연보/*적용인구*.xls*

        Returns: Path or None
    """
    year_dir_pattern = f"{year}년_건강보험통계연보"
    year_dir = nhis_base_dir / year_dir_pattern
    if not year_dir.exists():
        return None
    
    # Priority: .xlsx over .xls. For 2013 and 2014 only .xls exists.
    matches = list(year_dir.glob("*적용인구*.xlsx"))
    if not matches:
        matches = list(year_dir.glob("*적용인구*.xls"))
    
    if len(matches) == 0:
        return None
    if len(matches) > 1:
        # If there are several, take the first and warn
        print(f"  [WARN] {year}: 복수 매칭 ({len(matches)}건), 첫 번째 사용: {matches[0].name}",
              file=sys.stderr)
    return matches[0]


# =================================================================
# 1. Mapping rules
# =================================================================

# Province name in Korean -> region_code (17 provinces)
SIDO_KR_TO_CODE = {
    "서울": "11000", "부산": "26000", "대구": "27000", "인천": "28000",
    "광주": "29000", "대전": "30000", "울산": "31000", "세종": "36000",
    "경기": "41000", "강원": "51000",  # Gangwon Special Self-Governing Province since June 2023
    "충북": "43000", "충남": "44000",
    "전북": "52000",  # Jeonbuk Special Self-Governing Province since January 2024
    "전남": "46000", "경북": "47000", "경남": "48000", "제주": "50000",
}

# Province names in English (used to identify province rows)
SIDO_EN = set("Seoul Busan Daegu Incheon Gwangju Daejeon Ulsan Sejong "
              "Gyonggi Gangwon Chungbuk Chungnam Jeonbuk Jeonnam "
              "Gyongbuk Gyongnam Jeju".split())

# Twelve cities that have, or had, non-autonomous districts
#   - Bucheon: districts abolished on 4 July 2016; NHIS data before that (2013-2015) are at district level
#   - Hwaseong: districts created in April 2025
# In the NHIS files these appear concatenated as "{city}{district}".
MEGACITY_PARENTS = {
    "수원시", "성남시", "안양시", "안산시", "고양시", "용인시",
    "청주시", "전주시", "천안시", "창원시", "포항시",
    "부천시",  # abolished on 4 July 2016 (historical districts)
    "화성시",  # created in April 2025
}

# The same mapping rules as for the resident-registration population series
REGION_MAP = {
    "28170": "28177",  # Incheon Nam-gu -> Michuhol-gu
    "47720": "27720",  # Gunwi: Gyeongbuk -> Daegu
    # At province level the NHIS files never contain the old and the new code together
    # (e.g. the 2013 file has only "강원" and "전북"), so SIDO_KR_TO_CODE maps them to the new
    # codes 51000 and 52000.
}

# Branch offices are skipped (if found in the NHIS files)
SKIP_KEYWORDS = ["출장소"]


# =================================================================
# 2. Normalizing province names
# =================================================================

def normalize_sido_kr(s):
    """'서  울' -> '서울' (spaces, tabs, and line breaks removed)"""
    if not s:
        return None
    return re.sub(r"\s+", "", str(s))


def get_sido_code(sido_kr_raw):
    """Province cell in Korean -> region_code"""
    norm = normalize_sido_kr(sido_kr_raw)
    return SIDO_KR_TO_CODE.get(norm)


# =================================================================
# 3. City/county/district lookup (from region_master)
# =================================================================

def build_sigungu_lookup(db_path):
    """Build the lookup of cities, counties, and districts from region_master.

        Returns: {(sido_code, sigungu_short): region_code}
                 {(sido_code, mocity_short, district_short): region_code}  # non-autonomous districts
    """
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""SELECT region_code, region_name, region_kind 
                   FROM region_master 
                   WHERE region_kind IN ('DISTRICT','CITY','MOTHER')""")
    
    by_sido_short = {}  # {(sido_code, short_name): region_code}
    by_megacity = {}  # {(sido_code, mocity, district): region_code}
    
    for code, name, kind in cur.fetchall():
        tokens = name.split()
        if len(tokens) < 2:
            continue
        sido_kr = tokens[0]
        # The first token (the province name) is converted to the provincial region_code.
        sido_code = code[:2] + "000"
        
        if len(tokens) == 2:
            # "서울특별시 종로구" / "충청남도 천안시" / "강원특별자치도 춘천시"
            short = tokens[1]
            by_sido_short[(sido_code, short)] = code
        elif len(tokens) == 3:
            # "충청남도 천안시 동남구" — a non-autonomous district
            mocity, district = tokens[1], tokens[2]
            by_megacity[(sido_code, mocity, district)] = code
    
    conn.close()
    return by_sido_short, by_megacity


# =================================================================
# 4. Matching cities/counties/districts (including splitting concatenated names)
# =================================================================

def match_sigungu(sido_code, sigungu_kr_raw, by_sido_short, by_megacity):
    """Match sido_code (e.g. '41000') and sigungu_kr_raw (e.g. '수원시장안구', '종로구', '남구')
        to a region_code.

        Returns: (region_code, error_msg or None)
    """
    if not sigungu_kr_raw:
        return None, "empty"
    sigungu_kr = normalize_sido_kr(sigungu_kr_raw)  # remove spaces
    
    # skip branch offices
    if any(kw in sigungu_kr for kw in SKIP_KEYWORDS):
        return None, "skip_chuljangso"
    
    # 1. Try splitting a concatenated city + district name
    for parent in MEGACITY_PARENTS:
        if sigungu_kr.startswith(parent) and len(sigungu_kr) > len(parent):
            district = sigungu_kr[len(parent):]
            # Try matching the non-autonomous district
            code = by_megacity.get((sido_code, parent, district))
            if code:
                return code, None
            # District not matched -> fall back to the city
            code = by_sido_short.get((sido_code, parent))
            if code:
                return code, f"district_unknown_fallback_to_mocity({district})"
            # City not found either -> fail
            return None, f"concatenate_unknown({parent}+{district})"
    
    # 2. Single token — match province + short name directly
    code = by_sido_short.get((sido_code, sigungu_kr))
    if code:
        # Apply REGION_MAP (old code -> new code)
        return REGION_MAP.get(code, code), None
    
    return None, f"unmatched({sigungu_kr})"


# =================================================================
# 5. File readers (three sheet-naming patterns)
# =================================================================

def find_sheet(sheet_names, target):
    """Match a sheet name after normalization (handles trailing spaces, etc.)"""
    for s in sheet_names:
        if s.strip() == target or s.strip().replace(" ", "") == target.replace(" ", ""):
            return s
    return None


def get_sheets_for_year(year, all_sheets):
    """Sheet names by year. Returns: {logical_name: actual_sheet_name}"""
    result = {}
    
    if year <= 2016:
        # page-numbered
        targets = {"I-3-1": "p.10", "I-3-4": "p.16",
                   "I-5-a": "p.20", "I-5-b": "p.22", "I-5-c": "p.24", "I-5-d": "p.26"}
    elif year == 2017:
        # Roman numerals
        targets = {"I-3-1": "Ⅰ-3-1", "I-3-4": "Ⅰ-3-4",
                   "I-5-a": "Ⅰ-5", "I-5-b": "Ⅰ-5(계속1)",
                   "I-5-c": "Ⅰ-5(계속2)", "I-5-d": "Ⅰ-5(계속3)"}
    else:
        # 2018-2019 — Arabic numerals + circled numbers
        targets = {"I-3-1": "1-3-1", "I-3-4": "1-3-4",
                   "I-5-a": "1-5 ①", "I-5-b": "1-5 ②",
                   "I-5-c": "1-5 ③", "I-5-d": "1-5 ④"}
    
    for logical, target in targets.items():
        actual = find_sheet(all_sheets, target)
        if actual is None:
            print(f"  [WARN] {year} 시트 미발견: {target}", file=sys.stderr)
        result[logical] = actual
    return result


class WorkbookReader:
    """Common interface for xls and xlsx"""
    def __init__(self, path):
        self.path = str(path)
        if self.path.endswith(".xls"):
            self.engine = "xlrd"
            self.wb = xlrd.open_workbook(self.path)
            self.sheet_names = self.wb.sheet_names()
        else:
            self.engine = "openpyxl"
            self.wb = load_workbook(self.path, read_only=True)
            self.sheet_names = self.wb.sheetnames
    
    def get_sheet_rows(self, sheet_name):
        """sheet -> list of rows (each row = list of cell values)"""
        if sheet_name is None:
            return []
        if self.engine == "xlrd":
            ws = self.wb.sheet_by_name(sheet_name)
            return [[ws.cell_value(r, c) for c in range(ws.ncols)] for r in range(ws.nrows)]
        else:
            ws = self.wb[sheet_name]
            return [list(row) for row in ws.iter_rows(values_only=True)]
    
    def close(self):
        if self.engine == "openpyxl":
            self.wb.close()


# =================================================================
# 6. Parsing provinces (I-3-1, I-3-4)
# =================================================================

def parse_sido_sheet(rows, year, source_label, value_field):
    """Parse sheet I-3-1 or I-3-4.

        Args:
            rows: list of lists (the whole sheet)
            year: year
            source_label: e.g. 'NHIS Ⅰ-3-1 ...'
            value_field: 'health_insured_total' (I-3-1) or 'medical_aid' (I-3-4)

        Returns: list of dict {region_code, year, source, value_field: value}
    """
    output = []
    for r_idx, row in enumerate(rows):
        if len(row) < 3:
            continue
        a, b, c = row[0], row[1], row[2]
        
        # Province rows are identified by column B (English) holding a province name
        if isinstance(b, str) and b.strip() in SIDO_EN:
            if not isinstance(c, (int, float)):
                continue
            sido_code = get_sido_code(a)
            if not sido_code:
                print(f"  [WARN] {year} {source_label} R{r_idx+1} 시·도 매칭 실패: {a!r}",
                      file=sys.stderr)
                continue
            output.append({
                "region_code": sido_code,
                "year": year,
                "source": source_label,
                value_field: int(c),
            })
    return output


# =================================================================
# 7. Parsing cities/counties/districts (the four I-5 sheets)
# =================================================================

def is_noise_row(kr):
    """Identify rows that are not data: notes, headers, grand totals, page numbers"""
    if kr is None:
        return True
    # Page numbers (cells containing only digits) — footers of the 2013-2014 xls files
    if isinstance(kr, (int, float)):
        return True
    if not isinstance(kr, str):
        return False
    s = kr.strip()
    if not s:
        return True
    # notes
    if s.startswith("주") or s.startswith("주 :") or s.startswith("주:"):
        return True
    if re.match(r"^\d+\)", s):  # starts with "1)", "2)"
        return True
    if "주민등록" in s or "연도말 기준" in s:
        return True
    # headers
    if s in ("구분", "Division"):
        return True
    if "구분\n" in kr or "Division" in s:
        return True
    if "시·군·구별 적용" in s or "Beneficiaries" in s:
        return True
    if "인구 현황" in s or "by District" in s or "Cont'd" in s:
        return True
    # grand total
    if "총" in s and "합계" in s:
        return True
    return False


def process_sigungu_row(cols, sido_context, year, source_label, r_idx, col_offset,
                        by_sido_short, by_megacity, stats):
    """Process one row of one column set. sido_context is a mutable [code] (shared with the caller).
        Returns: dict (a city/county/district row) or None
    """
    kr, _, pop_total, _, iw_total, iw_insured, iw_dep, se_insured, se_hh, en = cols[:10]
    
    # Noise rows (headers, notes, grand totals) — sido_context is not changed
    if is_noise_row(kr):
        return None
    
    # Provincial parent row — en is in SIDO_EN (or, without en, kr is a single token found in SIDO_KR_TO_CODE)
    sido_candidate = None
    if isinstance(en, str) and en.strip() in SIDO_EN:
        sido_candidate = get_sido_code(kr)
    elif isinstance(kr, str):
        # en missing or None — if the Korean name is a short province name, it is a province row (e.g. '세 종')
        if get_sido_code(kr):
            sido_candidate = get_sido_code(kr)
    
    if sido_candidate:
        sido_context[0] = sido_candidate
        return None  # provincial parent rows are skipped
    
    # City/county/district row
    if sido_context[0] is None:
        stats["no_sido_context"] += 1
        return None
    
    # skip branch offices
    if isinstance(kr, str) and any(kw in kr for kw in SKIP_KEYWORDS):
        stats["skipped_chuljangso"] += 1
        return None
    
    # match
    code, err = match_sigungu(sido_context[0], kr, by_sido_short, by_megacity)
    if code is None:
        stats["unmatched"] += 1
        # Debug output goes to stderr
        print(f"  [WARN] {year} {source_label} R{r_idx} col{col_offset} "
              f"sido={sido_context[0]} kr={kr!r} en={en!r} err={err}",
              file=sys.stderr)
        return None
    
    if not isinstance(pop_total, (int, float)):
        return None
    
    return {
        "region_code": code,
        "year": year,
        "source": source_label,
        "health_insured_total": int(pop_total) if pop_total else None,
        "health_insured_workplace": int(iw_total) if isinstance(iw_total, (int, float)) else None,
        "workplace_insured": int(iw_insured) if isinstance(iw_insured, (int, float)) else None,
        "workplace_dependent": int(iw_dep) if isinstance(iw_dep, (int, float)) else None,
        "region_insured": int(se_insured) if isinstance(se_insured, (int, float)) else None,
        "region_households": int(se_hh) if isinstance(se_hh, (int, float)) else None,
    }


def parse_sigungu_sheets(reader, sheets_map, year, by_sido_short, by_megacity, stats):
    """The four I-5 sheets — column-major order: in each sheet the whole left column set, then
        the whole right column set. sido_context carries over between sheets and between the
        left and right sets.
    """
    all_rows = []
    sido_context = [None]
    source_label = f"NHIS 건강보험통계연보 {year} Ⅰ-5"
    
    for logical in ["I-5-a", "I-5-b", "I-5-c", "I-5-d"]:
        sheet_name = sheets_map.get(logical)
        if not sheet_name:
            continue
        rows = reader.get_sheet_rows(sheet_name)
        # whole left set -> whole right set (column-major)
        for col_offset in [0, 10]:
            for r_idx, row in enumerate(rows, start=1):
                cols = list(row[col_offset:col_offset+10])
                while len(cols) < 10:
                    cols.append(None)
                result = process_sigungu_row(
                    cols, sido_context, year, source_label, r_idx, col_offset,
                    by_sido_short, by_megacity, stats)
                if result:
                    all_rows.append(result)
                    stats["emitted_sigungu"] += 1
    
    return all_rows


# =================================================================
# 8. Processing all years
# =================================================================

def process_year(path, year, by_sido_short, by_megacity, stats):
    """Process one NHIS file (one year). Returns: list of dict (provinces and cities/counties/districts together)."""
    reader = WorkbookReader(path)
    sheets_map = get_sheets_for_year(year, reader.sheet_names)
    output = []
    
    # Provinces, I-3-1 (health_insured_total)
    sheet_i31 = sheets_map.get("I-3-1")
    if sheet_i31:
        rows = reader.get_sheet_rows(sheet_i31)
        source = f"NHIS 건강보험통계연보 {year} Ⅰ-3-1"
        sido_i31 = parse_sido_sheet(rows, year, source, "health_insured_total")
        output.extend(sido_i31)
        stats["emitted_sido_i31"] += len(sido_i31)
    
    # Provinces, I-3-4 (medical_aid)
    sheet_i34 = sheets_map.get("I-3-4")
    if sheet_i34:
        rows = reader.get_sheet_rows(sheet_i34)
        source = f"NHIS 건강보험통계연보 {year} Ⅰ-3-4"
        sido_i34 = parse_sido_sheet(rows, year, source, "medical_aid")
        output.extend(sido_i34)
        stats["emitted_sido_i34"] += len(sido_i34)
    
    # Cities/counties/districts, I-5
    sigungu = parse_sigungu_sheets(reader, sheets_map, year,
                                   by_sido_short, by_megacity, stats)
    output.extend(sigungu)
    
    reader.close()
    return output


# =================================================================
# 9. Database INSERT
# =================================================================

INSERT_SQL = """
INSERT INTO region_population_structure
    (region_code, reference_year, source,
     health_insured_total, health_insured_workplace,
     workplace_insured, workplace_dependent,
     region_insured, region_households,
     medical_aid)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def row_to_tuple(r):
    return (
        r["region_code"], r["year"], r["source"],
        r.get("health_insured_total"),
        r.get("health_insured_workplace"),
        r.get("workplace_insured"),
        r.get("workplace_dependent"),
        r.get("region_insured"),
        r.get("region_households"),
        r.get("medical_aid"),
    )


def apply_to_db(db_path, all_rows, dry_run=False):
    print(f"\n[적용] DB={db_path}, 행={len(all_rows):,}", file=sys.stderr)
    if dry_run:
        print("[DRY-RUN] DB 변경 없음", file=sys.stderr)
        return
    
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys = ON")
    
    # Check for rows of the NHIS source already in the table
    cur.execute("""SELECT COUNT(*) FROM region_population_structure
                   WHERE source LIKE 'NHIS 건강보험통계연보%'""")
    existing = cur.fetchone()[0]
    if existing > 0:
        print(f"\n  [WARN] 이미 NHIS source {existing:,}행 존재 — 재실행 시 PK 충돌",
              file=sys.stderr)
        print(f"  해결: DELETE FROM region_population_structure WHERE source LIKE 'NHIS 건강보험통계연보%';",
              file=sys.stderr)
        conn.close()
        sys.exit(1)
    
    data = [row_to_tuple(r) for r in all_rows]
    try:
        t0 = time.time()
        cur.executemany(INSERT_SQL, data)
        conn.commit()
        print(f"✓ INSERT 완료: {len(data):,}행 ({time.time()-t0:.1f}초)", file=sys.stderr)
    except sqlite3.IntegrityError as e:
        conn.rollback()
        print(f"\n[ERROR] IntegrityError: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


# =================================================================
# 10. CLI
# =================================================================

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH),
                    help=f"DB 파일 경로 (기본: {DEFAULT_DB_PATH})")
    ap.add_argument("--nhis-base-dir", default=str(NHIS_BASE_DIR),
                    help=f"NHIS base 디렉터리 (기본: {NHIS_BASE_DIR}) — "
                         f"하위에 {{year}}년_건강보험통계연보/ 디렉터리가 있어야 함")
    ap.add_argument("--year", type=int, action="append",
                    help="처리할 연도 (반복 가능, 기본: 2013~2019)")
    ap.add_argument("--dry-run", action="store_true", help="DB 변경 없이 통계만")
    args = ap.parse_args()
    
    nhis_base_dir = Path(args.nhis_base_dir)
    years = sorted(args.year) if args.year else NHIS_YEARS
    
    # Build the region_master lookup
    print(f"[1/4] region_master lookup 구축...", file=sys.stderr)
    by_sido_short, by_megacity = build_sigungu_lookup(args.db)
    print(f"  시·도·시·군·구·자치구·모-시: {len(by_sido_short)}개", file=sys.stderr)
    print(f"  일반구: {len(by_megacity)}개", file=sys.stderr)
    
    # Find and parse the files
    print(f"\n[2/4] NHIS 파일 파싱 ({len(years)}년치)...", file=sys.stderr)
    all_rows = []
    stats = {"emitted_sido_i31": 0, "emitted_sido_i34": 0, "emitted_sigungu": 0,
             "skipped_chuljangso": 0, "unmatched": 0, "no_sido_context": 0}
    
    # Pass 1: find the files of all years
    year_paths = {}
    for year in years:
        path = find_nhis_file(nhis_base_dir, year)
        if path is None:
            print(f"  [ERROR] {year}: 파일 미발견 - 디렉터리 또는 glob 패턴 확인", file=sys.stderr)
            print(f"          탐색 위치: {nhis_base_dir}/{year}년_건강보험통계연보/*적용인구*.xls*",
                  file=sys.stderr)
            sys.exit(1)
        year_paths[year] = path
    
    # Pass 2: parse
    for year in years:
        path = year_paths[year]
        print(f"  - {year}: {path.name}", file=sys.stderr)
        rows = process_year(path, year, by_sido_short, by_megacity, stats)
        all_rows.extend(rows)
        print(f"    → {len(rows):,}행", file=sys.stderr)
    
    print(f"\n[3/4] 파싱 통계:", file=sys.stderr)
    print(f"  시·도 Ⅰ-3-1: {stats['emitted_sido_i31']:,}", file=sys.stderr)
    print(f"  시·도 Ⅰ-3-4: {stats['emitted_sido_i34']:,}", file=sys.stderr)
    print(f"  시·군·구 Ⅰ-5: {stats['emitted_sigungu']:,}", file=sys.stderr)
    print(f"  SKIP (출장소): {stats['skipped_chuljangso']}", file=sys.stderr)
    print(f"  unmatched: {stats['unmatched']}", file=sys.stderr)
    print(f"  no sido context: {stats['no_sido_context']}", file=sys.stderr)
    print(f"  총 INSERT 대상: {len(all_rows):,}", file=sys.stderr)
    
    print(f"\n[4/4] DB 적용...", file=sys.stderr)
    apply_to_db(Path(args.db), all_rows, dry_run=args.dry_run)


if __name__ == "__main__":
    main()