# measure_distance.py — measures distance and travel time from each project site
# ─────────────────────────────────────────────────────────────────────────────
# Distance and travel time
#
# Destination = the city/county/district office (guideline, section 3-2, 2-ra, third item:
#   "city hall, provincial office, district office, or community center"). Coordinates come
#   from cityhall_points.csv, produced by find_office_coordinates.py with Kakao keyword
#   search; places that needed checking are corrected with cityhall_overrides.csv.
# Fixed departure time = Kakao Mobility directions with future traffic information
#   (/v1/future/directions, departure_time=YYYYMMDDHHMM). All cases and regions use the
#   same departure time.
# Straight-line distance (haversine) and road distance are stored separately.
# Unit of measurement = the statistical unit (non-autonomous districts excluded; a city with
#   such districts is measured to its city hall). A non-autonomous district has no
#   statistics and cannot be a unit of the catchment calculation (see region_units).
# The earlier table case_distance_time (destination = boundary centroid) is not deleted.
#
# Run (environment variable KAKAO_API_KEY):
#   1) test: python preprocessing/measure_distance.py --test
#   2) full: python preprocessing/measure_distance.py
#   Option: --departure 202611041000 (default: 10:00 on the next Wednesday)
#   Calls: 3 cases x about 229 units = about 690, plus 3 for the project sites
# Output:
#   database table case_distance_time_v3 (deleted and refilled per case)
#   distance_<YYYYMMDDHHMM>.csv in this folder (a backup of the same content)
# ─────────────────────────────────────────────────────────────────────────────

import argparse
import csv
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))  # pipeline/
from paths import DB_PATH, HOS_ROOT, DIST_TABLE                    # noqa: E402
from agents.sub.region_units import is_ilban_gu                    # noqa: E402

HALL_DIR = Path(__file__).resolve().parent
HALL_CSV = HALL_DIR / "cityhall_points.csv"
OVERRIDE_CSV = HALL_DIR / "cityhall_overrides.csv"

KEY = os.environ.get("KAKAO_API_KEY") or os.environ.get("KAKAO_REST_API_KEY")
H = {"Authorization": f"KakaoAK {KEY}"} if KEY else {}
URL_KW = "https://dapi.kakao.com/v2/local/search/keyword.json"
URL_ADDR = "https://dapi.kakao.com/v2/local/search/address.json"
URL_FUTURE = "https://apis-navi.kakaomobility.com/v1/future/directions"
API_SOURCE = "Kakao Mobility 미래 운행 정보 길찾기 (/v1/future/directions, priority=RECOMMEND)"

# Search terms for the project sites
CASES = {
    "HOS_ASAN":    ("keyword", "아산 국립경찰병원"),
    "HOS_ULSAN":   ("address", "울산광역시 북구 창평동 1232-12"),
    "HOS_SIHEUNG": ("keyword", "배곧서울대학교병원"),
}


def get(url, params, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, headers=H, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(1.5 * (i + 1))
                continue
            print(f"  [{r.status_code}] {r.text[:200]}")
            return None
        except requests.RequestException as e:
            print(f"  요청 실패: {e}")
            time.sleep(1.0)
    return None


def next_wednesday_10() -> str:
    now = datetime.now()
    d = now + timedelta(days=((2 - now.weekday()) % 7) or 7)
    return d.replace(hour=10, minute=0).strftime("%Y%m%d%H%M")


def haversine_km(a, b):
    R = 6371.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(h))


def case_origin(method, query):
    url = URL_KW if method == "keyword" else URL_ADDR
    js = get(url, {"query": query, "size": 5})
    docs = (js or {}).get("documents", [])
    if not docs:
        return None
    d = docs[0]
    return {"lat": float(d["y"]), "lon": float(d["x"]),
            "name": d.get("place_name", ""),
            "address": d.get("road_address_name") or d.get("address_name", "")}


def future_directions(o, d, departure):
    js = get(URL_FUTURE, {"origin": f"{o['lon']:.6f},{o['lat']:.6f}",
                          "destination": f"{d['lon']:.6f},{d['lat']:.6f}",
                          "departure_time": departure,
                          "priority": "RECOMMEND"})
    routes = (js or {}).get("routes", [])
    if not routes or routes[0].get("result_code", 0) != 0:
        msg = routes[0].get("result_msg") if routes else "응답 없음"
        return None, None, msg
    s = routes[0].get("summary", {})
    km = (s.get("distance") or 0) / 1000.0
    mins = round((s.get("duration") or 0) / 60.0)
    return km, mins, None


def load_units(conn):
    """Targets = the statistical units in region_administrative_center (non-autonomous districts excluded)."""
    rows = conn.execute("SELECT region_code, center_name FROM region_administrative_center "
                        "ORDER BY region_code").fetchall()
    return [(rc, (nm or "").strip()) for rc, nm in rows if not is_ilban_gu(rc)]


def load_halls(units):
    halls = {r["region_code"]: r for r in csv.DictReader(open(HALL_CSV, encoding="utf-8-sig"))}
    overrides = {}
    if OVERRIDE_CSV.exists():
        overrides = {r["region_code"]: r for r in
                     csv.DictReader(open(OVERRIDE_CSV, encoding="utf-8-sig"))}
    out, problems = {}, []
    for rc, nm in units:
        if rc in overrides:
            o = overrides[rc]
            out[rc] = {"name": o["hall_name"], "address": o.get("hall_address", ""),
                       "lat": float(o["hall_lat"]), "lon": float(o["hall_lon"]),
                       "basis": f"청사(수동 확인: {o.get('note', '')})"}
            continue
        h = halls.get(rc)
        if not h or not h.get("hall_lat"):
            problems.append(f"{rc} {nm}: 청사 좌표 없음")
            continue
        if "예정" in h["hall_name"] or "임시" in h["hall_name"]:
            problems.append(f"{rc} {nm}: '{h['hall_name']}' ({h['hall_address']}) — "
                            "건립 예정/임시 표기, 현재 청사 좌표 확인 필요")
            continue
        out[rc] = {"name": h["hall_name"], "address": h["hall_address"],
                   "lat": float(h["hall_lat"]), "lon": float(h["hall_lon"]),
                   "basis": f"청사(Kakao 키워드 검색, {h['match']})"}
    return out, problems


def ensure_table(conn):
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {DIST_TABLE} (
            project_id            TEXT NOT NULL,
            region_code           TEXT NOT NULL,
            straight_distance_km  REAL,
            road_distance_km      REAL,
            travel_time_car_min   INTEGER,
            dest_name             TEXT,
            dest_address          TEXT,
            dest_lat              REAL,
            dest_lon              REAL,
            dest_basis            TEXT,
            origin_name           TEXT,
            origin_lat            REAL,
            origin_lon            REAL,
            departure_time        TEXT,
            api_source            TEXT,
            result_msg            TEXT,
            measured_at           TEXT,
            PRIMARY KEY (project_id, region_code),
            FOREIGN KEY (region_code) REFERENCES region_master(region_code)
        )""")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--departure", default=None, help="YYYYMMDDHHMM (기본: 다음 수요일 10:00)")
    ap.add_argument("--cases", nargs="+", default=list(CASES))
    ap.add_argument("--test", action="store_true", help="1회 호출로 API 응답만 확인(DB 무변경)")
    a = ap.parse_args()
    if not KEY:
        sys.exit("환경변수 KAKAO_API_KEY(또는 KAKAO_REST_API_KEY)가 없습니다.")
    dep = a.departure or next_wednesday_10()
    print(f"출발 시각: {dep}")

    conn = sqlite3.connect(DB_PATH)
    units = load_units(conn)
    halls, problems = load_halls(units)
    print(f"측정 단위 {len(units)}개 (일반구 제외) / 청사 좌표 확보 {len(halls)}개")

    if a.test:
        o = case_origin(*CASES["HOS_ASAN"])
        print("사업 위치(아산):", o)
        h = halls.get("44200")
        print("도착(아산시청):", h)
        print("결과:", future_directions(o, h, dep))
        if problems:
            print("\n[확인 필요 — 본 측정 전 cityhall_overrides.csv 로 해결]")
            for p in problems:
                print("  -", p)
        conn.close()
        return

    if problems:
        print("\n[중단] 청사 좌표 확인이 필요한 곳이 있습니다. "
              f"{OVERRIDE_CSV.name} 에 region_code,hall_name,hall_address,hall_lat,hall_lon,note "
              "형식으로 적은 뒤 다시 실행하십시오.")
        for p in problems:
            print("  -", p)
        sys.exit(1)

    ensure_table(conn)
    stamp = datetime.now().isoformat(timespec="seconds")
    backup = HALL_DIR / f"distance_{dep}.csv"
    with open(backup, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["project_id", "region_code", "region_name", "straight_km", "road_km",
                    "time_min", "dest_name", "dest_lat", "dest_lon", "departure_time",
                    "result_msg"])
        for pid in a.cases:
            o = case_origin(*CASES[pid])
            if o is None:
                sys.exit(f"사업 위치 조회 실패: {pid}")
            print(f"\n{pid}: {o['name'] or o['address']} ({o['lat']:.5f}, {o['lon']:.5f})")
            conn.execute(f"DELETE FROM {DIST_TABLE} WHERE project_id=?", (pid,))
            fail = 0
            for i, (rc, nm) in enumerate(units, 1):
                h = halls[rc]
                sk = haversine_km((o["lat"], o["lon"]), (h["lat"], h["lon"]))
                rk, tm, msg = future_directions(o, h, dep)
                if tm is None:
                    fail += 1
                conn.execute(
                    f"INSERT INTO {DIST_TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, rc, round(sk, 3), rk, tm, h["name"], h["address"], h["lat"],
                     h["lon"], h["basis"], o["name"] or o["address"], o["lat"], o["lon"],
                     dep, API_SOURCE, msg, stamp))
                w.writerow([pid, rc, nm, f"{sk:.3f}", rk, tm, h["name"], h["lat"],
                            h["lon"], dep, msg or ""])
                if i % 50 == 0:
                    print(f"  {i}/{len(units)}")
                time.sleep(0.05)
            conn.commit()
            print(f"  완료: {len(units)}개 중 경로 실패 {fail}개")
    conn.close()
    print(f"\n저장: {DIST_TABLE} / 백업 {backup}")


if __name__ == "__main__":
    main()
