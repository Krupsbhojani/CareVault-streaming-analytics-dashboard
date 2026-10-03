# CareVault — Project State

CGM streaming pipeline for a Big Data course project. Local PostgreSQL 18 in Docker; Azure deployment planned but not yet exercised.

## Data

- **AZT1D 2025** (25 T1D patients, 5-min Dexcom CGM + insulin/carb/mode) — CSVs, no timezone in file, README says Arizona local.
- **Shanghai T1DM+T2DM** (12 T1D + 100 T2D, 15-min FreeStyle Libre) — 125 Excel files converted to CSV once (`scripts/convert_shanghai.py`, 30-sec/file timeout). CGM stored as `mmol/L × 18` (labelled `mg/dL`).
- Both loaded into `raw.readings_raw` by `loader/load_raw.py`; see `docs/data-inventory.md` for column-by-column analysis and `docs/schema-proposal.md` for the transformation contract.

## Built

| layer | file(s) | what it does |
|---|---|---|
| **Loader** | `loader/load_raw.py`, `sql/schema_raw.sql` | Idempotent COPY into `raw.readings_raw`. R1 dedup (median CGM, first-non-null others), R2 (Subject 14 CGM/BGM rename), R3 device-mode mapping, tz-localize, fixed clamps (AZ 40/400, Shanghai 39.6). |
| **Replay engine** | `replay/build_vps.py`, `replay/run.py` | Deterministic vp assignments from seed. Streams `raw.readings_raw` into `live.vitals_readings` via COPY → staging → `INSERT ON CONFLICT DO NOTHING` → `NOTIFY vitals_new`. Synthetic HR/BP/SpO2 with daily rhythm + hypo-linked HR rise. Injections: spike (400–600 mg/dL, single-point), sensor_dropout (NULL cgm), stuck (18-reading runs), firmware_offset (device batch `sim-v1-batch42`, +15 mg/dL). Also 0.5% dup sends, 2% late (up to 60 s), 0.2% malformed → `dead_letter`. Sustained **~3,900 rows/min at 2,000 vps × 10×** with 1-second ticks. |
| **Stream processor** | `processor/worker.py`, `processor/outliers.py`, `sql/schema_live.sql` | Sharded by `hash(vp_id) % N`. Two connections (fetches on one, LISTEN on the other) to avoid snapshot-isolation stalls under tight-loop fetch. 10 s overlap window; per-vp dedup via `worker_state.last_event_time`. Cursor capped to `now()` so late-arrival future-`ingested_at` rows can't strand the stream. NOTIFY + 2 s fallback poll. Atomic commit of alerts + `worker_state` + `worker_cursor` + `vitals_1min` in one txn. |
| **1-min aggregator** | inside `processor/worker.py::_upsert_1min_rollups` | Not pg_cron: same-txn upsert as alerts/cursor, recomputes from source rows per affected `(vp, minute)` bucket so late arrivals are self-correcting. Verified: 7,429/7,429 bucket counts and aggregates match a fresh re-scan. |
| **Detection** | `processor/outliers.py` — 4 layers | Rules / rate-of-change / MAD / batch analyzer. See "Final detector config" below. |
| **Ops/infra** | `docker/Dockerfile.postgres`, `docker-compose.yml`, `sql/schema_live.sql` | Postgres 18 + pg_partman baked in. `live.vitals_readings` partitioned daily by `event_time` via partman (`create_parent(..., '1 day', p_premake:=4)`), BRIN on `event_time`, UNIQUE `(vp, event_time)`. DIY `ensure_vitals_partition()` retained as belt-and-braces (yields to partman when registered). |
| **Eval** | `verification/eval_processor.py`, `verification/mad_sweep.py`, `eval/figures.py` | Per-fault-type P/R/F1, event-level stuck scoring, per-detector latency, alerts-per-patient-per-hour on clean readings, MAD sweep vs. rules-only vs. rules+ROC. Four PNGs under `eval/figures/`. |

## Final detector config + results (15-min AZT1D concurrent run, 2,000 vps × 10×)

| fault | detector(s) used | precision | recall | F1 | notes |
|---|---|---:|---:|---:|---|
| sensor_dropout | rules (NULL cgm) | 1.000 | 1.000 | 1.000 | |
| firmware_offset | Layer 4 batch analyzer (per-batch median vs fleet median over 120 s window) | 1.000 | 1.000 | 1.000 | 15 s detection latency by design |
| **spike** | rules ceiling (476 mg/dL, above organic max 475.2) **+ ROC rule** (`\|Δcgm/Δt\| > 15 mg/dL/min` in source-time + return-to-baseline within 30% of jump) | **1.000** | **0.984** | **0.992** | 0 FPs on 53 k clean rows |
| stuck (per-reading) | rules (≥15 consecutive identical CGM; organic max = 13) | 0.998 | 0.204 | 0.339 | per-reading understates by design |
| stuck (per-event) | as above | ~1.000 | 0.606 – 0.751 | ~0.7 – 0.8 | event-level score in eval output |

**MAD deliberately dropped from CGM spike detection** — sweep showed no shippable |z| threshold: even |z|=8.0 gives 4.2 alerts/pt/hr on clean readings (~100/day). Retained on HR (~1.07 alerts/pt/hr) and SpO2 (~0.29 alerts/pt/hr) where postprandial excursions don't apply; these emit as `custom` alerts but are not mapped to any injected fault type. See `eval/figures/pr_curve.png` and `eval/figures/mad_by_metric.png`.

**Latency (concurrent mode)**: rules/mad/cross_signal p95 ≈ 1 s. `batch_analysis` p95 ≈ 15 s (intrinsic to its 120 s window). Real-time detectors well under the 3 s target.

## Roles + grants (local Postgres)

Column-level SELECT on `live.vitals_readings` excludes `injected_type` for every non-admin role, so ground-truth labels never leak.

| role | password (local) | can SELECT | can write |
|---|---|---|---|
| `carevault` | `carevault` | everything (superuser) | everything |
| `processor_role` | `processor` | 11 whitelisted cols on `vitals_readings` (no `injected_type`), plus `virtual_patient`, `device`, own `worker_state` / `worker_cursor` / `vitals_1min` | `alerts`, `worker_state`, `worker_cursor`, `vitals_1min` |
| `dashboard_ro` | `dashboard` | same 11 whitelisted cols on `vitals_readings`, plus `alerts`, `device`, `virtual_patient`, `vitals_1min` | — |

Both `processor_role` and `dashboard_ro` are covered by tests (`tests/test_processor.py::test_no_injected_type_access[_dashboard]`). Both blocks are mirrored in `sql/schema_live.sql`; **`dashboard_ro` still needs to be created on Azure** — roles do not migrate with a schema-only `pg_dump`, so the CREATE ROLE + GRANT block from `schema_live.sql` must be run against Azure Flexible Server after schema deploy.

## What remains

- **Operator dashboard** — read-only view over `live.vitals_1min` + `live.alerts` + `live.device` + `live.virtual_patient`, connecting as `dashboard_ro`. Design not started.
- **Online ML** — glucose forecasting (short-horizon: 15–30 min) writing to `live.predictions`. Table exists, no producer yet. Should use the same worker's row stream to avoid a second cursor system.
- **Azure benchmark runs** — Docker-compose target is Postgres 18 with partman; Azure Flexible Server has the same. Need to (a) create `dashboard_ro` on Azure (see above), (b) run the loader against a fresh Azure DB, (c) reproduce the 15-min 2,000-vp concurrent run there, (d) compare throughput, p95 latency, and cost.

## How to reproduce a full run locally

```
docker compose up -d                                  # postgres:18 + partman
python loader/load_raw.py                             # ~13 s
python replay/build_vps.py --count 2000 --seed 42 --speed 10 --source-cohort AZT1D --reset
# terminal A:
python replay/run.py --vp-count 2000 --speed 10 --duration 900
# terminal B (concurrent):
python processor/worker.py --shard 0 --num-shards 1
# after A finishes, stop B, then:
python verification/eval_processor.py
python verification/mad_sweep.py
python eval/figures.py
```
