"""Replay engine. Streams raw.readings_raw traces into live.vitals_readings
under a virtual wall clock.

Per-vp behavior driven by (source_trace_start_utc, speed_factor, seed) from
live.virtual_patient — same triple -> identical replay.

Injection profile 'eval_v1':
  - Device batch: 5% of vps have firmware_offset (+15 mg/dL, constant). Every
    row from these devices is labeled injected_type='firmware_offset'.
  - Point faults on non-firmware devices: 1% of rows become one of
    {spike, sensor_dropout, stuck}.
  - Duplicate sends: 0.5% of rows emitted twice (UNIQUE (vp, event_time)
    drops the second at insert time).
  - Late arrivals: 2% of rows have ingested_at pushed 5-60s past event_time.
  - Malformed: 0.2% of rows land in live.dead_letter instead.

Write path: per-tick batch -> session TEMP table stg_vitals -> COPY ->
INSERT ... SELECT ... ON CONFLICT DO NOTHING -> NOTIFY vitals_new.

CLI:
    python replay/run.py --vp-count 200 --speed 10 --duration 180
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from loader.load_raw import CONNINFO_DEFAULT  # noqa: E402

WORKER_ID = os.environ.get("WORKER_ID", "worker-1")
# 1s tick: with 2000 vps emitting ~67 rows/sec, batches average ~67 rows
# vs. 1-2 at 0.25s. COPY+INSERT round-trip cost amortized ~40x. Diagnostic
# run showed sleep dominated at 97% of tick time with the shorter interval.
TICK_SEC = 1.0
TRACE_PREFETCH = 500        # rows preloaded per vp at start (enough for a short run)
INJECT_TYPES = ("spike", "sensor_dropout", "stuck")
STUCK_RUN_LENGTH = 18  # inject 18 identical readings in a row when stuck fires
                       # (comfortably above measured organic max of 13 in AZT1D)


# --- state ------------------------------------------------------------------

@dataclass
class VpState:
    vp_id: str
    device_id: str
    source_pid: str
    seed: int
    speed: float
    firmware_offset: float
    sampling_sec: int
    trace: list                          # [(source_ts_utc, cgm_mgdl)]
    source_start: datetime
    rng: np.random.Generator
    idx: int = 0
    last_cgm: float | None = None
    n_emitted: int = 0
    last_source_ts_utc: datetime | None = None
    # In-flight stuck injection: when a stuck event starts, we keep re-emitting
    # the same value for `stuck_remaining` more emissions with injected_type='stuck'.
    stuck_remaining: int = 0
    stuck_value: float | None = None


@dataclass
class Metrics:
    attempted: int = 0
    inserted: int = 0
    duplicates_suppressed: int = 0
    dead_letter: int = 0
    late: int = 0
    injected: dict = field(default_factory=lambda: {
        "firmware_offset": 0, "spike": 0, "sensor_dropout": 0, "stuck": 0,
    })
    batches: int = 0
    # Per-tick phase timings (seconds). Populated only when instrument=True.
    t_schedule: list = field(default_factory=list)
    t_copy: list = field(default_factory=list)
    t_insert: list = field(default_factory=list)
    t_dead: list = field(default_factory=list)
    t_sleep: list = field(default_factory=list)
    batch_sizes: list = field(default_factory=list)


# --- synthetic vitals -------------------------------------------------------

def _synth_vitals(rng: np.random.Generator, local_hour: int, cgm: float | None,
                  last_cgm: float | None) -> tuple[int, int, int, float]:
    """HR / SBP / DBP / SpO2. Daily rhythm on HR; hypo (<70 mg/dL) drives HR up."""
    circadian = 15.0 * math.sin((local_hour - 6) / 24.0 * 2.0 * math.pi)  # peaks mid-afternoon
    hr = 65.0 + circadian + rng.normal(0.0, 3.0)
    cgm_for_hypo = cgm if cgm is not None else last_cgm
    if cgm_for_hypo is not None and cgm_for_hypo < 70.0:
        hr += float(rng.uniform(15.0, 30.0))
    sbp = 118.0 + rng.normal(0.0, 5.0)
    dbp = 76.0 + rng.normal(0.0, 4.0)
    spo2 = 97.0 + rng.normal(0.0, 0.5)
    return int(round(hr)), int(round(sbp)), int(round(dbp)), min(100.0, max(0.0, float(spo2)))


# --- injection --------------------------------------------------------------

def _maybe_point_inject(vp: VpState, cgm: float) -> tuple[float | None, str | None]:
    """~1% chance of a point-fault on non-firmware-offset devices.
    Stuck injection now starts a MULTI-EMISSION run (STUCK_RUN_LENGTH readings).
    Spike injection now emits a physiologically-impossible value (400–600 mg/dL),
    not a 1.5–2x multiplier (which usually landed inside normal range)."""
    # A stuck run is already in flight for this vp: replay the stuck value and
    # decrement the counter. Overrides any fresh injection roll.
    if vp.stuck_remaining > 0:
        vp.stuck_remaining -= 1
        return vp.stuck_value, "stuck"
    if vp.rng.random() >= 0.01:
        return cgm, None
    choice = INJECT_TYPES[int(vp.rng.integers(0, len(INJECT_TYPES)))]
    if choice == "spike":
        # 400–600 mg/dL — beyond the sensor's physiologic ceiling; clearly artifact.
        return float(vp.rng.uniform(400.0, 600.0)), "spike"
    if choice == "sensor_dropout":
        return None, "sensor_dropout"
    if choice == "stuck":
        # Start a run of STUCK_RUN_LENGTH identical emissions (this one + N-1 more).
        stuck_val = vp.last_cgm if vp.last_cgm is not None else cgm
        vp.stuck_value = stuck_val
        vp.stuck_remaining = STUCK_RUN_LENGTH - 1
        return stuck_val, "stuck"
    return cgm, None


# --- vp loading -------------------------------------------------------------

def _load_vps(conn) -> list[VpState]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT vp.virtual_patient_id, vp.source_patient_id, vp.source_trace_start_utc,
                   vp.speed_factor, vp.seed,
                   d.device_id, d.firmware_offset_mgdl
              FROM live.virtual_patient vp
              JOIN live.device d ON d.virtual_patient_id = vp.virtual_patient_id
             ORDER BY vp.virtual_patient_id
        """)
        heads = cur.fetchall()

    vps: list[VpState] = []
    with conn.cursor() as cur:
        for vp_id, pid, trace_start, speed, seed, dev_id, fw_offset in heads:
            cur.execute("""
                SELECT ts_utc, cgm_mgdl, sampling_sec
                  FROM raw.readings_raw
                 WHERE patient_id = %s
                   AND ts_utc >= %s
                 ORDER BY ts_utc
                 LIMIT %s
            """, (pid, trace_start, TRACE_PREFETCH))
            rows = cur.fetchall()
            if not rows:
                continue
            trace = [(r[0], float(r[1])) for r in rows]
            sampling_sec = int(rows[0][2])
            vps.append(VpState(
                vp_id=vp_id, device_id=dev_id, source_pid=pid,
                seed=int(seed), speed=float(speed), firmware_offset=float(fw_offset),
                sampling_sec=sampling_sec, trace=trace, source_start=trace_start,
                rng=np.random.default_rng(int(seed)),
            ))
    return vps


# --- write path -------------------------------------------------------------

_STG_DDL = """
CREATE TEMP TABLE IF NOT EXISTS stg_vitals (
    virtual_patient_id  text,
    device_id           text,
    event_time          timestamptz,
    ingested_at         timestamptz,
    cgm_mgdl            real,
    heart_rate_bpm      smallint,
    systolic_bp_mmhg    smallint,
    diastolic_bp_mmhg   smallint,
    spo2_pct            real,
    injected_type       text,
    sampling_sec        smallint,
    source_trace_ref    text
)
"""

_STG_COLS = (
    "virtual_patient_id", "device_id", "event_time", "ingested_at", "cgm_mgdl",
    "heart_rate_bpm", "systolic_bp_mmhg", "diastolic_bp_mmhg", "spo2_pct",
    "injected_type", "sampling_sec", "source_trace_ref",
)


def _ensure_partitions(conn, start_utc: datetime, end_utc: datetime) -> None:
    with conn.cursor() as cur:
        d = start_utc.date()
        while d <= end_utc.date():
            cur.execute("SELECT live.ensure_vitals_partition(%s)", (d,))
            d += timedelta(days=1)


def _write_batch(conn, batch: list[tuple], metrics: Metrics, instrument: bool = False) -> None:
    if not batch:
        return
    t_copy_s = t_ins_s = 0.0
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("TRUNCATE stg_vitals")
            copy_sql = f"COPY stg_vitals ({', '.join(_STG_COLS)}) FROM STDIN"
            t0 = time.perf_counter()
            with cur.copy(copy_sql) as cp:
                for r in batch:
                    cp.write_row(r)
            t_copy_s = time.perf_counter() - t0
            t1 = time.perf_counter()
            cur.execute(f"""
                INSERT INTO live.vitals_readings ({', '.join(_STG_COLS)})
                SELECT {', '.join(_STG_COLS)} FROM stg_vitals
                ON CONFLICT (virtual_patient_id, event_time) DO NOTHING
            """)
            t_ins_s = time.perf_counter() - t1
            inserted = cur.rowcount
            metrics.inserted += inserted
            metrics.duplicates_suppressed += (len(batch) - inserted)
            cur.execute("NOTIFY vitals_new")
    if instrument:
        metrics.t_copy.append(t_copy_s)
        metrics.t_insert.append(t_ins_s)
        metrics.batch_sizes.append(len(batch))


def _write_dead_letter(conn, dead: list[tuple], metrics: Metrics, instrument: bool = False) -> None:
    if not dead:
        return
    t0 = time.perf_counter()
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO live.dead_letter (virtual_patient_id, device_id, raw_payload, reason)
                VALUES (%s, %s, %s::jsonb, %s)
            """, dead)
    metrics.dead_letter += len(dead)
    if instrument:
        metrics.t_dead.append(time.perf_counter() - t0)


# --- main loop --------------------------------------------------------------

def run(conn, vps: list[VpState], duration_sec: float, speed_override: float | None,
        instrument: bool = False, tick_sec: float | None = None) -> Metrics:
    """tick_sec overrides the module-level TICK_SEC for this run only."""
    effective_tick = TICK_SEC if tick_sec is None else float(tick_sec)
    metrics = Metrics()

    # Apply speed override if requested (does not persist to DB).
    if speed_override is not None:
        for vp in vps:
            vp.speed = speed_override

    wall_start = datetime.now(timezone.utc)
    end_wall_mono = time.monotonic() + duration_sec
    for vp in vps:
        # replay begins with each vp's source_start mapped to wall_start
        pass  # nothing to reset; idx=0

    # Pre-create partitions covering the run window (with a little slack).
    est_end_wall = wall_start + timedelta(seconds=duration_sec + 300)
    _ensure_partitions(conn, wall_start, est_end_wall)

    # session-scoped staging table
    with conn.cursor() as cur:
        cur.execute(_STG_DDL)

    while time.monotonic() < end_wall_mono:
        t_tick0 = time.perf_counter()
        now = datetime.now(timezone.utc)
        batch: list[tuple] = []
        dead: list[tuple] = []

        for vp in vps:
            while vp.idx < len(vp.trace):
                src_ts, src_cgm = vp.trace[vp.idx]
                delta_source_sec = (src_ts - vp.source_start).total_seconds()
                event_time = wall_start + timedelta(seconds=delta_source_sec / vp.speed)
                if event_time > now:
                    break
                vp.idx += 1

                # ---- CGM transformation ----
                cgm: float | None = float(src_cgm) + vp.firmware_offset
                injected: str | None = None
                if vp.firmware_offset != 0.0:
                    injected = "firmware_offset"
                    metrics.injected["firmware_offset"] += 1
                else:
                    cgm, inj = _maybe_point_inject(vp, cgm)
                    if inj is not None:
                        injected = inj
                        metrics.injected[inj] += 1

                if cgm is not None:
                    vp.last_cgm = cgm

                # ---- synthetic vitals ----
                hr, sbp, dbp, spo2 = _synth_vitals(vp.rng, event_time.hour, cgm, vp.last_cgm)

                # ---- ingested_at: normal vs late ----
                if float(vp.rng.random()) < 0.02:
                    lag = float(vp.rng.uniform(5.0, 60.0))
                    metrics.late += 1
                else:
                    lag = float(vp.rng.uniform(0.01, 0.10))
                ingested_at = event_time + timedelta(seconds=lag)

                # ---- malformed rate: shunt to dead_letter and skip ----
                if float(vp.rng.random()) < 0.002:
                    payload = {
                        "virtual_patient_id": vp.vp_id,
                        "device_id": vp.device_id,
                        "event_time": event_time.isoformat(),
                        "cgm_mgdl": "NaN_str_corruption",
                        "source_trace_ref": f"readings_raw:{vp.source_pid}:{src_ts.isoformat()}",
                    }
                    dead.append((vp.vp_id, vp.device_id, json.dumps(payload), "malformed_cgm"))
                    continue

                row = (
                    vp.vp_id, vp.device_id, event_time, ingested_at,
                    None if cgm is None else float(cgm),
                    hr, sbp, dbp, float(spo2),
                    injected,
                    int(vp.sampling_sec),
                    f"readings_raw:{vp.source_pid}:{src_ts.isoformat()}",
                )
                batch.append(row)
                metrics.attempted += 1

                # ---- duplicate send ----
                if float(vp.rng.random()) < 0.005:
                    batch.append(row)
                    metrics.attempted += 1

                vp.n_emitted += 1
                vp.last_source_ts_utc = src_ts

        t_sched = time.perf_counter() - t_tick0
        _write_batch(conn, batch, metrics, instrument=instrument)
        _write_dead_letter(conn, dead, metrics, instrument=instrument)
        if batch or dead:
            metrics.batches += 1

        t_before_sleep = time.perf_counter()
        time.sleep(effective_tick)
        if instrument:
            metrics.t_schedule.append(t_sched)
            metrics.t_sleep.append(time.perf_counter() - t_before_sleep)

    # Persist worker_state at end (a longer replay would checkpoint periodically).
    with conn.cursor() as cur:
        params = [
            (WORKER_ID, vp.vp_id, vp.last_source_ts_utc,
             wall_start + timedelta(seconds=(vp.trace[vp.idx - 1][0] - vp.source_start).total_seconds() / vp.speed)
                 if vp.idx > 0 else None,
             vp.n_emitted)
            for vp in vps
        ]
        cur.executemany("""
            INSERT INTO live.worker_state (worker_id, virtual_patient_id, last_source_ts_utc, last_event_time, n_emitted)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (worker_id, virtual_patient_id) DO UPDATE
               SET last_source_ts_utc = EXCLUDED.last_source_ts_utc,
                   last_event_time    = EXCLUDED.last_event_time,
                   n_emitted          = EXCLUDED.n_emitted,
                   updated_at         = now()
        """, params)

    return metrics


# --- report -----------------------------------------------------------------

def _fmt_stats(xs: list[float], as_ms: bool = True) -> str:
    if not xs:
        return "(none)"
    scale = 1000.0 if as_ms else 1.0
    xs = sorted(xs)
    n = len(xs)
    p = lambda q: xs[max(0, min(n - 1, int(q * n)))] * scale
    return (f"n={n} mean={sum(xs) / n * scale:.2f} p50={p(0.50):.2f} "
            f"p95={p(0.95):.2f} max={xs[-1] * scale:.2f}")


def _print_speed_check(vps: list[VpState], wall_start: datetime) -> None:
    """For each vp with >=2 emissions, compute observed wall interval / source
    interval and compare to expected 1/speed."""
    ratios = []
    for vp in vps:
        if vp.idx < 2:
            continue
        first_src, _ = vp.trace[0]
        last_src, _ = vp.trace[vp.idx - 1]
        src_span = (last_src - first_src).total_seconds()
        if src_span <= 0:
            continue
        first_wall = wall_start + timedelta(seconds=0.0 / vp.speed)  # ==wall_start
        last_wall = wall_start + timedelta(seconds=src_span / vp.speed)
        wall_span = (last_wall - first_wall).total_seconds()
        # observed_speed = src_span / wall_span; expected = vp.speed
        observed = src_span / wall_span if wall_span > 0 else float("nan")
        ratios.append((vp.speed, observed))
    if not ratios:
        print("  (no vp with >=2 emissions — can't verify speed)")
        return
    speeds = {r[0] for r in ratios}
    for s in sorted(speeds):
        obs = [r[1] for r in ratios if r[0] == s]
        print(f"  configured speed={s}x -> observed speed on {len(obs)} vps: "
              f"min={min(obs):.3f} mean={sum(obs)/len(obs):.3f} max={max(obs):.3f}")


def _print_report(metrics: Metrics, n_vps: int, duration_sec: float, wall_elapsed: float,
                  vps: list[VpState] | None = None, wall_start: datetime | None = None,
                  instrument: bool = False) -> None:
    print()
    print("=" * 72)
    print("REPLAY METRICS")
    print("=" * 72)
    rows_per_min = metrics.inserted / (wall_elapsed / 60.0) if wall_elapsed > 0 else 0.0
    print(f"virtual patients:            {n_vps}")
    print(f"wall elapsed:                {wall_elapsed:.1f} s  ({wall_elapsed/60:.2f} min)")
    print(f"batches:                     {metrics.batches}")
    print(f"attempted inserts:           {metrics.attempted:,}")
    print(f"inserted into vitals:        {metrics.inserted:,}")
    print(f"duplicates suppressed:       {metrics.duplicates_suppressed:,}  (via UNIQUE (vp, event_time) ON CONFLICT DO NOTHING)")
    print(f"dead_letter rows:            {metrics.dead_letter:,}")
    print(f"late arrivals:               {metrics.late:,}")
    print(f"rows/min into vitals:        {rows_per_min:,.1f}")
    print()
    print("Injected faults:")
    for k in ("firmware_offset", "spike", "sensor_dropout", "stuck"):
        v = metrics.injected.get(k, 0)
        print(f"  {k:<20s} {v:,}")

    if instrument:
        print()
        print("Phase timings (ms per tick):")
        print(f"  schedule loop:  {_fmt_stats(metrics.t_schedule)}")
        print(f"  COPY to stg:    {_fmt_stats(metrics.t_copy)}")
        print(f"  INSERT ONCONF:  {_fmt_stats(metrics.t_insert)}")
        print(f"  dead_letter:    {_fmt_stats(metrics.t_dead)}")
        print(f"  sleep:          {_fmt_stats(metrics.t_sleep)}")
        print(f"  batch size:     n={len(metrics.batch_sizes)} "
              f"mean={sum(metrics.batch_sizes) / max(1, len(metrics.batch_sizes)):.1f} "
              f"max={max(metrics.batch_sizes) if metrics.batch_sizes else 0}")
        if vps is not None and wall_start is not None:
            print("\nSpeed-factor check (observed vs configured):")
            _print_speed_check(vps, wall_start)


# --- entrypoint -------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vp-count", type=int, default=None,
                    help="cap on number of vps loaded (default: all in live.virtual_patient)")
    ap.add_argument("--speed", type=float, default=None,
                    help="override per-vp speed_factor for this run (does not persist)")
    ap.add_argument("--duration", type=float, default=60.0, help="wall seconds to run")
    ap.add_argument("--tick", type=float, default=None,
                    help=f"override replayer tick interval in seconds (default {TICK_SEC}s)")
    ap.add_argument("--instrument", action="store_true",
                    help="collect per-tick phase timings and report them at end")
    ap.add_argument("--conninfo", default=os.environ.get("CAREVAULT_PG", CONNINFO_DEFAULT))
    args = ap.parse_args(argv)

    t0 = time.monotonic()
    wall_start_captured = None
    vps_captured: list[VpState] | None = None
    with psycopg.connect(args.conninfo, autocommit=True) as conn:
        vps = _load_vps(conn)
        if args.vp_count is not None:
            vps = vps[: args.vp_count]
        if not vps:
            print("no virtual patients found. Run replay/build_vps.py first.", file=sys.stderr)
            return 2

        speed_note = f" (override {args.speed}x)" if args.speed is not None else ""
        print(f"loaded {len(vps)} vps; running for {args.duration:.0f}s{speed_note}"
              f"{' [INSTRUMENTED]' if args.instrument else ''}",
              flush=True)
        wall_start_captured = datetime.now(timezone.utc)
        metrics = run(conn, vps, args.duration, args.speed, instrument=args.instrument,
                      tick_sec=args.tick)
        vps_captured = vps

    _print_report(metrics, len(vps_captured or []), args.duration, time.monotonic() - t0,
                  vps=vps_captured, wall_start=wall_start_captured, instrument=args.instrument)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
