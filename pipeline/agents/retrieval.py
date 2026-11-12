# agents/retrieval.py
# Retrieval agent: fetches the KDI guideline chunks assigned to a subsection (direct retrieval by identifier only)
#
# Retrieval does one thing: section_id -> the assigned chunks, returned deterministically in
# document order. There is no semantic search, so the citation path is explicit in the code.
# For section 3-2, five chunks on the gravity model from the culture and tourism sector
# guideline (202105) are included together with the medical-sector chunks. The medical-sector
# guideline names the gravity model as a Type 1 method without specifying it; the sector
# guideline of the same series does. They are stored in the same collection and fetched by
# identifier in the same way.
#
# Design: external text (the KDI guideline chunks) is fetched and injected into generation,
# but which chunks to fetch is decided by the deterministic mapping SECTION_CHUNK_IDS rather
# than by embedding similarity (rule-based retrieval).

from state import GraphState
from agents.sub.chunk_store import get_chunks

# ─────────────────────────────────────────────────────────────────────────────
# section_id -> list of assigned chunk ids (all are fetched, in document order)
# Chunks are not selected; they are passed in full in the order of the guideline.
# A subsection that is not in the mapping gets no chunks.
# ─────────────────────────────────────────────────────────────────────────────

SECTION_CHUNK_IDS: dict[str, list[str]] = {
    # Section 1 — overview of demand forecast and catchment setting (single chunk)
    "3-1": ["202212-III-1"],

    # Section 2 — catchment setting: the whole of Section 2 of the guideline, in document order,
    # plus the gravity-model chunks of the culture and tourism sector guideline (202105).
    # These are always included for section 3-2. Whether the method applies (it does not under
    # Types 2 and 3) is left to the Writer's judgment of the type criteria; nothing is hard-coded.
    "3-2": [
        "202212-III-1",  # conditions for a nationwide catchment; definition of the main target types
        "202212-III-2-1",  # 1. need for and overview of catchment setting
        "202212-III-2-2가",  # 2.ga methods; Table III-1 (Types 1-3); distance and travel time
        "202212-III-2-2나",  # 2.na implications of setting catchments by administrative unit
        "202212-III-2-2다",  # 2.da factors that determine the catchment (institution type, special-purpose hospitals, transport/competition/population)
        "202212-III-2-2라",  # 2.ra data to apply: (1) population (2) RI (3) distance (4) similar facilities
        "202212-III-2-특수목적",  # special-purpose medical facilities: (1) conversion rate (2) dual catchment (local + nationwide)
        "202105-VII-1-개요수식",  # [culture/tourism] gravity model: definition, basic equation, PFS equation A, four variables
        "202105-VII-1-절차",  # [culture/tourism] ten-step procedure for demand forecast with the gravity model (Figure VII-1)
        "202105-VII-2-매력도",  # [culture/tourism] attractiveness (rho): size ratio / regression / AHP; reference facility and choice of method
        "202105-VII-2-참여율",  # [culture/tourism] participation rate (alpha): estimation, no fixed form, handling of travel time (r<1)
        "202105-VII-2-쟁점원칙",  # [culture/tourism] issues in application: distance basis, reference point, caution on public transport, number of reference facilities
    ],

    # Section 3 — bed demand (Method 1 / Method 2; one chunk each, one per subsection)
    "3-3-1": ["202212-III-3-1"],  # Method 1: required beds assuming RI and CI of 100%
    "3-3-2": ["202212-III-3-2"],  # Method 2: bed supply relative to population

    # Section 4 — number of patients (inpatient / outpatient / new inflow; one chunk each, one per subsection)
    "3-4-1": ["202212-III-4-1"],  # annual number of inpatients
    "3-4-2": ["202212-III-4-2"],  # annual number of outpatients
    "3-4-3": ["202212-III-4-3"],  # size of new patient inflow

    # Section 5 — emergency care (three chunks, one per subsection)
    "3-5-1": ["202212-III-5-1"],  # 1. analysis of basic data (quantity and quality, Tables III-9 to III-13)
    "3-5-2": ["202212-III-5-2"],  # 2. estimating emergency deaths
    "3-5-3": ["202212-III-5-3"],  # 3. estimating preventable deaths (Table III-14)
}


def _fetch_by_ids(section_id: str, ids: list[str]) -> list[dict]:
    """Return all chunks for the given ids in the given order (= document order).

        The store does not guarantee the input order, so the result is mapped by id and
        reassembled in the given order. There is no notion of distance (direct retrieval).
    """
    # Fetch from the store (ChromaDB or its JSON dump) in the given order.
    got = {c["chunk_id"]: c for c in get_chunks(ids)}
    chunks = []
    for cid in ids:  # keeps the logical and document order of the guideline
        if cid not in got:
            print(f"[Retrieval] {section_id} → 경고: 청크 '{cid}' 없음(스킵)")
            continue
        chunks.append({**got[cid], "distance": 0.0})  # direct retrieval — no distance
    return chunks


def retrieval_agent(state: GraphState) -> GraphState:
    section_id = state["section_id"]

    ids = SECTION_CHUNK_IDS.get(section_id)
    if not ids:
        print(f"[Retrieval] {section_id} → 지정 청크 없음(범위 밖/미매핑), 지침 청크 스킵")
        return {**state, "guideline_chunks": []}

    chunks = _fetch_by_ids(section_id, ids)
    print(f"[Retrieval] {section_id} → 직접 회수 {len(chunks)}/{len(ids)}청크 "
          f"(문서 순서, 전체 반환)")
    for c in chunks:
        print(f"  - {c['heading']} ({len(c['text'])}자)")

    return {**state, "guideline_chunks": chunks}