"""Invariants for raw.readings_raw after loader/load_raw.py has run."""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
AZ_ROOT = ROOT / "data" / "AZT1D 2025" / "AZT1D 2025" / "CGM Records"
SH_ROOT = ROOT / "data" / "interim" / "shanghai"

_FILENAME_RE = re.compile(r"^(\d+)_(\d+)_(\d{8})$")


# ---- expected counts computed independently from the source files ---------

def _expected_az_counts() -> dict[str, int]:
    """Per-patient rows after: parse ts, drop null ts + null CGM, dedupe unique ts."""
    out: dict[str, int] = {}
    subs = sorted(
        [p for p in AZ_ROOT.iterdir() if p.is_dir() and p.name.startswith("Subject ")],
        key=lambda p: int(p.name.split()[1]),
    )
    for sub in subs:
        pid_num = int(sub.name.split()[1])
        raw = pd.read_csv(next(sub.glob("*.csv")))
        cgm_col = "CGM" if "CGM" in raw.columns else "Readings (CGM / BGM)"
        raw["_ts"] = pd.to_datetime(raw["EventDateTime"], errors="coerce")
        raw = raw.dropna(subset=["_ts", cgm_col])
        out[f"az:{pid_num}"] = int(raw["_ts"].nunique())
    return out


def _expected_sh_counts() -> dict[str, int]:
    """Shanghai has visits: sum unique timestamps across a patient's visits."""
    per_pid: dict[str, int] = {}
    for group_dir in (SH_ROOT / "Shanghai_T1DM", SH_ROOT / "Shanghai_T2DM"):
        for csv in sorted(group_dir.glob("*.csv")):
            m = _FILENAME_RE.match(csv.stem)
            if not m:
                continue
            pid = f"sh:{m.group(1)}"
            raw = pd.read_csv(csv)
            raw.columns = [c.strip() for c in raw.columns]
            # two T2DM files (2045, 2049) use bare "CGM" instead of "CGM (mg / dl)"
            if "CGM (mg / dl)" not in raw.columns and "CGM" in raw.columns:
                raw = raw.rename(columns={"CGM": "CGM (mg / dl)"})
            raw["_ts"] = pd.to_datetime(raw["Date"], errors="coerce")
            raw = raw.dropna(subset=["_ts", "CGM (mg / dl)"])
            per_pid[pid] = per_pid.get(pid, 0) + int(raw["_ts"].nunique())
    return per_pid


# ---- tests -----------------------------------------------------------------

def test_row_counts_per_source_patient(db, loaded):
    """DB rows per patient match: (unique parsed timestamps in source, per pid)."""
    expected = {}
    expected.update(_expected_az_counts())
    expected.update(_expected_sh_counts())
    with db.cursor() as cur:
        cur.execute("SELECT patient_id, count(*) FROM raw.readings_raw GROUP BY patient_id")
        got = dict(cur.fetchall())
    # symmetric-difference check first for clearer failure messages
    missing = set(expected) - set(got)
    extra = set(got) - set(expected)
    assert not missing, f"missing patients in DB: {sorted(missing)[:10]}"
    assert not extra, f"unexpected patients in DB: {sorted(extra)[:10]}"
    mismatches = {p: (expected[p], got[p]) for p in expected if expected[p] != got[p]}
    assert not mismatches, f"row-count mismatches (expected, got): {dict(list(mismatches.items())[:10])}"


def test_glucose_within_20_500(db, loaded):
    with db.cursor() as cur:
        cur.execute("SELECT min(cgm_mgdl), max(cgm_mgdl) FROM raw.readings_raw")
        lo, hi = cur.fetchone()
    assert lo is not None and hi is not None
    assert 20.0 <= lo, f"min cgm_mgdl {lo} below 20"
    assert hi <= 500.0, f"max cgm_mgdl {hi} above 500"


def test_no_null_cgm(db, loaded):
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw.readings_raw WHERE cgm_mgdl IS NULL")
        assert cur.fetchone()[0] == 0


def test_ts_utc_monotonic_per_patient(db, loaded):
    """For each patient, sorted ts_utc should be strictly increasing.
    Since (patient_id, ts_utc) is PK, non-strictly-increasing is impossible
    unless a duplicate slipped through. Test computes lag(ts_utc) and asserts
    every diff > 0.
    """
    q = """
    WITH ordered AS (
        SELECT patient_id, ts_utc,
               lag(ts_utc) OVER (PARTITION BY patient_id ORDER BY ts_utc) AS prev_ts
          FROM raw.readings_raw
    )
    SELECT count(*)
      FROM ordered
     WHERE prev_ts IS NOT NULL
       AND ts_utc <= prev_ts
    """
    with db.cursor() as cur:
        cur.execute(q)
        assert cur.fetchone()[0] == 0


def test_shanghai_values_are_multiples_of_1_8(db, loaded):
    """Every Shanghai CGM value is exactly mmol/L * 18 (0.1 mmol/L quantum)."""
    q = """
    SELECT count(*)
      FROM raw.readings_raw
     WHERE cohort = 'Shanghai'
       AND abs(round((cgm_mgdl * 10)::numeric) - (cgm_mgdl * 10)::numeric) > 0.01
    """
    with db.cursor() as cur:
        cur.execute(q)
        # ^ first assert: values are 1-decimal-place (mmol*18 with rounding)
        non_1dp = cur.fetchone()[0]
        assert non_1dp == 0, f"{non_1dp} Shanghai rows have >1dp precision"

        cur.execute("""
            SELECT count(*)
              FROM raw.readings_raw
             WHERE cohort = 'Shanghai'
               AND mod(round((cgm_mgdl * 10)::numeric)::int, 18) <> 0
        """)
        not_mul = cur.fetchone()[0]
        assert not_mul == 0, f"{not_mul} Shanghai rows have cgm_mgdl not divisible by 1.8"


def test_rerun_leaves_counts_unchanged(db, loaded, conninfo):
    """Run the loader a second time; per-patient counts and total row count must be identical."""
    from loader.load_raw import main as load_main

    with db.cursor() as cur:
        cur.execute("SELECT patient_id, count(*) FROM raw.readings_raw GROUP BY patient_id")
        before = dict(cur.fetchall())
        cur.execute("SELECT count(*) FROM raw.readings_raw")
        before_total = cur.fetchone()[0]

    rc = load_main(["--conninfo", conninfo])
    assert rc == 0

    with db.cursor() as cur:
        cur.execute("SELECT patient_id, count(*) FROM raw.readings_raw GROUP BY patient_id")
        after = dict(cur.fetchall())
        cur.execute("SELECT count(*) FROM raw.readings_raw")
        after_total = cur.fetchone()[0]

    assert before_total == after_total, f"total row count changed: {before_total} -> {after_total}"
    assert before == after, "per-patient counts changed on rerun"


def test_az_subject14_flag_set(db, loaded):
    """R2: only az:14 carries glucose_source_labeled_bgm_possible = TRUE."""
    q = """
    SELECT patient_id, count(*)
      FROM raw.readings_raw
     WHERE glucose_source_labeled_bgm_possible = TRUE
     GROUP BY patient_id
    """
    with db.cursor() as cur:
        cur.execute(q)
        rows = cur.fetchall()
    assert len(rows) == 1
    pid, _ = rows[0]
    assert pid == "az:14"


def test_device_mode_enum(db, loaded):
    """R3 mapping: only {normal, sleep, exercise, unknown} appear."""
    with db.cursor() as cur:
        cur.execute("SELECT DISTINCT device_mode FROM raw.readings_raw WHERE cohort = 'AZT1D'")
        vals = {r[0] for r in cur.fetchall()}
    assert vals <= {"normal", "sleep", "exercise", "unknown"}, vals
