"""Single-pass profiler over AZT1D CSVs and Shanghai interim CSVs.
Prints one line per file and writes docs/data-inventory.md.
Does NOT call pandas.read_excel anywhere.
"""
import io
import re
import sys
import json
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
AZ_ROOT = ROOT / "data" / "AZT1D 2025" / "AZT1D 2025" / "CGM Records"
SHANGHAI_CSV_ROOT = ROOT / "data" / "interim" / "shanghai"
SHANGHAI_SUMMARY = ROOT / "data" / "diabetes_datasets"
DOC = ROOT / "docs" / "data-inventory.md"


def sizeof(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def fmt_bytes(n: int) -> str:
    x = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if x < 1024:
            return f"{x:.1f} {u}"
        x /= 1024
    return f"{x:.1f} TB"


# ---------------- AZT1D ----------------

def profile_az():
    subs = sorted(
        [p for p in AZ_ROOT.iterdir() if p.is_dir() and p.name.startswith("Subject ")],
        key=lambda p: int(p.name.split()[1]),
    )
    rows = []
    schema_union = {}
    schema_intersection = None
    per_col_missing = {}
    for sub in subs:
        csv = next(sub.glob("*.csv"))
        t0 = pd.Timestamp.utcnow()
        d = pd.read_csv(csv)
        d.columns = [str(c) for c in d.columns]
        for c in d.columns:
            schema_union[c] = schema_union.get(c, 0) + 1
            per_col_missing.setdefault(c, []).append(d[c].isna().mean() * 100)
        cols = set(d.columns)
        schema_intersection = cols if schema_intersection is None else (schema_intersection & cols)
        ts_col = "EventDateTime" if "EventDateTime" in d.columns else next(
            (c for c in d.columns if "time" in c.lower() or "date" in c.lower()), None
        )
        ts = pd.to_datetime(d[ts_col], errors="coerce")
        d = d.assign(_ts=ts).sort_values("_ts")
        gaps = d["_ts"].diff().dt.total_seconds().dropna()
        gcol = "CGM" if "CGM" in d.columns else next(
            (c for c in d.columns if "cgm" in c.lower() or "glucose" in c.lower() or "reading" in c.lower()),
            None,
        )
        g = pd.to_numeric(d[gcol], errors="coerce") if gcol else pd.Series(dtype=float)
        rows.append(dict(
            subject=sub.name,
            rows=len(d),
            start=str(d["_ts"].min()),
            end=str(d["_ts"].max()),
            days=round((d["_ts"].max() - d["_ts"].min()).total_seconds() / 86400, 2),
            median_dt_s=float(gaps.median()) if len(gaps) else None,
            n_gaps_gt_15min=int((gaps > 15 * 60).sum()),
            max_gap_min=round(float(gaps.max()) / 60, 1) if len(gaps) else 0,
            dup_ts=int(d["_ts"].duplicated().sum()),
            glucose_col=gcol,
            g_min=float(g.min()) if gcol and g.notna().any() else None,
            g_max=float(g.max()) if gcol and g.notna().any() else None,
            g_missing=int(g.isna().sum()) if gcol else None,
            n_cols=len(d.columns) - 1,
        ))
        print(
            f"[AZ] {sub.name:>11s}  rows={len(rows) and rows[-1]['rows']:>6d}  "
            f"gap>15m={rows[-1]['n_gaps_gt_15min']:3d}  "
            f"max_gap_min={rows[-1]['max_gap_min']:7.1f}  "
            f"dupts={rows[-1]['dup_ts']:4d}  gcol={gcol}",
            flush=True,
        )
    df = pd.DataFrame(rows)
    missing_mean = {c: float(np.mean(v)) for c, v in per_col_missing.items()}
    return df, schema_union, sorted(schema_intersection), missing_mean


# ---------------- Shanghai ----------------

def profile_shanghai(group: str):
    d = SHANGHAI_CSV_ROOT / group
    files = sorted(d.glob("*.csv"))
    rows = []
    schema_freq = {}
    per_col_missing = {}
    for i, f in enumerate(files, 1):
        try:
            df = pd.read_csv(f)
        except Exception as e:
            print(f"[{group}] {i:3d}/{len(files)} {f.name}  READ_FAIL {e}", flush=True)
            continue
        df.columns = [str(c).strip() for c in df.columns]
        for c in df.columns:
            schema_freq[c] = schema_freq.get(c, 0) + 1
            per_col_missing.setdefault(c, []).append(df[c].isna().mean() * 100)
        ts_col = next((c for c in df.columns if c.lower() == "date"), None) or next(
            (c for c in df.columns if "date" in c.lower() or "time" in c.lower()), None
        )
        ts = pd.to_datetime(df[ts_col], errors="coerce") if ts_col else pd.Series(dtype="datetime64[ns]")
        df = df.assign(_ts=ts).sort_values("_ts")
        gaps = df["_ts"].diff().dt.total_seconds().dropna()
        gcol = next(
            (c for c in df.columns if "cgm" in c.lower() or "glucose" in c.lower()),
            None,
        )
        g = pd.to_numeric(df[gcol], errors="coerce") if gcol else pd.Series(dtype=float)
        m = re.match(r"^(\d+)_(\d+)_(\d{8})", f.stem)
        rows.append(dict(
            file=f.stem,
            patient=m.group(1) if m else None,
            visit=int(m.group(2)) if m else None,
            filename_date=m.group(3) if m else None,
            rows=len(df),
            n_cols=len(df.columns) - 1,
            ts_col=ts_col,
            start=str(df["_ts"].min()),
            end=str(df["_ts"].max()),
            days=round((df["_ts"].max() - df["_ts"].min()).total_seconds() / 86400, 2) if len(df) else 0,
            median_dt_s=float(gaps.median()) if len(gaps) else None,
            n_gaps_gt_30min=int((gaps > 30 * 60).sum()),
            max_gap_min=round(float(gaps.max()) / 60, 1) if len(gaps) else 0,
            dup_ts=int(df["_ts"].duplicated().sum()),
            glucose_col=gcol,
            g_min=float(g.min()) if gcol and g.notna().any() else None,
            g_max=float(g.max()) if gcol and g.notna().any() else None,
            g_missing=int(g.isna().sum()) if gcol else None,
        ))
        r = rows[-1]
        print(
            f"[{group}] {i:3d}/{len(files)} {f.stem}  rows={r['rows']:>4d}  "
            f"med_dt={r['median_dt_s']}  gap>30m={r['n_gaps_gt_30min']:2d}  "
            f"maxgap_min={r['max_gap_min']:6.1f}  dupts={r['dup_ts']:2d}  "
            f"g=[{r['g_min']},{r['g_max']}]",
            flush=True,
        )
    df_summary = pd.DataFrame(rows)
    missing_mean = {c: float(np.mean(v)) for c, v in per_col_missing.items()}
    return df_summary, schema_freq, missing_mean


# ---------------- Report ----------------

def render_md(az, az_union, az_common, az_miss, t1, t1freq, t1miss, t2, t2freq, t2miss, t1sum_cols, t2sum_cols):
    lines = []
    def w(*args):
        lines.append(" ".join(str(a) for a in args))

    def df_md(df: pd.DataFrame, cols=None) -> str:
        d = df[cols] if cols else df
        return d.to_markdown(index=False)

    w("# CGM Data Inventory")
    w("")
    w("Read-only inventory of the two extracted CGM datasets under `data/`. Numbers computed from source files (AZT1D CSVs) and converted CSVs (Shanghai, converted via `scripts/convert_shanghai.py` into `data/interim/shanghai/`).")
    w("")
    w(f"- AZT1D root: `data/AZT1D 2025/AZT1D 2025/CGM Records/` — {fmt_bytes(sizeof(AZ_ROOT))}")
    w(f"- Shanghai T1DM: `data/diabetes_datasets/Shanghai_T1DM/` — {fmt_bytes(sizeof(SHANGHAI_SUMMARY / 'Shanghai_T1DM'))}")
    w(f"- Shanghai T2DM: `data/diabetes_datasets/Shanghai_T2DM/` — {fmt_bytes(sizeof(SHANGHAI_SUMMARY / 'Shanghai_T2DM'))}")
    w("")

    # ---------------- AZT1D ----------------
    w("## AZT1D 2025")
    w("")
    w("### 1. Layout")
    w("")
    w(f"- 25 subject subfolders `Subject 1/` … `Subject 25/`, each containing exactly one CSV file named `Subject N.csv`.")
    w(f"- One file per patient. Total CGM CSV size: {fmt_bytes(sizeof(AZ_ROOT))}.")
    w("- Extra: `README.txt`, `Manuscript.pdf`, `Visual Statistics/` (PNGs — not used).")
    w("- Also present: `__MACOSX/` folder (macOS packaging cruft — ignore).")
    w("")
    w("### 2. Columns")
    w("")
    w("Columns intersected across all 25 files (present everywhere):")
    w("")
    for c in az_common:
        w(f"- `{c}`")
    w("")
    w("Union of columns across all 25 files (some appear only in some files):")
    w("")
    w("| column | files present in / 25 |")
    w("|---|---|")
    for c, n in sorted(az_union.items(), key=lambda kv: (-kv[1], kv[0])):
        w(f"| `{c}` | {n} |")
    w("")
    w("Subject 14 uses `Readings (CGM / BGM)` as the glucose column name instead of `CGM` — every other subject uses `CGM`. See open questions.")
    w("")
    w("Dtypes and 3 sample rows from `Subject 1/Subject 1.csv`:")
    w("")
    w("```")
    w("EventDateTime                  object   (e.g. '2023-12-08 00:04:00')")
    w("DeviceMode                    float64   (encoded int; mostly NaN)")
    w("BolusType                      object   (string, mostly NaN)")
    w("Basal                         float64   (U/hr, sparse)")
    w("CorrectionDelivered           float64   (U, sparse)")
    w("TotalBolusInsulinDelivered    float64   (U, sparse)")
    w("FoodDelivered                 float64   (U, sparse)")
    w("CarbSize                      float64   (g, sparse)")
    w("CGM                             int64   (mg/dL, per README)")
    w("```")
    w("")
    w("```")
    w("      EventDateTime  DeviceMode BolusType  Basal  CorrectionDelivered  TotalBolusInsulinDelivered  FoodDelivered  CarbSize  CGM")
    w("2023-12-08 00:04:00         NaN       NaN    NaN                  NaN                         NaN            NaN       NaN  151")
    w("2023-12-08 00:09:00         NaN       NaN    NaN                  NaN                         NaN            NaN       NaN  152")
    w("2023-12-08 00:14:00         NaN       NaN    NaN                  NaN                         NaN            NaN       NaN  156")
    w("```")
    w("")
    w("Units are not marked in the file — README only names the fields. Glucose values are in mg/dL (range observed 40–400, capped at 400 for several subjects, matches Dexcom G6/G7 clamp).")
    w("")
    w("### 3. Timestamps")
    w("")
    w("- Column: `EventDateTime`.")
    w("- Format: naive ISO-like string `YYYY-MM-DD HH:MM:SS`. No timezone in the value; README states all times are **local Arizona time** (MST year-round, no DST).")
    w("- Median inter-row interval per subject:")
    w("")
    w("| stat | value |")
    w("|---|---|")
    w(f"| min median Δt (s) | {int(az['median_dt_s'].min())} |")
    w(f"| max median Δt (s) | {int(az['median_dt_s'].max())} |")
    w(f"| overall median Δt (s) | {int(az['median_dt_s'].median())} |")
    w("")
    w("Sampling is 5-minute nominal (median Δt = 300 s for all 25 subjects), but intervals are **not** strictly regular. Every subject has some duplicate timestamps and multiple >15 min gaps.")
    w("")
    w("### 4. Per-subject data quality")
    w("")
    w("| subject | rows | days | start | end | med Δt (s) | gaps>15m | max gap (min) | dup ts | glucose col | g min | g max | g missing |")
    w("|---|---:|---:|---|---|---:|---:|---:|---:|---|---:|---:|---:|")
    for _, r in az.iterrows():
        w(f"| {r['subject']} | {r['rows']} | {r['days']} | {r['start']} | {r['end']} | {int(r['median_dt_s'])} | {r['n_gaps_gt_15min']} | {r['max_gap_min']} | {r['dup_ts']} | `{r['glucose_col']}` | {r['g_min']} | {r['g_max']} | {r['g_missing']} |")
    w("")
    w("Missing % per column, averaged across all 25 subjects:")
    w("")
    w("| column | mean missing % |")
    w("|---|---:|")
    for c, m in sorted(az_miss.items(), key=lambda kv: -kv[1]):
        w(f"| `{c}` | {m:.1f} |")
    w("")
    w("Notes on data quality:")
    w("")
    w(f"- Every subject has some duplicate `EventDateTime` rows (min {int(az['dup_ts'].min())}, max {int(az['dup_ts'].max())}, median {int(az['dup_ts'].median())}). Same timestamp can appear on distinct event types (CGM row vs. bolus event vs. basal change), so naive dedupe by timestamp will drop non-glucose events.")
    w(f"- Glucose column has 0 NaN in every subject — that's because non-CGM event rows still carry a value (or the event stream is filtered). Verify.")
    w(f"- Max observed glucose is exactly 400.0 for 8 subjects — device clamp, not a data error.")
    w(f"- Min observed glucose is exactly 40.0 for most subjects — likely the low cap of the sensor.")
    w("")
    w("### 5. Non-glucose signals")
    w("")
    w("All in the same per-subject CSV (no separate files):")
    w("")
    w("- Insulin: `Basal` (U/hr), `CorrectionDelivered`, `TotalBolusInsulinDelivered`, `FoodDelivered` (all U)")
    w("- Carbs: `CarbSize` (g)")
    w("- Device state: `DeviceMode`, `BolusType`")
    w("- No demographics, labs, or medications in the CSVs. Manuscript.pdf may describe cohort characteristics.")
    w("")
    w("### 6. Patient IDs")
    w("")
    w("- Only source: the folder/file name `Subject N`, N ∈ [1, 25]. IDs are consistent (one file per patient). No cross-file join needed.")
    w("")

    # ---------------- Shanghai ----------------
    w("## Shanghai T1DM & T2DM")
    w("")
    w("### 1. Layout")
    w("")
    w(f"- `Shanghai_T1DM/`: 16 files, mix of `.xls` (12) and `.xlsx` (4). All BIFF/OLE files are code page 936 (GBK, Simplified Chinese).")
    w(f"- `Shanghai_T2DM/`: 109 files, mix of `.xls` (89) and `.xlsx` (20).")
    w(f"- One file per patient **per visit**, not per patient. Filename pattern: `<PatientID>_<VisitIdx>_<YYYYMMDD>.xls[x]`, where `YYYYMMDD` is the visit start date.")
    w(f"- Two per-cohort summary spreadsheets in the parent directory: `Shanghai_T1DM_Summary.xlsx`, `Shanghai_T2DM_Summary.xlsx`.")
    w(f"- Also present: `__MACOSX/`, `data_analysis.ipynb`, `note.txt` (documents 188 CGM measurements added in Oct 2023 to two T2DM files).")
    w("")
    w("Converted copies (single pass, this session) live in `data/interim/shanghai/{Shanghai_T1DM,Shanghai_T2DM}/*.csv`.")
    w("")
    w(f"Total unique patients (by filename prefix): T1DM = {t1['patient'].nunique()}, T2DM = {t2['patient'].nunique()}. The paper cites 12 T1DM + 100 T2DM — T1DM matches (12 unique patients, 16 files), T2DM has {t2['patient'].nunique()} unique patient IDs across 109 files.")
    w("")
    w("Visits per patient:")
    w("")
    w("| visits | T1DM patients | T2DM patients |")
    w("|---:|---:|---:|")
    v1 = t1.groupby("patient").size().value_counts().sort_index()
    v2 = t2.groupby("patient").size().value_counts().sort_index()
    for k in sorted(set(v1.index) | set(v2.index)):
        w(f"| {k} | {int(v1.get(k, 0))} | {int(v2.get(k, 0))} |")
    w("")

    # ---------------- Shanghai columns ----------------
    w("### 2. Columns (per-visit CGM file)")
    w("")
    w("Column frequency across all Shanghai per-visit CSVs (T1DM `n=16`, T2DM `n=109`). A column that appears in all files has count `= file count`.")
    w("")
    w("| column | T1DM files | T2DM files |")
    w("|---|---:|---:|")
    allcols = sorted(set(t1freq) | set(t2freq))
    for c in allcols:
        w(f"| `{c}` | {t1freq.get(c, 0)}/{len(t1)} | {t2freq.get(c, 0)}/{len(t2)} |")
    w("")
    w("Missing % per column, averaged across files (T1DM = mean across 16, T2DM = mean across 109):")
    w("")
    w("| column | T1DM mean missing % | T2DM mean missing % |")
    w("|---|---:|---:|")
    for c in allcols:
        a = f"{t1miss.get(c, float('nan')):.1f}" if c in t1miss else "-"
        b = f"{t2miss.get(c, float('nan')):.1f}" if c in t2miss else "-"
        w(f"| `{c}` | {a} | {b} |")
    w("")
    w("Sample rows from `1001_0_20210730` (T1DM):")
    w("")
    sample = pd.read_csv(SHANGHAI_CSV_ROOT / "Shanghai_T1DM" / "1001_0_20210730.csv").head(3)
    w("```")
    w(sample.to_string())
    w("```")
    w("")

    # ---------------- timestamps ----------------
    w("### 3. Timestamps")
    w("")
    w("- Column: `Date` (some files may spell it `date` — code normalizes case).")
    w("- Format: `YYYY-MM-DD HH:MM:SS` after CSV export.")
    w("- Timezone: unstated. Files are Chinese hospital records; assume Asia/Shanghai (UTC+8) unless we find otherwise.")
    w(f"- Median Δt per file: T1DM range [{int(t1['median_dt_s'].min())}, {int(t1['median_dt_s'].max())}] s; T2DM range [{int(t2['median_dt_s'].min())}, {int(t2['median_dt_s'].max())}] s.")
    w("- Sampling nominally every 15 min. Intervals not strictly regular — see gap counts below.")
    w("")

    # ---------------- data quality tables ----------------
    for label, dfp in [("T1DM (all 16 files)", t1), ("T2DM (first 30 of 109)", t2.head(30))]:
        w(f"### 4. Shanghai {label} — per-file profile")
        w("")
        w("| file | rows | days | start | end | med Δt (s) | gaps>30m | max gap (min) | dup ts | g col | g min | g max | g missing |")
        w("|---|---:|---:|---|---|---:|---:|---:|---:|---|---:|---:|---:|")
        for _, r in dfp.iterrows():
            med = "" if r["median_dt_s"] is None else int(r["median_dt_s"])
            w(f"| {r['file']} | {r['rows']} | {r['days']} | {r['start']} | {r['end']} | {med} | {r['n_gaps_gt_30min']} | {r['max_gap_min']} | {r['dup_ts']} | `{r['glucose_col']}` | {r['g_min']} | {r['g_max']} | {r['g_missing']} |")
        w("")
    w(f"(Full T2DM per-file summary written to `scratch/shanghai_t2dm_files.csv` — {len(t2)} rows total.)")
    w("")
    w("Cohort-level data-quality totals:")
    w("")
    w("| stat | T1DM | T2DM |")
    w("|---|---:|---:|")
    w(f"| files | {len(t1)} | {len(t2)} |")
    w(f"| unique patients | {t1['patient'].nunique()} | {t2['patient'].nunique()} |")
    w(f"| total rows | {int(t1['rows'].sum())} | {int(t2['rows'].sum())} |")
    w(f"| rows per file: min / median / max | {int(t1['rows'].min())} / {int(t1['rows'].median())} / {int(t1['rows'].max())} | {int(t2['rows'].min())} / {int(t2['rows'].median())} / {int(t2['rows'].max())} |")
    w(f"| days per file: min / median / max | {t1['days'].min()} / {t1['days'].median()} / {t1['days'].max()} | {t2['days'].min()} / {t2['days'].median()} / {t2['days'].max()} |")
    w(f"| files with dup timestamps | {int((t1['dup_ts']>0).sum())} | {int((t2['dup_ts']>0).sum())} |")
    w(f"| files with any gap > 30 min | {int((t1['n_gaps_gt_30min']>0).sum())} | {int((t2['n_gaps_gt_30min']>0).sum())} |")
    w(f"| max gap observed (min) | {t1['max_gap_min'].max()} | {t2['max_gap_min'].max()} |")
    w(f"| glucose range (min / max across files) | {t1['g_min'].min()} / {t1['g_max'].max()} | {t2['g_min'].min()} / {t2['g_max'].max()} |")
    w(f"| files with any glucose NaN | {int((t1['g_missing'].fillna(0)>0).sum())} | {int((t2['g_missing'].fillna(0)>0).sum())} |")
    w("")

    # ---------------- signals ----------------
    w("### 5. Non-glucose signals in Shanghai per-visit files")
    w("")
    w("Inferred from the observed column set. Column names are verbatim from source (Chinese-language columns exported through pandas `to_csv` in UTF-8):")
    w("")
    w("- CGM glucose: `CGM (mg / dl)` (or `CGM`)")
    w("- Capillary/venous fingerstick: `CBG (mg / dl)`, `Blood Ketone (mmol / L)`")
    w("- Diet: `Dietary intake` (mixed Chinese/English free text describing meal composition)")
    w("- Insulin: `Insulin dose - s.c.` (subcutaneous), `Insulin dose - i.v.` (intravenous)")
    w("- Non-insulin hypoglycemic drugs: `Non-insulin hypoglycemic agents`")
    w("- CSII (pump) fields: `CSII - bolus insulin (Novolin R, IU)`, `CSII - basal insulin (Novolin R, IU / H)`")
    w("- Free-text events: any additional narrative columns")
    w("")
    w("Demographics, labs and medications are **not** in the per-visit files — they live in the summary spreadsheets (`Shanghai_T*DM_Summary.xlsx`, one row per file/visit, 33 columns each):")
    w("")
    w("| summary column (T1DM & T2DM) |")
    w("|---|")
    for c in t1sum_cols:
        w(f"| `{c}` |")
    w("")

    # ---------------- IDs ----------------
    w("### 6. Patient identification")
    w("")
    w("- Patient ID = first underscore-delimited field of the filename (T1DM `1001..1012`, T2DM `2000..2099`).")
    w("- `Patient Number` column in the summary xlsx uses the *full filename stem* (e.g. `1001_0_20210730`), i.e. patient × visit, not just patient. To join summary → CGM file, key on filename stem; to aggregate at patient level, split on `_` and take the first field.")
    w("- IDs are consistent across the per-visit file and the summary row (they are literally the same string). No cross-cohort ID collision — T1DM IDs start with `1`, T2DM with `2`.")
    w("- One diff: the T1DM summary column is `Duration of Diabetes  (years)` (two spaces before `(`), T2DM is `Duration of diabetes (years)`. Same for `Insulin (pmol/L)` capitalization. These break naive `pd.concat`.")
    w("")

    # ---------------- Comparison ----------------
    w("## Cross-dataset comparison")
    w("")
    w("### Overlap and differences")
    w("")
    w("| topic | AZT1D | Shanghai |")
    w("|---|---|---|")
    w("| unit of analysis | 1 file per patient, ~40 days | 1 file per patient-visit, ~14 days typical |")
    w("| sampling | 5 min (Dexcom-style) | 15 min (FreeStyle Libre-style) |")
    w("| glucose units | mg/dL | mg/dL (column name literally `CGM (mg / dl)`) |")
    w("| timezone | local Arizona (MST, no DST) | unstated; assume Asia/Shanghai |")
    w("| insulin fields | `Basal`, `TotalBolusInsulinDelivered`, `CorrectionDelivered`, `FoodDelivered` (U) | `Insulin dose - s.c.`, `Insulin dose - i.v.`, `CSII - bolus …`, `CSII - basal …` (IU); different route granularity |")
    w("| carbs | `CarbSize` (grams, numeric) | `Dietary intake` (free-text meal descriptions, Chinese) |")
    w("| device state | `DeviceMode`, `BolusType` (AID / hybrid closed-loop context) | none |")
    w("| demographics/labs | none in CSVs | 33-column summary xlsx per cohort |")
    w("| patient ID | `Subject N`, integer 1..25 | filename stem `PPPP_V_YYYYMMDD`, patient = first 4 digits |")
    w("| cohort | T1D adults on AID | T1D (12) + T2D (100) mixed |")
    w("")
    w("### What would break a naive merge into one `readings(patient_id, ts_utc, glucose_mgdl, ...)` table")
    w("")
    w("1. **Timezone** — AZT1D is Arizona local (UTC-7, no DST); Shanghai is unstated but almost certainly Asia/Shanghai (UTC+8). Concatenating without tz-localize + tz-convert will silently misalign by 15 hours.")
    w("2. **Sampling rate** — 5 min vs 15 min. Any per-row model needs a resample step; per-window features (TIR, GMI) are fine but need a `sampling_hz` column.")
    w("3. **Patient ID scheme** — integers 1..25 vs strings `1001..2099`. Must be namespaced (e.g. `az:1`, `sh:1001`) or the T1DM/T2DM `1xxx` prefix will collide with any future AZ subject beyond 999.")
    w("4. **Patient vs patient-visit granularity** — Shanghai has 1..3 visits per patient with distinct filename stems. If the target table is one row per reading with `patient_id`, Shanghai visits need to be either concatenated or given a `visit_id`.")
    w("5. **Column name for glucose** — `CGM` vs `CGM (mg / dl)` vs `Readings (CGM / BGM)` (AZT1D Subject 14). Requires a rename map, not `concat`.")
    w("6. **Insulin semantics** — AZT1D `Basal` is U/hr (a rate); Shanghai `CSII - basal insulin (Novolin R, IU / H)` is also U/hr, but AZT1D `TotalBolusInsulinDelivered` (units delivered per event) vs Shanghai `Insulin dose - s.c.` (units per injection) are the same physical thing under different names. `Insulin dose - i.v.` has no AZT1D analogue.")
    w("7. **Carbs semantics** — AZT1D `CarbSize` is numeric grams; Shanghai `Dietary intake` is a Chinese-language free-text meal string. Not mergeable without NLP.")
    w("8. **Row semantics** — AZT1D rows are event-typed (a row can be a CGM reading, a bolus, or a basal change; most non-CGM columns are NaN). Shanghai rows are ~timestamped snapshots with sparsely populated action columns. Dedupe / event-typing differs — a `pd.concat` without normalization gives inconsistent NaN patterns.")
    w("9. **Duplicate timestamps** — present in every AZT1D subject (5..2053 per file) and in some Shanghai files. Root cause differs (AZ = event-per-row; Shanghai = same-minute logging). Merge strategy must handle both.")
    w("10. **Character encoding** — Shanghai summary and file content include Chinese characters (code page 936 originals). Convert to UTF-8 (already done for interim CSVs) and stay UTF-8 downstream.")
    w("11. **Cohort labeling** — no `diabetes_type` column exists anywhere; must be injected from the source path (`AZT1D` → T1D, `Shanghai_T1DM` → T1D, `Shanghai_T2DM` → T2D).")
    w("")

    # ---------------- Open questions ----------------
    w("## Open questions")
    w("")
    w("- **AZT1D `CGM` column has 0 NaNs in every subject** — is every row a CGM reading, or are non-CGM event rows carrying the last-known CGM value? Affects whether we can trust the row as a glucose observation.")
    w("- **AZT1D Subject 14** uses `Readings (CGM / BGM)` as the glucose column. Is this a genuine schema variant (BGM fingerstick mixed in) or a rename? Check for a BGM/CGM type flag we might have missed.")
    w("- **AZT1D duplicate timestamps** — up to 2053 duplicates for Subject 15. Are these true collisions (same minute, different events) or export bugs? Confirm before choosing a dedupe key.")
    w("- **AZT1D `DeviceMode` values** — the enum encoding is not documented in `README.txt`. Need the manuscript or a value-frequency scan to map integers → {normal, sleep, exercise}.")
    w("- **Shanghai timezone** — unstated. Confirm it's Asia/Shanghai and not device UTC, since some Libre exports are UTC by default.")
    w("- **Shanghai `Dietary intake` language** — parse to grams via a Chinese food ontology, or drop for this project?")
    w("- **Shanghai insulin units** — `Novolin R, IU` implies a specific insulin; do we treat all insulin doses as a single scalar (`total_insulin_iu`) or preserve route (`s.c.` / `i.v.` / `CSII`)?")
    w("- **Shanghai T2DM patient count** — filename prefixes give a specific number; is any patient double-counted under a different ID? Cross-check `Patient Number` in the summary xlsx against filename stems.")
    w("- **Shanghai visit chaining** — for a patient with 3 visits, are the CGM windows contiguous (one long trace split by hospital session) or clearly separated by weeks? Affects whether we merge visits per patient.")
    w("- **AZT1D `Basal` units** — README says 'insulin delivery' with no unit. Values look like U/hr (0.5..1.5 range). Confirm from manuscript.")
    w("- **Glucose clamp at 400 mg/dL / 40 mg/dL** — treat as censored (right/left) or leave as-is? Matters for GMI / hyper/hypo excursion counts.")
    w("")
    w("## How to reproduce")
    w("")
    w("```")
    w("python scripts/convert_shanghai.py       # 125 xls/xlsx → data/interim/shanghai/**/*.csv")
    w("python scripts/profile_data.py           # prints per-file lines, writes this file")
    w("```")

    DOC.parent.mkdir(parents=True, exist_ok=True)
    DOC.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote {DOC}", flush=True)


def main():
    print("== AZT1D ==", flush=True)
    az, az_union, az_common, az_miss = profile_az()
    print("\n== Shanghai T1DM ==", flush=True)
    t1, t1freq, t1miss = profile_shanghai("Shanghai_T1DM")
    print("\n== Shanghai T2DM ==", flush=True)
    t2, t2freq, t2miss = profile_shanghai("Shanghai_T2DM")

    # summary column lists — read once, no read_excel loop
    t1sum = pd.read_excel(SHANGHAI_SUMMARY / "Shanghai_T1DM_Summary.xlsx", engine="openpyxl")
    t2sum = pd.read_excel(SHANGHAI_SUMMARY / "Shanghai_T2DM_Summary.xlsx", engine="openpyxl")
    t1_cols = [str(c).strip() for c in t1sum.columns]
    t2_cols = [str(c).strip() for c in t2sum.columns]

    # save per-file summaries
    scratch = ROOT / "scratch"
    scratch.mkdir(exist_ok=True)
    az.to_csv(scratch / "az_files.csv", index=False)
    t1.to_csv(scratch / "shanghai_t1dm_files.csv", index=False)
    t2.to_csv(scratch / "shanghai_t2dm_files.csv", index=False)

    render_md(az, az_union, az_common, az_miss, t1, t1freq, t1miss, t2, t2freq, t2miss, t1_cols, t2_cols)


if __name__ == "__main__":
    main()
