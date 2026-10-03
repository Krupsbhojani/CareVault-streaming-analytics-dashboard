"""Resolve schema-proposal tradeoff #7: is AZT1D TotalBolusInsulinDelivered
double-counting Correction + Food?

Approach: for every row where all three fields are non-null, compare
Total to Correction + Food. Report exact-match, near-match (<= 0.05 U tol),
and residual stats. Also check the two 2-way subsets, in case the
relationship is Total = Correction (correction-only rows) or Total = Food
(food-only rows).
"""
import sys, io
from pathlib import Path
import pandas as pd
import numpy as np

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
AZ_ROOT = ROOT / "data" / "AZT1D 2025" / "AZT1D 2025" / "CGM Records"
TOL = 0.05  # units; pump doses are typically to 0.05 U resolution

subs = sorted(
    [p for p in AZ_ROOT.iterdir() if p.is_dir() and p.name.startswith("Subject ")],
    key=lambda p: int(p.name.split()[1]),
)

def load(sub: Path) -> pd.DataFrame:
    d = pd.read_csv(next(sub.glob("*.csv")))
    d.columns = [str(c) for c in d.columns]
    return d

frames = []
for sub in subs:
    d = load(sub).assign(_sub=sub.name)
    frames.append(d[["_sub","BolusType","TotalBolusInsulinDelivered","CorrectionDelivered","FoodDelivered"]])
all_ = pd.concat(frames, ignore_index=True)
tot = pd.to_numeric(all_["TotalBolusInsulinDelivered"], errors="coerce")
cor = pd.to_numeric(all_["CorrectionDelivered"], errors="coerce")
foo = pd.to_numeric(all_["FoodDelivered"], errors="coerce")

print("=" * 72)
print("Non-null counts across all 25 AZT1D subjects")
print("=" * 72)
print(f"Total non-null:      {tot.notna().sum():>7d}")
print(f"Correction non-null: {cor.notna().sum():>7d}")
print(f"Food non-null:       {foo.notna().sum():>7d}")

# Case A: all three non-null
mask3 = tot.notna() & cor.notna() & foo.notna()
print(f"\nRows with ALL THREE non-null: {int(mask3.sum())}")
if mask3.any():
    resid = tot[mask3] - (cor[mask3] + foo[mask3])
    exact = int((resid.abs() < 1e-9).sum())
    near = int((resid.abs() <= TOL).sum())
    tot_eq_sum = int((resid.abs() <= TOL).sum())
    tot_gt = int((resid > TOL).sum())
    tot_lt = int((resid < -TOL).sum())
    print(f"  Total == Correction + Food (exact):        {exact:>6d}  ({100*exact/mask3.sum():.1f}%)")
    print(f"  Total ≈  Correction + Food (|Δ| ≤ {TOL} U): {near:>6d}  ({100*near/mask3.sum():.1f}%)")
    print(f"  Total >  Correction + Food (Δ  > {TOL} U):  {tot_gt:>6d}  ({100*tot_gt/mask3.sum():.1f}%)")
    print(f"  Total <  Correction + Food (Δ  < -{TOL} U): {tot_lt:>6d}  ({100*tot_lt/mask3.sum():.1f}%)")
    print(f"  residual stats: mean={resid.mean():+.4f} U  median={resid.median():+.4f} U  min={resid.min():.3f}  max={resid.max():.3f}")
    print("\n  BolusType breakdown for the 3-way rows:")
    print(all_.loc[mask3, "BolusType"].value_counts(dropna=False).head(10).to_string())

# Case B: total non-null but only correction non-null (food NaN)
maskCor = tot.notna() & cor.notna() & foo.isna()
if maskCor.any():
    resid = tot[maskCor] - cor[maskCor]
    print(f"\nRows Total & Correction non-null, Food NaN: {int(maskCor.sum())}")
    print(f"  Total == Correction (|Δ| ≤ {TOL}): {int((resid.abs()<=TOL).sum())} ({100*(resid.abs()<=TOL).mean():.1f}%)")
    print(f"  residual mean={resid.mean():+.4f} median={resid.median():+.4f}")

# Case C: total non-null but only food non-null (correction NaN)
maskFoo = tot.notna() & foo.notna() & cor.isna()
if maskFoo.any():
    resid = tot[maskFoo] - foo[maskFoo]
    print(f"\nRows Total & Food non-null, Correction NaN: {int(maskFoo.sum())}")
    print(f"  Total == Food (|Δ| ≤ {TOL}): {int((resid.abs()<=TOL).sum())} ({100*(resid.abs()<=TOL).mean():.1f}%)")
    print(f"  residual mean={resid.mean():+.4f} median={resid.median():+.4f}")

# Case D: total non-null, both cor & foo NaN — solo bolus rows
maskSolo = tot.notna() & cor.isna() & foo.isna()
print(f"\nRows Total non-null, Correction & Food both NaN: {int(maskSolo.sum())}")
print(f"  Total distribution: min={tot[maskSolo].min()} med={tot[maskSolo].median()} max={tot[maskSolo].max()}")
print("  BolusType breakdown:")
print(all_.loc[maskSolo,"BolusType"].value_counts(dropna=False).head(10).to_string())

# Sample rows of each case
print("\n" + "=" * 72)
print("Sample rows (10 each)")
print("=" * 72)
print("\n-- Case A (all three non-null), first 10 --")
print(all_.loc[mask3].head(10).to_string())
print("\n-- Case B (total+correction, food NaN), first 5 --")
print(all_.loc[maskCor].head(5).to_string())
print("\n-- Case C (total+food, correction NaN), first 5 --")
print(all_.loc[maskFoo].head(5).to_string())
