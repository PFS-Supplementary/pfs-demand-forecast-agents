# agents/sub/data_cap.py — computes the upper bound on data years in one place
# ─────────────────────────────────────────────────────────────────────────────
# Bound = project_config.survey_year - paths.DATA_YEAR_LAG.
# survey_year is the year the original report was published (Siheung 2020.02, Ulsan 2021.10,
# Asan 2024.05). Sources such as the National Health Insurance Statistical Yearbook are
# published in the second half of the year after their reference year, so data for the survey
# year itself were not available to the original analysts. One year is subtracted uniformly
# instead of keeping a publication date for each source.
# Period, Computation, and Memory all use this function so that the bound is the same everywhere.
# ─────────────────────────────────────────────────────────────────────────────
from paths import DATA_YEAR_LAG


def survey_year(conn, project_id: str) -> int | None:
    row = conn.execute("SELECT value FROM project_config WHERE project_id=? AND key='survey_year'",
                       (project_id,)).fetchone()
    try:
        return int(row[0]) if row and row[0] is not None else None
    except (TypeError, ValueError):
        return None


def data_year_cap(conn, project_id: str) -> int | None:
    """Upper bound on data years (only data up to this year are used). None if survey_year is missing (no bound)."""
    sy = survey_year(conn, project_id)
    return None if sy is None else sy - DATA_YEAR_LAG
