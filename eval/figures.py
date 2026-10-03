"""Generate report figures.

Two PNGs:
  eval/figures/pr_curve.png  — MAD threshold sweep (baseline vs slope-filter)
                                with rules-only reference; alerts-per-patient-
                                per-hour on clean shown as a second Y axis.
  eval/figures/per_type.png  — per-fault-type P/R/F1 bar chart from current
                                live.alerts + live.vitals_readings.

Inputs:
  scratch/mad_sweep.csv  — produced by verification/mad_sweep.py.
  DB (admin conninfo)    — reads live.alerts and live.vitals_readings.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless (Windows/CI safe)
import matplotlib.pyplot as plt
import numpy as np
import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from loader.load_raw import CONNINFO_DEFAULT  # noqa: E402
from verification.eval_processor import DETECTS  # noqa: E402


def _load_sweep_csv(path: Path):
    rows = []
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({
                "variant": r["variant"],
                "threshold": float(r["threshold"]),
                "precision": float(r["precision"]),
                "recall": float(r["recall"]),
                "f1": float(r["f1"]),
                "alerts_ppph_clean": float(r["alerts_per_patient_per_hour_on_clean"]),
                "n_spike_positives": int(r["n_spike_positives"]),
                "n_rules_hits": int(r["n_rules_hits"]),
            })
    return rows


def _load_roc_point(conninfo: str) -> tuple[float, float] | None:
    """Query the current live.alerts for the ROC-rule PR point:
    (recall, precision) using rules.cgm_roc_spike as the sole predictor."""
    try:
        with psycopg.connect(conninfo, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                      (SELECT count(*) FROM live.vitals_readings WHERE injected_type='spike') AS positives,
                      (SELECT count(*) FROM live.vitals_readings v
                        WHERE v.injected_type='spike'
                          AND EXISTS (
                            SELECT 1 FROM live.alerts a
                             WHERE a.virtual_patient_id=v.virtual_patient_id
                               AND a.trigger_event_time=v.event_time
                               AND a.rule_id='rules.cgm_roc_spike'
                          )) AS tp,
                      (SELECT count(DISTINCT (a.virtual_patient_id, a.trigger_event_time))
                         FROM live.alerts a
                        WHERE a.rule_id='rules.cgm_roc_spike') AS predicted
                """)
                positives, tp, predicted = cur.fetchone()
        if not positives or predicted is None:
            return None
        precision = tp / predicted if predicted else 0.0
        recall = tp / positives if positives else 0.0
        return recall, precision
    except Exception as e:
        print(f"  (ROC point query failed: {e})")
        return None


def figure_pr_curve(csv_path: Path, out_path: Path, conninfo: str) -> None:
    rows = _load_sweep_csv(csv_path)
    variants = {}
    for r in rows:
        variants.setdefault(r["variant"], []).append(r)
    for v in variants.values():
        v.sort(key=lambda x: x["threshold"])

    n_positives = rows[0]["n_spike_positives"]
    n_rules = rows[0]["n_rules_hits"]
    rules_precision = 1.0  # by construction (ceiling above every organic value)
    rules_recall = n_rules / n_positives if n_positives else 0.0
    roc_point = _load_roc_point(conninfo)

    fig, ax = plt.subplots(figsize=(10, 6.5))
    colors = {"baseline": "#c0392b", "with_slope_filter": "#2874a6"}
    for name, series in variants.items():
        xs = [p["recall"] for p in series]
        ys = [p["precision"] for p in series]
        ax.plot(xs, ys, marker="o", color=colors.get(name, "#333"),
                label=f"MAD ({name})", linewidth=2, markersize=6)
        # Threshold annotations at every other point.
        for i, p in enumerate(series):
            if i % 2 == 0:
                ax.annotate(f"{p['threshold']:.1f}", xy=(p["recall"], p["precision"]),
                            xytext=(4, 4), textcoords="offset points", fontsize=8,
                            color=colors.get(name, "#333"))

    # Rules-only reference (single point).
    ax.scatter([rules_recall], [rules_precision], marker="*", s=280,
               color="#1e8449", zorder=5, label="rules only (cgm > ceiling)")
    ax.annotate(f"rules alone\n({rules_recall*100:.1f}% recall, P=1.0)",
                xy=(rules_recall, rules_precision),
                xytext=(-10, -22), textcoords="offset points", fontsize=9,
                color="#1e8449", ha="right")

    # ROC rule reference point (new — recommendation 5).
    if roc_point is not None:
        rr, rp = roc_point
        ax.scatter([rr], [rp], marker="D", s=180, color="#8e44ad", zorder=5,
                   label="rules + ROC (Δ/Δt + return-to-baseline)")
        ax.annotate(f"rules + ROC\n(R={rr*100:.1f}%, P={rp:.3f})",
                    xy=(rr, rp),
                    xytext=(10, 6), textcoords="offset points", fontsize=9,
                    color="#8e44ad")

    # Shipability band: precision >= 0.5 AND recall >= 0.5 (informal).
    ax.axhspan(0.5, 1.02, alpha=0.06, color="#2ecc71", zorder=0)
    ax.text(0.02, 0.95, "precision ≥ 0.5 (broadly shippable)", fontsize=8,
            color="#186a3b", transform=ax.transData)

    ax.set_xlabel("Recall (spike detection)")
    ax.set_ylabel("Precision")
    ax.set_title("MAD threshold sweep — spike P/R\n(labels = |z| threshold; 30-min AZT1D run, 2000 vps @ 10x)")
    ax.set_xlim(0, 1.02)
    ax.set_ylim(0, 1.02)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper center", fontsize=10)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
    print(f"wrote {out_path}")

    # Second figure: alerts/pt/hr vs threshold.
    fig2, ax2 = plt.subplots(figsize=(9, 5.5))
    for name, series in variants.items():
        xs = [p["threshold"] for p in series]
        ys = [p["alerts_ppph_clean"] for p in series]
        ax2.plot(xs, ys, marker="o", color=colors.get(name, "#333"),
                 label=name, linewidth=2)
    ax2.axhline(0.04, color="#186a3b", linestyle="--", linewidth=1,
                label="≈ 1 alert/patient/day (shippable)")
    ax2.axhline(0.5, color="#b7950b", linestyle="--", linewidth=1,
                label="≈ 12 alerts/patient/day (alarm fatigue)")
    ax2.set_xlabel("|z| threshold")
    ax2.set_ylabel("MAD alerts / patient / hour on clean readings")
    ax2.set_title("MAD false-positive rate on clean readings, by threshold\n(bar = ≈1 alert/patient/day)")
    ax2.set_yscale("log")
    ax2.grid(True, alpha=0.3, which="both")
    ax2.legend(loc="upper right", fontsize=9)
    fig2.tight_layout()
    out2 = out_path.with_name("mad_alerts_per_patient_per_hour.png")
    fig2.savefig(out2, dpi=140)
    plt.close(fig2)
    print(f"wrote {out2}")


def figure_per_type(conninfo: str, out_path: Path) -> None:
    """Query per-fault-type detection stats from current DB and bar-chart them."""
    with psycopg.connect(conninfo) as conn:
        stats = {}
        for fault, detect_expr in DETECTS.items():
            with conn.cursor() as cur:
                cur.execute(f"""
                    WITH inj AS (
                        SELECT virtual_patient_id, event_time
                          FROM live.vitals_readings
                         WHERE injected_type = %s
                    ),
                    matched AS (
                        SELECT DISTINCT a.virtual_patient_id, a.trigger_event_time
                          FROM live.alerts a
                         WHERE {detect_expr}
                    )
                    SELECT
                        (SELECT count(*) FROM inj)     AS positives,
                        (SELECT count(*) FROM inj i
                           WHERE EXISTS (
                               SELECT 1 FROM matched m
                                WHERE m.virtual_patient_id=i.virtual_patient_id
                                  AND m.trigger_event_time=i.event_time)) AS tp,
                        (SELECT count(*) FROM matched) AS predicted;
                """, (fault,))
                positives, tp, predicted = cur.fetchone()
                fp = predicted - tp
                fn = positives - tp
                p = tp / predicted if predicted else 0.0
                r = tp / positives if positives else 0.0
                f1 = (2 * p * r / (p + r)) if (p + r) > 0 else 0.0
                stats[fault] = {"precision": p, "recall": r, "f1": f1,
                                "positives": positives, "tp": tp, "fp": fp, "fn": fn}

    faults = list(stats.keys())
    metrics = ["precision", "recall", "f1"]
    n_faults = len(faults)
    n_metrics = len(metrics)
    x = np.arange(n_faults)
    width = 0.26
    fig, ax = plt.subplots(figsize=(10, 6))
    colors = {"precision": "#2874a6", "recall": "#c0392b", "f1": "#186a3b"}
    for i, m in enumerate(metrics):
        vals = [stats[f][m] for f in faults]
        offsets = x + (i - (n_metrics - 1) / 2) * width
        bars = ax.bar(offsets, vals, width=width, label=m.capitalize(),
                      color=colors[m])
        for xi, v in zip(offsets, vals):
            ax.text(xi, v + 0.015, f"{v:.2f}", ha="center", va="bottom", fontsize=8)

    ax.set_xticks(x)
    ax.set_xticklabels([f + f"\n(n={stats[f]['positives']:,})" for f in faults])
    ax.set_ylabel("Score")
    ax.set_ylim(0, 1.1)
    ax.set_title("Per-fault-type detection — current live alerts vs injected_type\n"
                 "(spike mapping = rules OR MAD@current threshold)")
    ax.grid(True, alpha=0.3, axis="y")
    ax.legend(loc="upper right", fontsize=10)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
    print(f"wrote {out_path}")


def figure_mad_by_metric(conninfo: str, out_path: Path) -> None:
    """Per-metric MAD false-positive rate on clean readings — CGM vs HR vs SpO2.
    Justifies keeping MAD on HR/SpO2 and dropping it from CGM."""
    with psycopg.connect(conninfo) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT count(DISTINCT virtual_patient_id),
                       extract(epoch from max(event_time)-min(event_time))/3600.0
                  FROM live.vitals_readings
            """)
            n_patients, hours = cur.fetchone()
            hours = float(hours) if hours else 0.0
            cur.execute("""
                SELECT a.rule_id, count(*)
                  FROM live.alerts a
                  JOIN live.vitals_readings v
                    ON v.virtual_patient_id=a.virtual_patient_id
                   AND v.event_time=a.trigger_event_time
                 WHERE a.detector='mad' AND v.injected_type IS NULL
                 GROUP BY 1 ORDER BY 1
            """)
            by_rule = cur.fetchall()

    labels = []
    ppph = []
    counts = []
    label_map = {"mad.cgm_z_gt_3_5": "CGM", "mad.hr_z_gt_3_5": "HR", "mad.spo2_z_gt_3_5": "SpO2"}
    for rule_id, n in by_rule:
        labels.append(label_map.get(rule_id, rule_id))
        counts.append(n)
        ppph.append(n / (n_patients * hours) if n_patients and hours else 0.0)

    fig, ax = plt.subplots(figsize=(8, 5.5))
    colors = ["#c0392b", "#2874a6", "#186a3b"]
    bars = ax.bar(labels, ppph, color=colors[:len(labels)])
    for bar, val, n in zip(bars, ppph, counts):
        ax.text(bar.get_x() + bar.get_width() / 2, val * 1.05,
                f"{val:.3f}\n({n:,} alerts)", ha="center", va="bottom", fontsize=9)
    ax.axhline(0.04, color="#186a3b", linestyle="--", linewidth=1,
               label="≈ 1 alert/patient/day (shippable)")
    ax.axhline(0.5, color="#b7950b", linestyle="--", linewidth=1,
               label="≈ 12 alerts/patient/day (alarm fatigue)")
    ax.set_ylabel("MAD alerts / patient / hour on clean readings")
    ax.set_yscale("log")
    ax.set_ylim(0.01, max(50, max(ppph) * 2))
    ax.set_title("MAD FP rate on clean readings, by metric\n(same |z|=3.5 threshold; CGM is unshippable — HR/SpO2 workable)")
    ax.grid(True, alpha=0.3, which="both", axis="y")
    ax.legend(loc="upper right", fontsize=9)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
    print(f"wrote {out_path}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sweep-csv", default=str(ROOT / "scratch" / "mad_sweep.csv"))
    ap.add_argument("--out-dir", default=str(ROOT / "eval" / "figures"))
    ap.add_argument("--conninfo", default=os.environ.get("CAREVAULT_PG", CONNINFO_DEFAULT))
    args = ap.parse_args(argv)

    out_dir = Path(args.out_dir)
    figure_pr_curve(Path(args.sweep_csv), out_dir / "pr_curve.png", args.conninfo)
    figure_per_type(args.conninfo, out_dir / "per_type.png")
    figure_mad_by_metric(args.conninfo, out_dir / "mad_by_metric.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
