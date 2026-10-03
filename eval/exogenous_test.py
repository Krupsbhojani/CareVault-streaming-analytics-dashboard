"""Offline prequential evaluation on raw.readings_raw to answer one question:
do insulin + carbohydrate features let any model beat persistence at a 15-min
glucose forecasting horizon?

This file does not touch the live pipeline. It builds (features, target) pairs
directly from raw.readings_raw (source-time timestamps, no replay compression),
runs the same four models used by the live pipeline (persistence, linear_trend,
river_linreg, river_ht), and reports the Clarke Error Grid distribution and
MAE / RMSE for each.

Each row is scored twice — once WITHOUT the exogenous features (same feature
set the live stream has) and once WITH the exogenous features that
raw.readings_raw carries but live.vitals_readings does not (insulin bolus,
basal rate, carbohydrate grams). That isolates the effect of the missing
columns, so the answer is either "exog helps by X%" or "it doesn't, which
means the autocorrelation argument is stronger than we claimed".

Cohort: AZT1D only. Shanghai raw rows carry a Chinese free-text 'Dietary intake'
column but zero numeric carb grams, and far fewer insulin events, so it would
contribute noise to the comparison.
"""
from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
import time
from bisect import bisect_left
from collections import deque
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from loader.load_raw import CONNINFO_DEFAULT  # noqa: E402
from processor.ml import (  # noqa: E402
    HORIZON_SEC, LAG_MINUTES, ROLLING_WINDOW_MIN, SLOPE_WINDOW_MIN,
    TOLERANCE_SEC_SOURCE, build_models, clarke_error_grid_zone,
)

# Horizon in source seconds (same semantics as the live pipeline with speed=1).
HORIZON_TD_SEC = HORIZON_SEC
TOL_SEC = TOLERANCE_SEC_SOURCE
# How far back in source time to count insulin/carb events for the "recent"
# feature windows. 60 source minutes matches the live rolling-mean window.
EXOG_WINDOW_MIN = 60


def _lookup_lag(buffer: list, target_ts, tol_sec: float = 45):
    """Buffer is a list of (ts, cgm) sorted ascending. Return the CGM value
    whose ts is within ±tol_sec of target_ts, or None."""
    if not buffer:
        return None
    # Binary search for insertion point.
    i = bisect_left(buffer, (target_ts, -1e18))
    for j in (i - 1, i):
        if 0 <= j < len(buffer):
            ts, cgm = buffer[j]
            if abs((ts - target_ts).total_seconds()) <= tol_sec:
                return float(cgm)
    return None


def _slope(buffer: list, now_ts, window_min: float):
    """Least-squares slope mg/dL per source minute over the last window_min
    minutes of the buffer. None if < 2 points."""
    from datetime import timedelta
    cutoff = now_ts - timedelta(minutes=window_min)
    pts = [(ts, v) for ts, v in buffer if ts >= cutoff]
    if len(pts) < 2:
        return None
    t0 = pts[0][0]
    xs = [(ts - t0).total_seconds() / 60.0 for ts, _ in pts]
    ys = [v for _, v in pts]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n))
    if den == 0:
        return 0.0
    return num / den


def build_features(cgm: float, event_time, cgm_buffer: list, source_tz: str,
                   diabetes_type: str,
                   insulin_recent_u: float | None,
                   carb_recent_g: float | None,
                   basal_rate_u_hr: float | None,
                   with_exog: bool) -> dict:
    """Same shape as the live feature vector, plus the exogenous columns when
    with_exog=True. When with_exog=False the exogenous slots are omitted so
    the models actually see the same feature space the live stream has."""
    from datetime import timedelta
    f: dict = {"cgm": float(cgm)}
    for lag_min in LAG_MINUTES:
        target = event_time - timedelta(minutes=lag_min)
        v = _lookup_lag(cgm_buffer, target)
        f[f"lag_{lag_min}min"] = v if v is not None else cgm
    sl = _slope(cgm_buffer, event_time, SLOPE_WINDOW_MIN)
    f["slope_15min"] = sl if sl is not None else 0.0
    vals = [v for ts, v in cgm_buffer
            if event_time - timedelta(minutes=ROLLING_WINDOW_MIN) <= ts <= event_time]
    if len(vals) >= 2:
        f["roll_mean_60min"] = float(statistics.mean(vals))
        f["roll_std_60min"] = float(statistics.pstdev(vals))
    else:
        f["roll_mean_60min"] = cgm
        f["roll_std_60min"] = 0.0
    # Local hour sin/cos. We don't have minute-level tz here (we are in the
    # patient's own source timezone — raw.readings_raw.source_tz), so just use
    # the raw timestamp's hour; patient-local timezone is already encoded.
    hour = event_time.hour + event_time.minute / 60.0
    f["hour_sin"] = math.sin(2 * math.pi * hour / 24.0)
    f["hour_cos"] = math.cos(2 * math.pi * hour / 24.0)
    f["is_t1"] = 1.0 if diabetes_type == "T1" else 0.0
    f["is_t2"] = 1.0 if diabetes_type == "T2" else 0.0
    f["sampling_sec"] = 300  # AZT1D cohort cadence

    if with_exog:
        f["insulin_recent_u"] = float(insulin_recent_u or 0.0)
        f["carb_recent_g"]    = float(carb_recent_g or 0.0)
        f["basal_rate_u_hr"]  = float(basal_rate_u_hr or 0.0)
    return f


def _collect_recent_exog(patient_rows: list, idx: int, window_min: float
                         ) -> tuple[float, float]:
    """Sum insulin and carb events in the last `window_min` source minutes
    strictly BEFORE patient_rows[idx] (no leakage from current or future)."""
    from datetime import timedelta
    now_ts = patient_rows[idx][0]
    cutoff = now_ts - timedelta(minutes=window_min)
    ins = 0.0
    carb = 0.0
    j = idx - 1
    while j >= 0 and patient_rows[j][0] >= cutoff:
        _, _, bolus_u, _basal, _iv_u, carb_g, _is_clamped = patient_rows[j]
        if bolus_u is not None:
            ins += float(bolus_u)
        if carb_g is not None:
            carb += float(carb_g)
        j -= 1
    return ins, carb


def _find_target_index(patient_rows: list, i: int) -> int | None:
    """Return the index j > i whose ts matches patient_rows[i].ts + HORIZON ±
    TOL_SEC, or None. patient_rows is sorted by ts ascending."""
    from datetime import timedelta
    target_lo = patient_rows[i][0] + timedelta(seconds=HORIZON_TD_SEC - TOL_SEC)
    target_hi = patient_rows[i][0] + timedelta(seconds=HORIZON_TD_SEC + TOL_SEC)
    # Linear scan forward; patient traces are short enough that this is fast,
    # and most matches are within a few rows.
    for j in range(i + 1, len(patient_rows)):
        ts_j = patient_rows[j][0]
        if ts_j < target_lo:
            continue
        if ts_j > target_hi:
            return None
        return j
    return None


def _build_examples(patients: dict, with_exog: bool, source_tz: str,
                    diabetes_type: str, buffer_keep_min: int = 65) -> list:
    """Build (ts, features, actual) tuples per patient, chronologically.
    Returns the list in GLOBAL ts order so prequential iteration below respects
    real-time ordering across patients."""
    from datetime import timedelta
    examples = []
    for pid, rows in patients.items():
        buffer: list = []  # [(ts, cgm)], sorted ascending
        n_rows = len(rows)
        for i in range(n_rows):
            ts, cgm, bolus_u, basal, iv_u, carb_g, is_clamped = rows[i]
            if cgm is None or is_clamped:
                continue
            # Target lookup in the patient's own trace.
            j = _find_target_index(rows, i)
            if j is None:
                continue
            target_ts, target_cgm, *_rest, target_is_clamped = rows[j]
            # 'rest' includes bolus/basal/iv/carb which we don't use for the label.
            if target_cgm is None:
                continue
            # Features at time ts (no leakage: buffer only has readings strictly
            # earlier than ts because we haven't appended the current row yet).
            if with_exog:
                ins_recent, carb_recent = _collect_recent_exog(rows, i, EXOG_WINDOW_MIN)
            else:
                ins_recent = carb_recent = None
            feats = build_features(float(cgm), ts, buffer, source_tz, diabetes_type,
                                   insulin_recent_u=ins_recent,
                                   carb_recent_g=carb_recent,
                                   basal_rate_u_hr=basal if with_exog else None,
                                   with_exog=with_exog)
            examples.append((ts, feats, float(target_cgm)))
            # Only now add the current reading to the buffer, so lag_5min on
            # the NEXT row looks up a reading that was already observed.
            buffer.append((ts, float(cgm)))
            # Trim.
            cutoff = ts - timedelta(minutes=buffer_keep_min)
            while buffer and buffer[0][0] < cutoff:
                buffer.pop(0)
    examples.sort(key=lambda x: x[0])
    return examples


def _run_prequential(examples: list) -> dict:
    """Prequential loop: for each example, PREDICT with each model (that's the
    score the metrics are built on), then LEARN. Returns {model_name: [(pred,
    actual), ...]}."""
    models = build_models()
    series: dict[str, list[tuple[float, float]]] = {k: [] for k in models}
    for _ts, feats, actual in examples:
        for name, model in models.items():
            try:
                y = model.predict(feats)
            except Exception:
                y = float("nan")
            if y == y and math.isfinite(y):
                series[name].append((float(y), float(actual)))
            try:
                model.learn(feats, float(actual))
            except Exception:
                pass
    return series


def _metrics(series: dict) -> dict:
    """MAE, RMSE, Clarke zones per model. Returns {name: {...}}."""
    out = {}
    for name, pairs in series.items():
        if not pairs:
            out[name] = {"n": 0, "mae": float("nan"), "rmse": float("nan"),
                         "zones": {z: 0 for z in "ABCDE"}}
            continue
        n = len(pairs)
        abs_errs = [abs(p - a) for p, a in pairs]
        sq_errs  = [(p - a) ** 2 for p, a in pairs]
        mae = sum(abs_errs) / n
        rmse = math.sqrt(sum(sq_errs) / n)
        zones = {z: 0 for z in "ABCDE"}
        for p, a in pairs:
            zones[clarke_error_grid_zone(a, p)] += 1
        out[name] = {"n": n, "mae": mae, "rmse": rmse, "zones": zones}
    return out


def _print_block(label: str, metrics: dict, baseline_name: str = "persistence") -> None:
    base = metrics.get(baseline_name, {}).get("mae")
    print(label)
    print(f"  {'model':<16s} {'n':>10s} {'MAE':>8s} {'RMSE':>10s} {'% vs pers':>12s} "
          f"{'A':>8s} {'B':>8s} {'C':>7s} {'D':>7s} {'E':>7s}")
    for name in ("persistence", "linear_trend", "river_linreg", "river_ht"):
        m = metrics.get(name, {})
        n = m.get("n", 0)
        if n == 0:
            print(f"  {name:<16s} {0:>10d} {'n/a':>8s}")
            continue
        mae = m["mae"]
        rmse = m["rmse"]
        if base and name != baseline_name and base > 0:
            pct = (base - mae) / base * 100.0
            pct_str = f"{pct:+.1f}%"
        else:
            pct_str = ""
        z = m["zones"]
        print(f"  {name:<16s} {n:>10,d} {mae:>8.2f} {rmse:>10.2f} {pct_str:>12s} "
              f"{z['A']:>8,d} {z['B']:>8,d} {z['C']:>7,d} {z['D']:>7,d} {z['E']:>7,d}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--conninfo", default=os.environ.get("CAREVAULT_PG", CONNINFO_DEFAULT))
    ap.add_argument("--cohort", default="AZT1D",
                    help="raw cohort to use (AZT1D has usable insulin+carbs; Shanghai does not).")
    args = ap.parse_args(argv)

    print(f"Loading raw.readings_raw cohort={args.cohort}...", flush=True)
    t0 = time.perf_counter()
    patients: dict = {}
    with psycopg.connect(args.conninfo) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT patient_id, ts_utc, cgm_mgdl,
                       bolus_u, basal_rate_u_hr, insulin_iv_u, carb_g,
                       is_clamped, diabetes_type, source_tz
                  FROM raw.readings_raw
                 WHERE cohort = %s
                 ORDER BY patient_id, ts_utc
            """, (args.cohort,))
            diabetes_type = "T1"  # all cohort rows share diabetes_type in AZT1D
            source_tz = "UTC"
            for pid, ts, cgm, bolus, basal, iv, carb, is_clamped, dtype, tz in cur.fetchall():
                diabetes_type = dtype or diabetes_type
                source_tz = tz or source_tz
                patients.setdefault(pid, []).append(
                    (ts, cgm, bolus, basal, iv, carb, bool(is_clamped))
                )
    dt_load = time.perf_counter() - t0
    n_rows = sum(len(v) for v in patients.values())
    print(f"  {n_rows:,} rows across {len(patients)} patients  (load {dt_load:.1f}s)",
          flush=True)

    for with_exog in (False, True):
        label = "WITH insulin / carb / basal" if with_exog else "WITHOUT exog (same features as live stream)"
        print(f"\nBuilding examples [{label}]...", flush=True)
        t0 = time.perf_counter()
        examples = _build_examples(patients, with_exog=with_exog,
                                   source_tz=source_tz, diabetes_type=diabetes_type)
        print(f"  {len(examples):,} (features, target) pairs  "
              f"(build {time.perf_counter() - t0:.1f}s)", flush=True)
        print("Scoring prequentially...", flush=True)
        t0 = time.perf_counter()
        series = _run_prequential(examples)
        print(f"  done in {time.perf_counter() - t0:.1f}s", flush=True)
        metrics = _metrics(series)
        print()
        _print_block(label, metrics)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
