"""Page 1 — Who needs attention.

Landing page. Ranked list of people whose latest reading warrants a look,
each translated into one plain-language sentence. Nothing on this page
uses clinical jargon, internal identifiers, rule ids or detector names.

Refreshes as parallel @st.fragment(run_every="5s") panels.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import icons  # noqa: E402
from db import assert_configured, panel_error, timed_query  # noqa: E402
from domain import (  # noqa: E402
    BATCH_BIAS_SQL, SEVERITY_COLOR, SEVERITY_RANK, age_from_ts, age_sentence,
    batch_suspect_from_df, build_situation, person_line,
)
from theme import (  # noqa: E402
    BG_DARK, EMERGENCY, RECOVERING, SENSOR, TEXT_MUTED,
    TEXT_ON_DARK, TEXT_SECONDARY, WATCH, WATCH_TEXT, page_config,
)

# Only these rule ids appear on the triage page. Everything else (bare MAD
# alerts, cross_signal outcomes derived from MAD) is detection noise — showing
# a row for every one of them buries the actual problems. See
# processor/outliers.py + verification/eval_processor.py for the full picture.
ACTIONABLE_RULES = (
    "rules.hypo_threshold",
    "rules.hyper_threshold",
    "rules.cgm_null",
    "rules.cgm_stuck",
    "rules.cgm_roc_spike",
    "rules.cgm_out_of_range",
    "batch_analysis.firmware_offset",
    "cross_signal.hypo_confirmed_by_hr",
)
# "Real health event" set — used both for filter counts and for the small
# card. Includes cross-signal confirmation as its strongest evidence.
REAL_EVENT_RULES = (
    "rules.hypo_threshold",
    "rules.hyper_threshold",
    "cross_signal.hypo_confirmed_by_hr",
)
# Sensor / firmware faults we're prepared to defend as "caught".
BROKEN_SENSOR_RULES = (
    "rules.cgm_null",
    "rules.cgm_stuck",
    "rules.cgm_roc_spike",
    "rules.cgm_out_of_range",
    "batch_analysis.firmware_offset",
)
LIST_CAP = 25

page_config("Who needs attention")
assert_configured()  # halts here if CAREVAULT_DASHBOARD_PG isn't set

# --------------------------------------------------------------------------
# small utilities
# --------------------------------------------------------------------------
def _person_label(vp_id: str) -> str:
    """'vp:0051' -> 'Person 0051'. Anonymised name for display."""
    n = vp_id.split(":")[-1] if ":" in vp_id else vp_id
    return f"Person {n}"


def _going_icon(direction: str) -> str:
    if direction == "down":
        return icons.arrow_down(16, EMERGENCY)
    if direction == "up":
        return icons.arrow_up(16, WATCH_TEXT)
    return icons.arrow_flat(16, TEXT_MUTED)


# --------------------------------------------------------------------------
# STRAP fragment — right side of the header (live status)
# --------------------------------------------------------------------------
@st.fragment(run_every="5s", parallel=True)
def strap_fragment() -> None:
    """Right-side strap: green dot + rows/min + N people + checked-under-1-s."""
    df = timed_query("attn_strap", """
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
    """)
    if df.empty:
        st.markdown(
            '<div class="strap-status">'
            '<span class="t-muted">connecting to pipeline…</span>'
            '</div>', unsafe_allow_html=True,
        )
        return
    r = df.iloc[0]
    rows_min = int(r["rows_last_min"] or 0)
    n_ppl = int(r["n_people"] or 0)
    lat_s = r["latency_p95_s"]
    lat_txt = "checked within 1 second" if lat_s is None or lat_s < 1.0 \
        else f"checking within {lat_s:.1f} s"
    live = r["cursor_age_s"] is not None and r["cursor_age_s"] < 10
    dot_html = '<span class="dot-live"></span>' if live else \
        f'<span class="dot-live" style="background:{WATCH};"></span>'
    st.markdown(
        f'<div class="strap-status">{dot_html} readings arriving now '
        f'&nbsp;·&nbsp; <b>{rows_min:,}/min</b> from {n_ppl:,} people '
        f'&nbsp;·&nbsp; {lat_txt}</div>',
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------
# Header row (static shell + live strap fragment)
# --------------------------------------------------------------------------
_page_title = "Who needs attention"
st.markdown(
    f'<div class="strap">'
    f'<div class="strap-title">{icons.pulse(24)}&nbsp;<span class="brand">CareVault</span>'
    f'<span class="divider">&nbsp;·&nbsp;</span><span>{_page_title}</span></div>'
    f'<div id="strap-anchor"></div></div>',
    unsafe_allow_html=True,
)
# The fragment writes into a placeholder inside the strap-anchor spot.
strap_fragment()


# --------------------------------------------------------------------------
# Main data fetches (kept split so each panel can be fast)
# --------------------------------------------------------------------------
LOOKBACK_MIN = 5


@st.cache_data(ttl=3600, show_spinner=False)
def _people_context() -> pd.DataFrame:
    """Per-vp: source cohort + diabetes type + optional age/duration.
    Cached for 1 h — this virtually never changes during a run."""
    return timed_query("attn_people_ctx", """
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
              SELECT diabetes_type, cohort
                FROM raw.readings_raw
               WHERE patient_id = vp.source_patient_id
               LIMIT 1
          ) rr ON true
          LEFT JOIN raw.patient p ON p.patient_id = vp.source_patient_id
    """)


def _fetch_top_alerts(lookback_min: int) -> pd.DataFrame:
    """One row per person, keyed on the most-informative *actionable* alert
    in the last N minutes of event-time.

    Two things the earlier version got wrong:
      1. It windowed by emitted_at, which included stale trigger_event_times
         (the batch analyzer emits for readings up to 120 s old).
      2. It included every cross_signal.mad_hr_* alert, which fires on almost
         every vp during a fault-heavy replay. Ranking by 'freshest cross
         signal' then buried the real thresholds under noise.

    We now filter to ACTIONABLE_RULES only. If a person has, say, a hypo
    threshold AND a cross_signal on the same event, the hypo is what shows.
    Severity is computed downstream in Python from the actual cgm value.
    """
    return timed_query("attn_top_alerts", """
      WITH filtered AS (
        SELECT * FROM live.alerts
         WHERE trigger_event_time > now() - make_interval(mins => %(lookback)s)
           AND rule_id = ANY(%(rules)s)
      ),
      top_per_vp AS (
        SELECT DISTINCT ON (virtual_patient_id)
               virtual_patient_id  AS vp,
               emitted_at,
               trigger_event_time,
               alert_type,
               severity            AS alert_severity,
               rule_id,
               detector,
               payload_json
          FROM filtered
         ORDER BY virtual_patient_id,
                  (CASE
                     WHEN rule_id = 'cross_signal.hypo_confirmed_by_hr' THEN 0
                     WHEN rule_id IN ('rules.cgm_roc_spike',
                                      'rules.cgm_out_of_range',
                                      'rules.cgm_null')                THEN 1
                     WHEN rule_id = 'batch_analysis.firmware_offset'   THEN 2
                     WHEN rule_id = 'rules.cgm_stuck'                  THEN 3
                     WHEN rule_id IN ('rules.hypo_threshold',
                                      'rules.hyper_threshold')         THEN 4
                     ELSE                                                    5
                   END),
                  trigger_event_time DESC
      )
      SELECT * FROM top_per_vp
       ORDER BY (CASE alert_severity WHEN 'critical' THEN 0
                                     WHEN 'warn'     THEN 1
                                     ELSE                 2 END),
                trigger_event_time DESC
    """, {"lookback": lookback_min, "rules": list(ACTIONABLE_RULES)})


def _fetch_state(vps: list[str]) -> pd.DataFrame:
    """Latest reading, 3-min trend, had-recent-hypo, and batch-alerted-recently
    for the given set of vps. One trip."""
    if not vps:
        return pd.DataFrame()
    return timed_query("attn_state", """
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
        SELECT DISTINCT virtual_patient_id
          FROM live.alerts
         WHERE alert_type = 'hypo'
           AND emitted_at > now() - interval '20 minutes'
           AND virtual_patient_id = ANY(%(vps)s)
      ),
      recent_batch AS (
        SELECT DISTINCT virtual_patient_id
          FROM live.alerts
         WHERE detector = 'batch_analysis'
           AND emitted_at > now() - interval '3 minutes'
           AND virtual_patient_id = ANY(%(vps)s)
      )
      SELECT l.virtual_patient_id     AS vp,
             l.event_time             AS event_last,
             l.cgm_mgdl               AS cgm_now,
             l.heart_rate_bpm         AS hr_now,
             l.spo2_pct               AS spo2_now,
             l.sampling_sec           AS sampling_sec,
             extract(epoch from (now() - l.event_time))::real AS age_sec,
             CASE WHEN t.cgm_3m IS NOT NULL AND l.cgm_mgdl IS NOT NULL
                  THEN (l.cgm_mgdl - t.cgm_3m) / 3.0 ELSE NULL END AS delta_per_min,
             (rh.virtual_patient_id IS NOT NULL) AS had_recent_hypo,
             (rb.virtual_patient_id IS NOT NULL) AS batch_alerted
        FROM latest l
        LEFT JOIN three_min    t  USING (virtual_patient_id)
        LEFT JOIN recent_hypo  rh USING (virtual_patient_id)
        LEFT JOIN recent_batch rb USING (virtual_patient_id)
    """, {"vps": vps})


def _build_rows() -> pd.DataFrame:
    """Merged & enriched rows for the main table. Returns a DF of Situation
    fields plus person context and metadata."""
    alerts = _fetch_top_alerts(LOOKBACK_MIN)
    if alerts.empty:
        return pd.DataFrame()
    vps = alerts["vp"].tolist()
    state = _fetch_state(vps)
    people = _people_context()
    df = alerts.merge(state, on="vp", how="left").merge(people, on="vp", how="left")
    if df.empty:
        return df

    # Pull stuck consecutive out of payload_json for the "frozen NN min" phrase.
    def _stuck_n(row):
        if row["rule_id"] != "rules.cgm_stuck":
            return None
        p = row["payload_json"]
        if isinstance(p, str):
            import json as _json
            try:
                p = _json.loads(p)
            except Exception:
                return None
        if isinstance(p, dict):
            return p.get("consecutive")
        return None

    df["stuck_consecutive"] = df.apply(_stuck_n, axis=1)

    situations = df.apply(build_situation, axis=1)
    df["_sit"] = situations
    df["severity"] = df["_sit"].apply(lambda s: s.severity)
    df["severity_rank"] = df["severity"].map(SEVERITY_RANK).fillna(5).astype(int)
    df = df.sort_values(["severity_rank", "event_last"],
                        ascending=[True, False]).reset_index(drop=True)
    return df


# --------------------------------------------------------------------------
# Filter buttons
# --------------------------------------------------------------------------
def _apply_filter(df: pd.DataFrame, choice: str) -> pd.DataFrame:
    """Filter by the *underlying alert kind*, not the verdict label.

    Keeps this button in step with the 'in the last five minutes' card
    (which also counts by rule set). A row whose top alert is a hypo/hyper
    threshold counts under 'Real health events' even before cross-signal
    has decided the verdict.
    """
    if df.empty:
        return df
    if choice == "real":
        return df[df["rule_id"].isin(REAL_EVENT_RULES)]
    if choice == "sensor":
        return df[df["rule_id"].isin(BROKEN_SENSOR_RULES)]
    return df


def _filter_row(df: pd.DataFrame, active: str) -> None:
    n_all = len(df)
    n_real   = int(df["rule_id"].isin(REAL_EVENT_RULES).sum())   if not df.empty else 0
    n_sensor = int(df["rule_id"].isin(BROKEN_SENSOR_RULES).sum()) if not df.empty else 0

    def _btn(key: str, label: str, count: int) -> str:
        cls = "filter-btn active" if active == key else "filter-btn"
        return (f'<a class="{cls}" href="?filter={key}" target="_self">'
                f'{label}<span class="count">{count}</span></a>')

    st.markdown(
        '<div class="filter-row">'
        + _btn("everyone", "Everyone",             n_all)
        + _btn("real",     "Real health events",   n_real)
        + _btn("sensor",   "Broken sensors",       n_sensor)
        + '</div>',
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------
# Main table
# --------------------------------------------------------------------------
def _render_table(rows: pd.DataFrame, total_watched: int, n_people_total: int,
                  n_hidden: int) -> None:
    if rows.empty:
        # Empty state: what is the pipeline doing right now?
        probe = timed_query("attn_empty_probe", """
            SELECT (SELECT max(event_time) FROM live.vitals_readings) AS last_event,
                   (SELECT max(emitted_at)  FROM live.alerts)          AS last_alert,
                   (SELECT extract(epoch FROM now() - max(last_ingested_at))
                      FROM live.worker_cursor)                          AS cursor_age_s
        """)
        if probe.empty:
            st.markdown('<div class="card">'
                        '<h3>Waiting for the pipeline to warm up</h3>'
                        '<p>The dashboard is connected, but no readings have '
                        'been received yet.</p></div>', unsafe_allow_html=True)
            return
        r = probe.iloc[0]
        last_ev = r["last_event"]
        last_al = r["last_alert"]
        cage = r["cursor_age_s"]
        cage_txt = f"{int(cage)}s" if cage is not None else "unknown"
        st.markdown(
            f'<div class="card"><h3>Everyone is in a safe range right now</h3>'
            f'<p>Last reading arrived <b>{age_sentence(age_from_ts(last_ev))}</b> '
            f'· last alert of any kind <b>{age_sentence(age_from_ts(last_al))}</b> '
            f'· the checker is <b>{cage_txt}</b> behind the newest reading.</p></div>',
            unsafe_allow_html=True,
        )
        return

    body_rows = []
    for _, r in rows.iterrows():
        s = r["_sit"]
        cgm_txt = f"{int(round(s.cgm_now))}" if s.cgm_now is not None else "—"
        sugar_cls = "sugar strike" if s.is_untrustworthy and s.cgm_now is not None else "sugar"
        color = SEVERITY_COLOR[s.severity]
        row_cls = "row-emergency" if s.severity == "emergency" else ""
        person = _person_label(s.vp_id)
        ctx = person_line(r.get("cohort"), r.get("diabetes_type"),
                          r.get("age_years"), r.get("duration_years"))
        going_html = (
            f'<span class="going">{_going_icon(s.going_dir)}'
            f'<span>{s.going}</span></span>'
        )
        cgm_style = f'color:{color};' if s.cgm_now is not None else f'color:{TEXT_MUTED};'
        drop_color = color if s.cgm_now is not None else TEXT_MUTED
        sugar_html = (
            f'<span style="display:inline-flex; align-items:center; gap:6px;">'
            f'{icons.drop(20, drop_color)}<span class="{sugar_cls}" '
            f'style="{cgm_style}">{cgm_txt}</span></span>'
        )
        chip_html = f'<span class="chip {s.verdict_chip_class}">{s.verdict_label}</span>'
        age_html = f'<span class="age">{age_sentence(s.age_sec)}</span>'
        # Person name deep-links to Page 2.
        person_link = (
            f'<a class="person-name" href="/One_person?vp={s.vp_id}" '
            f'style="color:inherit; text-decoration:none;">{person}</a>'
        )
        body_rows.append(
            f'<tr class="{row_cls}">'
            f'  <td class="sev-bar" style="background:{color};"></td>'
            f'  <td><div>{person_link}</div><div class="person-ctx">{ctx}</div></td>'
            f'  <td>{sugar_html}</td>'
            f'  <td>{going_html}</td>'
            f'  <td><div class="headline">{s.headline}</div>'
            f'      <div class="evidence">{s.evidence}</div></td>'
            f'  <td>{chip_html}</td>'
            f'  <td>{age_html}</td>'
            f'</tr>'
        )

    st.markdown(
        '<table class="attn-table">'
        '<thead><tr>'
        '<th></th>'
        '<th>Person</th>'
        '<th>Blood sugar</th>'
        '<th>Going</th>'
        '<th>What happened</th>'
        '<th>Can we believe it</th>'
        '<th>Reading age</th>'
        '</tr></thead>'
        f'<tbody>{"".join(body_rows)}</tbody>'
        '</table>',
        unsafe_allow_html=True,
    )

    others = max(0, n_people_total - total_watched)
    more_line = (
        f'{n_hidden:,} more people also need a look — sorted below the '
        f'top {len(rows)}. The other {others:,} people are in a safe range.'
        if n_hidden > 0
        else f'The other {others:,} people are in a safe range.'
    )
    st.markdown(
        '<div style="display:flex; justify-content:space-between; '
        'font-size:12px; color:#5C6A70; margin-top:10px;">'
        f'<span>{more_line}</span>'
        '<span>Records carry no names. Blood sugar is in mg/dL, safe range 70 to 180.</span>'
        '</div>',
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------
# Right column cards
# --------------------------------------------------------------------------
def _card_device_check() -> None:
    """Dark card: 'device check' — how bad the suspect batch is.
    Uses the shared BATCH_BIAS_SQL so this number matches the headline on
    the All-monitors page exactly."""
    df = timed_query("attn_device_check", BATCH_BIAS_SQL)
    st.markdown('<div class="card-dark">', unsafe_allow_html=True)
    st.markdown(f'<h4>{icons.monitor(15, "#9EAAB0")} device check</h4>',
                unsafe_allow_html=True)
    if df.empty or df["median_cgm"].isna().all():
        st.markdown('<h3>Not enough readings yet</h3>'
                    '<p>Waiting for one full minute of data before we can '
                    'compare monitors against each other.</p>'
                    '</div>', unsafe_allow_html=True)
        return
    suspect, other = batch_suspect_from_df(df)
    if suspect is None:
        st.markdown('<h3>All monitors agree</h3>'
                    '<p>No batch is drifting away from the rest of the fleet '
                    'right now.</p></div>', unsafe_allow_html=True)
        return
    if other is None or other.empty:
        st.markdown('<h3>Only one batch of monitors is deployed</h3>'
                    '<p>Nothing to compare it against.</p></div>',
                    unsafe_allow_html=True)
        return
    suspect_median = float(suspect["median_cgm"])
    other_median   = float(other["median_cgm"].iloc[0])   # single row (2-fw project)
    diff = float(suspect["deviation_from_fleet"])
    n_suspect = int(suspect["n_devices"])
    direction = "high" if diff > 0 else "low"
    points = int(round(abs(diff)))
    other_n = int(other["n_devices"].sum())
    st.markdown(
        f'<h3>{n_suspect} monitors are reading {points} points too {direction}</h3>'
        f'<p>Their readings are running about {points} mg/dL {direction}er than '
        f'the {other_n:,} other monitors in the fleet.</p>',
        unsafe_allow_html=True,
    )

    # Two labelled bars, scaled to the higher median.
    hi = max(suspect_median, other_median)
    lo = min(0.0, min(suspect_median, other_median) - 20)
    def _bar_pct(v: float) -> int:
        return max(0, min(100, int(round((v - lo) / (hi - lo) * 100))))
    st.markdown(
        f'<div style="margin-top:12px;">'
        f'<div class="bar-row">'
        f'  <div class="label"><span>Suspect batch</span>'
        f'      <span class="mono">{suspect_median:.0f} mg/dL</span></div>'
        f'  <div class="bar-track"><div class="bar-fill suspect" '
        f'style="width:{_bar_pct(suspect_median)}%;"></div></div>'
        f'</div>'
        f'<div class="bar-row">'
        f'  <div class="label"><span>Every other monitor</span>'
        f'      <span class="mono">{other_median:.0f} mg/dL</span></div>'
        f'  <div class="bar-track"><div class="bar-fill baseline" '
        f'style="width:{_bar_pct(other_median)}%;"></div></div>'
        f'</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<a class="pill-btn" style="background:#22303A; border-color:#22303A; '
        'color:#fff; margin-top:8px; display:inline-block;" '
        'href="/All_monitors" target="_self">See all monitors →</a>'
        '</div>',
        unsafe_allow_html=True,
    )


def _card_last_five_minutes() -> None:
    """Two honest counts: distinct people flagged for a real health event,
    and distinct people whose monitor we've called broken. Both windows use
    trigger_event_time (the reading's clock), not emitted_at."""
    df = timed_query("attn_last_5min", """
      SELECT
        (SELECT count(DISTINCT virtual_patient_id) FROM live.alerts
          WHERE trigger_event_time > now() - interval '5 minutes'
            AND rule_id = ANY(%(real)s))                            AS real_events,
        (SELECT count(DISTINCT virtual_patient_id) FROM live.alerts
          WHERE trigger_event_time > now() - interval '5 minutes'
            AND rule_id = ANY(%(broken)s))                          AS broken_caught
    """, {"real": list(REAL_EVENT_RULES), "broken": list(BROKEN_SENSOR_RULES)})
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown('<h4>in the last five minutes</h4>', unsafe_allow_html=True)
    if df.empty:
        st.markdown('<p>Waiting for the first minute of readings.</p></div>',
                    unsafe_allow_html=True)
        return
    r = df.iloc[0]
    rows = [
        ("People with a real health event",  int(r["real_events"]   or 0), RECOVERING),
        ("Monitors called broken",           int(r["broken_caught"] or 0), SENSOR),
    ]
    body = []
    for i, (label, n, color) in enumerate(rows):
        sep = '<div class="hair"></div>' if i > 0 else ''
        body.append(sep + (
            f'<div style="display:flex; justify-content:space-between; align-items:baseline;">'
            f'<span style="font-size:13px; color:{TEXT_SECONDARY};">{label}</span>'
            f'<span class="mono" style="font-size:18px; color:{color};">{n}</span>'
            f'</div>'
        ))
    st.markdown("".join(body) + '</div>', unsafe_allow_html=True)


def _card_how_the_system_is_running() -> None:
    df = timed_query("attn_system_card", """
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
    """)
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown('<h4>how the system is running</h4>', unsafe_allow_html=True)
    if df.empty:
        st.markdown('<p>Waiting for the pipeline to send its first status.</p></div>',
                    unsafe_allow_html=True)
        return
    r = df.iloc[0]
    oldest = int(r["oldest_pending_s"] or 0)
    lat = r["latency_p95_s"]
    lat_txt = f"{lat:.1f} s" if lat is not None else "waiting"
    dead = int(r["dead_15m"] or 0)
    rows_15m = int(r["rows_15m"] or 0)
    dead_pct = (dead / rows_15m * 100.0) if rows_15m else 0.0
    body = (
        '<div style="display:grid; grid-template-columns:1fr auto; gap:6px 12px; '
        'align-items:baseline;">'
        f'<span style="font-size:13px; color:{TEXT_SECONDARY};">Oldest reading not yet checked</span>'
        f'<span class="mono">{oldest}s</span>'
        f'<span style="font-size:13px; color:{TEXT_SECONDARY};">Reading to alert, almost always under</span>'
        f'<span class="mono">{lat_txt}</span>'
        f'<span style="font-size:13px; color:{TEXT_SECONDARY};">Unreadable readings thrown out (last 15 min)</span>'
        f'<span class="mono">{dead:,} ({dead_pct:.2f}%)</span>'
        f'<span style="font-size:13px; color:{TEXT_SECONDARY};">Repeat readings ignored at write</span>'
        f'<span class="mono t-muted">auto</span>'
        '</div>'
        '<div style="margin-top:12px;">'
        '<a class="pill-btn ghost" href="/System" target="_self">Open system page →</a>'
        '</div>'
        '</div>'
    )
    st.markdown(body, unsafe_allow_html=True)


# --------------------------------------------------------------------------
# Table fragment
# --------------------------------------------------------------------------
@st.fragment(run_every="5s", parallel=True)
def table_fragment() -> None:
    active = st.query_params.get("filter", "everyone")
    if active not in ("everyone", "real", "sensor"):
        active = "everyone"
    df = _build_rows()
    err = panel_error("attn_top_alerts") or panel_error("attn_state")
    if err:
        st.error(f"query error: {err} · retrying every 5 s")
        return
    n_people_total = _people_context().shape[0]
    n_watched = 0 if df.empty else len(df)

    if not df.empty:
        sev = df["_sit"].apply(lambda s: s.severity)
        n_action = int((sev == "emergency").sum())
        # 'watch' = out-of-range but stable, 'recovering' = back inside safe
        # range. Both are "not urgent" from the operator's point of view.
        n_stable = int(((sev == "watch") | (sev == "recovering")).sum())
        n_sensor = int((sev == "sensor").sum())
        # Split subhead. Only mention the buckets that have someone in them.
        pieces = []
        if n_stable:
            pieces.append(
                f"another <b>{n_stable}</b> are outside the safe range or "
                f"just back inside it"
            )
        if n_sensor:
            pieces.append(
                f"<b>{n_sensor}</b> more have a monitor reading poorly"
            )
        subhead = (" · ".join(pieces).capitalize() + ".") if pieces else ""
        st.markdown(
            f'<h2 style="margin:6px 0 4px 0; font-weight:600;">'
            f'{n_action} people need action right now</h2>'
            f'<p class="t-secondary" style="margin:0 0 4px 0;">'
            f'{subhead}</p>'
            f'<p class="t-muted" style="margin:0 0 10px 0; font-size:12px;">'
            f'Action-now means blood sugar below 70, above 300, or falling '
            f'fast. About one in five readings from a diabetic population sits '
            f'outside the 70–180 safe range in the source data; most of those '
            f'are stable.</p>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f'<h2 style="margin:6px 0 4px 0; font-weight:600;">'
            f'Nobody needs a look right now</h2>'
            f'<p class="t-secondary" style="margin:0 0 10px 0;">'
            f'All {n_people_total:,} people are inside the safe range or below the '
            f'alert threshold.</p>',
            unsafe_allow_html=True,
        )

    _filter_row(df, active)
    matched = _apply_filter(df, active)
    visible = matched.head(LIST_CAP)
    n_hidden = max(0, len(matched) - len(visible))
    _render_table(visible, len(df), n_people_total, n_hidden)


@st.fragment(run_every="10s", parallel=True)
def device_card_fragment() -> None:
    _card_device_check()


@st.fragment(run_every="5s", parallel=True)
def last5_card_fragment() -> None:
    _card_last_five_minutes()


@st.fragment(run_every="5s", parallel=True)
def system_card_fragment() -> None:
    _card_how_the_system_is_running()


# --------------------------------------------------------------------------
# Two-column layout
# --------------------------------------------------------------------------
main_col, side_col = st.columns([3.3, 1], gap="medium")

with main_col:
    table_fragment()

with side_col:
    device_card_fragment()
    last5_card_fragment()
    system_card_fragment()
