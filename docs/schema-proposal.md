# Schema Proposal — CareVault CGM Warehouse

Prescriptive. Source-of-truth profiling is in `docs/data-inventory.md`; decisions here rest on the resolutions **R1** (dup-timestamp aggregation), **R2** (Subject 14 kept as CGM), and **R3** (DeviceMode mapping) recorded there. Fields marked **synth** are computed by the loader; **real** fields carry the source value unchanged (subject to R1 aggregation only).

## Two layers

The warehouse is split into two layers because the read patterns are different:

- **`readings_raw` (batch layer).** Immutable, one-time load from the source files. Written by the offline loader, **never touched at runtime.** Grain: one row per source reading, per patient. This is the historical corpus used to seed replays and train models.
- **`vitals_readings` (live layer).** Append-only Azure table written by the replayer as it streams data forward. Rows arrive at wall-clock time and carry ingest metadata (arrival time, source trace pointer). This is what the alerting and prediction services read.

Both layers use the same `cgm_mgdl` scale (`mg/dL`) and the same patient-ID convention (§4). The **virtual patients** driving the replay are recorded in `virtual_patient` with a source trace pointer, offset, speed factor, and RNG seed so any run is reproducible.

---

## 1. `readings_raw` — batch table, one row per actual reading

Grain: `(patient_id, ts_utc)`. **No re-gridding across datasets.** AZT1D is 5-min native; Shanghai is 15-min native. `sampling_sec` distinguishes cadence at query time. Cross-cohort variability metrics must window at ≥15 min to stay comparable.

| column | type | unit | AZT1D source | Shanghai source | transformation | real / synth |
|---|---|---|---|---|---|---|
| `patient_id` | string | - | `Subject N` → `az:N` | filename prefix → `sh:PPPP` | §4 | synth (prefix) |
| `visit_id` | string | - | `az:N/v0` | filename stem `sh:PPPP/PPPP_V_YYYYMMDD` | derived | synth (AZ) / real (SH) |
| `ts_utc` | timestamp | UTC | `EventDateTime` localized to `America/Phoenix` → UTC | `Date` localized to `Asia/Shanghai` → UTC | localize + convert | synth |
| `ts_local` | timestamp | naive local | `EventDateTime` | `Date` | pass-through | real |
| `source_tz` | string | IANA | `"America/Phoenix"` | `"Asia/Shanghai"` | injected from path | synth |
| `local_hour` | int | 0–23 | `ts_local.hour` | `ts_local.hour` | derived | synth |
| `sampling_sec` | int | s | `300` | `900` | injected from path | synth |
| `cgm_mgdl` | float | mg/dL | `CGM` (Subject 14: `Readings (CGM / BGM)` per R2) | `CGM (mg / dl)` | AZT1D: R1 aggregation (median per 5-min bucket if dup); Shanghai: pass-through, no interpolation | real |
| `is_clamped` | bool | - | `cgm_mgdl ≤ 40` **or** `cgm_mgdl ≥ 400` (fixed constants, Dexcom G6/G7 caps) | `cgm_mgdl ≤ 39.6` (fixed floor, no upper clamp on FreeStyle Libre used by Shanghai) | thresholded on **fixed constants per cohort**, not observed extremes | synth (censoring flag) |
| `bolus_u` | float | U | `TotalBolusInsulinDelivered` (R1 sum per bucket if dup) — see resolved tradeoff §5.R | `Insulin dose - s.c.` + `CSII - bolus insulin (Novolin R, IU)` | coalesce, sum for Shanghai only | real (AZ) / synth composite (SH) |
| `basal_rate_u_hr` | float | U/hr | `Basal`, first non-null per bucket (R1) | `CSII - basal insulin (Novolin R, IU / H)` | pass-through | real |
| `insulin_iv_u` | float | U | *not present* → NULL | `Insulin dose - i.v.` | pass-through | real (SH) / null (AZ) |
| `carb_g` | float | g | `CarbSize`, sum per bucket (R1) | *not directly available* → NULL | pass-through | real (AZ) / null (SH) |
| `carb_free_text` | string | - | *not present* → NULL | `Dietary intake` (drop duplicate `饮食`) | dedupe | real |
| `device_mode` | enum{`normal`,`sleep`,`exercise`,`unknown`} | - | R3 mapping of `DeviceMode` | *not present* → NULL | R3 | synth |
| `bolus_type` | enum | - | `BolusType` verbatim | *not present* → NULL | pass-through | real |
| `diabetes_type` | enum{`T1`,`T2`} | - | `T1` | `T1` or `T2` from path | injected | synth |
| `cohort` | enum{`AZT1D`,`Shanghai`} | - | `AZT1D` | `Shanghai` | injected | synth |
| `glucose_source_labeled_bgm_possible` | bool | - | `TRUE` for `az:14` only (per R2) | `FALSE` | flag | synth |

**Clamp constants** (fixed, not derived from data):

| cohort | device | lower clamp | upper clamp |
|---|---|---:|---:|
| AZT1D | Dexcom G6/G7 | 40 mg/dL | 400 mg/dL |
| Shanghai | FreeStyle Libre | 39.6 mg/dL (= 2.2 mmol/L × 18) | none |

Using fixed constants means a subject whose CGM never actually hits the cap still gets `is_clamped=FALSE` at 41 mg/dL, and a subject who bounces off the cap gets `is_clamped=TRUE` at exactly 40 — regardless of the observed max/min in *this* file. Observed extremes would silently mis-label edge subjects.

---

## 2. `vitals_readings` — live Azure table, append-only

Written by the replayer as it streams the batch corpus forward under a virtual clock. **Never mutated** after write.

| column | type | notes |
|---|---|---|
| `virtual_patient_id` | string | FK → `virtual_patient.virtual_patient_id`. Distinct from `patient_id` — many virtual patients can replay from the same source trace with different offsets. |
| `device_id` | string | FK → `device.device_id`. One virtual patient → one device (1:1 for now; keeps room for multi-device patients later). |
| `event_time` | timestamp | UTC. The moment in wall-clock time when the reading was *emitted* by the replayer (i.e. now + speed-adjusted delta from trace start). |
| `ingested_at` | timestamp | UTC. Server-side arrival timestamp — non-null even when the message is delayed by the transport. `ingested_at - event_time` is the ingest latency. |
| `cgm_mgdl` | float | mg/dL. From the source trace. |
| `heart_rate_bpm` | int | Synthesized (see §6 tradeoff about synthetic vitals). NULL when not generated for this virtual patient. |
| `systolic_bp_mmhg` | int | Synthesized. NULL when not generated. |
| `diastolic_bp_mmhg` | int | Synthesized. NULL when not generated. |
| `spo2_pct` | float | Synthesized. NULL when not generated. |
| `injected_type` | enum{`hypo`,`hyper`,`sensor_dropout`,`spike`, `null`} | Ground truth injection tag. **NULL when the reading is a clean replay** — any non-NULL value means the replayer injected an artificial event of that class at this moment. Used by evaluation, never by the alerting service. |
| `sampling_sec` | int | Source cadence propagated from `readings_raw`. |
| `source_trace_ref` | string | `"readings_raw:<patient_id>:<ts_utc>"` — pointer back to the exact batch row. Enables round-trip audits. |

Notes:
- The alerting and prediction services read this table by `virtual_patient_id, event_time` and MUST ignore `injected_type` (it's ground truth, not a signal). Enforce with column-level access if the Azure environment supports it.
- Ingested-order != event-time-order under replay lag. Downstream aggregations key on `event_time`; monitoring keys on `ingested_at`.

---

## 3. Supporting tables

### `virtual_patient` — reproducibility of a replay run

| column | type | notes |
|---|---|---|
| `virtual_patient_id` | string | e.g. `vp:001`, unique per replay session. |
| `source_patient_id` | string | FK → `readings_raw.patient_id`. Which real trace this vp is replaying. |
| `source_trace_start_utc` | timestamp | The `ts_utc` of the first replayed reading. Anything earlier in the trace is skipped. |
| `time_offset_seconds` | int | Added to `source_ts_utc` to compute `event_time`. Positive shifts the trace forward in wall time. |
| `speed_factor` | float | `1.0` = real-time; `60.0` = 1 minute of source per 1 second of wall clock. |
| `seed` | int | RNG seed for all synthetic vitals and injected events for this vp. Same `(source_trace_start_utc, speed_factor, seed)` → bit-identical replay. |
| `synthetic_vitals_enabled` | bool | Whether to generate heart rate / BP / SpO2. |
| `injection_profile` | string | Named policy for injecting `hypo`/`hyper`/`sensor_dropout`/`spike` events (e.g. `"eval_v1"`). |
| `created_at` | timestamp | UTC. |

### `device` — the simulated device attached to a virtual patient

| column | type | notes |
|---|---|---|
| `device_id` | string | e.g. `dev:vp001-cgm`. |
| `virtual_patient_id` | string | FK. |
| `device_type` | enum{`cgm_dexcom_g6`,`cgm_dexcom_g7`,`cgm_libre`,`pump`,`smartwatch`} | Determines nominal `sampling_sec` and clamp constants (per §1 table). |
| `firmware_version` | string | Free-text; useful when reproducing a bug. |
| `active_from`, `active_to` | timestamp | UTC, for device swaps mid-replay (rare). |

### `alerts` — every alert the alerting service emitted

| column | type | notes |
|---|---|---|
| `alert_id` | string | UUID. |
| `virtual_patient_id` | string | FK. |
| `emitted_at` | timestamp | UTC. |
| `trigger_event_time` | timestamp | UTC. The `event_time` of the reading that caused the alert. |
| `alert_type` | enum{`hypo`,`hyper`,`sensor_dropout`,`rapid_change`,`custom`} | |
| `severity` | enum{`info`,`warn`,`critical`} | |
| `rule_id` | string | Which rule fired. |
| `payload_json` | json | Full serialized rule input (last N readings, thresholds). |
| `acked_at`, `acked_by` | timestamp / string | NULL until acknowledged. |

Alert-vs-ground-truth join: `alerts.trigger_event_time = vitals_readings.event_time` → look up `injected_type` for precision/recall. Do this in the eval pipeline, not the alerting service.

### `predictions` — model outputs

| column | type | notes |
|---|---|---|
| `prediction_id` | string | UUID. |
| `virtual_patient_id` | string | FK. |
| `model_name`, `model_version` | string | e.g. `("glucose_forecast_lstm", "v3")`. |
| `input_window_end` | timestamp | UTC. Last `event_time` visible to the model. |
| `horizon_seconds` | int | e.g. `1800` for 30-min ahead. |
| `predicted_ts_utc` | timestamp | `input_window_end + horizon_seconds`. |
| `predicted_cgm_mgdl` | float | Point estimate. |
| `predicted_low`, `predicted_high` | float | Prediction interval (nullable). |
| `latency_ms` | int | Inference wall time. |
| `emitted_at` | timestamp | UTC. |

Prediction-vs-truth join: `predictions.predicted_ts_utc = vitals_readings.event_time`, evaluated once the true reading has arrived.

---

## 4. Patient dimension, medications, ID namespacing (unchanged)

See `patient` and `medication` sections below; conceptually the same as the prior draft. Included in full so this file is standalone.

### `patient` — one row per unique `patient_id`

Source: `Shanghai_T{1,2}DM_Summary.xlsx` (33 columns per row; one row per patient-visit → reduce to per-patient by keeping the **latest visit** for scalars and the **union** for morbidity flags). AZT1D contributes ID and cohort only.

Columns (Shanghai-only unless noted): `patient_id`, `diabetes_type` (both cohorts), `age_years`, `sex` (1→F, 2→M), `height_m`, `weight_kg`, `bmi`, `smoking_pack_years`, `alcohol_use` (drinker/non-drinker), `duration_diabetes_years` (**T1 header has two spaces before `(years)`; T2 has one — normalize snake_case before concat**), `hba1c_mmol_mol`, `glycated_albumin_pct`, `fpg_mgdl`, `ppg2h_mgdl`, `c_peptide_fast_nmol_l`, `c_peptide_2h_nmol_l`, `insulin_fast_pmol_l`, `insulin_2h_pmol_l` (**T1 uses `Insulin`, T2 uses `insulin` — normalize**), lipids (`tc`, `tg`, `hdl`, `ldl` mmol/L), renal (`creatinine_umol_l`, `egfr_ml_min_1_73m2`, `uric_acid_mmol_l`, `bun_mmol_l`), `has_acute_complications`, `has_macrovascular`, `has_microvascular`, `has_comorbidities` (bools: value ∉ {`none`, `""`, NULL}), `patient_notes` (JSON of the four morbidity free-text columns verbatim), `hypoglycemia_yes_no`.

If per-visit snapshots matter downstream, add `patient_visit` fact table with the same columns keyed by `visit_id`; `patient` stays one-row-per-patient.

### `medication` — decomposing `Hypoglycemic Agents` / `Other Agents`

Grain: `(patient_id, visit_id, drug_canonical)`.

Parsing: split raw text on `,` `;` `、` `，`; casefold; look up in `medication_dictionary.yaml` (canonical name → `drug_class` ∈ {`insulin_rapid`, `insulin_intermediate`, `insulin_long`, `insulin_premix`, `sulfonylurea`, `biguanide`, `dpp4`, `sglt2`, `glp1`, `alpha_glucosidase_inhibitor`, `thiazolidinedione`, `statin`, `ace_inhibitor`, `arb`, `ccb`, `beta_blocker`, `antiplatelet`, `anticoagulant`, `vitamin`, `other`}).

Unmapped tokens go to `medication_unmapped(patient_id, visit_id, drug_raw, source_column)` — **first-class table, not a debug log.** Do not launder ignorance as `other`.

Rows are visit-scoped (bind to `visit_id`). A patient with three visits and evolving therapy → three medication row sets, no collapse. AZT1D contributes zero rows.

### 4. Patient ID namespacing

Format: `<cohort>:<source_id>`.

- AZT1D: `az:1` … `az:25`.
- Shanghai T1DM: `sh:1001` … `sh:1012`.
- Shanghai T2DM: `sh:2000` … `sh:2099`.

The `1xxx`/`2xxx` Shanghai split *happens to* encode diabetes type; downstream code MUST join through `patient` for `diabetes_type` — do not switch on the numeric range.

`visit_id`: Shanghai = `sh:PPPP/PPPP_V_YYYYMMDD` (real); AZT1D = `az:N/v0` (synthesized single-visit).

Virtual patients live in a separate namespace (`vp:001`, `vp:002`, …) and reference `patient_id` via `virtual_patient.source_patient_id`. Never overload `patient_id` with virtual IDs.

---

## 5. Tradeoffs

### R. Resolved tradeoff — `bolus_u` composition (was open in prior draft)

Empirical check (`scripts/check_bolus.py`, 18,934 rows across all 25 AZT1D subjects where all three of `Total`, `Correction`, `Food` are non-null — and it is always all three or none):

| relation | rows | share |
|---|---:|---:|
| `Total == Correction + Food` (exact) | 16,998 | 89.8 % |
| `\|Total − (Correction + Food)\| ≤ 0.05 U` | 17,055 | 90.1 % |
| `Total > Correction + Food` by more than 0.05 U | 1,877 | 9.9 % |
| `Total < Correction + Food` by more than 0.05 U | 2 | 0.0 % |

Residual stats: mean `+0.27 U`, **median `0.00 U`**, min `−0.49`, max `+25.0`. Zero rows have any subset of the three non-null; it's always all-three or none.

**Decision: `bolus_u = TotalBolusInsulinDelivered` for AZT1D.** Summing all three would double-count 90% of boluses. The 9.9% where `Total > Correction + Food` are Extended / dual-wave boluses where `Total` captures the full committed delivery over the extension window (the `BolusType` string on those rows contains `Extended`); `Total` remains the right scalar.

### Open tradeoffs

1. **`DeviceMode = 0` for Subject 8 (5,262 rows).** Undocumented value, exclusive to one subject. Mapped to `unknown` in R3. Fold into `normal` only if the AZT1D manuscript confirms.
2. **`NaN → normal` for DeviceMode.** Inferred, not documented. If wrong, ~239 k rows re-label.
3. **Shanghai timezone.** Assumed `Asia/Shanghai`. Libre exports are sometimes device-UTC — confirm before any circadian analysis publishes.
4. **Clamp policy downstream.** Schema flags `is_clamped`; the loader takes no side on whether GMI should exclude clamped rows or treat them as censored.
5. **Shanghai visit chaining.** Same-patient visits weeks apart, stored as one time series with gaps. If a model penalizes gaps, add `visit_id` to the model's join key.
6. **`carb_g` for Shanghai left NULL.** Parsing Chinese free-text `Dietary intake` to grams is a separate NLP project; a naive parser is worse than NULL.
7. **Synthetic vitals in `vitals_readings`.** `heart_rate_bpm`, `systolic_bp_mmhg`, `diastolic_bp_mmhg`, `spo2_pct` are **generated by the replayer**, not from source. Currently: correlated Gaussian around age/BMI-derived baseline, with hypo/hyper-linked deflection (spec pending). Consumers must know these are synthetic — flag on `virtual_patient.synthetic_vitals_enabled` and warn in any UI. Alternative: leave NULL until a real device stream is added.
8. **Glucose-resolution asymmetry.** AZT1D 1 mg/dL vs Shanghai ~1.8 mg/dL quantum (`mmol/L × 18`). Variance metrics (SD, CV, MAGE) must be aware; schema does not resample.
9. **Medication dictionary coverage.** First pass will miss many Chinese and English brand names; `medication_unmapped` is where they land and require manual triage.
10. **`injected_type` leakage.** Column-level access control (services can't read it) is enforced in code review, not by the schema itself. Add a real ACL on the Azure table.
