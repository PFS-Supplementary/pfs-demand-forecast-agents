# run_batch.py — runs each case N times and writes the run records
# A failed run is retried at run level (default 4 attempts, waiting 60 s and doubling:
# 60 -> 120 -> 240 s), in addition to the call-level retry in llm.py.
#
# Outputs besides the run records:
#   failures.csv       Failures and retries. Dropping failures silently would remove the
#                      conditions that fail easily from the sample, so they are recorded.
#   report_coverage()  Prints, per case, the number of successful runs and the missing run
#                      numbers when the batch ends.
#   --fill             Adds runs with later numbers for those that failed after all retries,
#                      so that every case reaches N.
#   manifest.csv       Includes region_signature, the catchment normalized to a set of codes.
#                      The names of catchment categories differ from run to run, so the same
#                      composition can appear as different strings in the regions column.
# ─────────────────────────────────────────────────────────────────────────────
# Why it is needed:
#   driver.py always saves under the single name 'chapter3_{project_id}.json', so repeated
#   runs of a case would overwrite one another. The evaluation also treats the files in one
#   folder as repeated runs under the same condition, so cases must be kept in separate folders.
#
# What this script does:
#   1) Saves each run in the case folder with a run number   outputs/runs/{project_id}/..._r01.json
#   2) Skips files that already exist (a stopped batch can be resumed)
#   3) Clears the catalog cache before every run (no carry-over between runs)
#   4) Writes the console log of each run to a .log file
#   5) Builds manifest.csv at the end — catchment composition, base period, and key figures at a glance
#
# Usage:
#   python run_batch.py                       # 3 cases x 30 runs
#   python run_batch.py --n 10
#   python run_batch.py --cases HOS_ASAN --n 3
#   python run_batch.py --manifest-only       # rebuild the manifest without running
# ─────────────────────────────────────────────────────────────────────────────

import argparse
import csv
import json
import re
import sys
import time
import traceback
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

# Importing driver loads the graph and all agents.
from driver import run_chapter3, OUT_DIR

CASES_DEFAULT = ["HOS_ASAN", "HOS_ULSAN", "HOS_SIHEUNG"]
RUNS_ROOT = OUT_DIR / "runs"


class Tee:
    """Write to the console and to a file at the same time."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            st.write(s)
            st.flush()

    def flush(self):
        for st in self.streams:
            st.flush()


def _clear_catalog_cache():
    """Clear the module-level catalog cache of computation.

        When several runs are made in one process, the cache could reuse results from an earlier
        run. The calculation is deterministic, so values would not differ, but clearing it makes
        the independence of runs explicit.
        The module is looked up among the loaded modules rather than by a guessed path.
    """
    hit = 0
    for mod in list(sys.modules.values()):
        cache = getattr(mod, "_ch3_cache", None)
        if isinstance(cache, dict):
            cache.clear()
            hit += 1
    return hit


def run_one(project_id: str, idx: int, out_dir: Path,
            attempts: int = 4, backoff: float = 60.0) -> dict:
    """Run once and save JSON/MD/LOG. Skipped if the files already exist.

        Returns: {"ok": bool, "skipped": bool, "attempts": int, "errors": [strings]}

        Why retry at run level
          Transport-layer errors (e.g. `httpx.RemoteProtocolError: Server disconnected without
          sending a response`) are unrelated to the generated content but leave a run number
          empty. Regenerating only one subsection would mix generation times within a run, so
          the whole run is repeated and the surviving output is one complete sample.

        Note on bias
          A run obtained by retry is a new sample, not the failed one. This is harmless if
          failures are unrelated to content, but if runs with longer prompts failed more often
          the sample could lean towards shorter ones. The number of retries and the exception
          types are therefore recorded in failures.csv.
    """
    tag = f"{project_id}_r{idx:02d}"
    jpath = out_dir / f"chapter3_{tag}.json"
    if jpath.exists():
        print(f"  [skip] {jpath.name} 이미 있음")
        return {"ok": True, "skipped": True, "attempts": 0, "errors": []}

    lpath = out_dir / f"chapter3_{tag}.log"
    epath = out_dir / f"chapter3_{tag}.ERROR.txt"
    errors: list[str] = []

    for att in range(1, attempts + 1):
        _clear_catalog_cache()
        started = datetime.now()
        try:
            with open(lpath, "w", encoding="utf-8") as lf:
                with redirect_stdout(Tee(sys.__stdout__, lf)):
                    print(f"### {tag} 시작 {started:%Y-%m-%d %H:%M:%S}"
                          f"{'' if att == 1 else f'  (재시도 {att}/{attempts})'}")
                    results = run_chapter3(project_id)
                    print(f"### {tag} 종료 "
                          f"{(datetime.now() - started).total_seconds():.0f}초")
            break
        except Exception as e:                              # noqa: BLE001
            tb = traceback.format_exc()
            errors.append(f"{type(e).__name__}: {e}")
            epath.write_text(
                f"[시도 {att}/{attempts}] {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                + tb + "\n" + ("=" * 72 + "\n")
                + (epath.read_text(encoding="utf-8") if epath.exists() else ""),
                encoding="utf-8")
            if att < attempts:
                wait = backoff * (2 ** (att - 1))
                print(f"  [재시도] {tag} 시도 {att} 실패 "
                      f"({type(e).__name__}) — {wait:.0f}초 후 재실행")
                time.sleep(wait)
            else:
                print(f"  [실패] {tag} — {attempts}회 시도 모두 실패, "
                      f"ERROR.txt 참조")
                return {"ok": False, "skipped": False,
                        "attempts": attempts, "errors": errors}

    results["_run"]["run_index"] = idx
    results["_run"]["started_at"] = started.isoformat(timespec="seconds")
    # Leave a trace of retries in the output, so that it can be told afterwards whether a run
    # succeeded on the first attempt or was obtained by a retry.
    results["_run"]["attempts"] = att
    if errors:
        results["_run"]["retry_errors"] = errors
    jpath.write_text(json.dumps(results, ensure_ascii=False, indent=2,
                                default=str), encoding="utf-8")
    if errors and epath.exists():
        # The run succeeded in the end; the ERROR file is kept and distinguished by its name.
        epath.rename(out_dir / f"chapter3_{tag}.RETRIED.txt")

    lines = [f"# 제3장 수요 추정 — {tag}\n"]
    for sid, r in results.items():
        if sid.startswith("_"):
            continue
        lines.append(f"\n## [{sid}] {r['title']}  "
                     f"({'PASS' if r['passed'] else 'FAIL'})\n")
        lines.append(r["text"] or "(서술 없음)")
    (out_dir / f"chapter3_{tag}.md").write_text("\n".join(lines),
                                                encoding="utf-8")
    print(f"  [저장] {jpath.name}"
          + (f"  (시도 {att}회)" if att > 1 else ""))
    return {"ok": True, "skipped": False, "attempts": att, "errors": errors}


# ─────────────────────────────────────────────────────────────────────────────
# Manifest — a summary table for seeing run-to-run variation at a glance
# ─────────────────────────────────────────────────────────────────────────────
# Values that use bed utilization come in two variants, _catchment and _national.
WATCH = ["population", "inpatient_days", "delta_beds", "local_per_1000",
         "bed_utilization_catchment", "bed_utilization_national",
         "required_beds_catchment", "required_beds_national",
         "specific_type_beds_catchment", "specific_type_beds_national",
         "annual_inpatient_facility_catchment",
         "annual_inpatient_facility_national"]


def build_manifest(root: Path) -> Path:
    rows = []
    for jp in sorted(root.rglob("chapter3_*_r*.json")):
        try:
            d = json.loads(jp.read_text(encoding="utf-8"))
        except Exception:
            continue
        meta = d.get("_run", {})
        cr = (d.get("3-2") or {}).get("catchment_result") or {}
        catch = {k: (v.get("regions") or []) for k, v in cr.items()}
        n_regions = sum(len(v) for v in catch.values())

        row = {
            "file": jp.name,
            "project_id": meta.get("project_id", ""),
            "run": meta.get("run_index", ""),
            "started_at": meta.get("started_at", ""),
            "catchment_keys": " | ".join(catch.keys()),
            "n_regions_total": n_regions,
            "regions": " | ".join(",".join(v) for v in catch.values()),
            # Normalized set of codes. Names and order of catchment categories differ from run to run,
            # so grouping by the string would split identical compositions. Use this column for aggregation.
            "region_signature": ",".join(sorted(
                r for v in catch.values() for r in v)),
            "attempts": meta.get("attempts", 1),
            # Level of care judged in section 3-2, and whether the Period decision succeeded
            "facility_level": (meta.get("facility_level") or {}).get("level", ""),
            "period_decided": ((meta.get("base_period") or {}).get("decision_log") or {})
                              .get("decided", ""),
            "target_year": (meta.get("base_period", {}) or {})
                           .get("projection", {}).get("target_year", ""),
            "n_fail": sum(1 for s, r in d.items()
                          if not s.startswith("_") and not r.get("passed")),
            "n_unresolved": sum(len((r.get("verification_source") or {})
                                    .get("unresolved") or [])
                                for s, r in d.items() if not s.startswith("_")),
        }
        # Figures to track (general catchment). The first value found across subsections is used.
        seen = {}
        for s, r in d.items():
            if s.startswith("_"):
                continue
            nums = {**((r.get("verification_source") or {}).get("inputs") or {}),
                    **((r.get("verification_source") or {}).get("numbers") or {})}
            for k in WATCH:
                if k in nums and k not in seen:
                    seen[k] = nums[k]
        for k in WATCH:
            row[k] = seen.get(k, "")
        rows.append(row)

    out = root / "manifest.csv"
    if rows:
        with open(out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    return out


def summarize(root: Path):
    """Console summary of how the catchment composition varied across runs."""
    by_case = {}
    for jp in sorted(root.rglob("chapter3_*_r*.json")):
        try:
            d = json.loads(jp.read_text(encoding="utf-8"))
        except Exception:
            continue
        pid = (d.get("_run") or {}).get("project_id", "?")
        cr = (d.get("3-2") or {}).get("catchment_result") or {}
        sig = " | ".join(
            f"{k}:{len(v.get('regions') or [])}" for k, v in sorted(cr.items()))
        regions = tuple(sorted(
            r for v in cr.values() for r in (v.get("regions") or [])))
        by_case.setdefault(pid, []).append((sig, regions))

    print("\n" + "=" * 72)
    print("진료권 구성의 실행 간 변동")
    print("=" * 72)
    for pid, items in by_case.items():
        uniq = {}
        for sig, regions in items:
            uniq.setdefault(regions, [0, sig])[0] += 1
        print(f"\n[{pid}] {len(items)}회 중 서로 다른 구성 {len(uniq)}가지")
        for regions, (cnt, sig) in sorted(uniq.items(), key=lambda x: -x[1][0]):
            print(f"   {cnt:2}회  지역 {len(regions):2}개  ({sig})")
            print(f"         {', '.join(regions)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", nargs="+", default=CASES_DEFAULT)
    ap.add_argument("--n", type=int, default=30,
                    help="케이스별 목표 회차 수(--start 부터 세어 이 개수만큼)")
    ap.add_argument("--start", type=int, default=1, help="시작 실행 번호")
    ap.add_argument("--attempts", type=int, default=4,
                    help="run 1회당 최대 시도 횟수(전송 오류 재시도)")
    ap.add_argument("--backoff", type=float, default=60.0,
                    help="재시도 대기 초(시도마다 2배)")
    ap.add_argument("--fill", action="store_true",
                    help="재시도까지 실패한 회차만큼 뒤 번호로 채워 목표 수를 맞춘다")
    ap.add_argument("--max-extra", type=int, default=5,
                    help="--fill 시 케이스당 추가로 시도할 최대 회차 수")
    ap.add_argument("--manifest-only", action="store_true")
    a = ap.parse_args()

    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    failures: list[dict] = []
    retried: list[dict] = []

    if not a.manifest_only:
        total = len(a.cases) * a.n
        done = ok = 0
        t0 = datetime.now()
        for pid in a.cases:
            out_dir = RUNS_ROOT / pid
            out_dir.mkdir(parents=True, exist_ok=True)
            n_ok = 0
            for i in range(a.start, a.start + a.n):
                done += 1
                el = (datetime.now() - t0).total_seconds()
                print(f"\n{'='*72}\n[{done}/{total}] {pid} run {i:02d}  "
                      f"(경과 {el/60:.0f}분)\n{'='*72}")
                r = run_one(pid, i, out_dir, a.attempts, a.backoff)
                ok += bool(r["ok"])
                n_ok += bool(r["ok"])
                if r["ok"] and r["attempts"] > 1:
                    retried.append({"project_id": pid, "run": i,
                                    "attempts": r["attempts"],
                                    "errors": " | ".join(r["errors"])})
                if not r["ok"]:
                    failures.append({"project_id": pid, "run": i,
                                     "attempts": r["attempts"],
                                     "errors": " | ".join(r["errors"])})

            # If the target number of runs is not reached, add runs with later numbers.
            # A cap keeps this from growing without limit, and the additions are recorded as failures.
            if a.fill and n_ok < a.n:
                extra = a.start + a.n
                while n_ok < a.n and extra < a.start + a.n + a.max_extra:
                    print(f"\n{'='*72}\n[보충] {pid} run {extra:02d} "
                          f"(현재 {n_ok}/{a.n})\n{'='*72}")
                    r = run_one(pid, extra, out_dir, a.attempts, a.backoff)
                    if r["ok"]:
                        n_ok += 1
                        if r["attempts"] > 1:
                            retried.append({"project_id": pid, "run": extra,
                                            "attempts": r["attempts"],
                                            "errors": " | ".join(r["errors"])})
                    else:
                        failures.append({"project_id": pid, "run": extra,
                                         "attempts": r["attempts"],
                                         "errors": " | ".join(r["errors"])})
                    extra += 1
                if n_ok < a.n:
                    print(f"  [경고] {pid} 목표 {a.n}회 중 {n_ok}회만 성공")

        print(f"\n완료: {ok}/{total} 성공 · "
              f"총 {(datetime.now()-t0).total_seconds()/60:.0f}분")
        _write_failures(RUNS_ROOT, failures, retried)

    mf = build_manifest(RUNS_ROOT)
    print(f"\n매니페스트: {mf}")
    summarize(RUNS_ROOT)
    report_coverage(RUNS_ROOT)


def _write_failures(root: Path, failures: list[dict],
                    retried: list[dict]) -> None:
    """Record failures and retries.

        Needed to judge whether failures are unrelated to content: if failed runs were dropped
        silently, the conditions that fail easily would be missing from the sample.
    """
    if not (failures or retried):
        return
    path = root / "failures.csv"
    rows = ([{**f, "result": "failed"} for f in failures]
            + [{**r, "result": "retried_ok"} for r in retried])
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["project_id", "run", "result",
                                          "attempts", "errors"])
        w.writeheader()
        w.writerows(rows)
    print(f"\n실패·재시도 기록: {path} "
          f"(실패 {len(failures)} · 재시도 후 성공 {len(retried)})")


def report_coverage(root: Path) -> None:
    """Report, per case, the number of successful runs and the missing run numbers.

        Comparing cases with unequal numbers of runs would not be valid, so this is printed at
        the end of every batch.
    """
    print("\n" + "=" * 72)
    print("회차 확보 현황")
    print("=" * 72)
    for case_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        idxs = sorted(int(m.group(1)) for p in case_dir.glob("chapter3_*_r*.json")
                      if (m := re.search(r"_r(\d+)\.json$", p.name)))
        if not idxs:
            continue
        gaps = [i for i in range(1, max(idxs) + 1) if i not in idxs]
        line = f"  {case_dir.name:14s} {len(idxs):3d}회 (r{min(idxs):02d}~r{max(idxs):02d})"
        if gaps:
            line += f"  ⚠ 빠진 번호: {gaps}"
        print(line)


if __name__ == "__main__":
    main()