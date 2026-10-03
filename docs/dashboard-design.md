# Operator dashboard — design brief

The dashboard answers one question:

> **Which of 2,000 patients needs attention now, and can the reading be trusted?**

Every design choice below either sharpens that answer or gets out of the way.

Read-only against `dashboard_ro`. Streamlit 1.63. Live panels are fragments with
`run_every="5s"`, `parallel=True`; base tables are `live.vitals_1min`,
`live.alerts`, `live.device`, `live.virtual_patient`, and the raw
`live.vitals_readings` only when the 1-min rollup would erase the signal (spike
markers, latest reading age).

---

## 1. Design principle: severity is the only thing color is allowed to mean

The screen is looked at under time pressure. Color must survive that. Two rules:

1. Color encodes **either** clinical severity **or** fault type — never a
   category we invented for prettiness (patient sex, cohort, T1 vs T2).
   Non-severity categories are drawn with shape, position, or muted grey.
2. A row's dominant color is the **highest-severity live signal on it**. If
   a patient is both hyper and has a sensor dropout in the last minute, the row
   reads `critical`, and the fault-type badge sits next to it as a chip.

This is the whole reason the token set is only six colors. Fewer than that and
severity gets muddled with fault type; more than that and the operator has to
learn the palette.

---

## 2. Token system

### 2.1 Colors (6 total, hex, WCAG AA on white)

| token             | hex       | means                                                     | used on                                                        |
|-------------------|-----------|-----------------------------------------------------------|----------------------------------------------------------------|
| `sev.critical`    | `#B30021` | critical clinical alert (severe hypo, severe hyper)       | row background tint, alert marker, ranking bar                 |
| `sev.warn`        | `#D97706` | warn clinical alert (hypo 55–70, hyper 250–299)           | row tint, alert marker                                         |
| `sev.info`        | `#334155` | reading present, no alert                                 | row baseline, axis labels                                      |
| `fault.sensor`    | `#7C3AED` | detector judged the anomaly **not clinical** — sensor/    | chip on the row, marker for `sensor_dropout` and               |
|                   |           | firmware fault (dropout, stuck, firmware offset, ROC      | `cross_signal.mad_hr_sensor_error`                             |
|                   |           | spike, MAD-hr-normal)                                     |                                                                |
| `fault.clinical`  | `#0369A1` | detector judged the anomaly **clinical** (cross-signal    | chip on the row, marker for `cross_signal.hypo_confirmed_by_hr`|
|                   |           | HR-confirmed hypo, MAD with abnormal HR)                  |                                                                |
| `stale`           | `#94A3B8` | data age exceeds the panel's freshness budget             | age pill, gauge fill when the panel is not caught up           |

**Reference bands and gridlines** are neutral (`#E5E7EB` fills, `#CBD5E1`
strokes) — they must never compete with an alert marker.

**Why these six and not the Streamlit default palette:** the default `st.line_chart`
palette rotates through a categorical 10-hue Vega scheme. On our screen that
would put a distinguishable but meaningless hue on `spo2_pct` vs `heart_rate_bpm`
vs `systolic_bp`, competing for the eye with the alert markers on the CGM line.
Non-CGM series are drawn in `sev.info` at 60% opacity — same channel, different
row on the page.

### 2.2 Type

| token         | size / weight       | use                                                        |
|---------------|---------------------|------------------------------------------------------------|
| `t.metric`    | 28 px / 600         | the one number in the row (`current cgm`)                  |
| `t.body`      | 14 px / 400         | table cells, rule ids                                      |
| `t.mono`      | 13 px / 400 mono    | vp ids, event times, `age • 12 s`                          |
| `t.caption`   | 11 px / 500 caps    | column headers, page section labels                        |

Font stack: system UI for prose, `ui-monospace` for ids and ages. Tabular
figures on every numeric column (`font-variant-numeric: tabular-nums`) so
digits align vertically.

### 2.3 Data-age vocabulary

Age is shown next to *every* live value, not just as one banner at the top.
Format is `{seconds}s ago` when < 90 s, `{minutes}m ago` otherwise. Above the
panel's per-page freshness budget the age pill flips to `stale` grey and the
value dims to 40% opacity — the number is still there, it's just no longer
telling you what's happening now.

Freshness budgets:

- Triage list rows: 30 s (2× tick).
- Patient charts: 15 s.
- Fleet aggregates: 60 s (batch analyzer runs every 15 s).
- System panel: 10 s.

### 2.4 Empty states

Never the string `"no data"`. Every empty state answers *what is the pipeline
doing right now, and when did we last see anything?*

- Panel with no rows in the query window:
  `no {thing} in the last {window} • worker last committed {age} ago
  ({last_committed_ts})`
- Panel that never had data (post-reset):
  `no {thing} yet • replayer has been running for {age}`
- Panel where the query itself failed:
  `query error: {msg} • retrying in 5s`

---

## 3. Pages — ASCII wireframes

The four pages sit in a single sidebar nav. No dashboard-wide KPI banner —
one big-number row would either duplicate the Triage answer or drift from it.

### 3.1 Triage

The row is the primitive. Ranked by `(severity_rank DESC, minutes_since_alert ASC)`.
2,000 patients → default limit 50, paginated. Filters live in the sidebar,
not above the table (they persist across pages).

```
+──────────────────────────────────────────────────────────────────────────────+
| TRIAGE — patients needing attention now                                      |
| worker cursor: 12s ago  •  fleet median cgm: 142 mg/dL  •  50 / 2000 shown   |
+──────────────────────────────────────────────────────────────────────────────+
| VP           CGM    TREND     WHAT FIRED             VERDICT   AGE          |
| ────────── ────── ────────── ──────────────────────  ────────  ──────────   |
| ▮ v0142       47   ↓ 12/min  rules.hypo (crit)       clinical  8 s          |  # crit tint on left bar, chip = clinical
| ▮ v0891      312   ↑  4/min  rules.hyper (crit)      —         11 s         |
| ▮ v1408      580   ▲ single  rules.cgm_roc_spike     sensor    14 s         |  # sensor chip in fault.sensor purple
| ▯ v0023       62   ↓  2/min  rules.hypo (warn)       —         22 s         |  # warn tint
| ▯ v0705    (null)  —         rules.cgm_null          sensor    9 s          |
| ▯ v1902      168   ↑  1/min  batch.firmware_offset   sensor    18 s         |  # in fleet-wide firmware group
| · v0117      158   flat      —                       —         6 s          |  # no active alert, freshest hyper-warn cleared
| ...                                                                          |
| showing 1–50 of 137 patients with an alert in the last 5 min                 |
+──────────────────────────────────────────────────────────────────────────────+
```

- `▮` = critical, `▯` = warn, `·` = info/cleared. Same info as the tint — glyph
  is there for grayscale printouts / colorblind users.
- `TREND` = mg/dL/min over the last 3 minutes from `vitals_1min`. Arrow shape
  is redundant with the sign so it survives colorblind viewing.
- `VERDICT` is derived from the newest alert: any `batch_analysis`,
  `rules.cgm_null`, `rules.cgm_stuck`, `rules.cgm_roc_spike`, or
  `cross_signal.mad_hr_sensor_error` → `sensor`. `cross_signal.hypo_confirmed_by_hr`
  or `cross_signal.mad_hr_possible_clinical` → `clinical`. Everything else → `—`.
  Never a blank cell — the em-dash signals "no cross-signal verdict yet".
- Clicking a row deep-links to the Patient page with the vp pre-selected.

### 3.2 Patient

```
+──────────────────────────────────────────────────────────────────────────────+
| PATIENT  v0142   •   dev  v0142/cgm   •   fw  sim-v1                         |
| source: az:s07  •  speed 10×  •  last reading 8 s ago                        |
+──────────────────────────────────────────────────────────────────────────────+
|                                                                              |
|  CGM (mg/dL)  ── last 60 min in event-time ──────────────────────────────    |
|  300 ─                                                                       |
|      │      ●●                                                               |
|  250 ───────────────────────────────────────────────────────  hyper 250      |
|      │  ●●●●    ●●●                                                          |
|  180 ┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈  in-range hi   |
|      │            ●●                                                         |
|   70 ┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●┈  in-range lo   |
|      │                     ▲   ▲                        ▲                    |
|   40 ─────────────────────────────────────────────────────                   |
|             clinical         sensor              clinical                    |
|             (hypo)           (spike)             (hypo)                      |
|                                                                              |
|  HR / SpO2 / BP  ── same x-axis, half-height ───────────────                 |
|  (three thin lines, all sev.info @60%; annotations only on abnormal)         |
|                                                                              |
|  EVENTS  ── same x-axis, event-time ───────────────                          |
|  ▮ carb 45g                                                                  |
|      │ bolus 3.0u                                                            |
|      │                        ▲ basal 1.2u/hr                                |
+──────────────────────────────────────────────────────────────────────────────+
```

- Reference bands **70** and **180** are the two horizontal lines that decide
  every judgement here. They are drawn with `sev.info` at 30% and labeled at
  the right edge — the operator's eye needs to know instantly whether a point
  is above 180, below 70, or safe.
- Alert markers are triangles (▲) below the CGM line, colored by verdict:
  `sev.critical`/`sev.warn` for clinical, `fault.sensor` for sensor faults.
  Clicking a marker shows the rule_id + payload in a side popover.
- Insulin/carb events come from `raw.readings_raw` joined via
  `virtual_patient.source_patient_id`, with source-time → event-time remap:
  `event_time = min(event_time for this vp) + (src_ts − source_trace_start_utc) / speed_factor`.
  If `dashboard_ro` lacks read on `raw.readings_raw`, this panel shows
  `insulin/carb source not accessible — grant SELECT on raw.readings_raw(...)`.

### 3.3 Fleet

The point of this page is the one thing no individual patient reveals: a
systematic bias in one firmware batch. Everything else is subordinate to that.

```
+──────────────────────────────────────────────────────────────────────────────+
| FLEET — alert rate by firmware batch                                         |
| window: last 15 min • fleet median cgm 142 mg/dL • n devices: 2000           |
+──────────────────────────────────────────────────────────────────────────────+
|                                                                              |
| PER-BATCH MEDIAN CGM vs FLEET MEDIAN                                         |
|                                                                              |
|            fleet median = 142                                                |
|                    │                                                         |
|  sim-v1       n=1897   ●─── 141.6                                            |
|  sim-v1-batch42 n=103         ●────────────────── 156.9   (+14.9 mg/dL)      |
|                    │                                                         |
|              130         140         150         160                         |
|                                                                              |
| ALERT RATE (alerts / patient / hour, last 15 min)                            |
|                                                                              |
|  sim-v1        ██▏ 0.4                                                       |
|  sim-v1-batch42 ████████████████████████▏ 6.2       ← 15× baseline           |
|                                                                              |
| ROLLING FLEET MEDIAN (last 60 min, event-time)                               |
|  145 ─       ╱────╲                                                          |
|  140 ───────╱      ╲──────                                                   |
|  135 ─                                                                       |
+──────────────────────────────────────────────────────────────────────────────+
```

- The per-batch bar plot is drawn with `sev.info`; batches whose |deviation|
  from fleet median exceeds 8 mg/dL (the `BATCH_THRESHOLD_MGDL` the detector
  uses) flip to `fault.sensor`. The threshold line is drawn explicitly.
- The alert-rate bar uses the same rule so an operator sees the batch flagged
  in both views without cross-referencing labels.
- We do not plot per-firmware time-series for every batch — with only two
  firmware versions in this project it would be a two-line chart pretending
  to be a dashboard. The rolling fleet median gives the temporal reference.

### 3.4 System

```
+──────────────────────────────────────────────────────────────────────────────+
| SYSTEM — pipeline health                                                     |
+──────────────────────────────────────────────────────────────────────────────+
|                                                                              |
| ROWS/MIN (last 30 min, from ingested_at)                                     |
|                                                                              |
|  4000 ─                                                                      |
|  3000 ─    ╱──╲    ╱────╲       ╱────╲                                       |
|  2000 ─  ╱     ╲──╱      ╲─────╱      ╲                                      |
|  1000 ─                                                                      |
|                                                                              |
| WORKER LAG                     E2E LATENCY p95                               |
|   cursor age:  8 s              (ingested_at − event_time)                   |
|   lag budget: 15 s                                                           |
|   ▓▓▓▓▓▓▓▓▓▓░░░░░░░░  53%      p50 1.1 s   p95 2.4 s   p99 3.8 s             |
|                                                                              |
| DEAD LETTER (last 15 min)      DUPLICATES SUPPRESSED (last 15 min)           |
|   26 rows / 3,411,204 = 0.008%   142 rows / 3,411,204 = 0.004%               |
|   reasons: malformed 26           (unique(vp,event_time) at insert)          |
|                                                                              |
| PARTITION SIZES (live.vitals_readings)                                       |
|   vitals_readings_p20260929   4.2 GB  (today)                                |
|   vitals_readings_p20260928   3.8 GB                                         |
|   vitals_readings_p20260927   3.9 GB                                         |
|   ...                                                                        |
|                                                                              |
| SLOWEST DASHBOARD QUERIES (last 5 min, this session)                         |
|   panel                       p50     p95    n_calls                         |
|   triage_top_alerts           83 ms   142 ms   61                            |
|   patient_cgm_series          31 ms    55 ms   14                            |
|   fleet_batch_medians         310 ms  480 ms   61                            |
|   system_rows_per_min         44 ms    72 ms   61                            |
+──────────────────────────────────────────────────────────────────────────────+
```

- Query p50/p95 is measured *by the dashboard*, wrapping every panel's fetch
  in a `perf_counter()` pair — this is what the operator experiences, not what
  `pg_stat_statements` reports.
- Latency is `ingested_at − event_time` (in-pipeline lag), not `now() − event_time`
  (which would include the dashboard's own polling delay). The `worker cursor`
  gauge shows the separate "how far behind is the worker" signal.

---

## 4. Review — what got cut after re-reading the brief

I gave the first draft one pass looking for generic dashboard defaults. Five
things changed:

| Before                                                                | After                                                              | Why                                                                                                                                                                              |
|-----------------------------------------------------------------------|--------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| KPI row at top of Triage: `total alerts`, `patients at risk`, `p95 latency` | Removed. Only a one-line "fleet median cgm + worker lag" strap.    | The brief explicitly says no big-number KPI row. A KPI row also implicitly re-answers the question the ranked list already answers — the operator would look at both, then trust neither. |
| Trend column showed a mini sparkline per row.                          | Trend shown as `↑ 4/min` numeric with an arrow glyph.              | Sparklines at 50-row × 2,000-patient list scale are ~100 SVGs re-rendered every 5 s. And a 12-pixel sparkline can't distinguish 4 mg/dL/min from 12 mg/dL/min anyway.        |
| Fleet page had 4 panels: rate, medians, top-N firmwares by rate, alert-type mix pie. | Cut to 3: rate, medians, rolling fleet median. Pie removed.        | With only two firmwares in this dataset the "top-N" panel is degenerate, and the pie chart just re-encodes what the ranked Triage rows already show.                          |
| Verdict column showed the last full `rule_id` string.                  | Verdict is one word: `sensor` / `clinical` / `—`.                  | An operator scanning at 2,000-row scale needs a categorical answer, not a rule engineer's identifier. The rule_id is still there — one row below in the alert popover.        |
| Colors for row severity were red / yellow / green.                     | `#B30021` / `#D97706` / `#334155`. No green.                       | Green means "safe" — but a row on this page only appears if something warrants attention. Painting the info-tier row green would falsely reassure. Info-tier is neutral slate.  |

The remaining choices — 15 s / 60 s freshness budgets, six-token palette,
2-decimal deviation numbers on the Fleet page — trace directly back to
project constants (worker overlap window, batch analyzer interval,
`BATCH_THRESHOLD_MGDL`) rather than my defaults.
