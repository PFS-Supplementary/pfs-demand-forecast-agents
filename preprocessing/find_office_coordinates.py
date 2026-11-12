# find_office_coordinates.py — finds the coordinates of city/county/district offices (the database is not changed)
# ─────────────────────────────────────────────────────────────────────────────
# Purpose:
#   The guideline (Section 2, 2-ra, third item) sets the reference point for distance and
#   travel time at "the city hall, provincial office, district office, or community center
#   of the administrative unit". This script finds the office of each city/county/district
#   and compares the travel time to the office with the travel time to the boundary
#   centroid (region_administrative_center).
#
# What it does (the database is only read):
#   1) looks up the coordinates of each city/county/district office with Kakao Keyword
#      Search (public institutions, PO3)
#   2) from the project site of each of the three cases, measures Kakao Directions to
#      (a) the centroid and (b) the office at the same time, so that the difference due to
#      the reference point is separated from the difference due to the time of measurement
#   3) saves the results as CSV files in this folder
#
# Run (environment variable KAKAO_API_KEY):
#   python preprocessing/find_office_coordinates.py
# Calls: 264 office searches + 3 x 264 x 2 directions = about 1,850
# Output (in this folder):
#   cityhall_points.csv     office search result per city/county/district (for review)
#   distance_compare.csv    comparison table by case and city/county/district
#   case_origins.csv        coordinates found for the project sites
# ─────────────────────────────────────────────────────────────────────────────

import csv
import math
import os
import sqlite3
import sys
import time
from pathlib import Path

import requests

HOS = Path(__file__).resolve().parent.parent
DB_PATH = HOS / "data" / "project_data.db"
OUT_DIR = Path(__file__).resolve().parent

KEY = os.environ.get("KAKAO_API_KEY") or os.environ.get("KAKAO_REST_API_KEY")
if not KEY:
    sys.exit("환경변수 KAKAO_API_KEY(또는 KAKAO_REST_API_KEY)가 없습니다.")
H = {"Authorization": f"KakaoAK {KEY}"}
URL_KW = "https://dapi.kakao.com/v2/local/search/keyword.json"
URL_ADDR = "https://dapi.kakao.com/v2/local/search/address.json"
URL_DIR = "https://apis-navi.kakaomobility.com/v1/directions"

# Search terms for the project sites
CASES = {
    "HOS_ASAN":    ("keyword", "아산 국립경찰병원"),
    "HOS_ULSAN":   ("address", "울산광역시 북구 창평동 1232-12"),
    "HOS_SIHEUNG": ("keyword", "배곧서울대학교병원"),
}


def get(url, params, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, headers=H, params=params, timeout=10)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(1.5 * (i + 1))
                continue
            print(f"  [{r.status_code}] {r.text[:120]}")
            return None
        except requests.RequestException as e:
            print(f"  요청 실패: {e}")
            time.sleep(1.0)
    return None


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


def find_cityhall(full_name):
    """'서울특별시 종로구' -> 종로구청. Searches public institutions (PO3) and chooses by name and address.
        If no office can be settled on, the candidates are kept and marked match='review'.
    """
    parts = full_name.split()
    last = parts[-1]
    target = f"{last}청"  # 종로구청, 아산시청, 옹진군청
    js = get(URL_KW, {"query": f"{full_name}청", "category_group_code": "PO3", "size": 15})
    docs = (js or {}).get("documents", [])
    sido = parts[0][:2]
    exact = [d for d in docs if d.get("place_name", "").replace(" ", "") == target]
    if not exact:
        exact = [d for d in docs if d.get("place_name", "").replace(" ", "").startswith(target)
                 and "별관" not in d.get("place_name", "")]
    pick, match = None, "none"
    if exact:
        pick, match = exact[0], "exact"
    elif docs:
        pick, match = docs[0], "review"
    if pick is None:
        return {"match": match}
    addr = pick.get("road_address_name") or pick.get("address_name", "")
    return {"match": match, "name": pick.get("place_name", ""), "address": addr,
            "lat": float(pick["y"]), "lon": float(pick["x"]),
            "sido_in_address": sido in addr}


def directions(o, d):
    js = get(URL_DIR, {"origin": f"{o['lon']:.6f},{o['lat']:.6f}",
                       "destination": f"{d[1]:.6f},{d[0]:.6f}",
                       "priority": "RECOMMEND"})
    routes = (js or {}).get("routes", [])
    if not routes or routes[0].get("result_code", 0) != 0:
        return None, None
    s = routes[0].get("summary", {})
    return (s.get("distance") or 0) / 1000.0, round((s.get("duration") or 0) / 60.0)


def haversine_km(a, b):
    R = 6371.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def main():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)  # read-only
    regions = con.execute("SELECT region_code, center_name, latitude, longitude "
                          "FROM region_administrative_center ORDER BY region_code").fetchall()
    old = {(p, rc): (dk, t) for p, rc, dk, t in con.execute(
        "SELECT project_id, region_code, straight_distance_km, travel_time_car_min FROM case_distance_time")}
    con.close()

    # 1) Office coordinates
    halls = {}
    with open(OUT_DIR / "cityhall_points.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["region_code", "region_name", "centroid_lat", "centroid_lon", "match",
                    "hall_name", "hall_address", "hall_lat", "hall_lon", "sido_in_address", "offset_km"])
        for i, (rc, nm, la, lo) in enumerate(regions, 1):
            h = find_cityhall(nm)
            halls[rc] = h
            off = haversine_km((la, lo), (h["lat"], h["lon"])) if "lat" in h else ""
            w.writerow([rc, nm, la, lo, h["match"], h.get("name", ""), h.get("address", ""),
                        h.get("lat", ""), h.get("lon", ""), h.get("sido_in_address", ""),
                        f"{off:.2f}" if off != "" else ""])
            if i % 40 == 0:
                print(f"  청사 검색 {i}/{len(regions)}")
            time.sleep(0.05)
    print("청사 검색 완료:", sum(h['match'] == 'exact' for h in halls.values()), "건 확정 /",
          sum(h['match'] == 'review' for h in halls.values()), "건 검토 필요 /",
          sum(h['match'] == 'none' for h in halls.values()), "건 없음")

    # 2) Project sites
    origins = {}
    with open(OUT_DIR / "case_origins.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["project_id", "query", "name", "address", "lat", "lon"])
        for p, (m, q) in CASES.items():
            o = case_origin(m, q)
            if o is None:
                sys.exit(f"사업 위치 조회 실패: {p}")
            origins[p] = o
            w.writerow([p, q, o["name"], o["address"], o["lat"], o["lon"]])

    # 3) Directions — to the centroid and to the office at the same time
    with open(OUT_DIR / "distance_compare.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["project_id", "region_code", "region_name",
                    "db_road_km", "db_time_min",
                    "now_centroid_km", "now_centroid_min",
                    "now_hall_km", "now_hall_min", "hall_match"])
        n = 0
        for p, o in origins.items():
            for rc, nm, la, lo in regions:
                ck, cm = directions(o, (la, lo))
                h = halls[rc]
                hk, hm = directions(o, (h["lat"], h["lon"])) if "lat" in h else (None, None)
                dk, dt = old.get((p, rc), (None, None))
                w.writerow([p, rc, nm, dk, dt, ck, cm, hk, hm, h["match"]])
                n += 1
                if n % 50 == 0:
                    print(f"  길찾기 {n}/{len(origins) * len(regions)}")
                time.sleep(0.05)
    print("완료:", OUT_DIR)


if __name__ == "__main__":
    main()
