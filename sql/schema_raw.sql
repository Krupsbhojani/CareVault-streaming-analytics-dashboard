-- CareVault raw (batch) schema. Mirrors docs/schema-proposal.md §1-§4.
-- Loaded once from source files; never mutated at runtime.

CREATE SCHEMA IF NOT EXISTS raw;

-- ---------------------------------------------------------------------------
-- readings_raw: one row per actual reading, per patient.
-- Grain: (patient_id, ts_utc). No re-gridding across cohorts; sampling_sec
-- distinguishes cadence. AZT1D duplicates are collapsed per R1 aggregation.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.readings_raw (
    patient_id                              text        NOT NULL,
    visit_id                                text        NOT NULL,
    ts_utc                                  timestamptz NOT NULL,
    ts_local                                timestamp   NOT NULL,
    source_tz                               text        NOT NULL,
    local_hour                              smallint    NOT NULL,
    sampling_sec                            smallint    NOT NULL,
    cgm_mgdl                                real        NOT NULL,
    is_clamped                              boolean     NOT NULL,
    bolus_u                                 real,
    basal_rate_u_hr                         real,
    insulin_iv_u                            real,
    carb_g                                  real,
    carb_free_text                          text,
    device_mode                             text,
    bolus_type                              text,
    diabetes_type                           text        NOT NULL,
    cohort                                  text        NOT NULL,
    glucose_source_labeled_bgm_possible     boolean     NOT NULL,
    CONSTRAINT pk_readings_raw PRIMARY KEY (patient_id, ts_utc),
    CONSTRAINT ck_cohort        CHECK (cohort IN ('AZT1D', 'Shanghai')),
    CONSTRAINT ck_diabetes_type CHECK (diabetes_type IN ('T1', 'T2')),
    CONSTRAINT ck_device_mode   CHECK (device_mode IS NULL OR device_mode IN ('normal', 'sleep', 'exercise', 'unknown')),
    CONSTRAINT ck_sampling_sec  CHECK (sampling_sec IN (300, 900)),
    CONSTRAINT ck_local_hour    CHECK (local_hour BETWEEN 0 AND 23)
);

CREATE INDEX IF NOT EXISTS ix_readings_raw_cohort_ts ON raw.readings_raw (cohort, ts_utc);
CREATE INDEX IF NOT EXISTS ix_readings_raw_visit    ON raw.readings_raw (visit_id);

-- ---------------------------------------------------------------------------
-- patient: one row per unique patient. Populated from Shanghai summary sheets
-- + minimal (id, cohort, diabetes_type) rows for AZT1D subjects. This DDL is
-- present per schema-proposal.md §4; loader/load_raw.py does not populate it
-- (readings_raw only). Downstream loaders will fill it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.patient (
    patient_id                              text        PRIMARY KEY,
    cohort                                  text        NOT NULL,
    diabetes_type                           text        NOT NULL,
    age_years                               smallint,
    sex                                     text,
    height_m                                real,
    weight_kg                               real,
    bmi                                     real,
    smoking_pack_years                      real,
    alcohol_use                             text,
    duration_diabetes_years                 real,
    hba1c_mmol_mol                          real,
    glycated_albumin_pct                    real,
    fpg_mgdl                                real,
    ppg2h_mgdl                              real,
    c_peptide_fast_nmol_l                   real,
    c_peptide_2h_nmol_l                     real,
    insulin_fast_pmol_l                     real,
    insulin_2h_pmol_l                       real,
    tc_mmol_l                               real,
    tg_mmol_l                               real,
    hdl_mmol_l                              real,
    ldl_mmol_l                              real,
    creatinine_umol_l                       real,
    egfr_ml_min_1_73m2                      real,
    uric_acid_mmol_l                        real,
    bun_mmol_l                              real,
    has_acute_complications                 boolean,
    has_macrovascular                       boolean,
    has_microvascular                       boolean,
    has_comorbidities                       boolean,
    patient_notes                           jsonb,
    hypoglycemia_yes_no                     boolean,
    CONSTRAINT ck_patient_cohort        CHECK (cohort IN ('AZT1D', 'Shanghai')),
    CONSTRAINT ck_patient_diabetes_type CHECK (diabetes_type IN ('T1', 'T2')),
    CONSTRAINT ck_patient_sex           CHECK (sex IS NULL OR sex IN ('F', 'M'))
);

-- ---------------------------------------------------------------------------
-- medication: parsed rows from Hypoglycemic Agents / Other Agents free text.
-- Grain: (patient_id, visit_id, drug_canonical). Unmapped drugs go to
-- medication_unmapped; do not launder them into 'other'.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.medication (
    medication_id       bigserial   PRIMARY KEY,
    patient_id          text        NOT NULL,
    visit_id            text        NOT NULL,
    source_column       text        NOT NULL,
    drug_raw            text        NOT NULL,
    drug_canonical      text        NOT NULL,
    drug_class          text        NOT NULL,
    route               text,
    notes               text,
    CONSTRAINT ck_med_source_column CHECK (source_column IN ('hypoglycemic', 'other', 'per_visit_non_insulin')),
    CONSTRAINT ck_med_route         CHECK (route IS NULL OR route IN ('subcutaneous', 'oral', 'intravenous', 'inhaled', 'topical', 'other'))
);

CREATE INDEX IF NOT EXISTS ix_medication_patient ON raw.medication (patient_id);
CREATE INDEX IF NOT EXISTS ix_medication_class   ON raw.medication (drug_class);

CREATE TABLE IF NOT EXISTS raw.medication_unmapped (
    medication_unmapped_id  bigserial   PRIMARY KEY,
    patient_id              text        NOT NULL,
    visit_id                text        NOT NULL,
    source_column           text        NOT NULL,
    drug_raw                text        NOT NULL,
    first_seen_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_unmapped_source_column CHECK (source_column IN ('hypoglycemic', 'other', 'per_visit_non_insulin'))
);

CREATE INDEX IF NOT EXISTS ix_medication_unmapped_raw ON raw.medication_unmapped (drug_raw);
