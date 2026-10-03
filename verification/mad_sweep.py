"""Offline MAD threshold sweep against the current 30-min run data.

Re-simulates the detector across a range of |z| thresholds — including one
variant that suppresses MAD flags during rapid glucose-rise periods — and
reports precision, recall, F1, and alerts-per-patient-per-hour for spike
detection at each threshold.

Reads live.vitals_readings as the ADMIN role (so it can see injected_type).
Emits a CSV to scratch/mad_sweep.csv for verification/figures.py to plot.
"""
from __future__ import annotations

import argparse
import csv
import os
import statistics
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from loader.load_raw import CONNINFO_DEFAULT  # noqa: E402
from processor.outliers import (  # noqa: E402
    CLAMP_VALUES, MAD_WARMUP, MAD_WINDOW, RANGE as OUTLIER_RANGE,
)

THRESHOLDS = [round(3.0 + 0.5 * i, 1) for i in range(11)]  # 3.0 .. 8.0
SLOPE_MGDL_PER_MIN = 2.0     # postprandial excursion threshold
SLOPE_LOOKBACK = 3           # need 3 consecutive rising deltas to suppress
CGM_CEILING = OUTLIER_RANGE["cgm_mgdl"][1]  # 476


@dataclass
class VpMadState:
    window: deque = field(default_factory=lambda: deque(maxlen=MAD_WINDOW))
    warmup: int = 0
    # For slope filter: rolling buffer of (ts, cgm) values.
    recent: deque = field(default_factory=lambda: deque(maxlen=SLOPE_LOOKBACK + 1))

    def in_rapid_rise(self) -> bool:
        if len(self.recent) < SLOPE_LOOKBACK + 1:
            return False
        pts = list(self.recent)
        for i in range(1, len(pts)):
            (t0, v0), (t1, v1) = pts[i - 1], pts[i]
            dt_min = (t1 - t0).total_seconds() / 60.0
            if dt_min <= 0:
                return False
            slope = (v1 - v0) / dt_min
            if slope <= SLOPE_MGDL_PER_MIN:
                return False
        return True


def _mad_z(value: float, window: deque) -> float | None:
    if len(window) < 5:
        return None
    med = statistics.median(window)
    mad = statistics.median(abs(x - med) for x in window)
    if mad < 1e-9:
        return None
    return 0.6745 * (value - med) / mad


def simulate(rows, thresholds, use_slope_filter: bool):
    """rows: iterable of (vp_id, event_time, cgm_mgdl, injected_type).

    Returns:
      mad_hits_on_spike[threshold] = set of (vp, ts) MAD flagged that are labeled spike
      mad_hits_on_clean[threshold] = set of (vp, ts) MAD flagged that are labeled None
      rules_hits_spike             = set of (vp, ts) with cgm > CGM_CEILING and injected=spike
      rules_hits_clean             = set of (vp, ts) with cgm > CGM_CEILING and injected!=spike
      n_spike_positives            = total distinct injected='spike' rows
      n_clean_after_warmup         = total clean rows evaluated post-warmup
    """
    mad_hits_on_spike = {t: set() for t in thresholds}
    mad_hits_on_clean = {t: set() for t in thresholds}
    rules_hits_spike: set[tuple[str, datetime]] = set()
    rules_hits_clean: set[tuple[str, datetime]] = set()
    n_spike_positives_seen: set[tuple[str, datetime]] = set()

    vp_state: dict[str, VpMadState] = {}
    n_clean_after_warmup = 0

    for vp_id, ts, cgm, injected in rows:
        state = vp_state.setdefault(vp_id, VpMadState())

        if injected == "spike":
            n_spike_positives_seen.add((vp_id, ts))

        # Rules-based spike: values above the physiologic ceiling.
        if cgm is not None and cgm > CGM_CEILING:
            if injected == "spike":
                rules_hits_spike.add((vp_id, ts))
            else:
                rules_hits_clean.add((vp_id, ts))

        if cgm is None or cgm in CLAMP_VALUES:
            continue

        state.recent.append((ts, cgm))

        if state.warmup < MAD_WARMUP:
            state.window.append(cgm)
            state.warmup += 1
            continue

        z = _mad_z(cgm, state.window)
        if z is None:
            state.window.append(cgm)
            continue

        abs_z = abs(z)
        suppressed_by_slope = use_slope_filter and state.in_rapid_rise()

        if injected is None:
            n_clean_after_warmup += 1

        for thr in thresholds:
            if abs_z > thr and not suppressed_by_slope:
                key = (vp_id, ts)
                if injected == "spike":
                    mad_hits_on_spike[thr].add(key)
                elif injected is None:
                    mad_hits_on_clean[thr].add(key)

        # Window-inclusion: use the strictest (largest) threshold so window is
        # the same across sweep points (fair comparison).
        if abs_z <= max(thresholds):
            state.window.append(cgm)

    return {
        "mad_hits_on_spike": mad_hits_on_spike,
        "mad_hits_on_clean": mad_hits_on_clean,
        "rules_hits_spike": rules_hits_spike,
        "rules_hits_clean": rules_hits_clean,
        "n_spike_positives": len(n_spike_positives_seen),
        "n_clean_after_warmup": n_clean_after_warmup,
    }


def compute_stats(sim, duration_hours: float, n_patients: int):
    """Combine MAD hits with rules hits using set unions on (vp, ts)."""
    rules_hits_spike = sim["rules_hits_spike"]
    rules_hits_clean = sim["rules_hits_clean"]
    n_spike_positives = sim["n_spike_positives"]
    n_clean_after_warmup = sim["n_clean_after_warmup"]

    n_rules_hits = len(rules_hits_spike)
    rules_only_recall = n_rules_hits / n_spike_positives if n_spike_positives else 0.0

    results = []
    for thr in sorted(sim["mad_hits_on_spike"].keys()):
        mad_on_spike = sim["mad_hits_on_spike"][thr]
        mad_on_clean = sim["mad_hits_on_clean"][thr]

        # Real set unions — no over-counting.
        combined_hits_spike = rules_hits_spike | mad_on_spike
        combined_hits_clean = rules_hits_clean | mad_on_clean

        combined_tp = len(combined_hits_spike)
        combined_fp = len(combined_hits_clean)
        combined_fn = n_spike_positives - combined_tp
        precision = combined_tp / (combined_tp + combined_fp) if (combined_tp + combined_fp) else float("nan")
        recall = combined_tp / n_spike_positives if n_spike_positives else float("nan")
        f1 = (2 * precision * recall / (precision + recall)
              if precision and recall and (precision + recall) > 0 else float("nan"))

        # Alerts-per-patient-per-hour on CLEAN rows: MAD alerts on clean only.
        # (Rules by construction never fires on clean since cgm <= 476 for organic.)
        alerts_ppph_clean = len(mad_on_clean) / n_patients / duration_hours

        results.append({
            "threshold": thr,
            "combined_tp": combined_tp,
            "combined_fp": combined_fp,
            "combined_fn": combined_fn,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "mad_alerts_on_spike": len(mad_on_spike),
            "mad_alerts_on_clean": len(mad_on_clean),
            "mad_alerts_total": len(mad_on_spike) + len(mad_on_clean),
            "alerts_per_patient_per_hour_on_clean": alerts_ppph_clean,
        })
    return results, {
        "n_spike_positives": n_spike_positives,
        "n_rules_hits": n_rules_hits,
        "rules_only_recall": rules_only_recall,
        "rules_only_alerts_on_clean": 0,
        "n_clean_after_warmup": n_clean_after_warmup,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--conninfo", default=os.environ.get("CAREVAULT_PG", CONNINFO_DEFAULT))
    ap.add_argument("--out-csv", default=str(ROOT / "scratch" / "mad_sweep.csv"))
    args = ap.parse_args(argv)

    print("Loading vitals_readings from DB (admin, sees injected_type)...", flush=True)
    with psycopg.connect(args.conninfo) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT virtual_patient_id, event_time, cgm_mgdl, injected_type
                  FROM live.vitals_readings
                 ORDER BY virtual_patient_id, event_time
            """)
            rows = cur.fetchall()
            cur.execute("SELECT count(DISTINCT virtual_patient_id) FROM live.vitals_readings;")
            n_patients = cur.fetchone()[0]
            cur.execute("""
                SELECT extract(epoch from max(event_time) - min(event_time))/3600.0
                  FROM live.vitals_readings
            """)
            duration_hours = float(cur.fetchone()[0])
    print(f"  {len(rows):,} rows, {n_patients} vps, {duration_hours:.2f} h of data",
          flush=True)

    variants = {}
    for name, use_slope in (("baseline", False), ("with_slope_filter", True)):
        print(f"Simulating variant={name}...", flush=True)
        sim = simulate(rows, THRESHOLDS, use_slope)
        results, meta = compute_stats(sim, duration_hours, n_patients)
        variants[name] = {"results": results, "meta": meta}

    # Print header
    print()
    print("=" * 108)
    print("MAD THRESHOLD SWEEP  (30-min run; spike injections uniform in [400, 600]; ceiling for rules-based catch = 476)")
    print("=" * 108)
    for name, v in variants.items():
        m = v["meta"]
        print()
        print(f"variant: {name}   spike_positives={m['n_spike_positives']}   "
              f"rules_hits(cgm>{CGM_CEILING:.0f})={m['n_rules_hits']} "
              f"({m['rules_only_recall']*100:.1f}% recall alone)   "
              f"clean_after_warmup={m['n_clean_after_warmup']:,}")
        print(f"  {'threshold':>9}  {'MAD/spike':>10}  {'MAD/clean':>10}  "
              f"{'combined_TP':>11}  {'combined_FP':>11}  {'combined_FN':>11}  "
              f"{'precision':>9}  {'recall':>7}  {'F1':>6}  {'alerts/pt/hr(clean)':>19}")
        for r in v["results"]:
            print(f"  {r['threshold']:>9.1f}  {r['mad_alerts_on_spike']:>10d}  "
                  f"{r['mad_alerts_on_clean']:>10d}  "
                  f"{r['combined_tp']:>11d}  {r['combined_fp']:>11d}  {r['combined_fn']:>11d}  "
                  f"{r['precision']:>9.3f}  {r['recall']:>7.3f}  {r['f1']:>6.3f}  "
                  f"{r['alerts_per_patient_per_hour_on_clean']:>19.3f}")

    # Write CSV for figures.py
    out = Path(args.out_csv)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["variant", "threshold", "mad_on_spike", "mad_on_clean",
                    "combined_tp", "combined_fp", "combined_fn",
                    "precision", "recall", "f1", "alerts_per_patient_per_hour_on_clean",
                    "n_spike_positives", "n_rules_hits", "n_clean_after_warmup",
                    "duration_hours", "n_patients"])
        for name, v in variants.items():
            m = v["meta"]
            for r in v["results"]:
                w.writerow([name, r["threshold"], r["mad_alerts_on_spike"],
                            r["mad_alerts_on_clean"], r["combined_tp"],
                            r["combined_fp"], r["combined_fn"],
                            f"{r['precision']:.6f}", f"{r['recall']:.6f}", f"{r['f1']:.6f}",
                            f"{r['alerts_per_patient_per_hour_on_clean']:.6f}",
                            m["n_spike_positives"], m["n_rules_hits"], m["n_clean_after_warmup"],
                            f"{duration_hours:.4f}", n_patients])
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
