# build_tables.py — result tables by catchment composition
# ─────────────────────────────────────────────────────────────────────────────
# What it builds:
#   Reads outputs/runs/**/chapter3_*_r*.json and builds one table per case. Each row is a
#   catchment composition; runs with the same composition are grouped into one row.
#
# Why group this way: the figures follow the catchment. Among runs with the same catchment
# composition, delta_beds is identical, whereas required_beds can still vary within a
# composition because it depends on the base years and the institution type chosen by
# PeriodDecider. The table shows the fixed and the varying figures side by side in one row.
#
# Notation:
#   - If a value is the same in all runs, it is written once followed by '='.
#   - If the values differ, the minimum~maximum is written.
#   - If some runs did not produce the value, '(n회 미산출)' is added in parentheses.
#
# Usage:
#   python build_tables.py
#   python build_tables.py --root <runs folder> --out <output folder>
# ─────────────────────────────────────────────────────────────────────────────

import argparse
import csv
import json
from collections import Counter, OrderedDict
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Indicators in the table: (display name, subsection, key, decimals)
# The subsection is given because a value of the same name is exposed in several
# subsections and must be read from the subsection that produces it (Method 1 of section
# 3-3 and the facility basis of section 3-4 are different quantities).
# ─────────────────────────────────────────────────────────────────────────────
# Values that use bed utilization come in two variants (_catchment = average of the
# catchment, _national = national value for the type of the facility), so they take two
# rows. Values that do not (population, inpatient days, delta_beds, the emergency values)
# take one.
METRICS = [
    ("진료권 인구",            "3-3-1", "population",                0),
    ("입원의료이용률(일)",      "3-3-1", "inpatient_days",           4),
    ("병상이용률[진료권]",      "3-3-1", "bed_utilization_catchment", 4),
    ("병상이용률[전국]",        "3-3-1", "bed_utilization_national",  4),
    ("요구병상[진료권]",        "3-3-1", "required_beds_catchment",   0),
    ("요구병상[전국]",          "3-3-1", "required_beds_national",    0),
    ("특정유형 병상[진료권]",   "3-3-1", "specific_type_beds_catchment", 0),
    ("특정유형 병상[전국]",     "3-3-1", "specific_type_beds_national",  0),
    ("인구천명당 병상",         "3-3-2", "local_per_1000",           4),
    ("추가소요병상(방법2)",     "3-3-2", "delta_beds",               2),
    ("연간 입원환자[진료권]",   "3-4-1", "annual_inpatient_facility_catchment", 0),
    ("연간 입원환자[전국]",     "3-4-1", "annual_inpatient_facility_national",  0),
    ("연간 외래환자[진료권]",   "3-4-2", "annual_outpatient_catchment", 0),
    ("연간 외래환자[전국]",     "3-4-2", "annual_outpatient_national",  0),
    ("응급사망자",             "3-5-3", "emergency_deaths",          0),
    ("예방가능 사망자",         "3-5-3", "preventable_deaths",        0),
]

# Additional indicators for a case with a special-purpose catchment
# The guideline treats the special-purpose catchment as the whole country, so the two
# variants always give the same value. The table shows one line, and the agreement of the
# two values is checked below and reported in a footnote.
METRICS_SPECIAL = [
    ("특수 진료권 인구",        "3-3-1", "special_population",        0),
    ("특수 병상이용률",         "3-3-1", "special_bed_utilization_catchment", 4),
    ("특수 요구병상",           "3-3-1", "special_required_beds_catchment",   0),
    ("특수 특정유형 병상",      "3-3-1", "special_specific_type_beds_catchment", 0),
    ("특수 추가소요병상",       "3-3-2", "special_delta_beds",        2),
]

# Pairs whose two variants must agree (special-purpose catchment = the nation). A difference signals an input error.
SPECIAL_IDENTITY_PAIRS = [
    ("special_bed_utilization_catchment", "special_bed_utilization_national"),
    ("special_required_beds_catchment", "special_required_beds_national"),
    ("special_specific_type_beds_catchment", "special_specific_type_beds_national"),
]

# Values of the original reports — appended as comparison rows at the bottom of the table.
# Only values confirmed directly in the original report are entered (no estimates or conversions).
# The keys must equal the display names in METRICS.
#
# ── Principles of comparison ──
# 1) Every value found in the original report is entered. If its scope (base year, target
#    regions) differs from the pipeline's, the scope is written next to the value rather
#    than the value being left out. Whether the comparison holds is for the reader to judge.
# 2) If the original has no value, '— (reason)' is written.
# 3) No back-calculation or conversion.
#
# ── Correspondence of indicators ──
# (a) 'Required beds' / 'bed demand' in the original reports = specific_type_beds in the
#     pipeline. The originals use the name for the value after the share of inpatients at
#     general hospital level is applied
#     (Asan: 374,153 x 2.94 / (0.8544 x 365) = 3,527 -> x 0.1881 = 664;
#      Ulsan: 5,382,950 / 311.89 = 17,259 -> x 0.2958 = 5,105).
#     None of the three originals gives the intermediate value before the share is applied
#     (= required_beds in the pipeline).
# (b) 'Additional beds needed' in the originals is mostly based on Method 1 (required beds
#     - supplied beds). delta_beds in the pipeline is Method 2 (based on the national
#     average number of beds), so the two differ. Of the three originals, only Siheung
#     applies Method 2 of the guideline, with a national average of 2.9 beds (2020) rather
#     than the guideline's 2.96 (2021 Q1).
REFERENCE = {
    # ── Asan National Police Hospital (report of May 2024) ──
    # 2029 | 374,153 | 2.94 | 18.81% | 85.44% | 664 | supply 488 | shortfall 176 | with 120 new beds 56
    # 2029 | 374,153 | 176 | 146,769 | 135 | general 311 | shortfall 31
    # 2029 | 374,153 | emergency deaths 166.8 | reduction 28.7
    "HOS_ASAN": {
        "진료권 인구":          "374,153 (2029·아산시)",
        "입원의료이용률(일)":    "2.94 (2029)",
        "병상이용률":           "0.8544 (천안권 종합병원 평균)",
        "요구병상(방법1)":       "— 원문 미제시(비중 적용 전 값 없음)",
        "특정유형 병상":         "664 (2029) · 공급 488 · 과부족 176",
        "인구천명당 병상":       "— 방법2 미적용",
        "추가소요병상(방법2)":   "— 방법2 미적용",
        "응급사망자":           "166.8 (2029)",
        "예방가능 사망자":       "28.7 (2029·감소분)",
        "특수 진료권 인구":      "146,769 (경찰공무원)",
        "특수 특정유형 병상":    "135 (2029)",
    },

    # ── Ulsan Medical Center (report of Oct. 2021) ──
    # bed utilization 85.45% (national average of regional public medical centers, 2019); inpatient share 29.58%
    # Table III-76, 2028: Ulsan 1,106,743 / 4.86 / adjusted 4.61; Gyeongju 250,273 / 4.98 / adjusted 4.76 (no total given)
    # Table III-77, 2028: total bed demand 6,287 (base) / 5,974 (adjustment 1)
    # Table III-78, 2028: existing beds 4,015; additional beds 2,272 / 1,959 / 1,810 / 1,666
    # 2028 | population of the emergency catchment 202,489 | emergency deaths 266.0 | preventable 11.7
    "HOS_ULSAN": {
        "진료권 인구":          "울산 1,106,743 + 경주 250,273 (2028·합계 미제시)",
        "입원의료이용률(일)":    "울산 4.86 / 경주 4.98 (2028·기준값)",
        "병상이용률":           "0.8545 (전국 지방의료원 2019 평균)",
        "요구병상(방법1)":       "— 원문 미제시(비중 적용 전 값 없음)",
        "특정유형 병상":         "6,287 (2028·기준) / 5,974 (보정1) · 기존 4,015",
        "인구천명당 병상":       "— 방법2 미적용",
        "추가소요병상(방법2)":   "— 방법2 미적용(원문 추가소요 2,272은 방법1 기반)",
        "응급사망자":           "266.0 (2028·응급진료권 인구 202,489)",
        "예방가능 사망자":       "11.7 (2028)",
    },

    # ── Siheung Baegot Seoul National University Hospital (report of Feb. 2020) ──
    # Table 4: 2018 catchment (Siheung, Ansan, Yeonsu, Namdong) total 1,992,550
    # Table IV-26, 2026: Yeonsu 382,573; Namdong 559,486; Siheung 521,875; Ansan 652,834 (no catchment total)
    # Table IV-28, Method 1: Siheung demand 1,003 / supply 629 / additional 374;
    #   Yeonsu 808/335/473; Ansan 213; Namdong 239; total additional 1,299
    #   - Basis: total inpatients of the region in 2017, from the NHIS research database (stated
    #     in the text of the report; the table gives no year).
    #   - Bed utilization: 87.2%, the average of general-hospital-level institutions in the
    #     hospital management analysis statistics, applied to all four units because no
    #     comparable institution exists in Siheung.
    # Method 2 (option 1): national average of 2.9 beds (2020)
    # Table IV-29, Method 2, 2026: Yeonsu 727; Namdong 112; Siheung 783; Ansan 65;
    #   Yeonsu+Namdong 839; Siheung+Ansan 848; total 1,687
    # Table 25, 2026: population of Siheung 521,875 | emergency deaths 414.6 | reduction 81.3
    "HOS_SIHEUNG": {
        "진료권 인구":          "1,992,550 (2018·4개 지역) / 2026은 지역별만 제시",
        "입원의료이용률(일)":    "— 원문 미제시",
        "병상이용률":           "0.872 (병원경영분석 종합병원급 평균)",
        "요구병상(방법1)":       "— 원문 미제시(비중 적용 전 값 없음)",
        "특정유형 병상":         "시흥 1,003 · 공급 629 · 추가 374 (2017 입원환자 기준·합계 1,299)",
        "인구천명당 병상":       "전국평균 2.9 적용(2020) ※지침 2.96과 다름",
        "추가소요병상(방법2)":   "1,687 (2026·4개 지역 합계, 시흥 783)",
        "응급사망자":           "414.6 (2026·시흥시 단독)",
        "예방가능 사망자":       "81.3 (2026·시흥시 단독)",
    },
}

CASE_TITLE = {
    "HOS_ASAN":    "아산 경찰병원",
    "HOS_ULSAN":   "울산의료원",
    "HOS_SIHEUNG": "시흥배곧 서울대병원",
}


def load_runs(root: Path):
    """{project_id: [run_dict, ...]}"""
    out = {}
    for jp in sorted(root.rglob("chapter3_*_r*.json")):
        try:
            d = json.loads(jp.read_text(encoding="utf-8"))
        except Exception:
            continue
        pid = (d.get("_run") or {}).get("project_id") or jp.parent.name
        out.setdefault(pid, []).append(d)
    return out


def catchment_of(d: dict):
    """(tuple of region codes, list of catchment names, number of regions per category)"""
    cr = (d.get("3-2") or {}).get("catchment_result") or {}
    regions, names, sizes = [], [], []
    for k, v in sorted(cr.items()):
        rs = v.get("regions") or []
        regions.extend(rs)
        names.append(k)
        sizes.append(len(rs))
    return tuple(sorted(regions)), names, sizes


def value_of(d: dict, section: str, key: str):
    """Read a value from numbers/inputs of the given subsection. None if absent."""
    sec = d.get(section) or {}
    vs = sec.get("verification_source") or {}
    for bucket in ("numbers", "inputs"):
        v = (vs.get(bucket) or {}).get(key)
        if v is not None:
            return v
    return None


def fmt(vals, missing, dec, n_runs=None):
    """List of values -> table cell text.

        With n_runs=1 no '=' is added. '=' means the value is the same in all runs of the
        composition; with a single run nothing was compared, so it cannot claim reproducibility.
    """
    if not vals:
        return "—"
    lo, hi = min(vals), max(vals)
    same = (hi - lo) < 10 ** (-dec) / 2 if dec else (round(hi) == round(lo))
    if n_runs == 1:
        body = f"{lo:,.{dec}f}"
    else:
        body = (f"{lo:,.{dec}f} =" if same
                else f"{lo:,.{dec}f}~{hi:,.{dec}f}")
    if missing:
        body += f" ({missing}회 미산출)"
    return body


def saturation(runs: list) -> dict:
    """How saturated the observed catchment compositions are (Good-Turing).

        The share of compositions observed only once approximates the probability that the
        next run yields a composition not yet seen (Good 1953). The number of compositions is
        therefore reported as the number observed together with the unobserved mass, not as a
        definitive count.
    """
    cnt = Counter(catchment_of(d)[0] for d in runs)
    n = sum(cnt.values())
    f1 = sum(1 for v in cnt.values() if v == 1)
    return {"n_runs": n, "n_configs": len(cnt), "n_singleton": f1,
            "unseen_mass": (f1 / n) if n else None}


def build_case(pid: str, runs: list, table_no: str):
    """Markdown table and list of CSV rows for one case."""
    # Group by catchment composition (by the set of region codes — the labels differ between runs)
    groups = OrderedDict()
    for d in runs:
        regs, names, sizes = catchment_of(d)
        g = groups.setdefault(regs, {"runs": [], "names": [], "sizes": sizes})
        g["runs"].append(d)
        g["names"].append(" | ".join(names))
    # Sorted by number of runs (descending), then by number of regions (ascending).
    ordered = sorted(groups.items(),
                     key=lambda x: (-len(x[1]["runs"]), len(x[0])))

    has_special = any(value_of(d, "3-3-1", "special_population") is not None
                      for d in runs)
    metrics = METRICS + (METRICS_SPECIAL if has_special else [])

    header = ["진료권 구성", "지역 수", "실행 수"] + [m[0] for m in metrics]
    rows = []
    max_n = max(len(x[1]["runs"]) for x in ordered)
    for regs, g in ordered:
        n = len(g["runs"])
        label = f"{len(regs)}개 지역"
        if len(regs) <= 6:
            label += f" ({', '.join(regs)})"
        if n == max_n:
            label += " (최빈)"
        row = [label, str(len(regs)), f"{n}/{len(runs)}"]
        for _, sec, key, dec in metrics:
            vals, miss = [], 0
            for d in g["runs"]:
                v = value_of(d, sec, key)
                if v is None:
                    miss += 1
                else:
                    try:
                        vals.append(float(v))
                    except (TypeError, ValueError):
                        miss += 1
            row.append(fmt(vals, miss, dec, n_runs=n))
        rows.append(row)

    ref = REFERENCE.get(pid) or {}
    if ref:
        rows.append(["*원문 보고서*", "—", "—"] +
                    [str(ref.get(m[0], "—")) for m in metrics])

    # ── Markdown ──
    # Compositions are rows, so that the table grows only downward as compositions increase.
    sat = saturation(runs)
    md = [f"\n### 표 {table_no}. {CASE_TITLE.get(pid, pid)} — "
          f"진료권 구성별 수요추정 결과 (N={len(runs)})\n"]
    md.append("| " + " | ".join(header) + " |")
    md.append("|" + "---|" * len(header))
    for r in rows:
        md.append("| " + " | ".join(r) + " |")

    md.append(
        f"\n주 0) 관측된 진료권 구성 {sat['n_configs']}가지 중 "
        f"{sat['n_singleton']}가지는 1회만 나타났다. 다음 실행에서 아직 관측되지 "
        f"않은 구성이 나올 확률은 약 {sat['unseen_mass']:.1%}(Good-Turing 추정)이며, "
        f"따라서 이 표의 구성 목록은 가능한 구성의 전부가 아니다.")

    if has_special:
        # Check that the two variants agree -> reported in a footnote
        mismatch = []
        for a, b in SPECIAL_IDENTITY_PAIRS:
            for d in runs:
                va, vb = value_of(d, "3-3-1", a), value_of(d, "3-3-1", b)
                if va is None or vb is None:
                    continue
                if abs(float(va) - float(vb)) > 1e-6:
                    mismatch.append(f"{a}≠{b}")
                    break
        md.append(
            "\n주 0-1) 특수목적 진료권은 지침이 전국을 진료권으로 규정하므로"
            "(지침 L1038·L1686) '해당 진료권 평균'과 '전국 종별' 두 갈래가 같은 값이"
            "된다. 실제로 전 실행에서 두 값이 일치하여 한 줄로 표기하였다."
            if not mismatch else
            f"\n주 0-1) [!] 특수목적 진료권에서 두 갈래가 불일치한 항목: "
            f"{sorted(set(mismatch))} — 조달 확인 필요.")
        md.append("   다만 병상이용률의 기준연도·시설규모는 일반 진료권을 기준으로"
                  " 결정되므로, 일반 진료권 구성이 달라지면 그 선택이 바뀌어 특수"
                  " 진료권의 값도 함께 움직인다. 이는 조달 오류가 아니라 기준연도"
                  " 선택 변동의 파급이다.")

    md.append("\n주 1) '=' 는 해당 구성의 모든 실행에서 값이 동일함을 뜻하며, "
              "실행이 1회뿐인 구성에는 붙이지 않는다(비교 대상이 없으므로). "
              "범위로 표기된 값은 진료권 구성이 같아도 기준연도·시설종별 선택"
              "(Period 단계)에 따라 달라진 것이다.")
    md.append("주 2) 원문 열의 '특정유형 병상'은 원문이 '요구 병상수'·'병상 수요'라 "
              "부르는 값으로, 종합병원급 입원비중을 적용한 뒤의 수치이다. 비중 적용 "
              "전 값(요구병상(방법1)에 해당)은 세 원문 모두 제시하지 않는다.")
    md.append("주 3) 원문마다 병상이용률의 조달 근거가 다르다 — 아산은 진료권(천안권) "
              "종합병원 평균, 울산은 전국 지방의료원 평균, 시흥은 병원경영분석 "
              "종합병원급 평균. 지침은 '진료권 평균·종별·유사시설' 중 하나를 쓰라고만 "
              "하고 우선순위를 정하지 않는다.\n")
    return "\n".join(md), header, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    if a.root:
        root = Path(a.root)
    else:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
        from paths import OUT_DIR
        root = OUT_DIR / "runs"
    out_dir = Path(a.out) if a.out else root
    out_dir.mkdir(parents=True, exist_ok=True)

    runs_by_case = load_runs(root)
    if not runs_by_case:
        print(f"[!] {root} 에서 산출물을 찾지 못했습니다.")
        return

    order = [p for p in ("HOS_ASAN", "HOS_ULSAN", "HOS_SIHEUNG")
             if p in runs_by_case] + \
            [p for p in runs_by_case if p not in
             ("HOS_ASAN", "HOS_ULSAN", "HOS_SIHEUNG")]

    docs = ["# 진료권 구성별 수요추정 결과표\n"]
    for i, pid in enumerate(order, 1):
        runs = runs_by_case[pid]
        md, header, rows = build_case(pid, runs, f"5-{i}")
        docs.append(md)
        cp = out_dir / f"table_5-{i}_{pid}.csv"
        with open(cp, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        print(f"  표 5-{i} {pid}: {len(runs)}회 → 구성 "
              f"{len({catchment_of(d)[0] for d in runs})}가지  ({cp.name})")

    mp = out_dir / "tables_5.md"
    mp.write_text("\n".join(docs), encoding="utf-8")
    print(f"\n마크다운: {mp}")
    print("CSV 는 케이스별로 저장했습니다(한글 엑셀에서 바로 열림).")
    if not any(REFERENCE.values()):
        print("\n[i] REFERENCE 가 비어 있어 원문 대조행이 빠졌습니다. "
              "원문에서 확인한 값만 스크립트 상단에 채우십시오.")


if __name__ == "__main__":
    main()