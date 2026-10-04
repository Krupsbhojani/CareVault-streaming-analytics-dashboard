-- CareVault live schema. Tables the replayer and consumers touch.
-- Local Postgres for now; Azure comes later.

CREATE SCHEMA IF NOT EXISTS live;

-- ---------------------------------------------------------------------------
-- virtual_patient: one row per simulated patient in a replay run.
-- (source_trace_start_utc, speed_factor, seed) fully determine a replay when
-- combined with the loader-managed source trace.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS live.virtual_patient (
    virtual_patient_id          text        PRIMARY KEY,
    source_patient_id           text        NOT NULL,
    source_trace_start_utc      timestamptz NOT NULL,
    time_offset_seconds         integer     NOT NULL DEFAULT 0,
    speed_factor                real        NOT NULL DEFAULT 1.0,
    seed                        bigint      NOT NULL,
    synthetic_vitals_enabled    boolean     NOT NULL DEFAULT TRUE,
    injection_profile           text        NOT NULL DEFAULT 'eval_v1',
    created_at                  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_vp_speed CHECK (speed_factor > 0)
);

-- ---------------------------------------------------------------------------
-- device: one simulated device per virtual patient (1:1 for now).
-- firmware_offset_mgdl carries the "firmware offset on one device batch" fault.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS live.device (
    device_id                   text        PRIMARY KEY,
    virtual_patient_id          text        NOT NULL REFERENCES live.virtual_patient(virtual_patient_id) ON DELETE CASCADE,
    device_type                 text        NOT NULL,
    firmware_version            text,
    firmware_offset_mgdl        real        NOT NULL DEFAULT 0.0,
    active_from                 timestamptz NOT NULL DEFAULT now(),
    active_to                   timestamptz,
    CONSTRAINT ck_device_type CHECK (device_type IN ('cgm_dexcom_g6','cgm_dexcom_g7','cgm_libre','pump','smartwatch'))
);
CREATE INDEX IF NOT EXISTS ix_device_vp ON live.device (virtual_patient_id);

-- ---------------------------------------------------------------------------
-- vitals_readings: partitioned daily on event_time (UTC). BRIN on event_time,
-- UNIQUE (virtual_patient_id, event_time) enforces de-dup at insert time.
-- Partition boundaries are (day, day+1) UTC.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS live.vitals_readings (
    virtual_patient_id          text        NOT NULL,
    device_id                   text,
    event_time                  timestamptz NOT NULL,
    ingested_at                 timestamptz NOT NULL DEFAULT now(),
    cgm_mgdl                    real,
    heart_rate_bpm              smallint,
    systolic_bp_mmhg            smallint,
    diastolic_bp_mmhg           smallint,
    spo2_pct                    real,
    injected_type               text,
    sampling_sec                smallint    NOT NULL,
    source_trace_ref            text,
    CONSTRAINT ck_vitals_injected_type CHECK (
        injected_type IS NULL OR injected_type IN ('hypo','hyper','sensor_dropout','spike','stuck','firmware_offset')
    )
) PARTITION BY RANGE (event_time);

-- Indexes on the partitioned parent propagate to all current/future partitions.
CREATE UNIQUE INDEX IF NOT EXISTS ux_vitals_vp_event
    ON live.vitals_readings (virtual_patient_id, event_time);
CREATE INDEX IF NOT EXISTS ix_vitals_event_brin
    ON live.vitals_readings USING BRIN (event_time);

-- Helper: idempotently create the partition covering a given UTC day.
-- Yields to pg_partman when the extension is registered for the parent table
-- (partman's naming scheme differs — vitals_readings_p20260926 — and would
-- otherwise overlap-conflict with a DIY vitals_readings_20260926 for the
-- same day). Falls back to a DIY partition on vanilla images.
CREATE OR REPLACE FUNCTION live.ensure_vitals_partition(day date) RETURNS void AS $$
DECLARE
    p_name text := 'vitals_readings_' || to_char(day, 'YYYYMMDD');
    partman_owns boolean := false;
BEGIN
    BEGIN
        SELECT true INTO partman_owns
          FROM partman.part_config
         WHERE parent_table = 'live.vitals_readings'
         LIMIT 1;
    EXCEPTION WHEN undefined_table OR invalid_schema_name THEN
        partman_owns := false;
    END;
    IF partman_owns THEN
        RETURN;
    END IF;
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS live.%I PARTITION OF live.vitals_readings FOR VALUES FROM (%L) TO (%L)',
        p_name, day::timestamptz, (day + 1)::timestamptz
    );
END;
$$ LANGUAGE plpgsql;

-- pg_partman: preferred path for scheduled partition maintenance.
-- The local image (docker/Dockerfile.postgres) ships postgresql-18-partman;
-- Azure Flexible Server has the extension available too. If it is missing
-- (vanilla image), the DIY ensure_vitals_partition above still creates day
-- partitions on demand.
--
-- Note: partman.create_parent conflicts with any existing manually-created
-- partition covering the same day, so a mid-flight switch from DIY -> partman
-- requires dropping the DIY partitions first. A fresh install has no such
-- conflict.
CREATE SCHEMA IF NOT EXISTS partman;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_partman') THEN
        CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman;
        IF NOT EXISTS (
            SELECT 1 FROM partman.part_config WHERE parent_table = 'live.vitals_readings'
        ) THEN
            PERFORM partman.create_parent(
                p_parent_table    := 'live.vitals_readings',
                p_control         := 'event_time',
                p_interval        := '1 day',
                p_premake         := 4
            );
        END IF;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- alerts: every alert the alerting service emitted.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS live.alerts (
    alert_id                    bigserial   PRIMARY KEY,
    virtual_patient_id          text        NOT NULL,
    emitted_at                  timestamptz NOT NULL DEFAULT now(),
    trigger_event_time          timestamptz NOT NULL,
    alert_type                  text        NOT NULL,
    severity                    text        NOT NULL,
    rule_id                     text        NOT NULL,
    detector                    text        NOT NULL DEFAULT 'rules',
    score                       real,
    payload_json                jsonb,
    acked_at                    timestamptz,
    acked_by                    text,
    CONSTRAINT ck_alert_type     CHECK (alert_type IN ('hypo','hyper','sensor_dropout','rapid_change','custom')),
    CONSTRAINT ck_alert_severity CHECK (severity IN ('info','warn','critical')),
    CONSTRAINT ck_alert_detector CHECK (detector IN ('rules','mad','cross_signal','batch_analysis'))
);
-- Backfill columns on already-existing DBs (idempotent).
ALTER TABLE live.alerts ADD COLUMN IF NOT EXISTS detector text NOT NULL DEFAULT 'rules';
ALTER TABLE live.alerts ADD COLUMN IF NOT EXISTS score    real;
CREATE INDEX IF NOT EXISTS ix_alerts_vp_time  ON live.alerts (virtual_patient_id, emitted_at);
CREATE INDEX IF NOT EXISTS ix_alerts_trigger  ON live.alerts (virtual_patient_id, trigger_event_time);

-- ---------------------------------------------------------------------------
-- predictions: model outputs, joined to vitals_readings for eval.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS live.predictions (
    prediction_id               bigserial   PRIMARY KEY,
    virtual_patient_id          text        NOT NULL,
    model_name                  text        NOT NULL,
    model_version               text        NOT NULL,
    -- input_window_end is the event_time of the reading the prediction was
    -- MADE FROM (the source row). predicted_ts_utc = input_window_end +
    -- horizon_seconds is the target time the forecast is for.
    input_window_end            timestamptz NOT NULL,
    horizon_seconds             integer     NOT NULL,
    predicted_ts_utc            timestamptz NOT NULL,
    predicted_cgm_mgdl          real,
    predicted_low               real,
    predicted_high              real,
    -- Populated when the label arrives (a reading at ~predicted_ts_utc).
    actual_cgm_mgdl             real,
    abs_error_mgdl              real,
    resolved_at                 timestamptz,
    latency_ms                  integer,
    emitted_at                  timestamptz NOT NULL DEFAULT now()
);
-- Backfill columns on already-existing DBs (idempotent).
ALTER TABLE live.predictions ADD COLUMN IF NOT EXISTS actual_cgm_mgdl real;
ALTER TABLE live.predictions ADD COLUMN IF NOT EXISTS abs_error_mgdl  real;
ALTER TABLE live.predictions ADD COLUMN IF NOT EXISTS resolved_at     timestamptz;
CREATE INDEX IF NOT EXISTS ix_predictions_vp_time ON live.predictions (virtual_patient_id, predicted_ts_utc);
CREATE INDEX IF NOT EXISTS ix_predictions_model   ON live.predictions (model_name, resolved_at);
-- Idempotent reprocessing: same source reading + same model overwrites.
CREATE UNIQUE INDEX IF NOT EXISTS ux_predictions_vp_source_model
    ON live.predictions (virtual_patient_id, input_window_end, model_name);

-- ---------------------------------------------------------------------------
-- vitals_1min: 1-minute rollups. Populated by a downstream aggregator, not by
-- the replayer itself. DDL present so consumers can reference it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS live.vitals_1min (
    virtual_patient_id          text        NOT NULL,
    minute_start_utc            timestamptz NOT NULL,
    cgm_mgdl_mean               real,
    cgm_mgdl_min                real,
    cgm_mgdl_max                real,
    heart_rate_bpm_mean         real,
    systolic_bp_mmhg_mean       real,
    diastolic_bp_mmhg_mean      real,
    spo2_pct_mean               real,
    n_readings                  integer     NOT NULL,
    PRIMARY KEY (virtual_patient_id, minute_start_utc)
);

-- ---------------------------------------------------------------------------
-- dead_letter: rows that failed validation or serialization on the way in.
-- Keep raw payload so the failure is reproducible.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS live.dead_letter (
    dead_letter_id              bigserial   PRIMARY KEY,
    received_at                 timestamptz NOT NULL DEFAULT now(),
    virtual_patient_id          text,
    device_id                   text,
    raw_payload                 jsonb       NOT NULL,
    reason                      text        NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_dead_letter_time   ON live.dead_letter (received_at);
CREATE INDEX IF NOT EXISTS ix_dead_letter_reason ON live.dead_letter (reason);

-- ---------------------------------------------------------------------------
-- worker_state: per-worker cursor into each vp's source trace.
-- Enables resumable replays.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS live.worker_state (
    worker_id                   text        NOT NULL,
    virtual_patient_id          text        NOT NULL,
    last_source_ts_utc          timestamptz,
    last_event_time             timestamptz,
    n_emitted                   integer     NOT NULL DEFAULT 0,
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (worker_id, virtual_patient_id)
);

-- ---------------------------------------------------------------------------
-- worker_cursor: per-worker high-water mark on (ingested_at, virtual_patient_id).
-- The virtual_patient_id tiebreaker makes the fetch deterministic even when a
-- full FETCH_LIMIT chunk shares an ingested_at — otherwise the cursor could
-- stall at the boundary (observed on Azure 2026-10-03/04 when the step-4
-- benchmark burst-wrote >FETCH_LIMIT rows into a single overlap window).
-- Per-vp dedup still happens via live.worker_state.last_event_time.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS live.worker_cursor (
    worker_id                   text        PRIMARY KEY,
    last_ingested_at            timestamptz NOT NULL,
    last_virtual_patient_id     text        NOT NULL DEFAULT '',
    updated_at                  timestamptz NOT NULL DEFAULT now()
);
-- Backfill for existing databases (idempotent). Default '' is the lexically
-- smallest text, so an upgraded cursor starts "before" any real vp_id at the
-- same ingested_at — correct re-entry point.
ALTER TABLE live.worker_cursor
    ADD COLUMN IF NOT EXISTS last_virtual_patient_id text NOT NULL DEFAULT '';

-- ---------------------------------------------------------------------------
-- processor_role: the role the stream processor connects as.
-- Column-level SELECT on vitals_readings explicitly excludes injected_type,
-- so the processor cannot use ground-truth labels even by accident.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'processor_role') THEN
        CREATE ROLE processor_role LOGIN PASSWORD 'processor';
    END IF;
END $$;

GRANT USAGE ON SCHEMA live TO processor_role;
GRANT SELECT (virtual_patient_id, device_id, event_time, ingested_at,
              cgm_mgdl, heart_rate_bpm, systolic_bp_mmhg, diastolic_bp_mmhg,
              spo2_pct, sampling_sec, source_trace_ref)
    ON live.vitals_readings TO processor_role;
-- Intentionally NOT granting SELECT (injected_type).
GRANT SELECT, INSERT ON live.alerts TO processor_role;
GRANT USAGE ON SEQUENCE live.alerts_alert_id_seq TO processor_role;
GRANT SELECT, INSERT, UPDATE ON live.worker_state  TO processor_role;
GRANT SELECT, INSERT, UPDATE ON live.worker_cursor TO processor_role;
GRANT SELECT, INSERT, UPDATE ON live.vitals_1min   TO processor_role;
GRANT SELECT ON live.virtual_patient TO processor_role;
GRANT SELECT ON live.device          TO processor_role;
-- ML pipeline needs source-level metadata (source_tz, diabetes_type,
-- sampling_sec) per vp at startup. raw.readings_raw has no injected_type
-- column (that's a live-side ground-truth field only), so this doesn't
-- weaken the label-leakage guarantee.
GRANT USAGE ON SCHEMA raw TO processor_role;
GRANT SELECT ON raw.readings_raw TO processor_role;

-- ---------------------------------------------------------------------------
-- dashboard_ro: read-only role for the operator dashboard.
-- Column-level grants exclude live.vitals_readings.injected_type so the
-- dashboard cannot surface ground-truth labels (matches processor_role).
-- Mirror this exact block on Azure Flexible Server (roles do not migrate
-- with schema-only pg_dump).
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_ro') THEN
        CREATE ROLE dashboard_ro LOGIN PASSWORD 'dashboard';
    END IF;
END $$;

GRANT USAGE ON SCHEMA live TO dashboard_ro;
GRANT SELECT (virtual_patient_id, device_id, event_time, ingested_at,
              cgm_mgdl, heart_rate_bpm, systolic_bp_mmhg, diastolic_bp_mmhg,
              spo2_pct, sampling_sec, source_trace_ref)
    ON live.vitals_readings TO dashboard_ro;
-- Intentionally NOT granting SELECT (injected_type).
GRANT SELECT ON live.alerts          TO dashboard_ro;
GRANT SELECT ON live.device          TO dashboard_ro;
GRANT SELECT ON live.virtual_patient TO dashboard_ro;
GRANT SELECT ON live.vitals_1min     TO dashboard_ro;
GRANT SELECT ON live.predictions     TO dashboard_ro;
-- System page reads: pipeline health (worker cursor + lag, dead letters,
-- partition sizes). None expose injected_type.
GRANT SELECT ON live.worker_cursor   TO dashboard_ro;
GRANT SELECT ON live.worker_state    TO dashboard_ro;
GRANT SELECT (dead_letter_id, received_at, virtual_patient_id, device_id, reason)
    ON live.dead_letter              TO dashboard_ro;
-- Patient page reads: insulin/carb events from the source trace (not
-- re-emitted into the live pipeline). Explicit column-level grant.
GRANT USAGE ON SCHEMA raw            TO dashboard_ro;
GRANT SELECT (patient_id, ts_utc, bolus_u, basal_rate_u_hr, insulin_iv_u, carb_g, bolus_type)
    ON raw.readings_raw              TO dashboard_ro;
-- One-person page reads short-horizon forecasts if the online model has
-- populated them. Currently empty (no producer yet), but the query still
-- runs so the page falls back to "not built yet" instead of erroring.
GRANT SELECT ON live.predictions     TO dashboard_ro;
-- Person context: minimal demographic columns the "Who needs attention"
-- table uses to say "Type 1, age 34, 12y" instead of an internal id.
-- Only non-sensitive summary columns; nothing linking to a real identity.
-- raw.patient is currently empty (schema-proposal §4: downstream loaders
-- will fill it). Grant is future-proof.
GRANT SELECT (patient_id, cohort, diabetes_type, age_years, sex, duration_diabetes_years)
    ON raw.patient                   TO dashboard_ro;
-- We also need diabetes_type + cohort per source patient, which today only
-- lives on raw.readings_raw rows. Add those columns to the existing grant.
GRANT SELECT (diabetes_type, cohort) ON raw.readings_raw TO dashboard_ro;
