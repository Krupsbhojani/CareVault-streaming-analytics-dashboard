"""Three invariants required by the plan:

  test_no_injected_type_access  — processor_role must be blocked from reading
                                   the injected_type column (ground truth).
  test_mad_math                 — modified-z-score matches hand-computed values
                                   on a known input.
  test_restart_safety           — kill the worker mid-batch, restart, and every
                                   reading gets processed exactly once (no
                                   duplicate alerts, cursor advances past all
                                   readings).
"""
from __future__ import annotations

import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loader.load_raw import CONNINFO_DEFAULT  # admin conninfo  # noqa: E402
from processor.outliers import (  # noqa: E402
    CLAMP_VALUES, Detector, MAD_WARMUP, MAD_Z_THRESHOLD, MetricWindow, Reading,
)
from processor.worker import (  # noqa: E402
    PROCESSOR_CONNINFO_DEFAULT, _worker_id, process_one_cycle, run as worker_run,
)
from processor import worker as worker_mod  # noqa: E402  — for monkeypatching FETCH_LIMIT


ADMIN_CONNINFO      = os.environ.get("CAREVAULT_PG", CONNINFO_DEFAULT)
PROC_CONNINFO       = os.environ.get("CAREVAULT_PROCESSOR_PG", PROCESSOR_CONNINFO_DEFAULT)
DASHBOARD_CONNINFO  = os.environ.get(
    "CAREVAULT_DASHBOARD_PG",
    "host=127.0.0.1 port=5433 dbname=carevault user=dashboard_ro password=dashboard",
)


@pytest.fixture(scope="module")
def admin_conn():
    import psycopg
    try:
        conn = psycopg.connect(ADMIN_CONNINFO, connect_timeout=3, autocommit=True)
    except Exception as e:
        pytest.skip(f"Postgres unreachable (admin): {e}")
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def proc_conn():
    import psycopg
    try:
        conn = psycopg.connect(PROC_CONNINFO, connect_timeout=3, autocommit=True)
    except Exception as e:
        pytest.skip(f"Postgres unreachable (processor_role): {e}")
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def dashboard_conn():
    import psycopg
    try:
        conn = psycopg.connect(DASHBOARD_CONNINFO, connect_timeout=3, autocommit=True)
    except Exception as e:
        pytest.skip(f"Postgres unreachable (dashboard_ro): {e}")
    yield conn
    conn.close()


# ============================================================================
# Test 1 — processor_role must not have SELECT on injected_type
# ============================================================================

def test_no_injected_type_access(proc_conn):
    """processor_role's column-level grants exclude injected_type. Trying to
    read it must fail with insufficient_privilege (SQLSTATE 42501)."""
    import psycopg
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with proc_conn.cursor() as cur:
            cur.execute("SELECT injected_type FROM live.vitals_readings LIMIT 1")
            cur.fetchone()
    # Sanity: other columns MUST work (i.e. it's not blanket denial).
    with proc_conn.cursor() as cur:
        cur.execute("SELECT virtual_patient_id, cgm_mgdl FROM live.vitals_readings LIMIT 1")
        cur.fetchone()  # may be None if table empty — that's fine, no exception


def test_no_injected_type_access_dashboard(dashboard_conn):
    """dashboard_ro must be blocked from injected_type (same guarantee as
    processor_role) so the operator dashboard cannot surface ground truth."""
    import psycopg
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with dashboard_conn.cursor() as cur:
            cur.execute("SELECT injected_type FROM live.vitals_readings LIMIT 1")
            cur.fetchone()
    # Sanity: the whitelisted columns + companion tables must be readable.
    with dashboard_conn.cursor() as cur:
        cur.execute("SELECT virtual_patient_id, cgm_mgdl FROM live.vitals_readings LIMIT 1")
        cur.fetchone()
        cur.execute("SELECT count(*) FROM live.alerts")
        cur.fetchone()
        cur.execute("SELECT count(*) FROM live.device")
        cur.fetchone()
        cur.execute("SELECT count(*) FROM live.virtual_patient")
        cur.fetchone()
        cur.execute("SELECT count(*) FROM live.vitals_1min")
        cur.fetchone()


# ============================================================================
# Test 2 — MAD modified z-score matches hand computation on known input
# ============================================================================

def test_mad_math_known_input():
    """
    Reference computation, hand-verified:
      window = [100, 101, 99, 102, 98, 100, 101, 99, 102, 98,
                100, 101, 99, 102, 98, 100, 101, 99, 102, 98]   # 20 values
    then process value = 200 (a clear spike).
        median(window) = 100
        deviations     = [0,1,1,2,2,0,1,1,2,2,0,1,1,2,2,0,1,1,2,2]
        MAD            = median(deviations) = 1
        modified z     = 0.6745 * (200 - 100) / 1 = 67.45
    """
    d = Detector()
    vp = "vp:test"
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    # 20 near-100 values to complete warm-up.
    baseline = [100, 101, 99, 102, 98, 100, 101, 99, 102, 98,
                100, 101, 99, 102, 98, 100, 101, 99, 102, 98]
    for i, v in enumerate(baseline):
        r = Reading(vp, None, now + timedelta(minutes=i), now + timedelta(minutes=i),
                    v, 70, 118, 76, 97.0, 300, None)
        alerts = d.process(r)
        # In warm-up + immediately after, no MAD alert should fire from these
        # tight-cluster values.
        assert not any(a.detector == "mad" for a in alerts), f"unexpected MAD alert on warmup value {v}"

    # Now the spike.
    spike = Reading(vp, None, now + timedelta(minutes=100), now + timedelta(minutes=100),
                    200, 70, 118, 76, 97.0, 300, None)
    alerts = d.process(spike)
    mad_alerts = [a for a in alerts if a.detector == "mad"]
    assert len(mad_alerts) == 1, f"expected 1 MAD alert on spike, got {len(mad_alerts)}"
    z = mad_alerts[0].score
    expected_z = 0.6745 * (200 - 100) / 1.0  # 67.45
    assert abs(z - expected_z) < 1e-6, f"z={z}, expected {expected_z}"
    assert z > MAD_Z_THRESHOLD

    # And the spike must NOT enter the window (excluded flagged values).
    win = d.state(vp).m("cgm_mgdl").window
    assert 200 not in win, "flagged value must be excluded from the MAD window"

    # A subsequent 100 (baseline) should NOT be flagged.
    normal = Reading(vp, None, now + timedelta(minutes=101), now + timedelta(minutes=101),
                     100, 70, 118, 76, 97.0, 300, None)
    alerts = d.process(normal)
    assert not any(a.detector == "mad" for a in alerts), "baseline value should not trigger MAD"


def test_mad_clamp_values_excluded():
    """Clamp values (40, 400, 39.6) must NOT enter the MAD window and must
    NOT be flagged as MAD outliers (they are sensor censoring, not spikes)."""
    d = Detector()
    vp = "vp:test_clamp"
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Warm-up around 100.
    for i in range(MAD_WARMUP + 5):
        r = Reading(vp, None, now + timedelta(minutes=i), now + timedelta(minutes=i),
                    100 + (i % 3 - 1), 70, 118, 76, 97.0, 300, None)
        d.process(r)
    # Now feed clamp value 40. Should produce a clinical hypo (rules), not MAD.
    r = Reading(vp, None, now + timedelta(minutes=999), now + timedelta(minutes=999),
                40.0, 70, 118, 76, 97.0, 300, None)
    alerts = d.process(r)
    assert not any(a.detector == "mad" for a in alerts), "clamp value must not trigger MAD"
    assert any(a.alert_type == "hypo" for a in alerts), "clamp 40 mg/dL is severe hypo, should alert"
    win = d.state(vp).m("cgm_mgdl").window
    assert 40.0 not in win, "clamp value must not enter MAD window"


# ============================================================================
# Test 3 — restart safety: kill mid-batch, restart, exactly-once processing
# ============================================================================

@pytest.fixture
def clean_live_tables(admin_conn, monkeypatch):
    """Isolate this test by deleting only test-vp rows / test-worker state.
    Do NOT truncate the full live.vitals_readings table — an evaluation dataset
    may be sitting there (30-min replay for scoring), and destroying it just
    to run a unit test wastes 10+ minutes of setup.

    Pin the worker_id to a test-only value so a concurrently-running
    processor (local or azure, both default to proc:0/1) cannot race on
    our cursor row and advance it past the test's inserted ingested_at.
    """
    monkeypatch.setattr(worker_mod, "_worker_id",
                        lambda shard, num_shards: f"proc:test_restart/{shard}_{num_shards}")
    test_worker_id = worker_mod._worker_id(0, 1)
    with admin_conn.cursor() as cur:
        cur.execute("DELETE FROM live.vitals_readings WHERE virtual_patient_id LIKE 'vp:test%'")
        cur.execute("DELETE FROM live.alerts          WHERE virtual_patient_id LIKE 'vp:test%'")
        cur.execute("DELETE FROM live.worker_state    WHERE worker_id = %s", (test_worker_id,))
        cur.execute("DELETE FROM live.worker_cursor   WHERE worker_id = %s", (test_worker_id,))
    yield test_worker_id
    # Teardown: don't leave the test cursor around.
    with admin_conn.cursor() as cur:
        cur.execute("DELETE FROM live.worker_state  WHERE worker_id = %s", (test_worker_id,))
        cur.execute("DELETE FROM live.worker_cursor WHERE worker_id = %s", (test_worker_id,))


def _insert_readings(admin_conn, rows: list[tuple]) -> None:
    """Insert (vp_id, event_time, cgm) test readings via the admin role."""
    with admin_conn.cursor() as cur:
        for vp, ts, cgm in rows:
            cur.execute("""
                INSERT INTO live.vitals_readings
                    (virtual_patient_id, device_id, event_time, ingested_at, cgm_mgdl,
                     heart_rate_bpm, systolic_bp_mmhg, diastolic_bp_mmhg, spo2_pct,
                     injected_type, sampling_sec, source_trace_ref)
                VALUES (%s, %s, %s, %s, %s, 70, 118, 76, 97.0, NULL, 300, 'test:seed')
                ON CONFLICT DO NOTHING
            """, (vp, f"{vp}/cgm", ts, ts, cgm))


def test_restart_safety_exactly_once(admin_conn, proc_conn, clean_live_tables):
    """
    Setup: 6 readings across 2 vps. Cadence 5 minutes apart in event_time.
    First run: --max-cycles 1 --exit-when-idle=False so worker processes one
      cycle (which sees all 6 rows in one fetch), commits, and exits.
    Simulate mid-batch crash BEFORE processing: manually re-set the cursor to
      before all rows, then re-run and confirm no duplicate alerts on the
      already-processed rows (i.e., per-vp last_event_time gates them).
    """
    base = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        ("vp:test_a", base + timedelta(minutes=0), 100),
        ("vp:test_a", base + timedelta(minutes=5), 60),   # clinical hypo -> alert
        ("vp:test_a", base + timedelta(minutes=10), 110),
        ("vp:test_b", base + timedelta(minutes=0), 260),  # clinical hyper -> alert
        ("vp:test_b", base + timedelta(minutes=5), 130),
        ("vp:test_b", base + timedelta(minutes=10), 55),  # clinical hypo -> alert
    ]
    _insert_readings(admin_conn, rows)
    test_worker_id = clean_live_tables  # fixture now returns the monkeypatched worker_id

    # --- Run 1: one cycle. ---
    # NB: stats1["rows"] counts ALL rows the worker kept (not just test-vp
    # rows), so we don't assert on it when the DB has other data. Instead
    # verify the test rows were processed by checking test-scoped state.
    worker_run(PROC_CONNINFO, shard=0, num_shards=1, exit_when_idle=True, max_cycles=50)

    # Snapshot alerts (test-scoped) + worker state after run 1.
    with admin_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM live.alerts WHERE virtual_patient_id LIKE 'vp:test%'")
        alerts_after_run1 = cur.fetchone()[0]
        cur.execute("""
            SELECT virtual_patient_id, last_event_time
              FROM live.worker_state
             WHERE worker_id = %s AND virtual_patient_id LIKE 'vp:test%%'
             ORDER BY 1
        """, (test_worker_id,))
        ws_after_run1 = dict(cur.fetchall())

    # Verify per-vp cursor advanced past every test reading.
    assert ws_after_run1.get("vp:test_a") == rows[2][1], \
        f"vp:test_a cursor at wrong event_time: {ws_after_run1.get('vp:test_a')}"
    assert ws_after_run1.get("vp:test_b") == rows[5][1]
    assert alerts_after_run1 >= 3, \
        f"expected at least 3 clinical alerts on test vps, got {alerts_after_run1}"

    # --- Simulate a crash-then-restart with a stale ingested_at cursor. ---
    # Rewinding the cursor forces re-fetch of the test rows; per-vp
    # last_event_time must gate them (no duplicate alerts).
    old_cursor = rows[0][1] - timedelta(minutes=1)
    with admin_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO live.worker_cursor
                (worker_id, last_ingested_at, last_virtual_patient_id)
            VALUES (%s, %s, '')
            ON CONFLICT (worker_id) DO UPDATE
               SET last_ingested_at        = EXCLUDED.last_ingested_at,
                   last_virtual_patient_id = EXCLUDED.last_virtual_patient_id
        """, (test_worker_id, old_cursor))

    # --- Run 2: drain until idle. Rows get re-fetched, Python-filter drops dups. ---
    worker_run(PROC_CONNINFO, shard=0, num_shards=1, exit_when_idle=True, max_cycles=50)

    with admin_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM live.alerts WHERE virtual_patient_id LIKE 'vp:test%'")
        alerts_after_run2 = cur.fetchone()[0]
        # Duplicate detection: no (vp, trigger_event_time, rule_id) should appear
        # twice FOR TEST VPs. Pre-existing duplicates on real vps from prior
        # eval runs are out of scope for this test.
        cur.execute("""
            SELECT virtual_patient_id, trigger_event_time, rule_id, count(*) AS n
              FROM live.alerts
             WHERE virtual_patient_id LIKE 'vp:test%'
             GROUP BY 1, 2, 3
            HAVING count(*) > 1
        """)
        dups = cur.fetchall()

    assert not dups, f"duplicate alerts after restart: {dups}"
    assert alerts_after_run2 == alerts_after_run1, (
        f"test-vp alert count changed after restart replay: "
        f"{alerts_after_run1} -> {alerts_after_run2}"
    )

    # Reconciliation: every test-vp reading has event_time <=
    # worker_state.last_event_time. Scoped to test vps because other replays
    # could have left data this test's worker didn't bother to catch up on.
    with admin_conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FROM live.vitals_readings v
             WHERE v.virtual_patient_id LIKE 'vp:test%%'
               AND NOT EXISTS (
                 SELECT 1 FROM live.worker_state w
                  WHERE w.worker_id = %s
                    AND w.virtual_patient_id = v.virtual_patient_id
                    AND w.last_event_time >= v.event_time
             )
        """, (test_worker_id,))
        unprocessed = cur.fetchone()[0]
    assert unprocessed == 0, f"{unprocessed} test readings not covered by worker_state cursor"


# ============================================================================
# Test 4 — cursor must not stall when > FETCH_LIMIT rows share an ingested_at
# ============================================================================
# Reproduces the Azure 2026-10-03/04 incident. Before the composite-key fix,
# a chunk of FETCH_LIMIT rows at a single ingested_at could deadlock: SQL
# returned the chunk, per-vp dedup filtered everything, sql_max_ingested
# equalled the current cursor, GREATEST(cursor, cursor) = cursor, next cycle
# re-fetched the same chunk, forever. With the composite (ingested_at,
# virtual_patient_id) seek, sql_max_cursor advances past the tied group.

def test_cursor_advances_past_tied_ingested_at(admin_conn, proc_conn, monkeypatch):
    """When > FETCH_LIMIT rows share an ingested_at and all are per-vp dupes,
    the cursor must still make progress on the next fetch."""
    # Shrink FETCH_LIMIT so a handful of rows is enough to reproduce the
    # condition. The underlying mechanism (seek pagination at a boundary of
    # tied ingested_at values) is scale-invariant.
    monkeypatch.setattr(worker_mod, "FETCH_LIMIT", 5)

    # Unique worker_id so no concurrently-running proc:0/1 worker (if any)
    # can race on this cursor row.
    test_worker = "proc:stall_test/1"
    test_vps = [f"vp:test_stall_{c}" for c in "bcdefg"]  # 6 vps = FETCH_LIMIT+1
    tied_ia  = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    future_ia = tied_ia + timedelta(microseconds=723)   # exactly the Azure pattern
    future_vp = "vp:test_stall_zz_future"
    cursor_vp_pin = "vp:test_stall_a"  # lexically before all test_vps

    def _cleanup(cur):
        for vp in test_vps + [future_vp]:
            cur.execute("DELETE FROM live.vitals_readings WHERE virtual_patient_id = %s", (vp,))
            cur.execute("DELETE FROM live.alerts          WHERE virtual_patient_id = %s", (vp,))
        cur.execute("DELETE FROM live.worker_state  WHERE worker_id = %s", (test_worker,))
        cur.execute("DELETE FROM live.worker_cursor WHERE worker_id = %s", (test_worker,))

    with admin_conn.cursor() as cur:
        _cleanup(cur)
        # 6 rows at the same ingested_at, 6 different vps lexically > cursor_vp_pin
        for vp in test_vps:
            cur.execute("""
                INSERT INTO live.vitals_readings
                    (virtual_patient_id, device_id, event_time, ingested_at, cgm_mgdl,
                     heart_rate_bpm, systolic_bp_mmhg, diastolic_bp_mmhg, spo2_pct,
                     injected_type, sampling_sec, source_trace_ref)
                VALUES (%s, NULL, %s, %s, 110, 70, 118, 76, 97.0, NULL, 300, 'test')
            """, (vp, tied_ia, tied_ia))
        # One row microseconds past the tie — the one the OLD code could not reach.
        cur.execute("""
            INSERT INTO live.vitals_readings
                (virtual_patient_id, device_id, event_time, ingested_at, cgm_mgdl,
                 heart_rate_bpm, systolic_bp_mmhg, diastolic_bp_mmhg, spo2_pct,
                 injected_type, sampling_sec, source_trace_ref)
            VALUES (%s, NULL, %s, %s, 110, 70, 118, 76, 97.0, NULL, 300, 'test')
        """, (future_vp, future_ia, future_ia))
        # Seed per-vp dedup so every one of the 6 tied rows gets dropped by
        # the Python filter (reproducing the Azure stall condition exactly).
        for vp in test_vps:
            cur.execute("""
                INSERT INTO live.worker_state (worker_id, virtual_patient_id, last_event_time)
                VALUES (%s, %s, %s)
            """, (test_worker, vp, tied_ia))
        # Pin the cursor at the stall boundary. (ingested_at, vp) comparison
        # with cursor=(tied_ia, cursor_vp_pin) selects the 6 tied rows (b..g)
        # plus the future row — but LIMIT=5 cuts off before the future row,
        # so SQL returns exactly the 5 tied rows b..f. Those all get filtered
        # by the per-vp dedup → 0 kept. This is the Azure situation.
        cur.execute("""
            INSERT INTO live.worker_cursor
                (worker_id, last_ingested_at, last_virtual_patient_id)
            VALUES (%s, %s, %s)
        """, (test_worker, tied_ia, cursor_vp_pin))

    try:
        cursor0 = worker_mod._load_cursor(proc_conn, test_worker)
        assert cursor0 == (tied_ia, cursor_vp_pin), f"setup check: {cursor0}"
        vp_last = worker_mod._load_vp_state(proc_conn, test_worker)
        assert all(vp in vp_last for vp in test_vps), "setup: vp_last_ts not populated"

        rows, sql_max, sql_count = worker_mod._fetch_batch(
            proc_conn, 0, 1, cursor0, vp_last
        )
        # The stall condition is reproduced:
        assert sql_count >= worker_mod.FETCH_LIMIT, \
            f"test setup expected ≥FETCH_LIMIT rows; got sql_count={sql_count}"
        assert len(rows) == 0, \
            f"test setup expected 0 kept rows; got {len(rows)} kept"

        # THE FIX: cursor must advance despite 0 kept rows.
        assert sql_max > cursor0, (
            f"REGRESSION — composite cursor did not advance past tied ingested_at. "
            f"before={cursor0}  after={sql_max}  "
            f"(this is the Azure 2026-10-03/04 stall signature)"
        )

        # Commit the new cursor and verify the next fetch reaches the future row.
        worker_mod._commit_batch(proc_conn, test_worker, [], {}, sql_max)
        cursor1 = worker_mod._load_cursor(proc_conn, test_worker)
        assert cursor1 == sql_max, f"cursor commit discarded the advance: {cursor1}"

        rows2, sql_max2, sql_count2 = worker_mod._fetch_batch(
            proc_conn, 0, 1, cursor1, vp_last
        )
        future_in_batch = [r for r in rows2 if r.virtual_patient_id == future_vp]
        assert future_in_batch, (
            f"REGRESSION — future row at ingested_at > tied is still unreachable "
            f"after cursor advance. sql_count={sql_count2}, kept={len(rows2)}, "
            f"cursor_after_advance={cursor1}"
        )
    finally:
        with admin_conn.cursor() as cur:
            _cleanup(cur)
