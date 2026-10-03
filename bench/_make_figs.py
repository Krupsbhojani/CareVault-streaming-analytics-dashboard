"""Generate azure_throughput.png and local_vs_azure.png from the bench CSVs."""
import csv
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "eval" / "figures"
OUT.mkdir(parents=True, exist_ok=True)

# ---------- azure_throughput.png ----------
rows = []
with (ROOT / "bench" / "azure_results.csv").open() as f:
    for r in csv.DictReader(f):
        rows.append({
            "target": int(r["target_rows_min"]),
            "achieved": int(r["achieved_rows_min"]),
            "e2e_p95_ms": float(r["e2e_latency_p95_ms"]),
            "lag_sec": float(r["worker_lag_sec_end"]),
            "label": r["label"],
        })

fig, ax1 = plt.subplots(figsize=(9.5, 5.5))
targets   = [r["target"]   for r in rows]
achieved  = [r["achieved"] for r in rows]
latencies = [r["e2e_p95_ms"] for r in rows]

ax1.plot(targets, targets,   "k--", alpha=0.4, label="y = x (perfect)")
ax1.plot(targets, achieved,  "o-", color="#2874a6", linewidth=2, markersize=10,
         label="achieved rows/min")
for t, a, lbl, lag in zip(targets, achieved, [r["label"] for r in rows],
                           [r["lag_sec"] for r in rows]):
    note = f"\nlag={lag:.0f}s" if lag > 10 else ""
    ax1.annotate(f"{a:,}{note}", xy=(t, a), xytext=(8, -14),
                 textcoords="offset points", fontsize=9, color="#1b4f72")

ax1.set_xlabel("target rows / min")
ax1.set_ylabel("achieved rows / min")
ax1.set_xscale("log")
ax1.set_yscale("log")
ax1.set_title("Azure step-load — achieved vs target throughput\n"
              "(Standard_B1ms, 1 vCore burstable, northcentralus; AZT1D 5-min cadence)")
ax1.grid(True, which="both", alpha=0.3)

ax2 = ax1.twinx()
ax2.plot(targets, latencies, "s-", color="#c0392b", linewidth=1.6, markersize=8,
         label="e2e latency p95")
ax2.set_ylabel("end-to-end latency p95 (ms)", color="#c0392b")
ax2.tick_params(axis="y", labelcolor="#c0392b")

# Saturation marker: wherever achieved first falls below 90% of target.
sat = None
for r in rows:
    if r["achieved"] < 0.9 * r["target"]:
        sat = r
        break
if sat is not None:
    ax1.axvline(sat["target"], color="#b7950b", linestyle=":", linewidth=1)
    ax1.text(sat["target"] * 1.05, min(achieved) * 1.2,
             f"saturation: target ≥ {sat['target']:,}\n"
             f"→ achieved only {sat['achieved']:,}/min",
             color="#b7950b", fontsize=9)

h1, l1 = ax1.get_legend_handles_labels()
h2, l2 = ax2.get_legend_handles_labels()
ax1.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=9)
fig.tight_layout()
fig.savefig(OUT / "azure_throughput.png", dpi=140)
plt.close(fig)
print(f"wrote {OUT / 'azure_throughput.png'}")

# ---------- local_vs_azure.png ----------
# Pair same configurations against known local numbers from CLAUDE.md:
# Local sustained ~3,900 rows/min at 2000 vps × 10x (= 4k target).
# Azure sustained 4,081 rows/min at 2500 vps × 10x (= 5k target).
# For a fair same-configuration comparison, use the Azure step 2 (5k target).
labels = ["RTT p50 (ms)", "COPY p50 @ 5k (ms)", "INSERT p50 @ 5k (ms)",
          "Rows/min @ 5k target", "Rows/min @ 50k target"]
local = [1.7, 4.6, 2.6, 3900, 3900]   # local plateau is ~3,900 (CPU-bound on docker)
azure = [41.8, 96.3, 48.1, 4081, 40825]

import numpy as np
x = np.arange(len(labels))
w = 0.38
fig, ax = plt.subplots(figsize=(10, 5.5))
bars1 = ax.bar(x - w/2, local, width=w, color="#2874a6", label="local Docker")
bars2 = ax.bar(x + w/2, azure, width=w, color="#c0392b", label="Azure B1ms")
ax.set_yscale("log")
ax.set_xticks(x, labels, rotation=10, ha="right", fontsize=9)
ax.set_ylabel("value (log scale)")
ax.set_title("Local vs Azure at the same workload\n"
             "(Azure is 25× slower per round-trip and ~20× slower per batch, "
             "but matches local rows/min until load exceeds server capacity)")
ax.grid(True, axis="y", which="both", alpha=0.3)
ax.legend(loc="upper right")
for bars in (bars1, bars2):
    for b in bars:
        ax.text(b.get_x() + b.get_width() / 2, b.get_height() * 1.08,
                f"{b.get_height():,g}", ha="center", va="bottom", fontsize=8)
fig.tight_layout()
fig.savefig(OUT / "local_vs_azure.png", dpi=140)
plt.close(fig)
print(f"wrote {OUT / 'local_vs_azure.png'}")
