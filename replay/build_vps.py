"""Deterministically create N virtual patients from raw.readings_raw traces.

Same --seed and --count -> identical live.virtual_patient + live.device rows,
regardless of when the script is run. Sampling is with-replacement (200 vps
can be created from 137 source patients).

CLI:
    python replay/build_vps.py --count 200 --seed 42 [--speed 1.0] [--profile eval_v1] [--reset]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from loader.load_raw import CONNINFO_DEFAULT  # noqa: E402

SCHEMA_LIVE = ROOT / "sql" / "schema_live.sql"

# vps whose seed % 100 falls below this threshold get a systematic firmware
# offset on their device (the "one device batch" fault).
FIRMWARE_OFFSET_VP_PCT = 5
FIRMWARE_OFFSET_MGDL = 15.0

# Firmware versions. FAULTY_FIRMWARE is the "one device batch" the plan names
# and it is the only firmware carrying a non-zero firmware_offset_mgdl. This
# lets a downstream service filter/quarantine by firmware_version alone.
GOOD_FIRMWARE = "sim-v1"
FAULTY_FIRMWARE = "sim-v1-batch42"

# Maximum random offset (in source-time seconds) applied to each vp's start
# into its source trace. Spreads vps across different points in the trace
# so daily rhythms don't line up.
MAX_START_OFFSET_SEC = 3 * 24 * 3600  # up to 3 days into the source trace


def _derive_seed(parent_seed: int, i: int) -> int:
    """Deterministic per-vp seed from (parent_seed, index). SHA-256 -> first 63 bits."""
    h = hashlib.sha256(f"{parent_seed}:{i}".encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big", signed=False) & ((1 << 63) - 1)


def _device_type_for(source_pid: str) -> str:
    return "cgm_dexcom_g6" if source_pid.startswith("az:") else "cgm_libre"


def _apply_schema(cur) -> None:
    cur.execute(SCHEMA_LIVE.read_text(encoding="utf-8"))


def build(count: int, parent_seed: int, speed: float, profile: str, reset: bool,
          source_cohort: str, conninfo: str) -> None:
    rng = np.random.default_rng(parent_seed)
    with psycopg.connect(conninfo) as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            _apply_schema(cur)

            # Per-patient trace span so we can compute a safe random start offset.
            # Cohort filter: 'all' keeps the mixed 25 AZ + 112 SH pool; 'AZT1D'
            # restricts to the 25 AZ patients (5-min cadence, needed to hit the
            # 4000 rows/min throughput target at 2000 vps @ 10x); 'Shanghai'
            # restricts to the 112 SH patients.
            if source_cohort == "all":
                where = ""
                params: tuple = ()
            elif source_cohort in ("AZT1D", "Shanghai"):
                where = "WHERE cohort = %s"
                params = (source_cohort,)
            else:
                raise SystemExit(f"invalid --source-cohort {source_cohort!r}")
            cur.execute(f"""
                SELECT patient_id, min(ts_utc), max(ts_utc)
                  FROM raw.readings_raw
                 {where}
                 GROUP BY patient_id
                 ORDER BY patient_id
            """, params)
            pool = cur.fetchall()
            if not pool:
                raise SystemExit("raw.readings_raw is empty. Run loader/load_raw.py first.")

            if reset:
                # CASCADE clears device + worker_state via FK.
                cur.execute("TRUNCATE live.virtual_patient CASCADE")
                cur.execute("TRUNCATE live.worker_state")

            # Deterministic sample-with-replacement.
            picks = rng.choice(len(pool), size=count, replace=True)
            # Deterministic random-start offsets (one draw per vp).
            offset_draws = rng.uniform(0.0, 1.0, size=count)

            vp_rows: list[tuple] = []
            dev_rows: list[tuple] = []
            for i, pick in enumerate(picks):
                source_pid, trace_min, trace_max = pool[int(pick)]
                trace_span_sec = (trace_max - trace_min).total_seconds()
                # Clamp offset so at least one hour of trace remains after the start.
                max_offset = min(MAX_START_OFFSET_SEC, max(0.0, trace_span_sec - 3600.0))
                offset_sec = float(offset_draws[i]) * max_offset
                vp_start = trace_min + timedelta(seconds=offset_sec)

                vp_id = f"vp:{i+1:04d}"
                seed = _derive_seed(parent_seed, i)
                is_faulty = (seed % 100) < FIRMWARE_OFFSET_VP_PCT
                fw_offset = FIRMWARE_OFFSET_MGDL if is_faulty else 0.0
                fw_version = FAULTY_FIRMWARE if is_faulty else GOOD_FIRMWARE

                vp_rows.append((
                    vp_id, source_pid, vp_start, int(offset_sec), speed, seed, True, profile,
                ))
                dev_rows.append((
                    f"{vp_id}/cgm", vp_id, _device_type_for(source_pid), fw_version, fw_offset,
                ))

            cur.executemany("""
                INSERT INTO live.virtual_patient
                    (virtual_patient_id, source_patient_id, source_trace_start_utc,
                     time_offset_seconds, speed_factor, seed, synthetic_vitals_enabled, injection_profile)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (virtual_patient_id) DO UPDATE
                   SET source_patient_id       = EXCLUDED.source_patient_id,
                       source_trace_start_utc  = EXCLUDED.source_trace_start_utc,
                       time_offset_seconds     = EXCLUDED.time_offset_seconds,
                       speed_factor            = EXCLUDED.speed_factor,
                       seed                    = EXCLUDED.seed,
                       injection_profile       = EXCLUDED.injection_profile
            """, vp_rows)
            cur.executemany("""
                INSERT INTO live.device
                    (device_id, virtual_patient_id, device_type, firmware_version, firmware_offset_mgdl)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (device_id) DO UPDATE
                   SET device_type          = EXCLUDED.device_type,
                       firmware_version     = EXCLUDED.firmware_version,
                       firmware_offset_mgdl = EXCLUDED.firmware_offset_mgdl
            """, dev_rows)
        conn.commit()

    n_offset = sum(1 for r in dev_rows if r[4] > 0)
    print(f"created/updated {len(vp_rows)} virtual patients + {len(dev_rows)} devices "
          f"(seed={parent_seed}, speed={speed}, profile={profile!r}, firmware_offset_devices={n_offset})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--count", type=int, required=True, help="number of virtual patients to create")
    ap.add_argument("--seed", type=int, default=42, help="parent RNG seed; same seed+count -> identical assignments")
    ap.add_argument("--speed", type=float, default=1.0, help="default replay speed factor for these vps")
    ap.add_argument("--profile", default="eval_v1", help="injection profile name")
    ap.add_argument("--reset", action="store_true", help="TRUNCATE virtual_patient (cascades to device) before insert")
    ap.add_argument("--source-cohort", choices=("all", "AZT1D", "Shanghai"), default="all",
                    help="filter source patient pool; use 'AZT1D' for throughput demos (5-min cadence)")
    ap.add_argument("--conninfo", default=os.environ.get("CAREVAULT_PG", CONNINFO_DEFAULT))
    args = ap.parse_args(argv)
    build(args.count, args.seed, args.speed, args.profile, args.reset,
          args.source_cohort, args.conninfo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
