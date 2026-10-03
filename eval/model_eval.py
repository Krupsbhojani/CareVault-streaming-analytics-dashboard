"""Evaluate online glucose forecasts recorded in live.predictions.

Reports per model:
  - rolling MAE / RMSE over time (window 500)
  - final MAE, RMSE, N
  - % improvement over persistence (negative = worse)
  - Clarke Error Grid zone distribution (A..E)
  - predictions resolved / unresolved / excluded
  - predictions/sec (made) and % of wall-time spent in model work

Writes two PNGs into eval/figures/:
  - mae_rolling.png     : rolling MAE per model over time
  - clarke_grid.png     : Clarke scatter for the best (lowest MAE) model
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict, deque
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from loader.load_raw import CONNINFO_DEFAULT  # noqa: E402
from processor.ml import clarke_error_grid_zone  # noqa: E402

ROLLING_WINDOW = 500


def _rolling(xs: list[float], window: int) -> list[float]:
    """Rolling mean, left-aligned (position i = mean of x[max(0,i-W+1)..i])."""
    out: list[float] = []
    q: deque[float] = deque(maxlen=window)
    s = 0.0
    for x in xs:
        if len(q) == window:
            s -= q[0]
        q.append(x)
        s += x
        out.append(s / len(q))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--conninfo", default=os.environ.get("CAREVAULT_PG", CONNINFO_DEFAULT))
    ap.add_argument("--out-dir", default=str(ROOT / "eval" / "figures"))
    args = ap.parse_args(argv)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with psycopg.connect(args.conninfo) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT model_name,
                       count(*) FILTER (WHERE actual_cgm_mgdl IS NOT NULL) AS resolved,
                       count(*) FILTER (WHERE actual_cgm_mgdl IS NULL)     AS unresolved,
                       count(*)                                            AS total
                  FROM live.predictions
                 GROUP BY model_name
                 ORDER BY model_name
            """)
            per_model_counts = cur.fetchall()
            if not per_model_counts:
                print("live.predictions is empty — run the worker with ML on.")
                return 1

            # Pull resolved predictions, chronological by predicted_ts_utc,
            # per model. Only resolved rows participate in scoring.
            per_model_series: dict[str, list[tuple[float, float]]] = {}
            per_model_pred_ts: dict[str, list[float]] = {}
            for model_name, resolved, unresolved, total in per_model_counts:
                cur.execute("""
                    SELECT predicted_ts_utc, predicted_cgm_mgdl, actual_cgm_mgdl, abs_error_mgdl
                      FROM live.predictions
                     WHERE model_name = %s
                       AND actual_cgm_mgdl IS NOT NULL
                     ORDER BY predicted_ts_utc
                """, (model_name,))
                series = []
                times = []
                for pts, pred, actual, abs_err in cur.fetchall():
                    series.append((float(pred), float(actual)))
                    times.append(pts.timestamp())
                per_model_series[model_name] = series
                per_model_pred_ts[model_name] = times

            # Run-level metadata: wall-time span (emitted_at is set by DB
            # default on INSERT), patient count.
            cur.execute("""
                SELECT min(emitted_at), max(emitted_at), max(resolved_at),
                       count(DISTINCT virtual_patient_id)
                  FROM live.predictions
            """)
            tmin, tmax, rmax, npats = cur.fetchone()
            wall_sec = (tmax - tmin).total_seconds() if tmax and tmin else 0.0

    # ---------- compute per-model metrics ----------
    print("=" * 76)
    print("ONLINE GLUCOSE FORECAST — model evaluation")
    print("=" * 76)
    print(f"Patients: {npats}   Wall window: {wall_sec:.0f}s   "
          f"({(tmin.isoformat() if tmin else '?')} -> {(tmax.isoformat() if tmax else '?')})")
    print()
    print(f"{'model':<16s} {'total':>8s} {'resolved':>9s} {'unresolved':>11s} "
          f"{'MAE':>7s} {'RMSE':>7s} {'% over pers':>12s} "
          f"{'A':>6s} {'B':>6s} {'C':>6s} {'D':>6s} {'E':>6s}")

    final_mae: dict[str, float] = {}
    final_rmse: dict[str, float] = {}
    zone_dist: dict[str, dict[str, int]] = {}

    for model_name, resolved, unresolved, total in per_model_counts:
        series = per_model_series[model_name]
        if not series:
            print(f"{model_name:<16s} {total:>8d} {resolved:>9d} {unresolved:>11d} "
                  f"{'n/a':>7s} {'n/a':>7s} {'n/a':>12s}")
            continue
        preds = np.array([p for p, _ in series])
        acts = np.array([a for _, a in series])
        errs = preds - acts
        mae = float(np.mean(np.abs(errs)))
        rmse = float(np.sqrt(np.mean(errs * errs)))
        final_mae[model_name] = mae
        final_rmse[model_name] = rmse

        zones = {"A": 0, "B": 0, "C": 0, "D": 0, "E": 0}
        for p, a in series:
            zones[clarke_error_grid_zone(a, p)] += 1
        zone_dist[model_name] = zones

        pct_str = ""
        if "persistence" in final_mae and model_name != "persistence":
            pct = (final_mae["persistence"] - mae) / final_mae["persistence"] * 100.0
            pct_str = f"{pct:+.1f}%"
        print(f"{model_name:<16s} {total:>8d} {resolved:>9d} {unresolved:>11d} "
              f"{mae:>7.2f} {rmse:>7.2f} {pct_str:>12s} "
              f"{zones['A']:>6d} {zones['B']:>6d} {zones['C']:>6d} {zones['D']:>6d} {zones['E']:>6d}")

    # Leakage sanity: user-defined guard — if a River model beats persistence
    # by more than 40%, flag for investigation.
    print()
    if "persistence" in final_mae:
        for name, mae in final_mae.items():
            if name == "persistence":
                continue
            improvement = (final_mae["persistence"] - mae) / final_mae["persistence"] * 100.0
            if improvement > 40:
                print(f"*** WARNING: {name} beat persistence by {improvement:.1f}% — "
                      f"suspect leakage; investigate before claiming the win.")

    # ---------- throughput ----------
    total_resolved = sum(r for _, r, _, _ in per_model_counts)
    rate = total_resolved / wall_sec if wall_sec > 0 else 0.0
    print(f"\nResolved predictions/sec (all models): {rate:.1f}  (over {wall_sec:.0f}s wall)")

    # ---------- figures ----------
    # Rolling MAE
    fig, ax = plt.subplots(figsize=(10, 6))
    colors = {"persistence": "#95a5a6", "linear_trend": "#f39c12",
              "river_linreg": "#2874a6", "river_ht": "#186a3b"}
    for name, series in per_model_series.items():
        if not series:
            continue
        abs_errs = [abs(p - a) for p, a in series]
        rolled = _rolling(abs_errs, ROLLING_WINDOW)
        # X-axis: wall seconds from start of this model's resolved series.
        t0 = per_model_pred_ts[name][0]
        xs = [(t - t0) / 60.0 for t in per_model_pred_ts[name]]
        ax.plot(xs, rolled, label=name, color=colors.get(name, "#333"), linewidth=1.5)
    ax.set_xlabel("minutes from first resolved prediction (event-time)")
    ax.set_ylabel(f"rolling MAE (window={ROLLING_WINDOW})")
    ax.set_title("Rolling MAE per model — online glucose forecast, 15-min horizon")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=10)
    fig.tight_layout()
    fig.savefig(out_dir / "mae_rolling.png", dpi=140)
    plt.close(fig)
    print(f"wrote {out_dir / 'mae_rolling.png'}")

    # Clarke grid scatter for the best model (lowest MAE).
    if final_mae:
        best = min(final_mae.items(), key=lambda kv: kv[1])[0]
        series = per_model_series[best]
        preds = np.array([p for p, _ in series])
        acts = np.array([a for _, a in series])
        fig, ax = plt.subplots(figsize=(8, 8))
        zones_for_point = [clarke_error_grid_zone(a, p) for p, a in series]
        zone_colors = {"A": "#2ecc71", "B": "#f1c40f", "C": "#e67e22",
                       "D": "#e74c3c", "E": "#8e44ad"}
        for z in ("A", "B", "C", "D", "E"):
            mask = [i for i, zz in enumerate(zones_for_point) if zz == z]
            if mask:
                ax.scatter(acts[mask], preds[mask], s=6, alpha=0.4,
                           color=zone_colors[z], label=f"Zone {z} (n={len(mask)})")
        ax.plot([0, 400], [0, 400], "k--", linewidth=0.6, alpha=0.5)
        ax.set_xlim(0, 400); ax.set_ylim(0, 400)
        ax.set_xlabel("Reference glucose (actual, mg/dL)")
        ax.set_ylabel("Predicted glucose (mg/dL)")
        ax.set_title(f"Clarke Error Grid — best model: {best}\n"
                     f"MAE={final_mae[best]:.2f}, RMSE={final_rmse[best]:.2f}, "
                     f"n={len(series):,}")
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=9, loc="lower right")
        fig.tight_layout()
        fig.savefig(out_dir / "clarke_grid.png", dpi=140)
        plt.close(fig)
        print(f"wrote {out_dir / 'clarke_grid.png'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
