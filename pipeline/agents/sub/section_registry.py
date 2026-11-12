# agents/sub/section_registry.py
# ─────────────────────────────────────────────────────────────────────────────
# List of the Chapter 3 subsections (id and title only).
#
#
# Section 3-2 (catchment setting) is a single subsection. Retrieval fetches its chunks
# directly through SECTION_CHUNK_IDS, the Writer derives the catchment from them in one pass,
# and catchment_resolver extracts the catchment codes, which are passed on to section 3-3.
#
# This file holds only subsection ids and titles. They follow the table of contents of the
# guideline and carry no analytical conclusion or processing rule.
# Titles are used for display and logs; they are not passed to the Writer as instructions.
#
# Notes:
#   - Subsections are in the order of the report's table of contents. The driver obtains the
#     processing order from get_section_ids().
#   - The hospital_type argument is accepted for compatibility and ignored (no branching).
# ─────────────────────────────────────────────────────────────────────────────

# (id, title) — table of contents of Chapter 3 of the KDI medical-sector guideline
CHAPTER3_SECTIONS: list[tuple[str, str]] = [
    # Section 1
    ("3-1",   "수요 추정 및 진료권 설정 개요"),
    # Section 2 — catchment setting (single subsection)
    ("3-2",   "진료권 설정"),
    # Section 3 — bed demand
    ("3-3-1", "지역 내 병상이용률·지역친화도·지역환자구성비를 이용한 병상 추정"),
    ("3-3-2", "지역별 인구 대비 병상 공급량에 기반한 적정 병상 규모 추정"),
    # Section 4 — number of patients
    ("3-4-1", "연간 입원환자 수의 추정"),
    ("3-4-2", "연간 외래환자 수의 추정"),
    ("3-4-3", "신규 유입 환자규모 추정"),
    # Section 5 — emergency care
    ("3-5-1", "응급사망 기초자료 분석"),
    ("3-5-2", "응급사망자 수 추정"),
    ("3-5-3", "예방 가능 사망자 수 추정"),
]

# For quick lookup (id -> title)
_TITLE_BY_ID: dict[str, str] = {sid: title for sid, title in CHAPTER3_SECTIONS}


def get_section_ids(hospital_type: str = None) -> list[str]:
    """Return the Chapter 3 subsection ids in table-of-contents order.

        The hospital_type argument is accepted for call compatibility and not used.
    """
    return [sid for sid, _ in CHAPTER3_SECTIONS]


def get_section_title(section_id: str) -> str | None:
    """Look up a subsection title. For logs and report headings; not a processing instruction."""
    return _TITLE_BY_ID.get(section_id)
