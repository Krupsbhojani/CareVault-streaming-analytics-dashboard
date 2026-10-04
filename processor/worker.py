"""Sharded stream processor for live.vitals_readings.

Connects as `processor_role` (which lacks SELECT on live.vitals_readings.injected_type,
per sql/schema_live.sql). Reads rows by ingested_at with a 10-second overlap
window, filters via per-vp last_event_time in live.worker_state, runs the
three-layer detector (processor/outliers.py), commits alerts + advanced
worker_state atomically. Wakes on NOTIFY vitals_new; falls back to 2-second
polling.

Sharding:
    --num-shards N  --shard S
Only rows where mod(abs(hashtext(virtual_patient_id)), N) = S are processed by
this worker.

CLI:
    python processor/worker.py --shard 0 --num-shards 1 [--exit-when-idle]
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from processor.outliers import Alert, BatchAnalyzer, Detector, Reading  # noqa: E402
from processor.ml import MlPipeline  # noqa: E402

BATCH_ANALYSIS_INTERVAL_SEC = 15.0  # run cross-vp batch analyzer every N seconds

PROCESSOR_CONNINFO_DEFAULT = os.environ.get(
    "CAREVAULT_PROCESSOR_PG",
    "host=127.0.0.1 port=5433 dbname=carevault user=processor_role password=processor",
)
OVERLAP_SEC     = 10        # re-scan window to catch late arrivals
POLL_TIMEOUT_S  = 2.0       # 2s fallback when no NOTIFY arrives
FETCH_LIMIT     = 5_000     # per fetch cycle
NOTIFY_CHANNEL  = "vitals_new"
# Lexically smallest text — safe default for an unseeded composite cursor,
# so (now, "") < (now, any real vp_id) and the first vp at a given ingested_at
# is reachable on first run.
_SENTINEL_VP    = ""

# Cursor is a composite (last_ingested_at, last_virtual_patient_id). The
# vp_id tiebreaker exists so a chunk of FETCH_LIMIT rows that share an
# ingested_at value can never stall the cursor at the boundary — the fetch
# advances through the chunk vp-by-vp, deterministically, instead of
# repeatedly re-reading the same rows (observed stall on Azure 2026-10-03/04).
Cursor = tuple[datetime, str]

# Columns we actually SELECT. Notably absent: injected_type.
_FETCH_COLS = (
    "virtual_patient_id", "device_id", "event_time", "ingested_at",
    "cgm_mgdl", "heart_rate_bpm", "systolic_bp_mmhg", "diastolic_bp_mmhg",
    "spo2_pct", "sampling_sec", "source_trace_ref",
)

_STOP = False

def _install_signal_handlers() -> None:
    def _stop(_signum, _frame):
        global _STOP
        _STOP = True
    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)


# ---------------- state I/O ----------------

def _worker_id(shard: int, num_shards: int) -> str:
    return f"proc:{shard}/{num_shards}"


def _load_cursor(conn, worker_id: str) -> Cursor:
    """Return the composite (last_ingested_at, last_virtual_patient_id) cursor,
    or a very-old / empty sentinel on first run."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT last_ingested_at, last_virtual_patient_id
              FROM live.worker_cursor WHERE worker_id = %s
        """, (worker_id,))
        row = cur.fetchone()
    if row and row[0] is not None:
        return (row[0], row[1] or _SENTINEL_VP)
    return (datetime(1970, 1, 1, tzinfo=timezone.utc), _SENTINEL_VP)


def _load_vp_state(conn, worker_id: str) -> dict[str, datetime]:
    """Return {vp_id -> last_event_time} for all vps this worker has ever seen."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT virtual_patient_id, last_event_time
              FROM live.worker_state
             WHERE worker_id = %s AND last_event_time IS NOT NULL
        """, (worker_id,))
        return {r[0]: r[1] for r in cur.fetchall()}


def _prewarm_detector(conn, detector: Detector, vp_last_ts: dict[str, datetime]) -> None:
    """Backfill each vp's MAD window from the last N pre-cursor readings so
    the detector doesn't cold-start after a restart."""
    if not vp_last_ts:
        return
    with conn.cursor() as cur:
        for vp_id, last_ts in vp_last_ts.items():
            cur.execute("""
                SELECT cgm_mgdl
                  FROM live.vitals_readings
                 WHERE virtual_patient_id = %s
                   AND event_time <= %s
                   AND cgm_mgdl IS NOT NULL
                 ORDER BY event_time DESC
                 LIMIT 60
            """, (vp_id, last_ts))
            vals = [r[0] for r in cur.fetchall()]
            if vals:
                detector.prewarm(vp_id, reversed(vals))


# ---------------- fetch + process ----------------

def _fetch_batch(conn, shard: int, num_shards: int, cursor: Cursor,
                 vp_last_ts: dict[str, datetime]) -> tuple[list[Reading], Cursor, int]:
    """Read the next batch of rows for this shard, with a 10s overlap window.
    Per-vp dedup happens in Python after the SQL fetch.

    Composite seek: rows beyond the cursor are selected by lexicographic
    (ingested_at, virtual_patient_id) comparison. Rows strictly inside the
    overlap window (ingested_at < cursor_ia AND ingested_at > cursor_ia-10s)
    are also fetched for late-arrival catch-up. ORDER BY matches the composite
    so a next cycle resumes exactly where this one stopped.

    Returns (kept_rows, sql_max_cursor, sql_row_count).
    sql_max_cursor is the max composite seen in the SQL rows (bounded below by
    the current cursor). It is used to advance even when kept_rows is empty
    (all rows were per-vp dupes) — otherwise the cursor stays put and the next
    fetch returns the same rows again, forever.
    """
    cursor_ia, cursor_vp = cursor
    lower = cursor_ia - timedelta(seconds=OVERLAP_SEC)
    q = f"""
        SELECT {', '.join(_FETCH_COLS)}
          FROM live.vitals_readings
         WHERE mod(abs(hashtext(virtual_patient_id)), %s) = %s
           AND ingested_at > %s
           AND (
             ingested_at < %s
             OR (ingested_at, virtual_patient_id) > (%s, %s)
           )
         ORDER BY ingested_at, virtual_patient_id
         LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(q, (num_shards, shard, lower,
                        cursor_ia, cursor_ia, cursor_vp, FETCH_LIMIT))
        rows = cur.fetchall()

    sql_max_ia, sql_max_vp = cursor_ia, cursor_vp
    out: list[Reading] = []
    for row in rows:
        r = Reading(*row)
        key = (r.ingested_at, r.virtual_patient_id)
        if key > (sql_max_ia, sql_max_vp):
            sql_max_ia, sql_max_vp = key
        prev = vp_last_ts.get(r.virtual_patient_id)
        if prev is not None and r.event_time <= prev:
            continue  # already processed per vp_last_ts
        out.append(r)
    # Keep chronological within vp for MAD determinism.
    out.sort(key=lambda r: (r.virtual_patient_id, r.event_time))
    return out, (sql_max_ia, sql_max_vp), len(rows)


def _upsert_1min_rollups(cur, affected_minutes: set[tuple[str, datetime]]) -> None:
    """Recompute + upsert the 1-minute rollups for the (vp, minute) buckets
    that were touched by this micro-batch.

    Why not pg_cron:
      A cron-driven aggregator would run on a fixed schedule (e.g. every 60 s)
      and would either (a) recompute *every* minute in a rolling window
      (wasteful), or (b) track its own high-water mark separate from the
      processor's — a second cursor system with its own failure modes.
      More importantly, cron cannot commit atomically with the alerts that
      concern the same readings; a dashboard querying vitals_1min + alerts
      together could see a minute-bucket that hasn't caught up to its own
      alerts, or vice versa. Doing the upsert in the same transaction as
      the alerts + cursor commit removes both problems.

    Why "recompute from source" rather than incrementing existing counters:
      Late arrivals (~2 % of rows in this replayer, in production probably
      similar order) mean a minute-bucket can be re-touched after it was
      already aggregated. An additive update would either double-count the
      earlier rows or require tracking which specific rows were previously
      contributed. Re-scanning live.vitals_readings for the affected
      (vp, minute) buckets is idempotent and self-correcting.
    """
    if not affected_minutes:
        return
    keys = list(affected_minutes)
    values_ph = ",".join(["(%s, %s::timestamptz)"] * len(keys))
    params: list = []
    for vp, mstart in keys:
        params.extend([vp, mstart])
    sql = f"""
        INSERT INTO live.vitals_1min (
            virtual_patient_id, minute_start_utc,
            cgm_mgdl_mean, cgm_mgdl_min, cgm_mgdl_max,
            heart_rate_bpm_mean, systolic_bp_mmhg_mean, diastolic_bp_mmhg_mean,
            spo2_pct_mean, n_readings
        )
        SELECT
            v.virtual_patient_id,
            date_trunc('minute', v.event_time)                 AS m,
            avg(v.cgm_mgdl)::real,
            min(v.cgm_mgdl)::real,
            max(v.cgm_mgdl)::real,
            avg(v.heart_rate_bpm)::real,
            avg(v.systolic_bp_mmhg)::real,
            avg(v.diastolic_bp_mmhg)::real,
            avg(v.spo2_pct)::real,
            count(*)::int
          FROM live.vitals_readings v
          INNER JOIN (VALUES {values_ph}) AS k(vp, m_utc)
            ON k.vp    = v.virtual_patient_id
           AND k.m_utc = date_trunc('minute', v.event_time)
         GROUP BY v.virtual_patient_id, date_trunc('minute', v.event_time)
        ON CONFLICT (virtual_patient_id, minute_start_utc) DO UPDATE
           SET cgm_mgdl_mean          = EXCLUDED.cgm_mgdl_mean,
               cgm_mgdl_min           = EXCLUDED.cgm_mgdl_min,
               cgm_mgdl_max           = EXCLUDED.cgm_mgdl_max,
               heart_rate_bpm_mean    = EXCLUDED.heart_rate_bpm_mean,
               systolic_bp_mmhg_mean  = EXCLUDED.systolic_bp_mmhg_mean,
               diastolic_bp_mmhg_mean = EXCLUDED.diastolic_bp_mmhg_mean,
               spo2_pct_mean          = EXCLUDED.spo2_pct_mean,
               n_readings             = EXCLUDED.n_readings
    """
    cur.execute(sql, params)


def _commit_batch(conn, worker_id: str, alerts: list[Alert],
                  vp_advance: dict[str, datetime], new_cursor: Cursor,
                  affected_minutes: set[tuple[str, datetime]] | None = None,
                  prediction_inserts: list[tuple] | None = None,
                  prediction_resolutions: list[tuple] | None = None) -> None:
    """Insert alerts + upsert worker_state + upsert worker_cursor + upsert
    vitals_1min rollups + insert/update live.predictions — all in ONE
    transaction."""
    with conn.transaction():
        with conn.cursor() as cur:
            if alerts:
                params = [
                    (a.virtual_patient_id, a.trigger_event_time, a.alert_type,
                     a.severity, a.rule_id, a.detector, a.score, json.dumps(a.payload))
                    for a in alerts
                ]
                cur.executemany("""
                    INSERT INTO live.alerts
                        (virtual_patient_id, trigger_event_time, alert_type, severity,
                         rule_id, detector, score, payload_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """, params)
            if vp_advance:
                cur.executemany("""
                    INSERT INTO live.worker_state (worker_id, virtual_patient_id, last_event_time)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (worker_id, virtual_patient_id) DO UPDATE
                       SET last_event_time = GREATEST(worker_state.last_event_time, EXCLUDED.last_event_time),
                           updated_at      = now()
                """, [(worker_id, vp, ts) for vp, ts in vp_advance.items()])
            # Row-wise comparison in the WHERE prevents cursor regression if
            # the current row is already past the one we're trying to write.
            cur.execute("""
                INSERT INTO live.worker_cursor
                    (worker_id, last_ingested_at, last_virtual_patient_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (worker_id) DO UPDATE
                   SET last_ingested_at        = EXCLUDED.last_ingested_at,
                       last_virtual_patient_id = EXCLUDED.last_virtual_patient_id,
                       updated_at              = now()
                 WHERE (EXCLUDED.last_ingested_at, EXCLUDED.last_virtual_patient_id)
                     > (worker_cursor.last_ingested_at, worker_cursor.last_virtual_patient_id)
            """, (worker_id, new_cursor[0], new_cursor[1]))
            _upsert_1min_rollups(cur, affected_minutes or set())
            _write_predictions(cur, prediction_inserts, prediction_resolutions)


def _write_predictions(cur, inserts: list[tuple] | None,
                       resolutions: list[tuple] | None) -> None:
    """Prediction inserts and delayed-label resolutions, both inside the
    caller's transaction so they atomically land with the alerts and cursor
    that pertain to the same reading. Idempotent via
    UNIQUE (virtual_patient_id, input_window_end, model_name)."""
    if inserts:
        cur.executemany("""
            INSERT INTO live.predictions
                (virtual_patient_id, model_name, model_version,
                 input_window_end, horizon_seconds, predicted_ts_utc,
                 predicted_cgm_mgdl, latency_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (virtual_patient_id, input_window_end, model_name)
            DO NOTHING
        """, inserts)
    if resolutions:
        cur.executemany("""
            UPDATE live.predictions
               SET actual_cgm_mgdl = %s,
                   abs_error_mgdl  = %s,
                   resolved_at     = now()
             WHERE virtual_patient_id = %s
               AND input_window_end   = %s
               AND model_name         = %s
               AND actual_cgm_mgdl IS NULL
        """, resolutions)


# ---------------- main loop ----------------

def _wait_for_notify_or_timeout(conn, timeout: float) -> bool:
    """Block up to timeout seconds for a NOTIFY on the LISTEN channel.
    Returns True if a notification arrived, False on timeout."""
    try:
        for _ in conn.notifies(timeout=timeout, stop_after=1):
            return True
    except Exception:
        pass
    return False


def _drain_pending_notifies(conn) -> int:
    """Non-blocking: read any queued notifications without waiting. Returns count."""
    try:
        n = 0
        for _ in conn.notifies(timeout=0.0):
            n += 1
        return n
    except Exception:
        return 0


def process_one_cycle(conn, detector: Detector, shard: int, num_shards: int,
                      cursor: Cursor, vp_last_ts: dict[str, datetime],
                      ml: MlPipeline | None = None
                      ) -> tuple[int, int, Cursor, int]:
    """One fetch+process+commit cycle.
    Returns (rows_processed, alerts_emitted, new_cursor, sql_rows_fetched).
    ML predictions (when `ml` is provided) are written inside the same
    transaction as alerts + cursor.
    """
    worker_id = _worker_id(shard, num_shards)
    rows, sql_max_cursor, sql_count = _fetch_batch(conn, shard, num_shards, cursor, vp_last_ts)
    if sql_count == 0:
        return (0, 0, cursor, 0)

    # Loud stall signature: SQL filled the LIMIT but every row was per-vp
    # dedup'd. With the composite cursor the next cycle will advance via
    # sql_max_cursor; without the composite key (the Azure incident) it
    # couldn't. Log noisily so the pattern is never silent again.
    if sql_count >= FETCH_LIMIT and not rows:
        print(f"[worker] WARNING: fetched FETCH_LIMIT={FETCH_LIMIT} rows and "
              f"kept 0 after vp_last_ts dedup. cursor_before={cursor}, "
              f"sql_max={sql_max_cursor}, vp_last_ts_size={len(vp_last_ts)}. "
              f"Composite cursor should advance; if this persists, the fetch "
              f"is pinned at a boundary — check live.worker_cursor.",
              file=sys.stderr, flush=True)

    alerts: list[Alert] = []
    vp_advance: dict[str, datetime] = {}
    affected_minutes: set[tuple[str, datetime]] = set()

    # Detector runs first so its per-reading alerts can drive the ML pipeline's
    # untrustworthy-reading exclusion. We collect alerts per-reading (not just
    # a flat list) so ML sees only alerts that apply to the current reading.
    for r in rows:
        this_reading_alerts = detector.process(r)
        alerts.extend(this_reading_alerts)
        prev = vp_advance.get(r.virtual_patient_id)
        if prev is None or r.event_time > prev:
            vp_advance[r.virtual_patient_id] = r.event_time
        minute_start = r.event_time.replace(second=0, microsecond=0)
        affected_minutes.add((r.virtual_patient_id, minute_start))
        # ML pipeline gets the reading + the set of rule_ids the detector
        # produced for it THIS CYCLE. This is how the ML layer decides whether
        # to trust the reading as a feature/label — without ever reading
        # injected_type (its role wouldn't allow that anyway).
        if ml is not None:
            rule_ids = {a.rule_id for a in this_reading_alerts if a.virtual_patient_id == r.virtual_patient_id
                        and a.trigger_event_time == r.event_time}
            ml.process_reading(r, rule_ids)

    # Clamp future ingested_at so a single late-arrival row with
    # ingested_at > wall-clock can't strand real-time rows behind it.
    now = datetime.now(timezone.utc)
    new_ia, new_vp = sql_max_cursor
    if new_ia > now:
        new_ia, new_vp = now, _SENTINEL_VP
    new_cursor: Cursor = (new_ia, new_vp)

    ml_inserts, ml_resolutions = ([], [])
    if ml is not None:
        ml_inserts, ml_resolutions = ml.drain_writes()

    _commit_batch(conn, worker_id, alerts, vp_advance, new_cursor,
                  affected_minutes=affected_minutes,
                  prediction_inserts=ml_inserts,
                  prediction_resolutions=ml_resolutions)
    for vp, ts in vp_advance.items():
        vp_last_ts[vp] = ts
    return (len(rows), len(alerts), new_cursor, sql_count)


def _load_ml_contexts(conn, ml: MlPipeline) -> int:
    """Populate ML per-vp static context (source_tz, sampling_sec, diabetes_type,
    speed_factor) from live.virtual_patient JOIN raw.readings_raw."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT vp.virtual_patient_id, r.source_tz, r.sampling_sec,
                   r.diabetes_type, vp.speed_factor
              FROM live.virtual_patient vp
              JOIN LATERAL (
                  SELECT source_tz, sampling_sec, diabetes_type
                    FROM raw.readings_raw
                   WHERE patient_id = vp.source_patient_id
                   LIMIT 1
              ) r ON true
        """)
        n = 0
        for vp_id, tz, sec, dtype, speed in cur.fetchall():
            ml.set_context(vp_id,
                           source_tz=tz or "UTC",
                           sampling_sec=int(sec) if sec else 300,
                           diabetes_type=dtype or "T1",
                           speed_factor=float(speed) if speed else 1.0)
            n += 1
    return n


def run(conninfo: str, shard: int, num_shards: int,
        exit_when_idle: bool = False, max_cycles: int | None = None,
        enable_ml: bool = True) -> dict:
    """Long-running loop. If exit_when_idle, exit after one empty cycle
    (useful for backfill + tests). ML is on by default; disable with
    enable_ml=False (e.g. baseline runs)."""
    _install_signal_handlers()
    stats = {"cycles": 0, "rows": 0, "alerts": 0,
             "ml_predictions": 0, "ml_resolved": 0,
             "ml_evicted_queue_cap": 0, "ml_evicted_label_too_late": 0,
             "ml_evicted_label_untrustworthy": 0, "ml_still_pending_at_end": 0,
             "ml_excluded": 0}
    worker_id = _worker_id(shard, num_shards)

    log_startup = os.environ.get("PROCESSOR_STARTUP_LOG")
    def _log_startup(msg: str) -> None:
        if log_startup:
            with open(log_startup, "a", encoding="utf-8") as f:
                f.write(f"{time.perf_counter():.3f}\t{msg}\n")

    _log_startup("run_start")
    # Two connections: `conn` handles fetches, commits, state updates; `listen_conn`
    # is dedicated to LISTEN/NOTIFY. Sharing one connection caused snapshot
    # isolation to leak across cycles — tight-loop fetches kept seeing the same
    # stale snapshot even after commits from other backends had landed.
    with psycopg.connect(conninfo, autocommit=True) as conn, \
         psycopg.connect(conninfo, autocommit=True) as listen_conn:
        _log_startup("connect_done")
        with listen_conn.cursor() as cur:
            cur.execute(f"LISTEN {NOTIFY_CHANNEL}")
        _log_startup("listen_done")

        cursor = _load_cursor(conn, worker_id)
        _log_startup("load_cursor_done")
        vp_last_ts = _load_vp_state(conn, worker_id)
        _log_startup(f"load_vp_state_done vps={len(vp_last_ts)}")
        detector = Detector()
        _prewarm_detector(conn, detector, vp_last_ts)
        _log_startup("prewarm_done")
        batch_analyzer = BatchAnalyzer()
        last_batch_analysis = time.monotonic()

        ml = None
        if enable_ml:
            ml = MlPipeline()
            n_ml = _load_ml_contexts(conn, ml)
            _log_startup(f"ml_ready contexts={n_ml}")

        cycles_since_progress = 0
        # Structured log to file (env FETCH_LOG=/path). One line per cycle, always.
        log_path = os.environ.get("PROCESSOR_FETCH_LOG")
        log_file = open(log_path, "w", buffering=1, encoding="utf-8") if log_path else None
        if log_file:
            log_file.write("cycle\tcursor_before\tsql_count\tkept\talerts\tsql_max_ingested\tcursor_after\tvp_state_size\tnotifies\ttook_ms\n")
        try:
            while not _STOP:
                if max_cycles is not None and stats["cycles"] >= max_cycles:
                    break
                # Drain any queued notifications on the listen connection (non-blocking).
                notifies_drained = _drain_pending_notifies(listen_conn)
                t0 = time.monotonic()
                cursor_before = cursor
                n_rows, n_alerts, cursor, sql_count = process_one_cycle(
                    conn, detector, shard, num_shards, cursor, vp_last_ts, ml=ml
                )
                dt = time.monotonic() - t0
                if log_file:
                    cb_ia, cb_vp = cursor_before
                    ca_ia, ca_vp = cursor
                    log_file.write(f"{stats['cycles']}\t{cb_ia.isoformat()}|{cb_vp}\t"
                                   f"{sql_count}\t{n_rows}\t{n_alerts}\t"
                                   f"{ca_ia.isoformat()}|{ca_vp}\t"
                                   f"{ca_ia.isoformat()}|{ca_vp}\t"
                                   f"{len(vp_last_ts)}\t{notifies_drained}\t{dt*1000:.1f}\n")
                stats["cycles"] += 1
                stats["rows"] += n_rows
                stats["alerts"] += n_alerts

                # Layer 4: batch analysis. Runs at interval, not per row.
                if time.monotonic() - last_batch_analysis >= BATCH_ANALYSIS_INTERVAL_SEC:
                    try:
                        batch_alerts = batch_analyzer.analyze(conn)
                        if batch_alerts:
                            _commit_batch(conn, worker_id, batch_alerts, {}, cursor)
                            stats["alerts"] += len(batch_alerts)
                    except Exception as e:
                        # Don't kill the main loop over an analyzer failure.
                        print(f"[worker] batch_analyzer error: {e}", file=sys.stderr, flush=True)
                    last_batch_analysis = time.monotonic()
                # Wait for NOTIFY only when the SQL fetch found nothing (idle).
                # When SQL returned rows but all were Python-filtered dupes, keep
                # draining immediately — the cursor advanced past them.
                if sql_count == 0:
                    cycles_since_progress += 1
                    if exit_when_idle and cycles_since_progress >= 2:
                        break
                    _wait_for_notify_or_timeout(listen_conn, POLL_TIMEOUT_S)
                else:
                    cycles_since_progress = 0
        finally:
            if log_file:
                log_file.close()
    if ml is not None:
        stats["ml_predictions"]                 = ml.n_predictions_made_total
        stats["ml_resolved"]                    = ml.n_resolved_total
        stats["ml_evicted_queue_cap"]           = ml.n_evicted_queue_cap_total
        stats["ml_evicted_label_too_late"]      = ml.n_evicted_label_too_late_total
        stats["ml_evicted_label_untrustworthy"] = ml.n_evicted_label_untrustworthy_total
        stats["ml_still_pending_at_end"]        = ml.count_still_pending()
        stats["ml_excluded"]                    = ml.n_excluded_untrustworthy_total
    return stats


# ---------------- CLI ----------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--num-shards", type=int, default=1)
    ap.add_argument("--exit-when-idle", action="store_true",
                    help="exit after two consecutive empty poll cycles (for backfill/tests)")
    ap.add_argument("--max-cycles", type=int, default=None,
                    help="hard cap on cycles (for tests)")
    ap.add_argument("--no-ml", action="store_true",
                    help="disable the online ML pipeline (baseline runs)")
    ap.add_argument("--conninfo", default=PROCESSOR_CONNINFO_DEFAULT,
                    help=f"default: {PROCESSOR_CONNINFO_DEFAULT}")
    args = ap.parse_args(argv)

    print(f"[worker {args.shard}/{args.num_shards}] starting"
          f"{' [ML=off]' if args.no_ml else ''}", flush=True)
    t0 = time.monotonic()
    stats = run(args.conninfo, args.shard, args.num_shards,
                enable_ml=not args.no_ml,
                exit_when_idle=args.exit_when_idle, max_cycles=args.max_cycles)
    elapsed = time.monotonic() - t0
    rps = stats["rows"] / elapsed if elapsed > 0 else 0
    print(f"[worker {args.shard}/{args.num_shards}] cycles={stats['cycles']} "
          f"rows={stats['rows']} alerts={stats['alerts']} "
          f"elapsed={elapsed:.1f}s rows/s={rps:.1f}", flush=True)
    if stats.get('ml_predictions'):
        print(f"[worker] ML: preds={stats['ml_predictions']:,} "
              f"resolved={stats['ml_resolved']:,} "
              f"evicted(queue_cap)={stats['ml_evicted_queue_cap']:,} "
              f"evicted(label_too_late)={stats['ml_evicted_label_too_late']:,} "
              f"evicted(label_untrustworthy)={stats['ml_evicted_label_untrustworthy']:,} "
              f"still_pending_at_end={stats['ml_still_pending_at_end']:,} "
              f"excluded_untrustworthy={stats['ml_excluded']:,}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
