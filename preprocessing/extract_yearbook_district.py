# extract_yearbook_district.py
# Extracts the inpatient and outpatient visit days of given regions from the table by
# city/county/district in Part 3 of the Health Insurance Statistical Yearbook.
# Use: to supplement Sejong (36110), which is missing from inpatient_by_age_facility, from the yearbook table.
# Check: another region of the same table (default Bucheon, 41190) is extracted as well and compared with the values in the database.
# Column layout: left block [0]=region name [1]=category [4]=visit days; right block [8]=region name [9]=category [12]=visit days
# Category rows: total / subtotal / -inpatient / -outpatient ... -> the '-입' row is inpatient and the '-외' row outpatient visit days
import re, sys, glob
from pathlib import Path

def _rows(path):
    p = str(path)
    if p.endswith(".xls"):
        import xlrd
        wb = xlrd.open_workbook(p)
        for sh in wb.sheets():
            yield sh.name, [sh.row_values(i) for i in range(sh.nrows)]
    else:
        import openpyxl
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        for ws in wb.worksheets:
            yield ws.title, [list(r) for r in ws.iter_rows(values_only=True)]

def _clean(x):
    return re.sub(r"\s+", "", str(x)) if x is not None else ""

def _num(x):
    if x in (None, ""): return None
    try: return float(str(x).replace(",", ""))
    except ValueError: return None

def extract(path, names):
    """Returns: {name: {"INPATIENT_DAYS": v, "OUTPATIENT_DAYS": v, "sheet": s, "row": i}}"""
    out = {}
    for sname, rows in _rows(path):
        head = " ".join(_clean(c) for r in rows[:3] for c in r if c)
        if "시·군·구별" not in head and "시군구별" not in head:
            continue
        if "총계" not in head:
            continue
        for i, r in enumerate(rows):
            for name_col, kind_col, val_col in ((0, 1, 4), (8, 9, 12)):
                if len(r) <= val_col: continue
                nm = _clean(r[name_col])
                if nm not in names or nm in out: continue
                rec = {"sheet": sname, "row": i}
                for k in range(i, min(i + 8, len(rows))):
                    rr = rows[k]
                    if k > i and _clean(rr[name_col]) and not re.match(r"^[A-Za-z\-\. ]+$", str(rr[name_col])):
                        break  # next region block
                    kind = _clean(rr[kind_col])
                    if kind.startswith("-입") and "INPATIENT_DAYS" not in rec:
                        rec["INPATIENT_DAYS"] = _num(rr[val_col])
                    elif kind.startswith("-외") and "OUTPATIENT_DAYS" not in rec:
                        rec["OUTPATIENT_DAYS"] = _num(rr[val_col])
                if "INPATIENT_DAYS" in rec and "OUTPATIENT_DAYS" in rec:
                    out[nm] = rec
    return out

if __name__ == "__main__":
    root = Path(sys.argv[1])
    for f in sorted(root.glob("*년_건강보험통계연보/**/*제3편*")):
        if f.suffix not in (".xls", ".xlsx"): continue
        y = re.search(r"(\d{4})년", f.parent.name if "년_" in f.parent.name else f.parent.parent.name).group(1)
        print(y, f.name, extract(f, {"세종", "부천시"}))
