# CareVault

A streaming analytics pipeline for continuous glucose monitoring data, built on Azure Database for PostgreSQL.

Real blood sugar traces from two public research datasets are replayed as a live stream into a partitioned PostgreSQL database. A stream processor checks every reading for sensor faults and clinical events, forecasts each patient's glucose 15 minutes ahead, and a Streamlit dashboard on Azure App Service shows the result with a five-second refresh.

---

## What problem this solves

A clinic watching hundreds of continuous glucose monitors has two failure modes that look identical in the raw numbers:

- A patient whose blood sugar is genuinely dangerous.
- A patient whose sensor is faulty and reporting numbers that are not real.

Treat the first as the second and someone gets hurt. Treat the second as the first and you generate alarms nobody trusts. CareVault separates them, and does so on a live stream rather than in batch.

---

## Data

No synthetic glucose values. Both sources are de-identified public research datasets.

| Source | Patients | Cadence | Readings |
|---|---|---|---|
| AZT1D | 25 | 5 minutes | see `docs/data-inventory.md` |
| Shanghai T1DM / T2DM | 112 | 15 minutes | see `docs/data-inventory.md` |
| **Total** | **137 traces** | | **429,080** |

`docs/data-inventory.md` profiles every column in both datasets and records the evidence behind each cleaning decision. `docs/schema-proposal.md` documents the resulting two-layer contract.

The datasets are not redistributed in this repository. Download instructions are in `docs/data-inventory.md`.

### From 137 traces to a fleet

The replay engine expands the 137 real traces into virtual patients, each with its own time offset, replay speed and random seed, so no two patients are in the same place in their trace. Heart rate, blood pressure and oxygen saturation are generated alongside the real glucose values, and labelled sensor faults are injected at a known rate so detection can be scored against ground truth.

The injected-fault label lives in `live.vitals_readings.injected_type`. Column-level grants deliberately exclude that column from both `processor_role` and `dashboard_ro`, so the detectors and the dashboard cannot see the answer key. Tests enforce this.

---

## Architecture

```
public CGM datasets
        |
     loader           cleans, de-duplicates, normalises units and time zones
        |
  raw.readings_raw    immutable, as-measured            [Azure PostgreSQL]
        |
   replay engine      137 traces -> N virtual patients, live stream
        |
 live.vitals_readings daily partitions (pg_partman)     [Azure PostgreSQL]
        |
 stream processor     4 detection layers + 4 forecasting models
        |
 live.alerts, live.predictions, live.vitals_1min        [Azure PostgreSQL]
        |
   dashboard          Streamlit, 5s refresh             [Azure App Service]
```

Only the database and the dashboard run in Azure. The loader, replay engine and stream processor run outside it.

### Two-layer schema

- `raw.readings_raw` is the batch layer: cleaned, immutable, kept exactly as measured.
- `live.*` is the streaming layer: `vitals_readings` (daily partitions, BRIN on `event_time`, unique on `(virtual_patient_id, event_time)`), plus `alerts`, `predictions`, `vitals_1min` rollups, `dead_letter`, `worker_state` and `worker_cursor`.

Extensions in use: `pg_partman` for partition management, `pg_cron` for retention, `pg_stat_statements` for query analysis.

### Effectively-once processing

The processor is sharded by `hash(virtual_patient_id) % N`. Each worker reads by `ingested_at` with a 10-second overlap window, filters already-seen rows using a per-patient `last_event_time` watermark in `live.worker_state`, and commits alerts, predictions, rollups and its own cursor position in a single transaction. The cursor is a composite key `(last_ingested_at, last_virtual_patient_id)` so that batches sharing one timestamp cannot stall it.

Workers wake on `NOTIFY vitals_new` and fall back to two-second polling.

---

## Detection

Four layers, each catching something the others cannot.

| Layer | What it catches |
|---|---|
| Rules | Clinical thresholds (hypo, hyper), sensor dropout, stuck values |
| Rate of change | Physiologically impossible jumps between consecutive readings |
| Cross-signal | Glucose anomalies confirmed or contradicted by heart rate and blood pressure |
| Fleet batch analyser | Whole batches of devices reading offset from the rest of the fleet |

The fleet analyser is the one no per-patient check can replicate. When a batch of monitors adds a constant offset to every reading, each affected patient's chart still rises and falls normally and looks entirely healthy. The fault only appears when that batch is compared against every other device.

Headline results (full numbers and methodology in `docs/results.md`):

- Rate-of-change rule: F1 0.992, zero false positives across 53k clean readings.
- Fleet firmware offset: precision 1.000, recall 1.000, detected in roughly 15 seconds.

A MAD-based detector was implemented, evaluated and **rejected**. No threshold produced a shippable false-positive rate; even at |z| = 8.0 it fired 4.2 alerts per patient per hour. `docs/results.md` records the sweep and the reasoning. Alerts per patient per hour is treated as a first-class metric throughout, because a detector that is accurate but noisy is not deployable.

---

## Forecasting

Four online models predict glucose 15 minutes ahead, trained incrementally on the stream with prequential evaluation and delayed labels. Nothing is pre-trained on a fixed dataset.

| Model | Notes |
|---|---|
| Persistence | Baseline |
| Linear trend | Slope extrapolation |
| River linear regression | SGD |
| River Hoeffding Adaptive Tree | Concept-drift aware |

Evaluation uses MAE and the Clarke Error Grid, which scores predictions by clinical consequence rather than by raw distance. Results are in `docs/results.md`.

### A note on event time

Because the stream is a replay, event time advances faster than wall clock by the replay speed factor. Four separate bugs came from conflating the two: the forecast horizon, the slope feature, the hour-of-day features, and the cursor advance. All four are documented in `CLAUDE.md` with before and after numbers. Fixing the horizon alone moved persistence MAE from 39.86 to 10.81 and Clarke Zone A from 49% to 94%.

This is the single most important thing to understand before modifying the processor: **event time is not wall clock time.**

---

## Dashboard

Four pages, written for readers with no clinical background.

1. **Who needs attention** — who to go to first, split into real health events and faulty sensors.
2. **One person** — a single patient's recent history and 15-minute forecast.
3. **All monitors** — the fleet comparison, where batch-level device faults appear.
4. **System** — processing lag, reading-to-alert latency, rows rejected. Operational honesty.

Deployed to Azure App Service (Linux, Python 3.13) via GitHub Actions. Streamlit 1.63 with `@st.fragment(run_every="5s", parallel=True)`, psycopg 3 with `psycopg_pool`.

---

## Operational findings

Running this on a Burstable B1ms (1 vCPU, 2 GB RAM) surfaced several problems that only appear at scale over time. They are recorded here because they are results, not just fixes.

**Missing index on the ingestion cursor.** The processor seeks on `(ingested_at, virtual_patient_id)`, but the table had only a BRIN on `event_time` and a unique index on `(virtual_patient_id, event_time)`. Every fetch cycle scanned. Fetch latency was 41 seconds before the index, sub-second after.

**Unbounded prediction retention.** `live.predictions` grew to 7.3 million rows and 2.2 GB, more than half the database, pushing the working set past available memory. Every rollup then read from a disk providing roughly 120 IOPS. A `pg_cron` retention window fixed it.

**Missing partition-pruning predicate.** The per-minute rollup joins `live.vitals_readings` against a VALUES list of patient and minute pairs with no explicit `event_time` bound, so Postgres cannot prune partitions and scans all of them. The symptom worsened every day as partitions accumulated. An explicit `event_time` range restores pruning.

**Stale watermarks across replayer restarts.** Restarting the replayer resets each patient's trace to its beginning, so new rows carry event times earlier than the stored watermark and are silently filtered out as already-seen. Rows keep arriving, the cursor freezes, no alerts are emitted, and nothing errors. `live.worker_state` must be cleared whenever the replayer restarts.

---

## Running it

### Prerequisites

- Python 3.13
- An Azure Database for PostgreSQL Flexible Server (PostgreSQL 18) with `pg_partman`, `pg_cron` and `pg_stat_statements` enabled
- The two datasets, downloaded per `docs/data-inventory.md`

### Setup

```bash
pip install -r requirements.txt
psql "$CAREVAULT_PG" -f sql/schema_raw.sql
psql "$CAREVAULT_PG" -f sql/schema_live.sql
python loader/run.py
```

### Running the stream

Set the connection strings, then start the processor and the replayer:

```bash
export CAREVAULT_PROCESSOR_PG="host=... user=processor_role ..."
export CAREVAULT_PG="host=... user=cvadmin ..."

python processor/worker.py --shard 0 --num-shards 1
python replay/run.py --vp-count 200 --speed 10 --duration 57600
```

Order matters. Clear `live.worker_state` before starting the processor if the replayer is being restarted.

### Checking health

```sql
SELECT now() - last_ingested_at AS lag FROM live.worker_cursor;
SELECT count(*) FROM live.vitals_readings WHERE ingested_at > now() - interval '60 seconds';
SELECT alert_type, detector, count(*) FROM live.alerts
 WHERE emitted_at > now() - interval '5 minutes' GROUP BY 1,2;
```

Healthy means lag under 30 seconds, steady row counts, and alerts from multiple detectors.

---

## Repository layout

```
loader/      one-off cleaning and load into raw.readings_raw
replay/      replay engine: traces to virtual patients
processor/   worker, detection layers, online ML
dashboard/   Streamlit app
sql/         schema definitions
docs/        data inventory, schema proposal, results
tests/       including grant enforcement
```

`CLAUDE.md` holds the working state of the project: what is built, final detector configuration, results, the event-time bug table, roles and grants, and what remains.

---

## Security notes

- Database roles are separated by privilege. `processor_role` and `dashboard_ro` have column-level grants that exclude `injected_type`.
- Connection strings are supplied through environment variables and are not committed.
- The firewall should be restricted to known addresses. A permissive rule was used during development and is a documented limitation.
- Credentials used during development have been rotated.

---

## Limitations

- `live.alerts` has no unique constraint, so the effectively-once guarantee rests on application-level deduplication rather than a database constraint.
- The loader, replay engine and processor run outside Azure, so the stream depends on a machine being available.
- Sustained throughput is bounded by the B1ms instance. The configuration above runs comfortably within it; higher rates require a larger instance.

---


Datasets are the property of their respective authors and are used under their original licenses.
