"""Batch loader for raw.readings_raw.

Reads AZT1D CSVs (data/AZT1D 2025/**/CGM Records/Subject N/Subject N.csv) and
the converted Shanghai CSVs (data/interim/shanghai/**/*.csv), applies:
  - R1 aggregation (dedupe duplicate timestamps: median CGM, first-non-null others)
  - R2 (Subject 14 kept as CGM; glucose_source_labeled_bgm_possible flag)
  - R3 (DeviceMode -> {normal, sleep, exercise, unknown})
  - Timezone localization (Arizona / Shanghai) -> UTC
  - Resolved bolus_u = TotalBolusInsulinDelivered alone (AZT1D)
  - Fixed per-cohort clamp constants for is_clamped

Idempotent: the load happens inside one transaction that TRUNCATEs
raw.readings_raw before COPYing the fresh rows. Rerun -> same row count.
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg

ROOT = Path(__file__).resolve().parent.parent
AZ_ROOT = ROOT / "data" / "AZT1D 2025" / "AZT1D 2025" / "CGM Records"
SH_ROOT = ROOT / "data" / "interim" / "shanghai"
SCHEMA_SQL = ROOT / "sql" / "schema_raw.sql"

CONNINFO_DEFAULT = "host=127.0.0.1 port=5433 dbname=carevault user=carevault password=carevault"

CLAMP = {
    "AZT1D":    (40.0, 400.0),
    "Shanghai": (39.6, None),   # per schema-proposal: Libre floor only, no upper
}
TZ = {
    "AZT1D":    "America/Phoenix",
    "Shanghai": "Asia/Shanghai",
}
SAMPLING_SEC = {"AZT1D": 300, "Shanghai": 900}

OUT_COLS = [
    "patient_id", "visit_id", "ts_utc", "ts_local", "source_tz", "local_hour",
    "sampling_sec", "cgm_mgdl", "is_clamped", "bolus_u", "basal_rate_u_hr",
    "insulin_iv_u", "carb_g", "carb_free_text", "device_mode", "bolus_type",
    "diabetes_type", "cohort", "glucose_source_labeled_bgm_possible",
]


@dataclass
class Rejections:
    """Rows dropped from source, per reason. For the load summary."""
    bad_ts: int = 0
    null_cgm: int = 0
    dup_extras_collapsed: int = 0
    files_read: int = 0
    src_rows: int = 0


@dataclass
class CohortResult:
    df: pd.DataFrame
    rej: Rejections = field(default_factory=Rejections)


# --- DeviceMode mapping (R3) ------------------------------------------------

_MODE_LITERAL = {"sleep": "sleep", "sleepsleep": "sleep", "exercise": "exercise", "0": "unknown"}

def _map_device_mode(v: object) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "normal"
    s = str(v).strip()
    return _MODE_LITERAL.get(s, "unknown")


# --- IU extraction from Shanghai free-text insulin columns ------------------
# Shanghai's "Insulin dose - s.c." is strings like "Novolin R, 4 IU"; the i.v.
# column is longer clinical text like "500ml 0.9% sodium chloride, 12 IU
# Novolin R, 10 ml 10% potassium chloride". Sum all IU numeric tokens to
# capture the case where a single cell lists two insulin components.

_IU_RE = re.compile(r"(\d+(?:\.\d+)?)\s*IU", re.IGNORECASE)

def _extract_iu(v: object) -> float:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return float("nan")
    if isinstance(v, (int, float)):
        return float(v)
    matches = _IU_RE.findall(str(v))
    if not matches:
        return float("nan")
    return sum(float(m) for m in matches)


# --- AZT1D ------------------------------------------------------------------

def build_azt1d() -> CohortResult:
    rej = Rejections()
    frames: list[pd.DataFrame] = []
    subs = sorted(
        [p for p in AZ_ROOT.iterdir() if p.is_dir() and p.name.startswith("Subject ")],
        key=lambda p: int(p.name.split()[1]),
    )
    for sub in subs:
        pid_num = int(sub.name.split()[1])
        pid = f"az:{pid_num}"
        vid = f"az:{pid_num}/v0"
        csv = next(sub.glob("*.csv"))
        rej.files_read += 1
        raw = pd.read_csv(csv)
        rej.src_rows += len(raw)

        # R2: Subject 14 has "Readings (CGM / BGM)" — same field, rename
        cgm_col = "CGM" if "CGM" in raw.columns else "Readings (CGM / BGM)"
        raw = raw.rename(columns={cgm_col: "_cgm"})

        raw["ts_local"] = pd.to_datetime(raw["EventDateTime"], errors="coerce")
        rej.bad_ts += int(raw["ts_local"].isna().sum())
        raw = raw.dropna(subset=["ts_local"])
        rej.null_cgm += int(raw["_cgm"].isna().sum())
        raw = raw.dropna(subset=["_cgm"])

        # R1: aggregate duplicate timestamps within this patient.
        # CGM: median (robust to both the Subject-15 repeated-snapshot bug and
        # the Subject-1 two-distinct-reads-per-second pattern).
        # Everything else: first non-null. See docs/data-inventory.md R1 and
        # the note about why summing was rejected (would over-count Basal
        # rates in the 20-copy snapshot bug).
        pre = len(raw)
        agg = raw.groupby("ts_local", as_index=False).agg({
            "_cgm": "median",
            "Basal": "first",
            "TotalBolusInsulinDelivered": "first",
            "CarbSize": "first",
            "DeviceMode": "first",
            "BolusType": "first",
        })
        rej.dup_extras_collapsed += pre - len(agg)

        agg["patient_id"] = pid
        agg["visit_id"] = vid
        agg["cohort"] = "AZT1D"
        agg["diabetes_type"] = "T1"
        agg["source_tz"] = TZ["AZT1D"]
        agg["sampling_sec"] = SAMPLING_SEC["AZT1D"]
        agg["glucose_source_labeled_bgm_possible"] = (pid == "az:14")
        # Arizona = America/Phoenix, no DST -> localize is safe.
        agg["ts_utc"] = (
            agg["ts_local"].dt.tz_localize(TZ["AZT1D"], nonexistent="shift_forward", ambiguous="NaT")
                            .dt.tz_convert("UTC")
        )
        agg["local_hour"] = agg["ts_local"].dt.hour.astype("int16")
        agg["cgm_mgdl"] = agg["_cgm"].astype("float64")
        lo, hi = CLAMP["AZT1D"]
        agg["is_clamped"] = (agg["cgm_mgdl"] <= lo) | (agg["cgm_mgdl"] >= hi)
        # Resolved tradeoff R: Total is the correct scalar (see docs/schema-proposal.md §5.R)
        agg["bolus_u"] = agg["TotalBolusInsulinDelivered"].astype("float64")
        agg["basal_rate_u_hr"] = agg["Basal"].astype("float64")
        agg["insulin_iv_u"] = np.nan
        agg["carb_g"] = agg["CarbSize"].astype("float64")
        agg["carb_free_text"] = None
        agg["device_mode"] = agg["DeviceMode"].apply(_map_device_mode)
        agg["bolus_type"] = agg["BolusType"]

        frames.append(agg[OUT_COLS])
    df = pd.concat(frames, ignore_index=True)
    return CohortResult(df=df, rej=rej)


# --- Shanghai ---------------------------------------------------------------

_FILENAME_RE = re.compile(r"^(\d+)_(\d+)_(\d{8})$")

def build_shanghai() -> CohortResult:
    rej = Rejections()
    frames: list[pd.DataFrame] = []
    for group_dir in (SH_ROOT / "Shanghai_T1DM", SH_ROOT / "Shanghai_T2DM"):
        dtype = "T1" if "T1DM" in group_dir.name else "T2"
        for csv in sorted(group_dir.glob("*.csv")):
            m = _FILENAME_RE.match(csv.stem)
            if not m:
                continue
            pid = f"sh:{m.group(1)}"
            vid = f"sh:{m.group(1)}/{csv.stem}"
            rej.files_read += 1
            raw = pd.read_csv(csv)
            rej.src_rows += len(raw)
            raw.columns = [c.strip() for c in raw.columns]
            # Drop the duplicate Chinese 饮食 column (same content as Dietary intake).
            if "饮食" in raw.columns:
                raw = raw.drop(columns=["饮食"])

            raw["ts_local"] = pd.to_datetime(raw["Date"], errors="coerce")
            rej.bad_ts += int(raw["ts_local"].isna().sum())
            raw = raw.dropna(subset=["ts_local"])
            # Two Shanghai T2DM files (2045, 2049) label the column bare "CGM"
            # instead of "CGM (mg / dl)"; normalize to the canonical name.
            if "CGM (mg / dl)" not in raw.columns and "CGM" in raw.columns:
                raw = raw.rename(columns={"CGM": "CGM (mg / dl)"})
            gcol = "CGM (mg / dl)"
            rej.null_cgm += int(raw[gcol].isna().sum())
            raw = raw.dropna(subset=[gcol])

            # Not every Shanghai file carries every optional column
            # (e.g. small subset of files omit the CSII columns). Fill
            # missing columns with NaN before aggregation.
            _optional = [
                "Insulin dose - s.c.",
                "CSII - bolus insulin (Novolin R, IU)",
                "CSII - basal insulin (Novolin R, IU / H)",
                "Insulin dose - i.v.",
                "Dietary intake",
            ]
            for c in _optional:
                if c not in raw.columns:
                    raw[c] = np.nan

            # Shanghai has no observed duplicate timestamps but run the same
            # aggregation for safety and consistency with the AZ pipeline.
            pre = len(raw)
            agg = raw.groupby("ts_local", as_index=False).agg({
                gcol: "median",
                "Insulin dose - s.c.": "first",
                "CSII - bolus insulin (Novolin R, IU)": "first",
                "CSII - basal insulin (Novolin R, IU / H)": "first",
                "Insulin dose - i.v.": "first",
                "Dietary intake": "first",
            })
            rej.dup_extras_collapsed += pre - len(agg)

            agg["patient_id"] = pid
            agg["visit_id"] = vid
            agg["cohort"] = "Shanghai"
            agg["diabetes_type"] = dtype
            agg["source_tz"] = TZ["Shanghai"]
            agg["sampling_sec"] = SAMPLING_SEC["Shanghai"]
            agg["glucose_source_labeled_bgm_possible"] = False
            agg["ts_utc"] = (
                agg["ts_local"].dt.tz_localize(TZ["Shanghai"], nonexistent="shift_forward", ambiguous="NaT")
                                .dt.tz_convert("UTC")
            )
            agg["local_hour"] = agg["ts_local"].dt.hour.astype("int16")
            agg["cgm_mgdl"] = agg[gcol].astype("float64")
            lo, _ = CLAMP["Shanghai"]  # no upper clamp for Libre
            agg["is_clamped"] = agg["cgm_mgdl"] <= lo
            # Insulin columns are mixed. s.c. and i.v. are free-text clinical
            # strings ("Novolin R, 4 IU", "500ml 0.9% sodium chloride, 12 IU
            # Novolin R, ...") -> use IU regex extractor. CSII columns are
            # numeric but a small number of cells carry status annotations
            # like "temporarily suspend insulin delivery" -> to_numeric coerce
            # (annotation becomes NaN, which correctly means "no delivery here").
            sc = agg["Insulin dose - s.c."].apply(_extract_iu).astype("float64")
            cs = pd.to_numeric(agg["CSII - bolus insulin (Novolin R, IU)"], errors="coerce").astype("float64")
            both_na = sc.isna() & cs.isna()
            agg["bolus_u"] = (sc.fillna(0.0) + cs.fillna(0.0)).where(~both_na, np.nan)
            agg["basal_rate_u_hr"] = pd.to_numeric(
                agg["CSII - basal insulin (Novolin R, IU / H)"], errors="coerce"
            ).astype("float64")
            agg["insulin_iv_u"] = agg["Insulin dose - i.v."].apply(_extract_iu).astype("float64")
            agg["carb_g"] = np.nan
            agg["carb_free_text"] = agg["Dietary intake"].astype("object")
            agg["device_mode"] = None
            agg["bolus_type"] = None

            frames.append(agg[OUT_COLS])
    df = pd.concat(frames, ignore_index=True)
    return CohortResult(df=df, rej=rej)


# --- COPY into readings_raw -------------------------------------------------

def _prep_for_copy(df: pd.DataFrame) -> pd.DataFrame:
    """Convert a load frame to a shape suitable for CSV -> COPY."""
    out = df.copy()
    # ts_utc: aware datetime -> ISO string with tz. Postgres timestamptz accepts.
    out["ts_utc"] = out["ts_utc"].dt.strftime("%Y-%m-%d %H:%M:%S%z")
    out["ts_local"] = out["ts_local"].dt.strftime("%Y-%m-%d %H:%M:%S")
    out["is_clamped"] = out["is_clamped"].astype(bool)
    out["glucose_source_labeled_bgm_possible"] = out["glucose_source_labeled_bgm_possible"].astype(bool)
    return out


def load_into_db(conninfo: str, az: pd.DataFrame, sh: pd.DataFrame) -> tuple[int, int]:
    """TRUNCATE + COPY inside one transaction; return (n_az, n_sh) rows loaded."""
    schema_sql = SCHEMA_SQL.read_text(encoding="utf-8")
    with psycopg.connect(conninfo) as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(schema_sql)  # CREATE ... IF NOT EXISTS is idempotent
            cur.execute("TRUNCATE TABLE raw.readings_raw")

            cols = ", ".join(OUT_COLS)
            copy_sql = (
                f"COPY raw.readings_raw ({cols}) "
                f"FROM STDIN WITH (FORMAT CSV, NULL '', QUOTE '\"', ESCAPE '\"')"
            )

            n_az = _copy_frame(cur, copy_sql, _prep_for_copy(az))
            n_sh = _copy_frame(cur, copy_sql, _prep_for_copy(sh))
        conn.commit()
    return n_az, n_sh


def _copy_frame(cur, copy_sql: str, df: pd.DataFrame) -> int:
    buf = io.StringIO()
    df[OUT_COLS].to_csv(buf, index=False, header=False, na_rep="")
    buf.seek(0)
    with cur.copy(copy_sql) as cp:
        while True:
            chunk = buf.read(1 << 16)
            if not chunk:
                break
            cp.write(chunk)
    return len(df)


# --- summary ----------------------------------------------------------------

def _print_summary(az: pd.DataFrame, sh: pd.DataFrame, rj_az: Rejections, rj_sh: Rejections, elapsed_s: float) -> None:
    def fmt_range(d):
        if d.empty:
            return "(empty)"
        return f"{d['ts_local'].min()}  ->  {d['ts_local'].max()}"

    print()
    print("=" * 72)
    print("LOAD SUMMARY")
    print("=" * 72)
    print(f"{'cohort':<10s} {'rows loaded':>13s} {'patients':>10s} {'visits':>8s} {'files':>7s} {'source rows':>13s} {'local date range'}")
    for name, df, rj in (("AZT1D", az, rj_az), ("Shanghai", sh, rj_sh)):
        print(
            f"{name:<10s} "
            f"{len(df):>13,d} "
            f"{df['patient_id'].nunique():>10d} "
            f"{df['visit_id'].nunique():>8d} "
            f"{rj.files_read:>7d} "
            f"{rj.src_rows:>13,d} "
            f"{fmt_range(df)}"
        )
    total_rows = len(az) + len(sh)
    total_pat = pd.concat([az["patient_id"], sh["patient_id"]]).nunique()
    print(f"{'TOTAL':<10s} {total_rows:>13,d} {total_pat:>10d}")
    print()
    print("Rejected / collapsed rows (per cohort):")
    print(f"{'cohort':<10s} {'bad_ts':>8s} {'null_cgm':>10s} {'dup_extras':>12s}")
    for name, rj in (("AZT1D", rj_az), ("Shanghai", rj_sh)):
        print(f"{name:<10s} {rj.bad_ts:>8d} {rj.null_cgm:>10d} {rj.dup_extras_collapsed:>12d}")
    print()
    print(f"elapsed: {elapsed_s:.1f}s")


# --- entrypoint -------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--conninfo", default=os.environ.get("CAREVAULT_PG", CONNINFO_DEFAULT))
    ap.add_argument("--dry-run", action="store_true", help="Build frames + print summary, do not touch DB.")
    args = ap.parse_args(argv)

    t0 = time.perf_counter()
    print("Reading AZT1D...", flush=True)
    az = build_azt1d()
    print(f"  built {len(az.df):,} rows from {az.rej.files_read} files ({az.rej.src_rows:,} source rows)")
    print("Reading Shanghai...", flush=True)
    sh = build_shanghai()
    print(f"  built {len(sh.df):,} rows from {sh.rej.files_read} files ({sh.rej.src_rows:,} source rows)")

    if args.dry_run:
        _print_summary(az.df, sh.df, az.rej, sh.rej, time.perf_counter() - t0)
        return 0

    print("Loading into Postgres...", flush=True)
    n_az, n_sh = load_into_db(args.conninfo, az.df, sh.df)
    print(f"  COPY complete: {n_az:,} AZT1D + {n_sh:,} Shanghai rows into raw.readings_raw")

    _print_summary(az.df, sh.df, az.rej, sh.rej, time.perf_counter() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
