# Data sources and rebuilding the database

All data used in generation are stored in a single SQLite database, `data/project_data.db`. This repository does not include the database or the source data. The definitions of the 16 tables read by the code are in `docs/schema.sql`; this document describes, table by table, which data go in and in what form.

The data numbers match the data list (Table S) in Supplementary Material A of the paper. Row counts refer to the database used for the runs reported in the paper. Source names are given in English with the original Korean name in parentheses.

## 1. Tables

### Administrative units

| Table | Rows | Data | Content | Loading |
|---|---:|---|---|---|
| `region_master` | 296 | 12. Legal-dong codes, full list (법정동코드 전체자료), Ministry of the Interior and Safety | 1 nation, 17 provinces, 278 cities/counties/districts (including 13 cities that have non-autonomous districts). Includes 10 abolished codes. Hierarchy is kept in `parent_code` and `province_code` | Entered directly with SQL |

Every statistical table is linked to `region_master` through `region_code` (a five-digit administrative code).

### Population

| Table | Rows | Data | Content | Loading |
|---|---:|---|---|---|
| `region_population_timeseries` (`data_type='OBSERVED'`) | 344,586 | 1. Resident registration population statistics, population by age, annual (주민등록인구통계 연령별 인구현황), Ministry of the Interior and Safety, 2008–2025 | Region × year × sex × age group (five-year groups and totals) | `preprocessing/load_population.py` |
| `region_population_timeseries` (`data_type='PROJECTED'`) | 90,117 | 2. Population projections for provinces, medium variant, five-year age groups (장래인구추계 시도편), Statistics Korea, base years 2017, 2020, and 2022 | Province × year × sex × age group. The base year is in the `vintage` column | Loading code no longer available |
| `region_population_structure` | 1,986 | 3. National Health Insurance Statistical Yearbook, Part 1, covered population (건강보험통계연보 제1편 적용인구; tables Ⅰ-3-1, Ⅰ-3-4, Ⅰ-5), 2013–2019 | Health insurance covered population (employee and self-employed), medical aid recipients | `preprocessing/load_nhis_structure.py` |

### Health care utilization and beds

| Table | Rows | Data | Content | Loading |
|---|---:|---|---|---|
| `inpatient_by_age_facility` | 5,524 | 4. National Health Insurance Statistical Yearbook, Part 3, inpatient and outpatient visit days by city/county/district, by registered residence (건강보험통계연보 제3편 시·군·구별 입원·외래 내원일수), 2013–2024 | Inpatient visit days (`INPATIENT_DAYS`) and outpatient visit days (`OUTPATIENT_DAYS`) by city/county/district and year. Totals over all medical institutions only | Main loading code no longer available. The 54 supplementary rows are added by `preprocessing/supplement_regions.py` |
| `demand_parameter` (`inpatient_days_by_type`) | 2,148 | 5. National Health Insurance Statistical Yearbook, Part 4, claims by institution type and location, inpatient visit days (건강보험통계연보 제4편 요양기관 종별·소재지별 진료실적), 2013–2024 | Province and nation × year × institution type | Loading code no longer available |
| `survey5_beds` | 12,595 | 6. Fifth National Health and Medical Service Survey, city/county/district volume, Ⅰ.B.1 licensed beds (제5차 국민보건의료실태조사 시군구편 허가병상수), 2016–2020 | City/county/district × year × institution type × size | `preprocessing/load_survey5.py` |
| `survey5_inpatient_days` | 13,740 | 6. Same survey, Ⅱ.A.1 b) total inpatient days (총재원일수) | Same as above | `preprocessing/load_survey5.py` |
| `region_relevance_index` | 9,840 | 7. Same survey, Ⅱ.A.24 inpatient self-sufficiency rate (입원환자 자체충족률), 2016–2020 | 17 provinces and 229 cities/counties/districts × year × 8 patient categories. Unit: % | Entered directly with SQL |
| `demand_parameter` (`emergency_beds`) | 5,479 | 8. Same survey, Ⅰ.B.5 special beds (특수병상), emergency beds only, 2016–2020 | City/county/district, province, and nation × year × institution type | Loading code no longer available |
| `demand_parameter` (`supply_beds`) | 12,063 | 9. National health care statistics, city/county/district volume, 2018 and 2022 editions, Table Ⅰ.B.1 licensed beds (국민보건의료실태통계 시군구편 허가병상수), 2011–2020 | City/county/district × year × institution type | Loading code no longer available |
| `demand_parameter` (`emergency_death`) | 23,982 | 10. Causes of death statistics, deaths by city/county/district and cause, seven items for the three major emergency conditions (사망원인통계), Statistics Korea, 2010–2024 | City/county/district × year × cause of death | Loading code no longer available |
| `health_facility_inventory` | 384 | 11. Medical institution registry (의료기관 신고자료), Health Insurance Review and Assessment Service, as of December 2025 | 47 tertiary general hospitals and 337 general hospitals. Name, type, city/county/district, numbers of beds and physicians, opening date | Entered directly with SQL |

In addition to the row counts above, `demand_parameter` contains 157 supplementary rows for administrative boundary changes (Section 2).

### Distance and travel time

| Table | Rows | Data | Content | Loading |
|---|---:|---|---|---|
| `case_distance_time_v3` | 687 | 13. Kakao Mobility directions API | 3 cases × 229 cities/counties/districts. Straight-line distance (haversine), road distance, travel time by car, destination office, departure time | `preprocessing/find_office_coordinates.py` → `preprocessing/measure_distance.py` |

Office coordinates are obtained with the keyword search of the Kakao Local API. For Gangbuk-gu (Seoul) and Cheongju-si (Chungbuk), whose offices are being relocated or are split across sites, the addresses in `preprocessing/cityhall_overrides.csv` were used as reference points. The coordinates in that file are left blank, so they must be filled in for those addresses before measuring.

### Values taken from the guideline and an original report

| Table | Rows | Data | Content | Loading |
|---|---:|---|---|---|
| `kdi_reference_values` | 182 | 14. Medical-sector guideline (2022), Tables Ⅲ-9, Ⅲ-11, Ⅲ-14, and the national average in Method 2 of Section 3 | Tables Ⅲ-9 and Ⅲ-11: 90 rows each of province-level values; the others: 1 row each | Entered directly with SQL |
| `police_demand_input` | 17 | 15. PFS report for the Asan Police Hospital, Tables Ⅲ-10, Ⅲ-13, Ⅲ-23, Ⅲ-39, Ⅲ-43 | Projected number of police officers and related values, stored with their table numbers | Loading code no longer available |
| `project_config` | 90 | 16. Project overview (the project-plan part of each case's original report) | Case × item (project name, location, number of beds, opening year, survey year, project background and purpose, etc.) | Entered directly with SQL |

### Tables the code reads although their values are not used in calculation

| Table | Rows | Data | What the code reads | Loading |
|---|---:|---|---|---|
| `bed_utilization` | 3,103 | Fifth National Health and Medical Service Survey, Ⅱ.A.11 bed utilization by institution location and type (병상이용률), 2016–2020 | The Period agent reads the names of the bed-size bands (`facility_size`) for each institution type. The utilization values are not used | Loading code no longer available |
| `catchment_scheme_assignment` | 229 | City/county/district composition of the survey's catchment areas | Queried by the Period agent; the result is not used in calculation | Entered directly with SQL |
| `region_administrative_center` | 264 | List of cities/counties/districts | The distance measurement script reads the administrative codes and names of the units to measure | Entered directly with SQL |

## 2. Supplements for administrative boundary changes

`preprocessing/supplement_regions.py` adds the following rows (existing rows are not modified). Added rows carry a `[보충: …]` suffix in the `source` column.

- Inpatient and outpatient visit days of Sejong, 2013–2024: extracted from the "세종" row of the original yearbook tables (`preprocessing/extract_yearbook_district.py`).
- Values for 2013–2017 of Michuhol-gu, Incheon (renamed from Nam-gu in 2018): rows under the former code copied to the current code.
- Values up to 2022 of Gunwi-gun, Daegu (transferred from Gyeongbuk in 2023): rows under the former code copied to the current code.

## 3. Tables without loading code

The tables marked "Entered directly with SQL" or "Loading code no longer available" were filled by converting the source data with spreadsheet tools and SQL, so this repository has no script that rebuilds them from the source data. To rebuild them, convert the source data to match the column definitions in `schema.sql` and the descriptions above. Write the data name and table number in the `source` column of each row.

## 4. Source labels

The source names that appear in `data_records` and in the evidence markers of the run records are the strings stored in the `source` column of the database. For the National Health Insurance Statistical Yearbook, some labels read "NHIS" or "국민건강보험공단".
