# CareVault — measured results

Reference for the report. Numbers only. Every entry carries the file or command
that produced it. Items flagged **[superseded]** have a later number elsewhere
in this document; items flagged **[not met]** mark where the plan's quality bar
was not reached.

---

## 1. Data

### 1.1 Row counts and patients
Source: `loader/load_raw.py` output + `SELECT ... FROM raw.readings_raw`

| cohort | rows | patients | src files | sampling | timezone (per README) |
|---|---:|---:|---:|---|---|
| AZT1D 2025 | **300,923** | **25** | 25 CSVs | 5 min nominal (median Δt=300s) | America/Phoenix (no DST) |
| Shanghai T1DM | **16,048** (of 128,157 total) | **12** | 16 (12 xls + 4 xlsx) | 15 min nominal (median Δt=900s) | assumed Asia/Shanghai |
| Shanghai T2DM | **112,109** (of 128,157 total) | **100** | 109 (89 xls + 20 xlsx) | 15 min nominal | assumed Asia/Shanghai |
| Shanghai combined | **128,157** | **112** | 125 | | |

(Shanghai patient counts verified by filename-prefix uniqueness; 1 `patient_id` can appear in 1-3 visit files.)

### 1.2 Shanghai file conversion
Source: `scripts/convert_shanghai.py`

- **125/125 files converted** in ~2.5 min. 0 timeouts, 0 failures.
- Format of `.xls` files: Composite Document File V2, code page 936 (GBK). Verified with `file` command.
- `xlrd` 2.0.2 reads the `.xls`; `openpyxl` reads the `.xlsx`.

### 1.3 Cleaning decisions with evidence

**R1 — duplicate-timestamp semantics in AZT1D.**
Source: `scripts/investigate_az.py` output in `docs/data-inventory.md` §R1.

| subject | dup_ts | dup_rows | % groups with identical CGM | % non-CGM cells uniform |
|---|---:|---:|---:|---:|
| Subject 1 | 161 | 203 | 90.5 | 96.3 |
| Subject 8 | 99 | 149 | **2.0** (worst case — same-second distinct reads) | 98.6 |
| Subject 15 | 2,053 | 2,065 | 83.3 (snapshot-bug pattern) | 92.9 |
| ... | ... | ... | ... | ... |

Decision: aggregate per bucket (median CGM, first-non-null others), not blind dedup. Blind dedup would silently drop ~2,900 non-redundant readings across the cohort.

**R2 — Subject 14 `Readings (CGM / BGM)` column.**
Source: `scripts/investigate_az.py` output.
- Δt=300 s for **97.99 %** of consecutive readings — pure CGM cadence.
- CGM distribution: min=40, median=105, max=237, σ=27. No fingerstick-like spikes.
- 0 NaN in glucose across 13,003 rows.
- Decision: keep as CGM, rename on load, set `glucose_source_labeled_bgm_possible=TRUE` for `az:14` only. Dropping would lose 7 % of AZT1D rows for no data-level evidence.

**R3 — DeviceMode mapping.**
Source: `scripts/investigate_az.py` output.

| observed value | count | maps to | confidence |
|---|---:|---|---|
| `NaN` | 239,283 | `normal` | inferred (not in README) |
| `sleep` | 60,276 | `sleep` | literal |
| `0` (Subject 8 only) | 5,262 | `unknown` | undocumented — not laundered into `normal` |
| `exercise` | 1,858 | `exercise` | literal |
| `sleepsleep` (Subject 17 only) | 33 | `sleep` | string-doubled, treat as typo |

### 1.4 Thresholds derived from raw data
Source: SQL queries against `raw.readings_raw`, listed in `docs/data-inventory.md` and `CLAUDE.md`.

| quantity | AZT1D | Shanghai | threshold set |
|---|---:|---:|---|
| max cgm unclamped | 399 (clamp = 400) | 475.2 | `RANGE['cgm_mgdl'] = (20, 476)` in `processor/outliers.py` |
| max consecutive identical CGM | 13 | — | `STUCK_CONSECUTIVE = 15` in `processor/outliers.py` |
| rate-of-change p9999 (mg/dL/min) | 14.21 (max 44.42) | 4.11 (max 5.64) | `ROC_SPIKE_MGDL_PER_MIN = 15.0` in `processor/outliers.py` |

### 1.5 Shanghai CGM unit quirk
Source: `scripts/profile_data.py` output; cross-checked in `docs/data-inventory.md`.
- Shanghai `CGM (mg / dl)` column: **every observed value is a multiple of 1.8** (= 0.1 mmol/L × 18). Example first-file values: `113.4, 124.2, 129.6, 142.2, 156.6, 162.0`.
- Effective resolution ~1.8 mg/dL; AZT1D is 1 mg/dL integer. Flagged in merge-risk list.

---

## 2. Ingestion

### 2.1 Local Docker — write path, replayer side
Source: `replay/run.py --instrument` run on `postgres:18` in Docker, 2,000 vps × 10× × 10 min. Recorded in CLAUDE.md.

| metric | value |
|---|---:|
| sustained rows/min | **~3,920 — 3,937** (varies ±0.5 % run-to-run) |
| wall elapsed | ≈ 606 s for 600 s configured |
| mean batch size | ~68 rows at TICK_SEC=1 s |
| COPY to staging p50 / p95 | **4.6 / 8.3 ms** |
| INSERT ON CONFLICT p50 / p95 | **2.6 / 7.7 ms** |
| schedule-loop time (2,000 vps) p50 / p95 | 7.9 / 12.8 ms |

### 2.2 Round-trip to Azure vs Local
Source: 100× `SELECT 1` sequential, warm connection. Printed inline in `docs/benchmark-azure.md` §Baseline.

| target | p50 | p95 | min | max |
|---|---:|---:|---:|---:|
| local Docker (127.0.0.1:5433) | **1.69** | 2.34 | 1.29 | 2.98 |
| Azure (`carevault-pg-kb.postgres.database.azure.com`, SSL) | **40.27** | 50.68 | 37.47 | 88.39 |

→ **25× RTT penalty**.

### 2.3 Azure step-load table
Source: `bench/azure_results.csv`. Each step: AZT1D cohort, 10 min configured, replayer `--instrument`.

| step | target rows/min | config (vps×speed) | wall s | rows inserted | **achieved rows/min** | batches | mean rows/batch | COPY p50/p95 ms | INSERT p50/p95 ms | e2e p95 ms | worker lag end (s) | dups suppressed | dead_letter |
|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1,000 | 500 × 10 | 721 | 9,853 | 820 | 448 | 22 | 92 / 105 | 45 / 54 | 1,725 | 0 | 47 | 20 |
| 2 | 5,000 | 2500 × 10 | 725 | 49,318 | 4,081 | 435 | 114 | 96 / 111 | 48 / 60 | 2,373 | 40 | 245 | 97 |
| 3 | 20,000 | 2000 × 50 | 730 | 197,600 | 16,235 | 388 | 512 | 115 / 143 | 70 / 131 | 6,784 | 42 | 993 | 406 |
| 4 | 50,000 | 2500 × 100 | 725 | 493,412 | **40,825** | 352 | 1,409 | 146 / 182 | 165 / 284 | 17,229 | **649** | 2,493 | 957 |

**[not met]** Step 4 target 50,000 rows/min — achieved 40,825 (82 %). Worker fell 11 min behind, could not drain within 10-min step.

### 2.4 Azure tick-interval sweep (same 5k target, varying batch size)
Source: `bench/azure_tick_sweep.csv`. 5 min each, 2500 vps × 10×.

| tick s | batches | mean rows/batch | COPY p50 ms | INSERT p50 ms | achieved rows/min |
|---:|---:|---:|---:|---:|---:|
| 0.5 | 340 | 73 | 97.9 | 51.4 | **3,449** |
| 1.0 | 213 | 116 | 98.9 | 53.8 | 3,384 |
| 2.0 | 123 | 201 | 105.0 | 60.5 | 3,388 |
| 4.0 | 67 | 368 | 114.7 | 78.7 | 3,411 |

→ Throughput **flat ±2 %** across 5× batch-size range → round-trip count is not the limit on this tier.

### 2.5 Verdict — local vs Azure write path
Source: `docs/benchmark-azure.md` §Conclusion.
- COPY: Azure ~20× local (96 ms vs 4.6 ms at the same 5k target).
- INSERT: Azure ~18× local (48 ms vs 2.6 ms).
- Sustained rows/min at 5k target: **matched** (4,081 Azure vs ~3,900 local). Azure limit becomes visible only past 20k target (step 3) and is server-CPU-bound.
- Evidence base: (a) tick-sweep flat, (b) per-batch latency grows with batch size, (c) 4 active backends `wait_event IS NULL` on 1 vCore, (d) `blks_hit` cache ratio 99.991 % (not I/O-bound).

---

## 3. Processing

### 3.1 Per-detector latency (15-min concurrent local run, 2,000 vps × 10×)
Source: SQL in `verification/eval_processor.py`.

| detector | n alerts | p50 ms | **p95 ms** | p99 ms | max ms |
|---|---:|---:|---:|---:|---:|
| rules | 9,917 | 560 | **1,032** | 1,074 | 1,163 |
| mad | 18,601 | 575 | **1,038** | 1,081 | 1,334 |
| cross_signal | 18,654 | 575 | **1,038** | 1,081 | 1,334 |
| batch_analysis | 6,885 | 7,811 | 14,787 | 15,526 | 16,094 |

- Real-time detectors (rules / mad / cross_signal) **p95 ≈ 1 s** — well under the 3 s plan target.
- `batch_analysis` **p95 = 14.8 s** by design (120-s rolling window). **[not met]** against a flat 3-s target; argued as intrinsic to fleet-wide bias detection, not a bug.

### 3.2 Restart safety / effectively-once
Source: `tests/test_processor.py::test_restart_safety_exactly_once`. Status: **PASSED** (last green run: 2026-10-02 with full suite 5/5).

Invariants asserted:
- Every test-vp reading is covered by `worker_state.last_event_time`.
- Zero duplicate `(virtual_patient_id, trigger_event_time, rule_id)` tuples in `live.alerts` after a cursor-rewind restart.
- Alert count unchanged across restart replay.

### 3.3 Concurrent-mode cursor-stall diagnosis + fix
Source: debugging trace in prior session; code in `processor/worker.py::process_one_cycle`.
- First 5 fetch cycles (before fix): cursor stuck at 09:12:47 after cycle 3; 2 rows re-fetched every cycle while replayer committed 65 rows/sec.
- **Root cause**: late-arrival rows have `ingested_at = event_time + up to 60 s` → *future* `ingested_at`. A single late-future row pushed the cursor past wall time; subsequent normal rows (`ingested_at ≈ now`) fell below `cursor − 10 s` and were stranded.
- Fix: `new_cursor = min(sql_max_ingested, now())`. After fix: **100 % coverage of 118,538 vitals rows** in the 30-min concurrent run.

### 3.4 Row counts observed in the recent 30-min concurrent runs
Source: replayer stdout + post-run SQL in `verification/eval_processor.py`.

| run | vitals rows | alerts | predictions (total) | predictions (resolved) | unprocessed | 100 % coverage? |
|---|---:|---:|---:|---:|---:|---|
| 30-min, final tuned ML config, 2026-10-02 | 118,538 | 59,296 | — (truncated between) | — | 0 | yes |
| 30-min, with all 3 ML fixes, 2026-10-02 | 118,538 | 59,296 | 409,754 | 174,084 | 0 | yes |
| 30-min, initial ML, 2026-09-29 | 118,534 | 54,057 | — | — | 0 | yes |

### 3.5 vitals_1min aggregator consistency check
Source: 3-min run post-check SQL (recorded in CLAUDE.md and prior session).

| check | value |
|---|---:|
| distinct `(vp, minute)` buckets in `vitals_readings` | 7,429 |
| rows in `live.vitals_1min` | 7,429 |
| buckets where `n_readings` matches a fresh recompute | **7,429 / 7,429** |
| mismatched counts | 0 |

Aggregator writes atomically in the same transaction as alerts + cursor; recomputed-from-source on every (vp, minute) touched. **No pg_cron.**

---

## 4. Detection

### 4.1 Final per-fault P/R/F1 — 15-min concurrent, 2,000 vps × 10×
Source: `verification/eval_processor.py` output; `eval/figures/per_type.png`.

| fault | positives | TP | FP | FN | precision | recall | F1 | detector responsible |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| sensor_dropout | 183 | 183 | 0 | 0 | 1.000 | 1.000 | **1.000** | `rules.cgm_null` |
| firmware_offset | 3,453 | 3,453 | 0 | 0 | 1.000 | 1.000 | **1.000** | `batch_analysis.firmware_offset` (per-batch median vs fleet median, 120 s window) |
| spike | 184 | 181 | 0 | 3 | 1.000 | 0.984 | **0.992** | `rules.cgm_out_of_range` ∪ `rules.cgm_roc_spike` |
| stuck (per-reading) | 2,287 | 467 | 1 | 1,820 | 0.998 | 0.204 | 0.339 | `rules.cgm_stuck` (threshold ≥ 15) |
| stuck (per-event) | 165 | 100 | ~0 | 65 | ~1.000 | 0.606 | **~0.755** | same |

### 4.2 Per-fault at 30-min run
Source: same script, 30-min data, in CLAUDE.md.

| fault | precision | recall | F1 |
|---|---:|---:|---:|
| sensor_dropout | 1.000 | 1.000 | 1.000 |
| firmware_offset | 1.000 | 1.000 | 1.000 |
| spike | 1.000 | ~0.98 | ~0.99 |
| stuck (per-event) | ~1.000 | **0.751** | ~0.858 |

### 4.3 MAD threshold sweep — spike detection
Source: `verification/mad_sweep.py` → `scratch/mad_sweep.csv`; `eval/figures/pr_curve.png`.
Baseline (no slope filter), 30-min AZT1D run, 354 spike positives, 68,224 clean readings post warm-up.

| |z| | MAD/spike | MAD/clean | combined precision | combined recall | combined F1 | **alerts/patient/hour on clean** |
|---:|---:|---:|---:|---:|---:|---:|
| 3.0 | 234 | 11,649 | 0.026 | 0.862 | 0.050 | **11.69** |
| 3.5 | 233 | 9,443 | 0.031 | 0.859 | 0.060 | 9.48 |
| 4.0 | 232 | 7,875 | 0.037 | 0.856 | 0.071 | 7.90 |
| 5.0 | 231 | 5,970 | 0.048 | 0.856 | 0.091 | 5.99 |
| 6.0 | 218 | 4,994 | 0.056 | 0.836 | 0.105 | 5.01 |
| 8.0 | 208 | 4,189 | 0.065 | 0.825 | 0.121 | **4.20** |

→ Even at |z|=8 the FP rate is **4.2 alerts/patient/hour on clean readings ≈ 100/patient/day** — unshippable at any threshold.

### 4.4 MAD by metric (same |z|=3.5 across CGM, HR, SpO2)
Source: `verification/eval_processor.py` output; `eval/figures/mad_by_metric.png`.

| metric | clean alerts | alerts/pt/hr | ≈ alerts/pt/day | shippable? |
|---|---:|---:|---:|---|
| CGM | 2,732 | **5.49** | ~132 | no (dropped from spike mapping) |
| HR | 530 | 1.07 | ~26 | marginal (retained as `custom` alert) |
| SpO2 | 145 | 0.29 | ~7 | marginal (retained as `custom` alert) |

### 4.5 Final detection configuration decisions
Source: `processor/outliers.py` constants + `verification/eval_processor.py::DETECTS`.

- **Spike** detected by **rules only**: `cgm > 476` (above Shanghai max 475.2) ∪ ROC rule `|Δcgm/Δt| > 15 mg/dL/min` AND `|cgm_T+1 − cgm_T−1| ≤ 0.30 × jump` (return-to-baseline). Fires one source-sample later (5 min AZ cadence).
- **Stuck**: 15 consecutive identical CGM (above measured organic max of 13).
- **Firmware_offset**: Layer-4 batch analyzer, 120 s window, trigger if `|batch_median − fleet_median| > 8 mg/dL` and batch has ≥ 30 readings.
- **CGM-MAD dropped from spike mapping** (not shippable); MAD retained on HR/SpO2 as `custom` alerts, not mapped to any injected fault.
- `cross_signal` emits a second alert for every MAD alert (sensor-error vs clinical classification by HR context).

### 4.6 Historical / superseded detection numbers
- **[superseded]** First-pass spike recall 0.000 at 10-min run (1.5–2× multiplier injection, below detector thresholds) → fixed by injecting [400, 600] range → 0.864 → 0.984 after ROC rule.
- **[superseded]** First-pass stuck precision 0.012 / recall 0.088 (threshold = 3 consecutive) → precision 0.998 / per-event recall 0.751 after threshold raised to 15.
- **[superseded]** First-pass firmware_offset recall 0.000 → 1.000 after batch analyzer added.
- **[superseded]** Spike precision ≈ 0.47 (10-min MAD-included mapping) → precision 1.000 after dropping MAD from spike mapping.

---

## 5. ML

### 5.1 Three-fix iteration ladder (5-min concurrent runs for comparability)
Source: inline eval across session, final numbers in `eval/model_eval.py`.

| iteration | model touched | MAE before | **MAE after** | change |
|---:|---|---:|---:|---|
| 1 | river_linreg (TargetStandardScaler + explicit SGD(1e-3) + non-finite guard) | 3.54 × 10¹⁰ | **974 → 1,064** | divergence resolved |
| 2 | linear_trend (slope window 15→30 min, clip Δ ≤ ±60 mg/dL) | 883.21 | **42.52** | 20× better |
| 3 | river_ht (warm-up gate = 500 learned examples + learn-during-warmup fix) | 16,897 (post-iter-2 variance) | **135.77** | 124× better |

### 5.2 Final 30-min prequential eval, 2,000 vps × 10× concurrent
Source: `eval/model_eval.py` output on 30-min run data; `eval/figures/mae_rolling.png`, `eval/figures/clarke_grid.png`.

| model | n resolved | **MAE** | RMSE | % vs persistence | Zone A | Zone B | Zone C | Zone D | Zone E |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **persistence** | 91,702 | **10.81** | **25.09** | baseline | **86,558 (94.4 %)** | 4,548 (5.0 %) | 269 | 321 | 6 |
| linear_trend | 91,702 | 52.90 | 59.08 | −389 % | 15,040 | 75,371 | 389 | 407 | 495 |
| river_linreg | 91,702 | 84.87 | 1,172.02 | −685 % | 76,291 | 9,164 | 3,426 | 757 | 2,064 |
| river_ht | 85,331 | 238.60 | 29,980 | −2,106 % | 70,094 | 13,768 | 37 | 1,429 | 3 |

Resolved predictions/sec: **200.4** across all models.

### 5.3 Delayed-label / eviction breakdown
Source: SQL CTE classifier on live.predictions, 5-min run.

| cause | count | share | explanation |
|---|---:|---:|---|
| still_pending_at_end | 5,935 | **97.4 %** | predicted target lands in the final 15 source min — label window hadn't closed. Structural, not a bug. |
| label_untrustworthy | 66 | 1.1 % | label-reading was sensor-fault-flagged in-cycle; pending evicted on purpose. |
| label_too_late | 52 | 0.9 % | no reading within tolerance of target (replayer's 2 % late + 0.2 % malformed can cause this). |
| queue_cap | 39 | 0.6 % | queue at 120 slots per vp was effectively never full. |

Resolution rate after raising queue cap from 40 → 120: **38 % → 78 %**.

### 5.4 Horizon-is-source-time invariance test
Source: `tests/test_ml.py::test_horizon_is_source_time_regardless_of_speed`. Parametrized across `speed ∈ {1, 10, 100}`. Status: **PASSED**.

### 5.5 Feature/label alignment test
Source: `tests/test_ml.py::test_feature_label_alignment`. Known sinusoidal patient; asserts recorded `actual == g(input_time + 15 source min)` to numerical precision. Status: **PASSED**.

### 5.6 Exogenous experiment (insulin + carbs + basal)
Source: `eval/exogenous_test.py` on `raw.readings_raw` AZT1D cohort. 299,236 (features, target) pairs over all 25 patients.

| configuration | model | MAE | RMSE | % vs persistence | Zone A | Zone B | Zone C | Zone D | Zone E |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| WITHOUT exog | persistence | **10.08** | 14.24 | baseline | 282,080 | 16,132 | 1 | 1,022 | 1 |
| WITHOUT exog | linear_trend | 11.32 | 16.66 | −12.3 % | 273,662 | 24,996 | 35 | 526 | 17 |
| WITHOUT exog | river_linreg | 404.38 | 1,841 | −3,913 % | 213,987 | 15,042 | 34,847 | 968 | 34,392 |
| WITHOUT exog | river_ht | 483.27 | 17,854 | −4,696 % | 225,986 | 65,383 | 351 | 7,012 | 4 |
| WITH exog | persistence | **10.08** | 14.24 | baseline | 282,080 | 16,132 | 1 | 1,022 | 1 |
| WITH exog | linear_trend | 11.32 | 16.66 | −12.3 % | 273,662 | 24,996 | 35 | 526 | 17 |
| WITH exog | river_linreg | 9.98 × 10¹⁰ | 1.63 × 10¹² | *diverged* | 18,307 | 2,484 | 137,815 | 78 | 140,552 |
| WITH exog | river_ht | 464.71 | 17,996 | −4,512 % | 221,425 | 70,419 | 345 | 6,539 | 8 |

→ **No model beats persistence** with or without insulin/carb/basal features. Best delta (`river_ht`): −18.56 MAE (3.8 % relative) — still 46× worse than persistence. `river_linreg` *diverged* on sparse insulin/carb columns.

### 5.7 Historical / superseded ML numbers
- **[superseded]** First-pass persistence MAE 39.86 at 30-min run — the 15 event-time min horizon was 150 source min = 2.5 h at 10× replay. Fixed by `HORIZON_SEC / speed_factor`; new MAE 10.81.
- **[superseded]** First-pass river_linreg MAE 3.5 × 10¹⁰ (unscaled target, default SGD). Fixed to 1,064 after target standardization + SGD(1e-3).
- **[superseded]** First-pass river_ht MAE 2,579 at 30-min / 16,897 post-fix-1 → 238.60 after warm-up gate at 500 examples.
- **[superseded]** First-pass linear_trend MAE 998 (15-min slope projected 15 min) → 52.90 after window=30 min + clip Δ ≤ ±60 mg/dL.

---

## 6. Dashboard

### 6.1 Panel-latency harness
Source: `dashboard/bench.py` script exists; replays each dashboard panel's SQL text N times and reports p50/p95/p99.

**[not met]** No measured panel p95 captured in this session's artifacts. The harness in `dashboard/bench.py` was authored but no CSV / log output from a benchmark run is in `scratch/` or `bench/`. The sample numbers quoted in `docs/dashboard-design.md` §4 (`triage_top_alerts 83ms/142ms`, `patient_cgm_series 31ms/55ms`, `fleet_batch_medians 310ms/480ms`, `system_rows_per_min 44ms/72ms`) **are illustrative in the design doc**, not measurements from this session.

Dashboard code exists under `dashboard/`:
- `pages/2_One_person.py`
- `pages/3_All_monitors.py`
- `pages/4_System.py`
- `app.py` (triage/attention page)

### 6.2 Dashboard role used
Source: `sql/schema_live.sql` + `tests/test_processor.py::test_no_injected_type_access_dashboard`.

- `dashboard_ro` role with column-level grants excluding `injected_type`.
- SELECT allowed on: `vitals_readings` (11 whitelisted cols), `alerts`, `device`, `virtual_patient`, `vitals_1min`, `predictions`.
- Test `test_no_injected_type_access_dashboard`: **PASSED** — `SELECT injected_type` raises `psycopg.errors.InsufficientPrivilege`.

---

## 7. Infrastructure (Azure constraints hit)

Source: `docs/benchmark-azure.md`; `CLAUDE.md`; `sql/schema_live.sql` comments.

### 7.1 Tier + region
- **Standard_B1ms** — 1 vCore burstable, 2 GiB RAM.
- Region: **northcentralus**.
- PostgreSQL 18.6 on x86_64 Linux (verified via `version()`).
- Server IP (inside Azure VNet): `10.19.0.4`.

### 7.2 Server-bound ceiling evidence
- Tick sweep **flat** at ~3,400 rows/min across 5× batch-size variation (section 2.4).
- Per-batch latency *grows* with batch size (COPY 98 → 115 ms as batch grows 73 → 368) → payload work bound, not RTT bound.
- `pg_stat_activity`: 4 active backends, **0 wait events**, 0 lock waits.
- Cache hit ratio **99.991 %** (`blks_hit / (blks_hit + blks_read)`) → not I/O-bound.
- Classic 1-vCore CPU saturation signature.

### 7.3 Extension / schema requirements
- **`pg_partman`** needed. Not in vanilla `postgres:18` Docker image → custom image built at `docker/Dockerfile.postgres` with `apt-get install postgresql-18-partman`.
- Azure Flexible Server has `pg_partman` in its extension allowlist (verified via `pg_available_extensions`).
- Placement: own schema `partman` (not `public`). `CREATE EXTENSION pg_partman SCHEMA partman;` in `sql/schema_live.sql`.
- DIY `live.ensure_vitals_partition(day)` kept as fallback; yields to pg_partman when `partman.part_config` has an entry for `live.vitals_readings`.

### 7.4 Missing grants on Azure
Source: `CLAUDE.md` §"Roles + grants" + `sql/schema_live.sql` comment above `dashboard_ro` block.

**[not met]** `dashboard_ro` role is defined in `sql/schema_live.sql` and tested locally, but roles **do not migrate with a schema-only `pg_dump`**. The role must be created manually on Azure Flexible Server after schema deploy. Status at last check: **not confirmed created on Azure**.

### 7.5 pg_cron deliberately avoided
Source: comment above `_upsert_1min_rollups` in `processor/worker.py`.
- `vitals_1min` aggregator is in the worker, not pg_cron. Rationale: cron can't commit atomically with the alerts + cursor update that pertain to the same reading; dashboard reads would see out-of-sync state.
- `pg_cron` is in Azure's allowlist but intentionally unused.

### 7.6 processor_role grants on Azure (required for ML startup)
Source: `sql/schema_live.sql` lines 255-272.
- `processor_role` needs `USAGE` on schema `raw` + `SELECT` on `raw.readings_raw` for `_load_ml_contexts` to fetch per-vp `source_tz`, `sampling_sec`, `diabetes_type`.
- No `injected_type` on `raw.readings_raw` (that is a live-side-only column) → no label leakage.
- Must be granted on Azure after schema deploy.

---

## 8. Plan quality bar — items not met

| plan bar | measured | gap | section |
|---|---|---|---|
| p95 end-to-end alert latency < 3 s | real-time detectors 1.0 s; `batch_analysis` 14.8 s | batch analyzer intrinsic (120 s window) — not fixable without changing the method | §3.1 |
| Azure 50,000 rows/min sustained | 40,825 rows/min achieved | server-bound on 1 vCore B1ms — needs tier upgrade | §2.3 |
| Processor keeps up at any step load | 649 s backlog at step 4 | same CPU bottleneck as above | §2.3 |
| Dashboard panel p95 reported | harness exists, no measured values captured this session | `dashboard/bench.py` was never run against the running stack with its output persisted | §6.1 |
| `dashboard_ro` created on Azure | role defined locally, not confirmed on Azure | one-time manual step | §7.4 |
| Clarke Zone A ≥ 95 % for best model | persistence at 94.4 % | 0.6 pp short on the 30-min run; literature baseline for 15-min persistence is 93–96 % so this is in-band | §5.2 |
| Any ML model beats persistence by any margin | none did | shown offline experiment with insulin+carbs also did not beat persistence | §5.6 |

---

## 9. Artifact index

| file | section(s) | purpose |
|---|---|---|
| `bench/azure_results.csv` | 2.3 | one row per step, every metric |
| `bench/azure_tick_sweep.csv` | 2.4 | one row per tick value at 5k target |
| `bench/_make_figs.py` | — | regenerates the Azure figures |
| `scratch/mad_sweep.csv` | 4.3 | MAD |z| sweep output |
| `scratch/az_files.csv` | 1.1, 1.3 | per-AZT1D-subject profile |
| `scratch/shanghai_t{1,2}dm_files.csv` | 1.1 | per-Shanghai-file profile |
| `eval/figures/pr_curve.png` | 4.3 | MAD sweep + rules + ROC on PR plane |
| `eval/figures/mad_by_metric.png` | 4.4 | CGM vs HR vs SpO2 FP rate |
| `eval/figures/mad_alerts_per_patient_per_hour.png` | 4.3 | log-scale FP rate vs |z| |
| `eval/figures/per_type.png` | 4.1 | per-fault P/R/F1 bars |
| `eval/figures/mae_rolling.png` | 5.2 | rolling MAE per ML model |
| `eval/figures/clarke_grid.png` | 5.2 | Clarke scatter for best model |
| `eval/figures/azure_throughput.png` | 2.3 | achieved vs target + latency axis |
| `eval/figures/local_vs_azure.png` | 2.5 | side-by-side comparison |
| `docs/data-inventory.md` | 1 | descriptive data profiling |
| `docs/schema-proposal.md` | — | prescriptive schema contract |
| `docs/benchmark-azure.md` | 2, 7 | method + numbers + conclusion for Azure |
| `docs/dashboard-design.md` | 6 | dashboard spec (illustrative numbers, not measured) |
| `CLAUDE.md` | all | living project state |

| command / script | section(s) | what it produces |
|---|---|---|
| `scripts/convert_shanghai.py` | 1.2 | 125 CSVs in `data/interim/shanghai/` |
| `scripts/investigate_az.py` | 1.3 | R1 / R2 / R3 evidence |
| `scripts/profile_data.py` | 1 | per-file / per-subject row counts |
| `loader/load_raw.py` | 1 | populates `raw.readings_raw` |
| `replay/build_vps.py` | 2 | populates `live.virtual_patient` + `live.device` |
| `replay/run.py --instrument` | 2.1, 2.3, 2.4 | writes vitals_readings + prints per-cycle timings |
| `processor/worker.py` | 3 | writes alerts, worker_state, worker_cursor, vitals_1min, predictions |
| `verification/eval_processor.py` | 3.1, 4.1, 4.4 | per-fault P/R/F1 + latency + event-level stuck |
| `verification/mad_sweep.py` | 4.3 | |z| sweep → `scratch/mad_sweep.csv` |
| `eval/model_eval.py` | 5.2 | rolling MAE + Clarke grid + final table |
| `eval/figures.py` | 4.3, 4.4 | PR curve, per-type bar, MAD-by-metric |
| `eval/exogenous_test.py` | 5.6 | offline experiment on raw.readings_raw |
| `bench/_make_figs.py` | 2 | Azure throughput + local-vs-Azure PNGs |
| `dashboard/bench.py` | 6.1 | **harness only — no stored output in this session** |
| `pytest tests/` | 3.2, 5.4, 5.5, 6.2 | 12 tests, last green run 2026-10-02 (local) |
