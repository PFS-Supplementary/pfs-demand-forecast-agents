# agents/sub/region_units.py — normalization to statistical units
# ─────────────────────────────────────────────────────────────────────────────
# Problem:
#   The distance data list a city with non-autonomous districts (e.g. Anyang-si 41170) and its
#   districts (Manan-gu 41171, Dongan-gu 41173) separately. If the Writer puts both in the
#   catchment, population is counted twice; if it puts only the districts, population is added
#   but inpatient days, beds, and the self-sufficiency rate are missing (the statistical tables
#   exist only at the city level).
#
# Approach:
#   Catchment calculations use one unit, the statistical unit. A non-autonomous district is
#   replaced by the city it belongs to.
#   - A code is a non-autonomous district if its first four digits match a MOTHER code in
#     region_master and it is not that code itself
#     (this includes districts loaded as CITY, such as Wonmi-gu, Sosa-gu, and Ojeong-gu of
#     Bucheon-si, reinstated in 2024)
#   - Autonomous districts of metropolitan cities (e.g. Buk-gu, Ulsan) have their own
#     statistics and are left as they are.
#   Normalization gathers place names into the same statistical unit; it is not a rule about
#   the extent of the catchment. The changes made are always returned and kept in the run record.
# ─────────────────────────────────────────────────────────────────────────────
import sqlite3
from functools import lru_cache

from paths import DB_PATH


@lru_cache(maxsize=1)
def _maps():
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT region_code, region_kind, region_name, short_name "
            "FROM region_master").fetchall()
    finally:
        conn.close()
    mothers = {rc for rc, k, _, _ in rows if k == "MOTHER"}
    child_to_mother = {}
    for rc, _, _, _ in rows:
        for m in mothers:
            if rc != m and rc[:4] == m[:4]:
                child_to_mother[rc] = m
    names = {rc: (rn or "").strip() for rc, _, rn, _ in rows}
    return child_to_mother, names


def to_stat_unit(code: str) -> str:
    """Non-autonomous district -> the city it belongs to. Other codes are returned unchanged."""
    c2m, _ = _maps()
    return c2m.get(str(code), str(code))


def is_ilban_gu(code: str) -> bool:
    c2m, _ = _maps()
    return str(code) in c2m


def children_of(mother_code: str) -> list[str]:
    """Codes of the non-autonomous districts under a city (for collecting lower-level data such as similar facilities)."""
    c2m, _ = _maps()
    return sorted(c for c, m in c2m.items() if m == str(mother_code))


def region_name(code: str) -> str:
    _, names = _maps()
    return names.get(str(code), str(code))


def normalize(codes: list[str]) -> tuple[list[str], list[str]]:
    """Normalize a list of codes to statistical units and remove duplicates (order preserved).

        Returns: (normalized list, list of strings describing the changes)
    """
    out, changes = [], []
    for c in codes:
        c = str(c)
        u = to_stat_unit(c)
        if u != c:
            changes.append(f"{region_name(c)}({c})→{region_name(u)}({u})")
        if u not in out:
            out.append(u)
        elif u == c:
            changes.append(f"{region_name(c)}({c}) 중복 제거")
    return out, changes
