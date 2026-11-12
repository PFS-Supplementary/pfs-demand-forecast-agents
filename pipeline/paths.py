# paths.py — shared paths and settings for the pipeline
# ─────────────────────────────────────────────────────────────────────────────
# All paths are managed here. Set the environment variable HOS_ROOT to point to another
# root folder (the folder that contains data/).
# ─────────────────────────────────────────────────────────────────────────────
import os
from pathlib import Path

HOS_ROOT    = Path(os.environ.get("HOS_ROOT",
                                  Path(__file__).resolve().parent.parent))
DB_PATH     = HOS_ROOT / "data" / "project_data.db"
CHROMA_PATH = HOS_ROOT / "data" / "chroma"
OUT_DIR     = HOS_ROOT / "outputs"  # output root (runs/ is created under it)
PIPELINE_VERSION = "v4"

# Language model used for generation (generation settings are left at their defaults)
GEN_MODEL   = "gemini-2.5-flash"

# Distance and travel-time table (destination: city/county/district office; fixed departure time)
DIST_TABLE  = "case_distance_time_v3"

# Number of cities/counties/districts shown in catchment data (3) distance and travel time.
# Data (4) similar medical facilities uses the same range.
N_DISTANCE_SHOWN = 30

# Number of LLM calls allowed when the response format is invalid (e.g. JSON parsing fails)
LLM_MAX_ATTEMPTS = 3

# On a transport error (dropped connection, 503, etc.), the same call is sent again after a wait.
# Number of attempts and the first wait in seconds; the wait doubles each time (30 -> 60 -> 120 s).
# When the attempts are used up, an exception is raised and handled by the run-level retry.
LLM_TRANSPORT_ATTEMPTS = 4
LLM_TRANSPORT_BACKOFF = 30.0

# Upper bound on data years = survey year (survey_year, the year the original report was
# published) - DATA_YEAR_LAG. Yearbooks and survey statistics are published in the year after
# their reference year, so data for the survey year itself were not available to the original
# analysts. Actual publication dates of individual sources are not taken into account.
DATA_YEAR_LAG = 1
