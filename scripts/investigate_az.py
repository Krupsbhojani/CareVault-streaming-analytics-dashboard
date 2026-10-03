"""Answer three questions before proposing schema:
  Q1. AZT1D duplicate timestamps: identical CGM? non-CGM ffill? 5-min grid safe?
  Q2. Subject 14 CGM/BGM type indicator: any column? verdict.
  Q3. DeviceMode value frequencies + proposed mapping.
"""
import sys, io, re
from pathlib import Path
import pandas as pd
import numpy as np

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
AZ_ROOT = ROOT / "data" / "AZT1D 2025" / "AZT1D 2025" / "CGM Records"

subs = sorted(
    [p for p in AZ_ROOT.iterdir() if p.is_dir() and p.name.startswith("Subject ")],
    key=lambda p: int(p.name.split()[1]),
)

def load(sub: Path) -> pd.DataFrame:
    csv = next(sub.glob("*.csv"))
    d = pd.read_csv(csv)
    d.columns = [str(c) for c in d.columns]
    d["_ts"] = pd.to_datetime(d["EventDateTime"], errors="coerce")
    return d


# ---------- Q1 ----------
print("=" * 72)
print("Q1. Duplicate-timestamp semantics per subject")
print("=" * 72)
print(f"{'subject':<11s} {'dup_ts':>6s} {'dup_rows':>8s} {'grp_all_same_cgm_%':>19s} {'nonCGM_looks_ffill_%':>22s} {'gcol'}")

q1_rows = []
for sub in subs:
    d = load(sub)
    gcol = "CGM" if "CGM" in d.columns else "Readings (CGM / BGM)"
    d_sorted = d.sort_values("_ts", kind="stable")
    dup_mask = d_sorted["_ts"].duplicated(keep=False)
    dup_slice = d_sorted[dup_mask]
    n_dup_ts = int(d_sorted["_ts"].duplicated().sum())     # extras beyond the first
    n_dup_rows = int(dup_mask.sum())                       # every row in a dup group

    # % of dup groups where all CGM values are equal
    if n_dup_rows == 0:
        pct_same = float("nan")
    else:
        grp = dup_slice.groupby("_ts")[gcol]
        # each group: number of distinct non-NaN CGM values
        distinct_per_grp = grp.apply(lambda s: s.dropna().nunique())
        # group counted "same" if it has 0 or 1 distinct CGM value
        same = int((distinct_per_grp <= 1).sum())
        pct_same = 100.0 * same / len(distinct_per_grp)

    # non-CGM ffill test: within a dup group, are non-CGM columns identical
    # OR do the extra rows look like carried-forward values from the immediately
    # preceding non-dup row? Simple proxy: within each dup group, count columns
    # (excluding CGM) whose values are all equal (NaN counted as equal).
    other = [c for c in d.columns if c not in (gcol, "EventDateTime", "_ts")]
    if n_dup_rows == 0:
        pct_ffill = float("nan")
    else:
        def _uniform(g):
            # count cols where every value (incl NaN) is equal to first
            first = g.iloc[0]
            eq = ((g == first) | (g.isna() & pd.isna(first))).all()
            return eq.sum()
        uniform_cnts = dup_slice.groupby("_ts")[other].apply(_uniform)
        # each column has len(other) total; fraction of "uniform" cells
        pct_ffill = 100.0 * uniform_cnts.sum() / (len(uniform_cnts) * len(other))
    print(f"{sub.name:<11s} {n_dup_ts:>6d} {n_dup_rows:>8d} {pct_same:>19.1f} {pct_ffill:>22.1f} {gcol}")
    q1_rows.append((sub.name, n_dup_ts, n_dup_rows, pct_same, pct_ffill))

q1 = pd.DataFrame(q1_rows, columns=["subject","dup_ts","dup_rows","grp_all_same_cgm_pct","nonCGM_uniform_pct"])

# Aggregate view + a peek at raw dup examples for one high-dup subject
print("\n-- Sample duplicate groups from Subject 15 (2053 dup ts) --")
d = load(next(s for s in subs if s.name == "Subject 15"))
d_sorted = d.sort_values("_ts", kind="stable")
dup_ts_list = d_sorted[d_sorted["_ts"].duplicated(keep=False)]["_ts"].unique()[:3]
for t in dup_ts_list:
    print(f"\nts={t}")
    print(d_sorted[d_sorted["_ts"] == t].to_string())

print("\n-- Sample duplicate groups from Subject 1 (161 dup ts) --")
d = load(next(s for s in subs if s.name == "Subject 1"))
d_sorted = d.sort_values("_ts", kind="stable")
dup_ts_list = d_sorted[d_sorted["_ts"].duplicated(keep=False)]["_ts"].unique()[:3]
for t in dup_ts_list:
    print(f"\nts={t}")
    print(d_sorted[d_sorted["_ts"] == t].to_string())

# ---------- Q2 ----------
print("\n" + "=" * 72)
print("Q2. Subject 14 CGM/BGM type indicator")
print("=" * 72)
d14 = load(next(s for s in subs if s.name == "Subject 14"))
print("columns:", list(d14.columns))
gcol = "Readings (CGM / BGM)"
# Are there any columns not in the standard AZ union that could be a type flag?
std = {"EventDateTime","DeviceMode","BolusType","Basal","CorrectionDelivered",
       "TotalBolusInsulinDelivered","FoodDelivered","CarbSize","CGM","_ts"}
extra = [c for c in d14.columns if c not in std]
print("non-standard columns:", extra)
print("BolusType value_counts (may double as type flag):")
print(d14["BolusType"].value_counts(dropna=False).head(20))
# Compare glucose value distribution vs a typical CGM-only subject
print(f"\n{gcol} describe:")
print(d14[gcol].describe())
print("BGM-suspicious: any values that are perfectly-round integers (fingerstick pattern)?")
gv = pd.to_numeric(d14[gcol], errors="coerce").dropna()
# CGM 5-min data usually integers already for AZ; not a strong signal
# Look for value gaps in the time series that would indicate manual entries mixed in
d14 = d14.sort_values("_ts")
dt = d14["_ts"].diff().dt.total_seconds()
print(f"Δt distribution (seconds): {dt.describe().to_string()}")
print("Δt bins (share):")
bins = pd.cut(dt.dropna(), [-1,60,180,299,301,600,900,3600,86400], right=True)
print((bins.value_counts(normalize=True).sort_index()*100).round(2).to_string())

# ---------- Q3 ----------
print("\n" + "=" * 72)
print("Q3. DeviceMode value frequencies (all subjects)")
print("=" * 72)
combined = pd.concat([load(s).assign(_sub=s.name) for s in subs], ignore_index=True)
print("dtype:", combined["DeviceMode"].dtype)
vc = combined["DeviceMode"].value_counts(dropna=False)
print("global value_counts:")
print(vc.to_string())
print("\nper-subject DeviceMode value counts (non-null only):")
per_sub = combined.dropna(subset=["DeviceMode"]).groupby("_sub")["DeviceMode"].value_counts().unstack(fill_value=0)
print(per_sub.to_string())
# BolusType as sanity companion
print("\nGlobal BolusType value_counts:")
print(combined["BolusType"].value_counts(dropna=False).head(20).to_string())
