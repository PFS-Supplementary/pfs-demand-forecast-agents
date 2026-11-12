# An LLM-based multi-agent decision support platform for urban development: Demand forecast in Benefit-Cost Analysis — code and run records

This repository contains the code, the 90 run records, the evaluation outputs, and the manual label files behind the paper "An LLM-based multi-agent decision support platform for urban development: Demand forecast in Benefit-Cost Analysis". The pipeline generates the demand forecast chapter (Chapter 3) of a medical-sector preliminary feasibility study (PFS) report. It was run 30 times for each of three cases (Asan Police Hospital, Ulsan Medical Center, and Siheung Baegot Seoul National University Hospital) and then evaluated. The pipeline and the evaluation procedure are described in the paper and in Supplementary Material A–F.

Code comments, prompts, and generated reports are all in Korean.

## 1. Repository layout

```
pipeline/                Generation pipeline
  run_batch.py           Runs each case N times and writes the run records
  driver.py              Runs the subsections of Chapter 3 in order for one case
  paths.py, state.py     Paths and settings; shared state
  agents/                Retrieval, Memory, Computation, Writer, Critic, orchestrator
  agents/sub/            Period, Catchment, formula catalog, LLM calls, etc.
evaluation/              Evaluation
  alce_eval.py           Entailment judgment of guideline-citing sentences
                         (citation recall and precision)
  eval_l1.py             Format checks on evidence markers
  eval_rg.py             Value matching for calculation and source markers
  build_tables.py        Result tables by catchment composition
  compare_explicature.py, classify_unsupported.py, eval_common.py
  premise_full_check.py  Sensitivity check on the judge input length (Section 4)
labeling/                Sheets for manual labeling
  make_label_sheet.py, make_e3_sheet.py, make_flip_audit.py
preprocessing/           Data preparation (Section 5)
  load_population.py, load_nhis_structure.py, load_survey5.py
                         Load source data into the database
  supplement_regions.py, extract_yearbook_district.py
                         Rows added for administrative boundary changes
  find_office_coordinates.py, measure_distance.py
                         City/county/district office coordinates; distance and travel time
  cityhall_overrides.csv Addresses of the two manually confirmed offices
outputs/
  runs/                  90 run records (30 per case); json, md, and log for each run
    manifest.csv         Summary of the 90 runs (catchment composition, base period, key outputs)
  eval/                  Evaluation results (sentences judged on their own)
  eval_explicature/      Evaluation results under the explicature condition and the list of
                         sentences whose judgment changed
  premise_full_check/    Results of the judge input length sensitivity check
  label/                 Cause labels for unsupported sentences (71 items)
  e3/                    Labels for judge–human agreement (100 items) and the re-labeling (71 items)
  flip_audit/            Review labels for the 137 sentences whose judgment changed under explicature
  critic_audit/          Post-hoc analysis of nominal-ending expressions (1,286 sentences)
  tables_90/, tables_86/ Result tables by catchment composition (all 90 runs / excluding the
                         4 runs in which the catchment could not be resolved)
docs/
  schema.sql             Database table definitions (no data)
  data_rebuild.md        Data sources and how to rebuild the database
  chunk_boundaries.md    Boundaries (opening and closing phrases) of the 20 guideline chunks
```

## 2. What is not in this repository

| Not included | Reason | Provided instead |
|---|---|---|
| Statistical data files and the database (`data/project_data.db`) | Source data are not redistributed | `docs/schema.sql`, `docs/data_rebuild.md`, `preprocessing/` |
| Guideline chunk texts and the ChromaDB store (`data/chroma`) | The guideline text is not republished | `docs/chunk_boundaries.md` |
| Coordinates, distances, and travel times obtained from the Kakao APIs | API terms of use | Measurement scripts (`preprocessing/find_office_coordinates.py`, `preprocessing/measure_distance.py`) |
| Manual labeling sheets that reproduce the chunk texts, and the guideline passages quoted as evidence during labeling (the `evidence_span` column of `label_entry.csv` and `e3_entry.csv`, left blank) | The guideline text is not republished | Label entry files (labels and notes) and sample keys |

What can and cannot be done with this repository alone:

- Possible as is: reading the run records and evaluation outputs and re-aggregating the figures reported in the paper (Section 6); re-running scripts that read only the run records, such as `build_tables.py`.
- Possible after obtaining the data yourself: running the generation pipeline, re-running the entailment judgment (`alce_eval.py`), and regenerating the labeling sheets. These require rebuilding the database and the guideline chunks as described in Section 5, and API keys.

## 3. Run records

`outputs/runs/<case>/chapter3_<case>_r<run>.json` is one run. The `.md` file with the same name is the generated report in readable form, and the `.log` file is the console output of the run.

Each subsection key in the json (`3-1` … `3-5-3`) holds the following.

| Key | Content |
|---|---|
| `text` | Final generated text (the 「」 brackets at the end of a sentence hold its evidence markers) |
| `passed`, `retries`, `critique` | Whether the Critic passed the text, number of regenerations, last verdict |
| `decision_record` | Critic verdict for each attempt, ruling on the special-purpose clause, result of parsing the catchment block, level of care |
| `catchment_result` | Administrative codes for each catchment category (subsection 3-2) |
| `verification_source` | Computed values (`numbers`), retrieved inputs and their sources (`inputs`, `input_sources`), items that could not be computed (`unresolved`, `skipped`), chunk identifiers passed to the Writer (`guideline_chunk_ids`), data values shown to the Writer (`data_records`) |

`_run` holds run-level information: the base-period decision and its rationale (`base_period`), the responding model and number of calls (`served_models`), the start time, hashes of the chunk texts (`chunk_store`), and the code hash (`code_sha256_16`).

The public copies differ from the originals in three places.

1. In `data_records`, the `value` of every record whose source is the Kakao Mobility directions API (straight-line distance, road distance, travel time) is set to `null` (90 records per run). Distances and travel times stated in the generated sentences are part of the generated report and are left as they are. As a consequence, re-running `eval_rg.py` on the public copies gives value-matching results for distance and travel-time figures that differ from `outputs/eval/rg_metrics.json`.
2. `_run.db_path` is changed to a relative path.
3. `_run.release_note` is added.

The code hash (`code_sha256_16`, `af9acd504aad8671` in all 90 runs) was computed from the paths and contents of the 30 .py files of the generation pipeline as they were at generation time. For this release the files were reorganized into folders, import paths and default paths were adjusted accordingly, and comments were rewritten in English, so recomputing the hash from the files in this repository does not give the same value. The generation logic was not changed.

In 4 runs the catchment block could not be converted to administrative codes (Siheung r14 and r17, Ulsan r05 and r21). The run records are kept as they are. Aggregates that depend on the catchment are provided both for the 86 runs without these four (`tables_86/`) and for all 90 runs (`tables_90/`).

## 4. Evaluation settings

- Generation model: Gemini 2.5 Flash (generation settings not specified). Runs were made on 2026-11-03.
- Judge: gpt-5.4-mini (`--judge openai --judge-model gpt-5.4-mini`). The judge prompt is `_JUDGE_PROMPT` in `alce_eval.py`.
- Judge input: the texts of the chunks cited by a sentence are concatenated, and the first 6,000 characters are passed to the judge (`alce_eval.py`).
  - The 243 guideline-citing sentences whose cited chunks exceed 6,000 characters in total were judged again with the full evidence text; the results are in `outputs/premise_full_check/`. Citation recall changes by no more than 0.2 percentage points in any case, and the number of unsupported sentences goes from 782 to 776. The script is `evaluation/premise_full_check.py`.
- Explicature: Gemini 2.5 Flash, temperature 0. Settings and prompt are in `alce_eval.py`.
- Sampling seeds: 20260805 for cause labeling, 20260807 for agreement.

## 5. Running it again

### Environment

The code was run on Python 3.12. Required packages: `langgraph`, `google-genai`, `chromadb`, `openai`, `python-dotenv`, `requests`, `openpyxl` (and `xlrd` for reading the .xls files of the 2013 and 2014 yearbooks).

Environment variables: `GEMINI_API_KEY` (generation, explicature), `OPENAI_API_KEY` (judge), `KAKAO_API_KEY` (distance and travel-time measurement). To use a data folder in another location, set `HOS_ROOT` to the path that contains the `data/` folder (the default is the repository root).

### Preparing the data

1. Obtain the data listed in `docs/data_rebuild.md` from their providers.
2. Create `data/project_data.db` from `docs/schema.sql` and load each table as described in `docs/data_rebuild.md`. Use the `load_*.py` scripts in `preprocessing/` for the tables that have a loading script.
3. Run `python preprocessing/supplement_regions.py --apply` to add the rows that compensate for administrative boundary changes.
4. Run `python preprocessing/find_office_coordinates.py` and `python preprocessing/measure_distance.py` to obtain office coordinates and measure distances and travel times. Travel times depend on the traffic information at the time of measurement, so they may differ from the values used in the paper.
5. Build the 20 chunks from the guideline PDFs following the boundaries in `docs/chunk_boundaries.md`. Either store them with their identifiers in the ChromaDB collection `kdi_guidelines` (`data/chroma`), or save them as `chunks_dump.json` in the repository root, a JSON list with the keys `chunk_id`, `text`, `heading`, and `version` (see `pipeline/agents/sub/chunk_store.py` for how they are read). The evaluation and labeling scripts take this file through `--chunk-map` or `--chunks`.

### Generation

```
cd pipeline
python run_batch.py --n 1            # one trial run per case
python run_batch.py --n 30 --fill    # 30 runs per case
```

Language model outputs differ from run to run, so the same reports will not be generated again. The paper treats this run-to-run variation as an object of analysis.

### Evaluation

Run from the repository root, in this order.

```
# 1) Citation recall and precision (each sentence judged on its own)
python evaluation/alce_eval.py --runs outputs/runs --out outputs/eval --judge openai --judge-model gpt-5.4-mini --chunk-map chunks_dump.json

# 2) Rule-based checks and result tables (no API calls)
python evaluation/eval_l1.py --runs outputs/runs --out outputs/eval
python evaluation/eval_rg.py --runs outputs/runs --out outputs/eval
python evaluation/build_tables.py

# 3) Explicature condition: copy the judge cache from 1) first, so that sentences already judged are not judged again
#    (copy outputs/eval/judge_cache.json to outputs/eval_explicature/judge_cache.json)
python evaluation/alce_eval.py --runs outputs/runs --out outputs/eval_explicature --judge openai --judge-model gpt-5.4-mini --chunk-map chunks_dump.json --explicature
python evaluation/compare_explicature.py --base outputs/eval/alce_metrics.json --exp outputs/eval_explicature/alce_metrics.json --out outputs/eval_explicature

# 4) Keyword-based classification of unsupported sentences (for reference)
python evaluation/classify_unsupported.py --metrics outputs/eval/alce_metrics.json --out outputs/eval

# 5) Sheets for manual labeling
python labeling/make_label_sheet.py --metrics outputs/eval/alce_metrics.json --chunks chunks_dump.json --runs outputs/runs --out outputs/label
python labeling/make_e3_sheet.py --cache outputs/eval/judge_cache.json --chunks chunks_dump.json --runs outputs/runs --out outputs/e3
python labeling/make_flip_audit.py --flips outputs/eval_explicature/explicature_flips.csv --out outputs/flip_audit
```

The default judge in `alce_eval.py` is Gemini, so the judge must be given explicitly as above.

## 6. Where the figures in the paper come from

| Content in the paper | Files (under `outputs/`) | Note |
|---|---|---|
| Evidence-marker rate; non-existent and out-of-set identifiers | `eval/l1_metrics.json`, `l1_summary.md` | |
| Citation recall and precision | `eval/alce_metrics.json`, `alce_summary.md`; judgments in `judge_cache.json` | |
| Value matching for calculation and source markers | `eval/rg_metrics.json`, `rg_summary.md` | |
| Recall and precision under explicature; sentences whose judgment changed | `eval_explicature/alce_metrics.json`, `compare_explicature.md`, `explicature_flips.csv` | |
| Causes of unsupported sentences | `label/label_entry.csv` + `label_key.json` | Stratum A: 30 randomly sampled items. Stratum B: the 41 items citing the culture and tourism sector guideline |
| Agreement between the judge and human labels | `e3/e3_entry.csv` + `e3_recheck.csv` + `e3_key.json` | For the second-stage label, `project_specific_v2` in `e3_recheck.csv` is the final value; overwrite the second-stage value in `e3_entry.csv` with it before computing (κ 0.800, weighted agreement 0.951) |
| Review of the 137 sentences whose judgment changed | `flip_audit/flip_audit_entry.csv` + `flip_audit_key.csv` | For category A, whether project-specific information was added is in the `ref_project` column; for category B it is given at the end of the `note` column, as "(B: 사업 정보 Y)" or "(B: 사업 정보 N)" |
| Run-to-run variation in catchment composition and bed demand | `runs/manifest.csv`, `tables_86/`, `tables_90/` | |
| Number of and reasons for Critic-triggered regenerations | `decision_record.critique_history` in the run json files | |
| Post-hoc analysis of nominal-ending expressions | `critic_audit/noun_interp_audit.csv` | The `재검토` column gives the outcome for the 26 sentences that were reviewed again |
| Full text of the representative runs | `runs/HOS_ASAN/…_r03`, `runs/HOS_ULSAN/…_r01`, `runs/HOS_SIHEUNG/…_r05` | |

## 7. License

- Code (.py files in `pipeline/`, `evaluation/`, `labeling/`, `preprocessing/`): MIT License (`LICENSE`).
- Run records, evaluation outputs, label files, and documentation (`outputs/`, `docs/`): CC BY 4.0 (`LICENSE-DATA.md`).
- Sentences restating the KDI sectoral guidelines and values taken from public statistics remain subject to the terms of their original sources.
