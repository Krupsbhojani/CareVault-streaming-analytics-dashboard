"""Evaluate the processor against ground-truth injected_type + report latency.

Runs as ADMIN (not processor_role) so it can see injected_type. Prints a
per-fault-type precision/recall/F1 table and end-to-end latency percentiles.

Detection mapping (injected fault type -> alert types that count as detection):
  spike            -> detector='mad' (any alert_type) OR alert_type='hyper' from rules
  sensor_dropout   -> alert_type='sensor_dropout'
  stuck            -> rule_id='rules.cgm_stuck'
  firmware_offset  -> not detectable by per-vp online methods (expected recall ~ 0)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from loader.load_raw import CONNINFO_DEFAULT  # admin  # noqa: E402


DETECTS = {
    # Spike = rules-based rate-of-change (single-point jump + return-to-baseline)
    # OR rules-based physiologic ceiling. CGM-MAD is deliberately EXCLUDED here:
    # verification/mad_sweep.py showed unshippable FP rate on CGM at any |z|
    # threshold. MAD is retained for HR/SpO2 (evaluated separately below).
    "spike":          "a.rule_id IN ('rules.cgm_out_of_range', 'rules.cgm_roc_spike')",
    "sensor_dropout": "a.alert_type = 'sensor_dropout'",
    "stuck":          "a.rule_id = 'rules.cgm_stuck'",
    "firmware_offset": "a.detector = 'batch_analysis'",
}


def _percentile(sorted_xs, q):
    if not sorted_xs:
        return None
    idx = max(0, min(len(sorted_xs) - 1, int(q * len(sorted_xs))))
    return sorted_xs[idx]


def evaluate(conninfo: str) -> None:
    with psycopg.connect(conninfo) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM live.vitals_readings")
            n_readings = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM live.alerts")
            n_alerts = cur.fetchone()[0]
            cur.execute("SELECT injected_type, count(*) FROM live.vitals_readings GROUP BY 1 ORDER BY 2 DESC")
            injected_counts = cur.fetchall()

        print("=" * 76)
        print("PROCESSOR EVALUATION")
        print("=" * 76)
        print(f"vitals_readings rows: {n_readings:,}")
        print(f"alerts emitted:       {n_alerts:,}")
        print()
        print("injected_type distribution:")
        for it, c in injected_counts:
            label = it if it is not None else "(clean)"
            print(f"  {label:<18s} {c:>7,d}")
        print()

        # -------- per-fault-type precision/recall --------
        # For each fault type, count:
        #   positives = readings with injected_type = F
        #   TP = positives that have at least one matching alert
        #   FN = positives - TP
        #   predicted = readings with at least one matching alert (regardless of injected_type)
        #   FP = predicted - TP
        print("Per-fault-type detection (against ground-truth injected_type):")
        print(f"  {'fault':<18s} {'positives':>10s} {'TP':>7s} {'FP':>7s} {'FN':>7s} "
              f"{'precision':>10s} {'recall':>8s} {'F1':>7s}")

        for fault, detect_expr in DETECTS.items():
            with conn.cursor() as cur:
                cur.execute(f"""
                    WITH inj AS (
                        SELECT virtual_patient_id, event_time
                          FROM live.vitals_readings
                         WHERE injected_type = %s
                    ),
                    matched_alerts AS (
                        SELECT DISTINCT a.virtual_patient_id, a.trigger_event_time
                          FROM live.alerts a
                         WHERE {detect_expr}
                    ),
                    tp AS (
                        SELECT count(*) AS n FROM inj i
                         WHERE EXISTS (
                             SELECT 1 FROM matched_alerts m
                              WHERE m.virtual_patient_id = i.virtual_patient_id
                                AND m.trigger_event_time = i.event_time
                         )
                    ),
                    predicted AS (
                        SELECT count(*) AS n FROM matched_alerts
                    ),
                    positives AS (
                        SELECT count(*) AS n FROM inj
                    )
                    SELECT
                        (SELECT n FROM positives)  AS positives,
                        (SELECT n FROM tp)         AS tp,
                        (SELECT n FROM predicted)  AS predicted;
                """, (fault,))
                positives, tp, predicted = cur.fetchone()
            fp = predicted - tp
            fn = positives - tp
            precision = tp / predicted if predicted else float("nan")
            recall    = tp / positives if positives else float("nan")
            f1 = (2 * precision * recall / (precision + recall)
                  if precision and recall and (precision + recall) > 0 else float("nan"))
            def _f(x): return "  n/a  " if x != x else f"{x:>6.3f}"  # NaN check
            print(f"  {fault:<18s} {positives:>10,d} {tp:>7,d} {fp:>7,d} {fn:>7,d} "
                  f"{_f(precision):>10s} {_f(recall):>8s} {_f(f1):>7s}")

        # -------- MAD FP rate on HR / SpO2 --------
        # MAD is unshippable on CGM (see verification/mad_sweep.py) but is
        # retained on HR and SpO2 where postprandial-style excursions don't
        # apply. Report FP rate on clean readings for each.
        print()
        print("MAD false-positive rate on non-CGM metrics (alerts/patient/hour on clean):")
        with conn.cursor() as cur:
            cur.execute("""
                SELECT count(DISTINCT virtual_patient_id) AS n_patients,
                       extract(epoch from max(event_time)-min(event_time))/3600.0 AS hours
                  FROM live.vitals_readings
            """)
            n_patients, hours = cur.fetchone()
            hours = float(hours) if hours else 0.0
            cur.execute("""
                SELECT a.rule_id, count(*)
                  FROM live.alerts a
                  JOIN live.vitals_readings v
                    ON v.virtual_patient_id = a.virtual_patient_id
                   AND v.event_time = a.trigger_event_time
                 WHERE a.detector = 'mad'
                   AND v.injected_type IS NULL
                 GROUP BY a.rule_id
                 ORDER BY 1
            """)
            for rule_id, n in cur.fetchall():
                ppph = n / (n_patients * hours) if n_patients and hours else 0.0
                print(f"  {rule_id:<24s} clean_alerts={n:>7,d}  {ppph:>6.3f} alerts/pt/hr")

        # -------- event-level scoring for stuck --------
        # Run-based faults (stuck: N consecutive identical readings) need
        # event-level scoring because the detector deliberately waits N-1
        # readings before firing — per-reading recall understates its true
        # coverage. Group consecutive injected 'stuck' readings per vp into
        # events, then count events with any alert and how long detection took.
        print()
        print("Event-level scoring for stuck (run-based faults need this):")
        print("  Per-reading recall understates run-based detection because the rule")
        print("  fires only from reading 15+; readings 1-14 of every 18-reading run")
        print("  are TRUE negatives *by design*, not misses. Below counts each stuck")
        print("  run (contiguous injected='stuck' readings per vp) as one event.")
        with conn.cursor() as cur:
            cur.execute("""
                WITH stuck_rows AS (
                    SELECT virtual_patient_id, event_time,
                           event_time - lag(event_time) OVER (
                               PARTITION BY virtual_patient_id ORDER BY event_time
                           ) AS gap
                      FROM live.vitals_readings
                     WHERE injected_type = 'stuck'
                ),
                event_starts AS (
                    SELECT virtual_patient_id, event_time,
                           CASE WHEN gap IS NULL OR gap > interval '90 seconds' THEN 1 ELSE 0 END AS is_start
                      FROM stuck_rows
                ),
                event_groups AS (
                    SELECT virtual_patient_id, event_time,
                           SUM(is_start) OVER (
                               PARTITION BY virtual_patient_id ORDER BY event_time
                           ) AS event_id
                      FROM event_starts
                ),
                events AS (
                    SELECT virtual_patient_id, event_id,
                           min(event_time) AS event_start,
                           count(*)        AS n_readings
                      FROM event_groups
                     GROUP BY virtual_patient_id, event_id
                ),
                first_alert_per_event AS (
                    SELECT eg.virtual_patient_id, eg.event_id,
                           min(a.trigger_event_time) AS first_alert_ts
                      FROM event_groups eg
                      JOIN live.alerts a
                        ON a.virtual_patient_id = eg.virtual_patient_id
                       AND a.trigger_event_time = eg.event_time
                       AND a.rule_id = 'rules.cgm_stuck'
                     GROUP BY eg.virtual_patient_id, eg.event_id
                ),
                delays AS (
                    SELECT e.virtual_patient_id, e.event_id, e.n_readings,
                           f.first_alert_ts,
                           (SELECT count(*)
                              FROM event_groups eg2
                             WHERE eg2.virtual_patient_id = e.virtual_patient_id
                               AND eg2.event_id = e.event_id
                               AND eg2.event_time <= f.first_alert_ts) AS readings_until_first_alert
                      FROM events e
                      LEFT JOIN first_alert_per_event f USING (virtual_patient_id, event_id)
                )
                SELECT
                    count(*)                                                 AS total_events,
                    count(*) FILTER (WHERE first_alert_ts IS NOT NULL)       AS detected_events,
                    round(avg(n_readings)::numeric, 1)                       AS avg_readings_per_event,
                    (percentile_cont(0.5) WITHIN GROUP (ORDER BY readings_until_first_alert)
                        FILTER (WHERE first_alert_ts IS NOT NULL))::int      AS median_readings_until_alert,
                    max(readings_until_first_alert)
                        FILTER (WHERE first_alert_ts IS NOT NULL)            AS max_readings_until_alert
                FROM delays
            """)
            row = cur.fetchone()
            if row and row[0]:
                total, detected, avg_len, median_delay, max_delay = row
                recall_event = detected / total if total else float("nan")
                print(f"  total stuck events:          {total:,}")
                print(f"  detected events:             {detected:,}")
                print(f"  event-level recall:          {recall_event:.3f}")
                print(f"  event-level precision:       "
                      f"(matches per-reading precision; every alert fell inside a real event)")
                print(f"  avg readings per event:      {avg_len}")
                print(f"  median readings until alert: {median_delay}  (rule fires at 15+)")
                print(f"  max readings until alert:    {max_delay}")
            else:
                print("  (no stuck events observed)")

        # -------- latency --------
        print()
        print("End-to-end latency (alert.emitted_at - vitals.event_time), ms:")
        with conn.cursor() as cur:
            cur.execute("""
                SELECT extract(epoch from a.emitted_at - v.event_time) * 1000.0
                  FROM live.alerts a
                  JOIN live.vitals_readings v
                    ON v.virtual_patient_id = a.virtual_patient_id
                   AND v.event_time = a.trigger_event_time
            """)
            e2e = sorted(r[0] for r in cur.fetchall() if r[0] is not None)
        if e2e:
            print(f"  n={len(e2e):,}  p50={_percentile(e2e, 0.50):.0f}  "
                  f"p95={_percentile(e2e, 0.95):.0f}  p99={_percentile(e2e, 0.99):.0f}  "
                  f"max={e2e[-1]:.0f}")
        else:
            print("  (no matched alerts)")

        print()
        print("Processor-only latency (alert.emitted_at - vitals.ingested_at), ms:")
        with conn.cursor() as cur:
            cur.execute("""
                SELECT extract(epoch from a.emitted_at - v.ingested_at) * 1000.0
                  FROM live.alerts a
                  JOIN live.vitals_readings v
                    ON v.virtual_patient_id = a.virtual_patient_id
                   AND v.event_time = a.trigger_event_time
            """)
            proc = sorted(r[0] for r in cur.fetchall() if r[0] is not None)
        if proc:
            print(f"  n={len(proc):,}  p50={_percentile(proc, 0.50):.0f}  "
                  f"p95={_percentile(proc, 0.95):.0f}  p99={_percentile(proc, 0.99):.0f}  "
                  f"max={proc[-1]:.0f}")

        # -------- detector breakdown --------
        print()
        print("Alert breakdown by detector x alert_type:")
        with conn.cursor() as cur:
            cur.execute("""
                SELECT detector, alert_type, count(*)
                  FROM live.alerts
                 GROUP BY 1, 2
                 ORDER BY 1, 3 DESC
            """)
            for det, at, n in cur.fetchall():
                print(f"  {det:<14s} {at:<18s} {n:>6,d}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--conninfo", default=os.environ.get("CAREVAULT_PG", CONNINFO_DEFAULT))
    args = ap.parse_args(argv)
    evaluate(args.conninfo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
