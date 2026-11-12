-- project_data.db (SQLite): definitions of the tables read by the code. No data rows.

CREATE TABLE bed_utilization (
    catchment_scheme TEXT NOT NULL,
    catchment_id     TEXT NOT NULL,
    reference_year   INTEGER NOT NULL,
    facility_type    TEXT NOT NULL,
    facility_size    TEXT NOT NULL DEFAULT 'N/A',
    utilization_rate REAL NOT NULL,
    source           TEXT NOT NULL,
    inserted_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (catchment_scheme, catchment_id, reference_year, facility_type, facility_size)
);

CREATE TABLE case_distance_time_v3 (
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
    );

CREATE TABLE catchment_scheme_assignment (
    scheme           TEXT    NOT NULL,
    catchment_id     TEXT    NOT NULL,
    region_code      TEXT    NOT NULL,
    source           TEXT    NOT NULL,
    reference_date   TEXT,
    inserted_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (scheme, catchment_id, region_code),
    FOREIGN KEY (region_code) REFERENCES region_master(region_code)
);

CREATE INDEX idx_catchment_lookup ON catchment_scheme_assignment(scheme, catchment_id);

CREATE INDEX idx_catchment_region ON catchment_scheme_assignment(region_code);

CREATE INDEX idx_catchment_scheme ON catchment_scheme_assignment(scheme);

CREATE TABLE demand_parameter (
    scope          TEXT    NOT NULL,
    region_code    TEXT,
    reference_year INTEGER NOT NULL,
    parameter      TEXT    NOT NULL,
    facility_type  TEXT    NOT NULL DEFAULT 'N/A',
    value          REAL    NOT NULL,
    unit           TEXT    NOT NULL,
    source         TEXT    NOT NULL,
    inserted_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (scope, region_code, reference_year, parameter, facility_type)
    );

CREATE TABLE health_facility_inventory (
    facility_id          TEXT    PRIMARY KEY,
    facility_name        TEXT    NOT NULL,
    facility_type        TEXT    NOT NULL,
    region_code          TEXT    NOT NULL,
    address              TEXT,
    latitude             REAL,
    longitude            REAL,
    coordinate_system    TEXT    DEFAULT 'WGS84_estimated',
    total_beds           INTEGER,
    physician_count      INTEGER,
    nurse_count          INTEGER,
    is_public            INTEGER CHECK (is_public IN (0, 1)),
    establishment_date   TEXT,
    reference_date       TEXT    NOT NULL,
    source               TEXT    NOT NULL,
    inserted_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (region_code) REFERENCES region_master(region_code)
);

CREATE INDEX idx_facility_region ON health_facility_inventory(region_code);

CREATE INDEX idx_facility_type   ON health_facility_inventory(facility_type);

CREATE TABLE inpatient_by_age_facility (
    region_code   TEXT    NOT NULL,
    reference_year INTEGER NOT NULL,
    facility_type TEXT    NOT NULL,
    facility_size TEXT    NOT NULL DEFAULT 'N/A',
    sex           TEXT    NOT NULL CHECK (sex IN ('M','F','T')),
    age_group     TEXT    NOT NULL,
    metric        TEXT    NOT NULL
    CHECK (metric IN ('PATIENT_COUNT','INPATIENT_DAYS','OUTPATIENT_DAYS','TOTAL_COST')),
    value         REAL,
    unit          TEXT    NOT NULL,
    source        TEXT    NOT NULL,
    inserted_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (region_code, reference_year, facility_type, facility_size, sex, age_group, metric),
    FOREIGN KEY (region_code) REFERENCES region_master(region_code)
    );

CREATE TABLE kdi_reference_values (
    source_table   TEXT    NOT NULL,   -- 출처 표 번호 ('표 Ⅲ-9', '표 Ⅲ-11', ...)
    region         TEXT    NOT NULL,   -- 시·도명 ('전국', '충남', '경기', ...) 또는 '전국'
    facility_type  TEXT    NOT NULL,   -- 시설 분류 ('계', '권역응급의료센터', ...)
    metric         TEXT    NOT NULL,   -- 측정 대상 ('시설수' / '병상수')
    source_version TEXT    NOT NULL,   -- KDI 지침 버전 ('202212')
    value          NUMERIC,            -- 값 (NULL 허용 — 표의 빈 셀 표현)
    note           TEXT,               -- 자유 메모 (예: '세종은 응급의료기관 미운영')
    inserted_at    TEXT    DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (source_table, region, facility_type, metric, source_version)
);

CREATE TABLE police_demand_input (
    project_id     TEXT NOT NULL,
    source_table   TEXT NOT NULL,   -- 원문 표번호 (예: 'III-13')
    source_title   TEXT NOT NULL,   -- 표 제목
    reference_year INTEGER,         -- 연도 (평균/추계는 NULL 가능)
    item           TEXT NOT NULL,   -- 항목명(원문 라벨 그대로)
    value          REAL,
    unit           TEXT,
    note           TEXT,            -- 출처/단서(결론 멘트 아님)
    inserted_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE project_config (
    project_id  TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT,
    PRIMARY KEY (project_id, key)
    );

CREATE TABLE region_administrative_center (
    region_code       TEXT    PRIMARY KEY,
    center_name       TEXT    NOT NULL,
    address           TEXT,
    latitude          REAL    NOT NULL,
    longitude         REAL    NOT NULL,
    coordinate_system TEXT    DEFAULT 'WGS84',
    source            TEXT    NOT NULL,
    inserted_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (region_code) REFERENCES region_master(region_code)
);

CREATE TABLE region_master (
    region_code     TEXT    PRIMARY KEY,    -- '00000' 전국 / 'XX000' 광역 / 'XXYYY' 시군구
    region_kind     TEXT    NOT NULL        -- ★ rev3 변경: region_level → region_kind
    CHECK (region_kind IN ('NATION', 'METROPOLITAN', 'MOTHER', 'DISTRICT', 'CITY')),
    -- NATION: 전국 (1행)
    -- METROPOLITAN: 광역시·도 + 세종특별자치시(광역 단위) (17행)
    -- MOTHER: 자치구를 둔 도(道) 모-시 (13행, 하드코딩 리스트)
    -- DISTRICT: 자치구 (광역시 자치구 + 도 모-시 자치구)
    -- CITY: 자치구 없는 일반 행정구역 (도 시·군 + 광역시 직속 군 + 세종 36110)
    region_name     TEXT    NOT NULL,       -- '전국' / '서울특별시' / '서울특별시 종로구'
    short_name      TEXT,                   -- '전국' / '서울' / '종로구'
    parent_code     TEXT,                   -- 상위 region_code
    province_code   TEXT,                   -- 광역 코드 (광역은 자기 자신)
    source          TEXT    NOT NULL,       -- '행정안전부 법정동코드 (전체자료)'
    valid_from      TEXT,                   -- 'YYYY-MM-DD'
    valid_to        TEXT,                   -- NULL이면 현재 유효
    inserted_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (parent_code) REFERENCES region_master(region_code)
);

CREATE INDEX idx_region_kind     ON region_master(region_kind);

CREATE INDEX idx_region_province ON region_master(province_code);

CREATE TABLE region_population_structure (
    region_code              TEXT    NOT NULL,
    reference_year           INTEGER NOT NULL,
    male_population          INTEGER,
    female_population        INTEGER,
    pop_under_65             INTEGER,
    pop_65_and_over          INTEGER,
    health_insured_total     INTEGER,
    health_insured_workplace INTEGER,
    workplace_insured        INTEGER,
    workplace_dependent      INTEGER,
    region_insured           INTEGER,
    region_households        INTEGER,
    medical_aid              INTEGER,
    disabled_severe          INTEGER,
    disabled_mild            INTEGER,
    source                   TEXT    NOT NULL,
    inserted_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (region_code, reference_year, source),
    FOREIGN KEY (region_code) REFERENCES region_master(region_code)
);

CREATE INDEX idx_pop_struct_region ON region_population_structure(region_code);

CREATE INDEX idx_pop_struct_year   ON region_population_structure(reference_year);

CREATE TABLE region_population_timeseries (
    region_code     TEXT    NOT NULL,
    year            INTEGER NOT NULL,
    data_type       TEXT    NOT NULL
    CHECK (data_type IN ('OBSERVED', 'PROJECTED')),
    sex             TEXT    NOT NULL
    CHECK (sex IN ('T', 'M', 'F')),
    age_group       TEXT    NOT NULL,
    population      INTEGER NOT NULL,
    source          TEXT    NOT NULL,
    reference_date  TEXT,
    inserted_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP, vintage TEXT,
    PRIMARY KEY (region_code, year, data_type, source, sex, age_group),
    FOREIGN KEY (region_code) REFERENCES region_master(region_code)
);

CREATE INDEX idx_pop_ts_region    ON region_population_timeseries(region_code);

CREATE INDEX idx_pop_ts_sex_age   ON region_population_timeseries(sex, age_group);

CREATE INDEX idx_pop_ts_type      ON region_population_timeseries(data_type);

CREATE INDEX idx_pop_ts_year      ON region_population_timeseries(year);

CREATE TABLE region_relevance_index (
    region_code              TEXT    NOT NULL,
    reference_year           INTEGER NOT NULL,
    patient_type             TEXT    NOT NULL
    CHECK (patient_type IN ('ALL', 'GENERAL', 'PSYCHIATRIC', 'REHABILITATION', 'LONG_TERM_CARE', 'OTHER')),
    facility_type            TEXT    NOT NULL DEFAULT 'N/A',
    metric                   TEXT    NOT NULL
    CHECK (metric IN ('SELF_SUFFICIENCY', 'COMPOSITION_RATIO', 'OUTFLOW_RATIO')),
    value                    REAL    NOT NULL,
    unit                     TEXT    NOT NULL
    CHECK (unit IN ('PCT', 'RATIO')),
    source                   TEXT    NOT NULL,
    inserted_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (region_code, reference_year, patient_type, facility_type, metric),
    FOREIGN KEY (region_code) REFERENCES region_master(region_code)
);

CREATE INDEX idx_ri_region ON region_relevance_index(region_code);

CREATE INDEX idx_ri_year   ON region_relevance_index(reference_year);

CREATE TABLE survey5_beds (
    region_code TEXT, reference_year INTEGER,
    facility_type TEXT, facility_size TEXT,
    category TEXT, licensed_beds REAL, source TEXT,
    PRIMARY KEY (region_code, reference_year, facility_type,
    facility_size, category));

CREATE TABLE survey5_inpatient_days (
    region_code TEXT, reference_year INTEGER,
    facility_type TEXT, facility_size TEXT,
    category TEXT, total_days REAL, source TEXT,
    PRIMARY KEY (region_code, reference_year, facility_type,
    facility_size, category));
