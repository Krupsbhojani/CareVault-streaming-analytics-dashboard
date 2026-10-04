"""Replay each dashboard panel's SQL against a live DB N times, report
p50/p95/p99 per panel. Same query text as the pages issue.

Usage:
    python dashboard/bench.py --iterations 30 [--vp vp:0142]
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from typing import Any

import psycopg

_ENV_VAR = "CAREVAULT_DASHBOARD_PG"


def _conninfo() -> str:
    v = os.environ.get(_ENV_VAR, "").strip()
    if not v:
        sys.stderr.write(
            f"error: environment variable {_ENV_VAR} is not set.\n"
            f"set it to a Postgres conninfo string (same one the dashboard uses), "
            f"e.g.  export {_ENV_VAR}='host=... dbname=... user=dashboard_ro ...'\n"
        )
        sys.exit(2)
    return v


CONNINFO = _conninfo()


def _pctile(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    return s[min(len(s) - 1, int(p * len(s)))]


def build_queries(vp: str) -> dict[str, tuple[str, Any]]:
    Q: dict[str, tuple[str, Any]] = {}
    LOOKBACK = 5

    # -------- Page 1 --------
    Q["attn_strap"] = ("""
        SELECT
          (SELECT count(*) FROM live.vitals_readings
            WHERE ingested_at > now() - interval '1 minute')          AS rows_last_min,
          (SELECT count(*) FROM live.virtual_patient)                 AS n_people,
          (SELECT extract(epoch FROM (now() - max(last_ingested_at)))
             FROM live.worker_cursor)                                 AS cursor_age_s,
          (SELECT percentile_cont(0.95) WITHIN GROUP
             (ORDER BY extract(epoch FROM (ingested_at - event_time)))
            FROM (SELECT ingested_at, event_time FROM live.vitals_readings
                   WHERE event_time > now() - interval '5 minutes'
                   LIMIT 20000) t)                                    AS latency_p95_s
    """, ())

    Q["attn_people_ctx"] = ("""
        SELECT vp.virtual_patient_id AS vp,
               vp.source_patient_id  AS source_pid,
               d.firmware_version    AS firmware_version,
               d.firmware_offset_mgdl AS firmware_offset,
               rr.diabetes_type      AS diabetes_type,
               rr.cohort             AS cohort,
               p.age_years           AS age_years,
               p.duration_diabetes_years AS duration_years
          FROM live.virtual_patient vp
          LEFT JOIN live.device d ON d.virtual_patient_id = vp.virtual_patient_id
          LEFT JOIN LATERAL (
              SELECT diabetes_type, cohort FROM raw.readings_raw
               WHERE patient_id = vp.source_patient_id LIMIT 1
          ) rr ON true
          LEFT JOIN raw.patient p ON p.patient_id = vp.source_patient_id
    """, ())

    Q["attn_top_alerts"] = ("""
      WITH top_per_vp AS (
        SELECT DISTINCT ON (a.virtual_patient_id)
               a.virtual_patient_id  AS vp,
               a.emitted_at, a.trigger_event_time, a.alert_type,
               a.severity           AS alert_severity,
               a.rule_id, a.detector, a.payload_json
          FROM live.alerts a
         WHERE a.emitted_at > now() - make_interval(mins => %(lookback)s)
         ORDER BY a.virtual_patient_id,
                  (CASE
                     WHEN a.detector = 'cross_signal'   THEN 0
                     WHEN a.detector = 'batch_analysis' THEN 1
                     WHEN a.rule_id IN ('rules.cgm_null','rules.cgm_stuck',
                                        'rules.cgm_roc_spike',
                                        'rules.cgm_out_of_range') THEN 2
                     WHEN a.rule_id IN ('rules.hypo_threshold',
                                        'rules.hyper_threshold') THEN 3
                     ELSE 4 END),
                  a.emitted_at DESC
      )
      SELECT * FROM top_per_vp
       ORDER BY (CASE alert_severity WHEN 'critical' THEN 0
                                     WHEN 'warn'     THEN 1
                                     ELSE                 2 END),
                emitted_at DESC
       LIMIT %(lim)s
    """, {"lookback": LOOKBACK, "lim": 200})

    # attn_state requires a vp list; use the top-N vps we happen to have.
    # We'll pass a fresh list per iteration in main().
    Q["attn_state"] = ("attn_state_placeholder", None)

    Q["attn_device_check"] = ("""
      SELECT d.firmware_version                                          AS fw,
             count(DISTINCT d.virtual_patient_id)                        AS n_devices,
             percentile_cont(0.5) WITHIN GROUP (ORDER BY v.cgm_mgdl_mean) AS median_cgm,
             max(d.firmware_offset_mgdl)                                 AS offset
        FROM live.device d
        LEFT JOIN live.vitals_1min v ON v.virtual_patient_id = d.virtual_patient_id
                                    AND v.minute_start_utc > now() - interval '15 minutes'
       GROUP BY d.firmware_version
       ORDER BY count(DISTINCT d.virtual_patient_id) DESC
    """, ())

    Q["attn_last_5min"] = ("""
      SELECT
        (SELECT count(DISTINCT virtual_patient_id) FROM live.alerts
          WHERE emitted_at > now() - interval '5 minutes'
            AND rule_id IN ('cross_signal.hypo_confirmed_by_hr',
                            'cross_signal.mad_hr_possible_clinical'))  AS real_events,
        (SELECT count(DISTINCT virtual_patient_id) FROM live.alerts
          WHERE emitted_at > now() - interval '5 minutes'
            AND (detector = 'batch_analysis'
                 OR rule_id IN ('rules.cgm_null','rules.cgm_stuck',
                                'rules.cgm_roc_spike','rules.cgm_out_of_range',
                                'cross_signal.mad_hr_sensor_error')))    AS broken_caught,
        (SELECT count(*) FROM live.alerts
          WHERE emitted_at > now() - interval '5 minutes'
            AND rule_id = 'cross_signal.mad_hr_sensor_error')            AS false_alarms
    """, ())

    Q["attn_system_card"] = ("""
      SELECT
        (SELECT extract(epoch FROM (now() - min(last_ingested_at)))::int
           FROM live.worker_cursor)                                       AS oldest_pending_s,
        (SELECT percentile_cont(0.95) WITHIN GROUP
           (ORDER BY extract(epoch FROM (ingested_at - event_time)))
          FROM (SELECT ingested_at, event_time FROM live.vitals_readings
                 WHERE event_time > now() - interval '5 minutes'
                 LIMIT 20000) t)                                          AS latency_p95_s,
        (SELECT count(*) FROM live.dead_letter
          WHERE received_at > now() - interval '15 minutes')              AS dead_15m,
        (SELECT sum(n_readings) FROM live.vitals_1min
          WHERE minute_start_utc > now() - interval '15 minutes')         AS rows_15m
    """, ())

    # -------- Page 2 --------
    Q["op_vp_list"] = (Q["attn_people_ctx"][0], ())
    Q["op_header"] = ("""
        SELECT event_time FROM live.vitals_readings
         WHERE virtual_patient_id = %s
         ORDER BY event_time DESC LIMIT 1
    """, (vp,))
    Q["op_latest"] = ("""
        SELECT event_time, cgm_mgdl, heart_rate_bpm, systolic_bp_mmhg,
               diastolic_bp_mmhg, spo2_pct, sampling_sec
          FROM live.vitals_readings
         WHERE virtual_patient_id = %s
           AND event_time > now() - interval '15 minutes'
         ORDER BY event_time DESC LIMIT 1
    """, (vp,))
    Q["op_trend"] = ("""
        SELECT avg(cgm_mgdl) AS cgm_3m
          FROM live.vitals_readings
         WHERE virtual_patient_id = %s
           AND event_time >  now() - interval '4 minutes'
           AND event_time <= now() - interval '3 minutes'
           AND cgm_mgdl IS NOT NULL
    """, (vp,))
    Q["op_top_alert"] = ("""
        SELECT emitted_at, alert_type, severity, rule_id, detector, payload_json
          FROM live.alerts
         WHERE virtual_patient_id = %s
           AND emitted_at > now() - interval '10 minutes'
         ORDER BY (CASE
                     WHEN detector = 'cross_signal'   THEN 0
                     WHEN detector = 'batch_analysis' THEN 1
                     WHEN rule_id IN ('rules.cgm_null','rules.cgm_stuck',
                                      'rules.cgm_roc_spike','rules.cgm_out_of_range') THEN 2
                     WHEN rule_id IN ('rules.hypo_threshold','rules.hyper_threshold') THEN 3
                     ELSE 4 END), emitted_at DESC
         LIMIT 1
    """, (vp,))
    Q["op_cgm_series"] = ("""
        SELECT minute_start_utc AS ts, cgm_mgdl_mean AS cgm
          FROM live.vitals_1min
         WHERE virtual_patient_id = %s
           AND minute_start_utc > now() - interval '180 minutes'
         ORDER BY minute_start_utc
    """, (vp,))
    Q["op_synth_series"] = ("""
        SELECT minute_start_utc AS ts,
               heart_rate_bpm_mean AS hr,
               systolic_bp_mmhg_mean AS sbp,
               diastolic_bp_mmhg_mean AS dbp,
               spo2_pct_mean AS spo2
          FROM live.vitals_1min
         WHERE virtual_patient_id = %s
           AND minute_start_utc > now() - interval '60 minutes'
         ORDER BY minute_start_utc
    """, (vp,))
    Q["op_forecast"] = ("""
        SELECT p.predicted_ts_utc, p.predicted_cgm_mgdl, p.input_window_end,
               p.horizon_seconds, p.emitted_at,
               (SELECT cgm_mgdl FROM live.vitals_readings
                  WHERE virtual_patient_id = p.virtual_patient_id
                    AND event_time         = p.input_window_end
                  LIMIT 1) AS from_cgm
          FROM live.predictions p
         WHERE p.virtual_patient_id = %s AND p.model_name = 'persistence'
         ORDER BY p.input_window_end DESC LIMIT 1
    """, (vp,))

    # -------- Page 3 --------
    Q["mon_batch_medians"] = (Q["attn_device_check"][0], ())
    Q["mon_batch_series"] = ("""
        SELECT d.firmware_version AS fw,
               v.minute_start_utc AS ts,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY v.cgm_mgdl_mean) AS median_cgm
          FROM live.vitals_1min v
          JOIN live.device d ON d.virtual_patient_id = v.virtual_patient_id
         WHERE v.minute_start_utc > now() - interval '120 minutes'
           AND v.cgm_mgdl_mean IS NOT NULL
         GROUP BY d.firmware_version, v.minute_start_utc
         ORDER BY v.minute_start_utc, d.firmware_version
    """, ())
    Q["mon_fleet_median"] = ("""
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY cgm_mgdl_mean) AS m
          FROM live.vitals_1min
         WHERE minute_start_utc > now() - interval '15 minutes'
           AND cgm_mgdl_mean IS NOT NULL
    """, ())

    # -------- Page 4 --------
    Q["sys_rows"] = ("""
        SELECT minute_start_utc AS ts, sum(n_readings) AS rows_in_minute
          FROM live.vitals_1min
         WHERE minute_start_utc > now() - interval '30 minutes'
         GROUP BY minute_start_utc ORDER BY minute_start_utc
    """, ())
    Q["sys_cursor"] = ("""
        SELECT worker_id, last_ingested_at,
               EXTRACT(EPOCH FROM (now() - last_ingested_at))::int AS age_sec
          FROM live.worker_cursor ORDER BY worker_id
    """, ())
    Q["sys_latency"] = (Q["attn_system_card"][0], ())
    Q["sys_partitions"] = ("""
        SELECT c.relname AS partition,
               pg_size_pretty(pg_total_relation_size(c.oid)) AS size,
               s.n_live_tup AS n_live_tup
          FROM pg_inherits i
          JOIN pg_class parent ON parent.oid = i.inhparent
          JOIN pg_namespace pn ON pn.oid = parent.relnamespace
          JOIN pg_class c      ON c.oid  = i.inhrelid
          LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
         WHERE pn.nspname = 'live' AND parent.relname = 'vitals_readings'
         ORDER BY c.relname DESC LIMIT 8
    """, ())

    return Q


def _attn_state_sql() -> str:
    return """
      WITH latest AS (
        SELECT DISTINCT ON (virtual_patient_id)
               virtual_patient_id, event_time, cgm_mgdl,
               heart_rate_bpm, spo2_pct, sampling_sec
          FROM live.vitals_readings
         WHERE virtual_patient_id = ANY(%(vps)s)
           AND event_time > now() - interval '15 minutes'
         ORDER BY virtual_patient_id, event_time DESC
      ),
      three_min AS (
        SELECT virtual_patient_id, avg(cgm_mgdl) AS cgm_3m
          FROM live.vitals_readings
         WHERE virtual_patient_id = ANY(%(vps)s)
           AND event_time >  now() - interval '4 minutes'
           AND event_time <= now() - interval '3 minutes'
           AND cgm_mgdl IS NOT NULL
         GROUP BY virtual_patient_id
      ),
      recent_hypo AS (
        SELECT DISTINCT virtual_patient_id FROM live.alerts
         WHERE alert_type = 'hypo'
           AND emitted_at > now() - interval '20 minutes'
           AND virtual_patient_id = ANY(%(vps)s)
      ),
      recent_batch AS (
        SELECT DISTINCT virtual_patient_id FROM live.alerts
         WHERE detector = 'batch_analysis'
           AND emitted_at > now() - interval '3 minutes'
           AND virtual_patient_id = ANY(%(vps)s)
      )
      SELECT l.virtual_patient_id AS vp, l.event_time AS event_last,
             l.cgm_mgdl AS cgm_now, l.heart_rate_bpm AS hr_now,
             l.spo2_pct AS spo2_now, l.sampling_sec,
             extract(epoch from (now() - l.event_time))::real AS age_sec,
             CASE WHEN t.cgm_3m IS NOT NULL AND l.cgm_mgdl IS NOT NULL
                  THEN (l.cgm_mgdl - t.cgm_3m) / 3.0 ELSE NULL END AS delta_per_min,
             (rh.virtual_patient_id IS NOT NULL) AS had_recent_hypo,
             (rb.virtual_patient_id IS NOT NULL) AS batch_alerted
        FROM latest l
        LEFT JOIN three_min t     USING (virtual_patient_id)
        LEFT JOIN recent_hypo rh  USING (virtual_patient_id)
        LEFT JOIN recent_batch rb USING (virtual_patient_id)
    """


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--iterations", type=int, default=30)
    ap.add_argument("--vp", default=None)
    args = ap.parse_args()

    with psycopg.connect(CONNINFO, autocommit=True) as conn:
        with conn.cursor() as cur:
            if args.vp:
                vp = args.vp
            else:
                cur.execute("""
                    SELECT virtual_patient_id FROM live.alerts
                     WHERE emitted_at > now() - interval '5 min'
                     GROUP BY virtual_patient_id
                     ORDER BY count(*) DESC LIMIT 1
                """)
                r = cur.fetchone()
                vp = r[0] if r else "vp:0000"
            # Pick a small set of vps for the attn_state benchmark: the top-50
            # most recently alerted, mimicking the landing page.
            cur.execute("""
                SELECT DISTINCT ON (virtual_patient_id) virtual_patient_id
                  FROM live.alerts
                 WHERE emitted_at > now() - interval '5 minutes'
                 ORDER BY virtual_patient_id, emitted_at DESC
                 LIMIT 50
            """)
            attn_vps = [r[0] for r in cur.fetchall()] or [vp]
        print(f"benchmarking · vp={vp} · attn vps={len(attn_vps)} · "
              f"iterations={args.iterations}", flush=True)

        queries = build_queries(vp)
        results: dict[str, list[float]] = {name: [] for name in queries}

        for i in range(args.iterations):
            for name, (sql, params) in queries.items():
                if name == "attn_state":
                    sql = _attn_state_sql()
                    params = {"vps": attn_vps}
                t0 = time.perf_counter()
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    cur.fetchall()
                results[name].append((time.perf_counter() - t0) * 1000.0)
            if i % 5 == 4:
                print(f"  iter {i+1}/{args.iterations} done", flush=True)

    print()
    print(f"{'panel':30s} {'p50_ms':>8s} {'p95_ms':>8s} {'p99_ms':>8s} {'n':>4s}")
    print("-" * 62)
    rows = [(name, _pctile(vals, 0.50), _pctile(vals, 0.95),
             _pctile(vals, 0.99), len(vals))
            for name, vals in results.items()]
    rows.sort(key=lambda r: r[2], reverse=True)
    for name, p50, p95, p99, n in rows:
        print(f"{name:30s} {p50:8.1f} {p95:8.1f} {p99:8.1f} {n:4d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
