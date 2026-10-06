"""Page 2 — One person.

Everything about one person, in plain language, with the same visual system
as Page 1. No clinical jargon, no rule ids, no invented values.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import icons  # noqa: E402
from db import assert_configured, panel_error, timed_query  # noqa: E402
from domain import (  # noqa: E402
    SEVERITY_COLOR, age_from_ts, age_sentence, build_situation, person_line,
)
from theme import (  # noqa: E402
    BG_CARD, BORDER, EMERGENCY, RECOVERING, SENSOR, TEXT_MUTED, TEXT_PRIMARY,
    TEXT_SECONDARY, WATCH, WATCH_TEXT, page_config,
)

page_config("One person")
assert_configured()

# --------------------------------------------------------------------------
# vp selection
# --------------------------------------------------------------------------
def _person_label(vp: str) -> str:
    n = vp.split(":")[-1] if ":" in vp else vp
    return f"Person {n}"


vps_df = timed_query("op_vp_list", """
    SELECT vp.virtual_patient_id AS vp,
           vp.source_patient_id  AS source_pid,
           d.firmware_version    AS firmware,
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
     ORDER BY vp.virtual_patient_id
""")
if vps_df.empty:
    st.markdown('<div class="card"><h3>No people are registered yet</h3>'
                '<p>Run build_vps to create virtual patients.</p></div>',
                unsafe_allow_html=True)
    st.stop()

qp = st.query_params
vp_from_url = qp.get("vp")
vp_ids = vps_df["vp"].tolist()
if vp_from_url in vp_ids:
    selected_vp = vp_from_url
else:
    selected_vp = vp_ids[0]

vp_row = vps_df[vps_df["vp"] == selected_vp].iloc[0]

# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------
context_line = person_line(
    vp_row["cohort"], vp_row["diabetes_type"],
    vp_row["age_years"], vp_row["duration_years"],
)


@st.fragment(run_every="5s", parallel=True)
def header_fragment() -> None:
    df = timed_query("op_header", """
        SELECT event_time
          FROM live.vitals_readings
         WHERE virtual_patient_id = %s
         ORDER BY event_time DESC
         LIMIT 1
    """, (selected_vp,))
    ev = df.iloc[0]["event_time"] if not df.empty else None
    age = age_sentence(age_from_ts(ev))
    st.markdown(
        '<div class="strap">'
        '<div class="strap-title">'
        f'<a href="/" target="_self" style="color:inherit; text-decoration:none; '
        f'display:inline-flex; align-items:center; gap:6px;">'
        f'{icons.arrow_left(14)} Back</a>'
        f'<span class="divider">·</span>'
        f'<span class="mono" style="font-size:15px; color:{TEXT_PRIMARY};">'
        f'{_person_label(selected_vp)}</span>'
        f'<span class="divider">·</span>'
        f'<span style="color:{TEXT_MUTED}; font-size:13px;">'
        f'{context_line} — no name on file</span>'
        '</div>'
        f'<div class="strap-status">newest reading arrived '
        f'<b>&nbsp;{age}</b></div>'
        '</div>',
        unsafe_allow_html=True,
    )


header_fragment()


# --------------------------------------------------------------------------
# Data fetches
# --------------------------------------------------------------------------
def _fetch_top(vp: str) -> pd.DataFrame:
    """Everything needed for the top card + verdict list."""
    latest = timed_query("op_latest", """
        SELECT event_time, cgm_mgdl, heart_rate_bpm, systolic_bp_mmhg,
               diastolic_bp_mmhg, spo2_pct, sampling_sec
          FROM live.vitals_readings
         WHERE virtual_patient_id = %s
           AND event_time > now() - interval '15 minutes'
         ORDER BY event_time DESC
         LIMIT 1
    """, (vp,))
    trend = timed_query("op_trend", """
        SELECT avg(cgm_mgdl) AS cgm_3m
          FROM live.vitals_readings
         WHERE virtual_patient_id = %s
           AND event_time >  now() - interval '4 minutes'
           AND event_time <= now() - interval '3 minutes'
           AND cgm_mgdl IS NOT NULL
    """, (vp,))
    alert = timed_query("op_top_alert", """
        SELECT emitted_at, alert_type, severity, rule_id, detector, payload_json
          FROM live.alerts
         WHERE virtual_patient_id = %s
           AND emitted_at > now() - interval '10 minutes'
         ORDER BY (CASE
                     WHEN detector = 'cross_signal'   THEN 0
                     WHEN detector = 'batch_analysis' THEN 1
                     WHEN rule_id IN ('rules.cgm_null','rules.cgm_stuck',
                                      'rules.cgm_roc_spike',
                                      'rules.cgm_out_of_range') THEN 2
                     WHEN rule_id IN ('rules.hypo_threshold',
                                      'rules.hyper_threshold') THEN 3
                     ELSE 4
                  END), emitted_at DESC
         LIMIT 1
    """, (vp,))
    hypo_recent = timed_query("op_hypo_recent", """
        SELECT 1 FROM live.alerts
         WHERE virtual_patient_id = %s
           AND alert_type = 'hypo'
           AND emitted_at > now() - interval '20 minutes'
         LIMIT 1
    """, (vp,))
    batch_recent = timed_query("op_batch_recent", """
        SELECT 1 FROM live.alerts
         WHERE virtual_patient_id = %s
           AND detector = 'batch_analysis'
           AND emitted_at > now() - interval '3 minutes'
         LIMIT 1
    """, (vp,))
    # Merge into one row for build_situation.
    if latest.empty:
        return pd.DataFrame()
    lat = latest.iloc[0]
    tr  = trend.iloc[0] if not trend.empty else None
    al  = alert.iloc[0] if not alert.empty else None
    row = {
        "vp": vp,
        "cgm_now": lat["cgm_mgdl"],
        "event_last": lat["event_time"],
        "hr_now": lat["heart_rate_bpm"],
        "spo2_now": lat["spo2_pct"],
        "sampling_sec": lat["sampling_sec"],
        "age_sec": age_from_ts(lat["event_time"]),
        "delta_per_min": (None if (tr is None or pd.isna(tr["cgm_3m"]) or lat["cgm_mgdl"] is None)
                          else (float(lat["cgm_mgdl"]) - float(tr["cgm_3m"])) / 3.0),
        "rule_id": None if al is None else al["rule_id"],
        "detector": None if al is None else al["detector"],
        "alert_type": None if al is None else al["alert_type"],
        "alert_severity": None if al is None else al["severity"],
        "payload_json": None if al is None else al["payload_json"],
        "had_recent_hypo": not hypo_recent.empty,
        "batch_alerted": not batch_recent.empty,
        "firmware_offset": vp_row.get("firmware_offset"),
        "stuck_consecutive": None,
    }
    if row["rule_id"] == "rules.cgm_stuck":
        import json
        p = row["payload_json"]
        if isinstance(p, str):
            try: p = json.loads(p)
            except Exception: p = None
        if isinstance(p, dict):
            row["stuck_consecutive"] = p.get("consecutive")
    return pd.DataFrame([row])


def _fetch_cgm_series(vp: str, minutes: int = 60) -> pd.DataFrame:
    """Raw CGM readings for the last `minutes` of THIS vp's event-time,
    anchored to its own max(event_time). Anchoring to the vp's clock rather
    than wall-clock `now()` matches scratch/forecast_check.py and keeps the
    chart useful even when the replay has paused (otherwise `now() - 60 min`
    returns nothing on a stale DB).

    Also returns per-reading rather than minute roll-ups because the forecast
    overlay below compares model predictions to the SAME readings the
    predictions were resolved against — minute averages would mis-align."""
    df = timed_query("op_cgm_series", """
        WITH anchor AS (
            SELECT max(event_time) AS vp_max_et
              FROM live.vitals_readings
             WHERE virtual_patient_id = %s
        )
        SELECT v.event_time AS ts, v.cgm_mgdl AS cgm
          FROM live.vitals_readings v, anchor a
         WHERE v.virtual_patient_id = %s
           AND v.event_time > a.vp_max_et - make_interval(mins => %s)
           AND v.event_time <= a.vp_max_et
           AND v.cgm_mgdl IS NOT NULL
         ORDER BY v.event_time
    """, (vp, vp, minutes))
    return df


def _fetch_recent_predictions(vp: str, minutes: int = 60) -> pd.DataFrame:
    """Historical predictions over the last `minutes` of this vp's event-time
    for the two baseline models. Joined to live.vitals_readings on
    predicted_ts_utc = event_time (same semantics as
    scratch/forecast_check.py), so every prediction row we return had an
    actual reading at exactly its target timestamp — i.e. the forecast was
    resolvable."""
    return timed_query("op_recent_predictions", """
        WITH anchor AS (
            SELECT max(event_time) AS vp_max_et
              FROM live.vitals_readings
             WHERE virtual_patient_id = %s
        )
        SELECT p.model_name          AS model,
               p.predicted_ts_utc    AS ts,
               p.predicted_cgm_mgdl  AS cgm
          FROM live.predictions p
          JOIN live.vitals_readings v
            ON v.virtual_patient_id = p.virtual_patient_id
           AND v.event_time         = p.predicted_ts_utc
         CROSS JOIN anchor a
         WHERE p.virtual_patient_id = %s
           AND p.model_name IN ('persistence', 'linear_trend')
           AND p.predicted_ts_utc > a.vp_max_et - make_interval(mins => %s)
           AND p.predicted_ts_utc <= a.vp_max_et
           AND p.predicted_cgm_mgdl IS NOT NULL
         ORDER BY p.model_name, p.predicted_ts_utc
    """, (vp, vp, minutes))


# The dashboard reads the persistence model (naive "next reading = current
# reading"). It wins the recent-error leaderboard against linear_trend,
# river_linreg and river_ht in this project — see processor/ml.py.
MODEL_NAME = "persistence"


def _fetch_latest_forecast(vp: str) -> pd.DataFrame:
    """The newest persistence prediction for this vp, ranked by the reading
    it was made from (input_window_end). No future-vs-now() filter — both
    predicted_ts_utc and input_window_end are in event-time, which trails
    wall-clock under replay (and would never be > now() in that case).

    Also returns the actual CGM at the input_window_end so the chart can
    anchor the dashed line to that reading, not to 'wall clock now'.
    """
    return timed_query("op_forecast", """
        SELECT p.predicted_ts_utc   AS ts,
               p.predicted_cgm_mgdl AS cgm,
               p.input_window_end   AS from_ts,
               p.horizon_seconds    AS horizon_src_sec,
               p.emitted_at         AS emitted_at,
               -- Actual reading taken at input_window_end — the forecast's
               -- anchor point. Looked up via the (vp, event_time) unique
               -- index on vitals_readings so this is a single index seek.
               (SELECT cgm_mgdl FROM live.vitals_readings
                  WHERE virtual_patient_id = p.virtual_patient_id
                    AND event_time         = p.input_window_end
                  LIMIT 1)          AS from_cgm
          FROM live.predictions p
         WHERE p.virtual_patient_id = %s
           AND p.model_name = %s
         ORDER BY p.input_window_end DESC
         LIMIT 1
    """, (vp, MODEL_NAME))


def _fetch_recent_error(vp: str) -> pd.DataFrame:
    """Person's recent typical error (median of |predicted - actual|) over
    the last hour of resolved predictions, plus a count of how many data
    points fed it. Fallback to the global persistence error if this person
    hasn't accumulated enough resolved predictions yet."""
    return timed_query("op_recent_error", """
        WITH mine AS (
            SELECT abs_error_mgdl
              FROM live.predictions
             WHERE virtual_patient_id = %s
               AND model_name = %s
               AND resolved_at IS NOT NULL
               AND resolved_at > now() - interval '60 minutes'
        ),
        global_tail AS (
            SELECT abs_error_mgdl
              FROM live.predictions
             WHERE model_name = %s
               AND resolved_at IS NOT NULL
               AND resolved_at > now() - interval '15 minutes'
        )
        SELECT
          (SELECT count(*) FROM mine)                                        AS n_mine,
          (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY abs_error_mgdl)
             FROM mine)                                                       AS p50_mine,
          (SELECT count(*) FROM global_tail)                                  AS n_global,
          (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY abs_error_mgdl)
             FROM global_tail)                                                AS p50_global
    """, (vp, MODEL_NAME, MODEL_NAME))


def _fetch_model_leaderboard() -> pd.DataFrame:
    """Per-model typical error over the last 15 min of resolved predictions.
    Lets the right-column card say 'the fancier model is X worse per guess'."""
    return timed_query("op_model_leaderboard", """
        SELECT model_name,
               count(*)                                                       AS n,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY abs_error_mgdl)    AS p50
          FROM live.predictions
         WHERE resolved_at IS NOT NULL
           AND resolved_at > now() - interval '15 minutes'
         GROUP BY model_name
         ORDER BY p50
    """)


def _fetch_vp_reading_count(vp: str) -> pd.DataFrame:
    """How many readings we've received for this person — honest denominator
    for the "learned from" row (persistence doesn't 'learn', but this is the
    data the system has gathered so far)."""
    return timed_query("op_reading_count", """
        SELECT count(*) AS n
          FROM live.vitals_readings
         WHERE virtual_patient_id = %s
    """, (vp,))


def _fetch_synth_series(vp: str) -> pd.DataFrame:
    return timed_query("op_synth_series", """
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


def _fetch_events(vp: str, minutes: int = 720) -> pd.DataFrame:
    """Insulin + carb events from the source trace, mapped to event-time."""
    anchor = timed_query("op_anchor", """
        SELECT vp.source_trace_start_utc AS src_start,
               vp.speed_factor           AS speed,
               (SELECT min(event_time) FROM live.vitals_readings
                 WHERE virtual_patient_id = %s) AS event_start
          FROM live.virtual_patient vp
         WHERE vp.virtual_patient_id = %s
    """, (vp, vp))
    if anchor.empty or anchor.iloc[0]["event_start"] is None:
        return pd.DataFrame(columns=["ts", "bolus_u", "carb_g", "basal_rate_u_hr"])
    a = anchor.iloc[0]
    src_start: datetime = a["src_start"]
    ev_start:  datetime = a["event_start"]
    speed = float(a["speed"])
    now = datetime.now(timezone.utc)
    ev_lo = now - timedelta(minutes=minutes)
    src_lo = src_start + (ev_lo - ev_start) * speed
    src_hi = src_start + (now  - ev_start) * speed
    ev = timed_query("op_events", """
        SELECT ts_utc AS src_ts, bolus_u, carb_g, basal_rate_u_hr
          FROM raw.readings_raw
         WHERE patient_id = %s
           AND ts_utc BETWEEN %s AND %s
           AND (bolus_u IS NOT NULL OR carb_g IS NOT NULL
                OR basal_rate_u_hr IS NOT NULL)
         ORDER BY ts_utc DESC
         LIMIT 20
    """, (vp_row["source_pid"], src_lo, src_hi))
    if ev.empty:
        return ev
    ev["ts"] = ev["src_ts"].apply(lambda t: ev_start + (t - src_start) / speed)
    return ev


# --------------------------------------------------------------------------
# Top card
# --------------------------------------------------------------------------
@st.fragment(run_every="5s", parallel=True)
def top_card_fragment() -> None:
    df = _fetch_top(selected_vp)
    if df.empty:
        st.markdown(
            '<div class="card"><h3>No readings yet for this person</h3>'
            '<p>Waiting for the first minute of data.</p></div>',
            unsafe_allow_html=True,
        )
        return
    row = df.iloc[0]
    s = build_situation(row)
    sev_color = SEVERITY_COLOR.get(s.severity, TEXT_PRIMARY)

    # Forecast slot (persistence model, 15-min source-time horizon).
    fx = _fetch_latest_forecast(selected_vp)
    err = _fetch_recent_error(selected_vp)
    typical_err: float | None = None
    err_source = "none"
    if not err.empty:
        r = err.iloc[0]
        if (r.get("n_mine") or 0) >= 20 and r.get("p50_mine") is not None:
            typical_err = float(r["p50_mine"]); err_source = "mine"
        elif (r.get("n_global") or 0) >= 50 and r.get("p50_global") is not None:
            typical_err = float(r["p50_global"]); err_source = "global"

    if fx.empty:
        n_readings = _fetch_vp_reading_count(selected_vp)
        n = int(n_readings.iloc[0]["n"]) if not n_readings.empty else 0
        if n == 0:
            msg = "The system has not seen a reading from this person yet."
        else:
            msg = (f"Gathered {n:,} readings so far. The first forecast appears "
                   f"after the system has one full window of history.")
        forecast_html = (
            '<div style="border-left:1px solid #ECEFEE; padding-left:16px; '
            'min-width:220px;">'
            '<div class="small-caps">forecast</div>'
            '<div class="mid-value t-muted" style="margin-top:6px;">gathering readings</div>'
            f'<div style="font-size:12px; color:#5C6A70; margin-top:4px;">{msg}</div>'
            '</div>'
        )
    else:
        f = fx.iloc[0]
        v = float(f["cgm"]) if f["cgm"] is not None else None
        v_txt = f"{int(round(v))}" if v is not None else "—"
        # Horizon in plain source-minutes. 'horizon_src_sec' is the source-time
        # horizon the model was configured with (900 s = 15 min at the data's
        # own clock). Independent of replay speed, independent of lag.
        horizon_min = int(round(float(f["horizon_src_sec"]) / 60.0)) if f["horizon_src_sec"] else 15
        # Timestamp of the reading the forecast was made from. Shown as the
        # 'from' clock, so the operator sees forecast is anchored to the data,
        # not to wall-clock now().
        from_ts = f["from_ts"]
        try:
            from_hhmm = from_ts.strftime("%-I:%M %p")
        except (ValueError, TypeError, AttributeError):
            from_hhmm = from_ts.strftime("%H:%M") if from_ts is not None else "—"
        if typical_err is not None:
            pm_txt = f"give or take {int(round(typical_err))} mg/dL"
        else:
            pm_txt = "give or take — (not enough resolved predictions yet)"
        forecast_html = (
            '<div style="border-left:1px solid #ECEFEE; padding-left:16px;">'
            '<div class="small-caps">forecast</div>'
            f'<div class="mid-value" style="margin-top:6px; color:{sev_color};">{v_txt}</div>'
            f'<div style="font-size:12px; color:#5C6A70; margin-top:4px;">{pm_txt}</div>'
            f'<div style="font-size:11px; color:#8B9499; margin-top:4px;">'
            f'{horizon_min} min ahead of the reading at '
            f'<span class="mono">{from_hhmm}</span></div>'
            '</div>'
        )

    # Sugar block
    cgm_txt = "—" if s.cgm_now is None else f"{int(round(s.cgm_now))}"
    sugar_style = f'color:{sev_color};'
    if s.is_untrustworthy and s.cgm_now is not None:
        sugar_style += 'text-decoration:line-through; text-decoration-thickness:3px;'

    # Trend block
    if s.going_dir == "down":
        going_icon = icons.arrow_down(20, EMERGENCY)
    elif s.going_dir == "up":
        going_icon = icons.arrow_up(20, WATCH_TEXT)
    else:
        going_icon = icons.arrow_flat(20, TEXT_MUTED)
    going_plain = {
        "down fast":   "dropping quickly",
        "down":        "drifting down",
        "up fast":     "climbing quickly",
        "up slowly":   "drifting up",
        "steady":      "holding steady",
        "recovering":  "recovering",
    }.get(s.going, s.going)

    st.markdown(
        '<div class="card">'
        '<div style="display:flex; gap:24px; align-items:flex-start; flex-wrap:wrap;">'
        # Sugar
        '<div style="display:flex; align-items:center; gap:12px;">'
        f'  {icons.drop(28, sev_color)}'
        f'  <span class="big-value" style="{sugar_style}">{cgm_txt}</span>'
        f'  <span class="small-caps" style="align-self:flex-end; margin-bottom:8px;">mg/dL</span>'
        '</div>'
        # Trend
        '<div style="border-left:1px solid #ECEFEE; padding-left:16px; min-width:220px;">'
        '<div class="small-caps">how it is going</div>'
        f'<div style="display:flex; align-items:center; gap:8px; margin-top:6px;">'
        f'  {going_icon}<span class="mid-value">{going_plain}</span>'
        '</div>'
        f'<div style="font-size:12px; color:#5C6A70; margin-top:4px;">'
        f'{s.evidence}</div>'
        '</div>'
        f'{forecast_html}'
        '</div>'
        '</div>',
        unsafe_allow_html=True,
    )


top_card_fragment()


# --------------------------------------------------------------------------
# Chart
# --------------------------------------------------------------------------
_WINDOW_MIN            = 60    # last 60 min of event-time
# Colour the model overlays the same way as scratch/forecast_check.py so the
# two artefacts read identically.
_COLOR_PERSISTENCE     = "#2874a6"
_COLOR_LINEAR_TREND    = "#b7950b"


def _build_cgm_chart(cgm: pd.DataFrame, preds: pd.DataFrame,
                     forecast: pd.DataFrame) -> alt.Chart:
    # Anchor the domain to THIS vp's latest reading, not wall-clock. Matches
    # what forecast_check.py does. Under live replay the two coincide; under
    # stale data (replay paused) wall-clock would leave the chart empty.
    if not cgm.empty:
        vp_max_et = pd.to_datetime(cgm["ts"]).max()
        if vp_max_et.tzinfo is None:
            vp_max_et = vp_max_et.tz_localize("UTC")
        vp_max_et = vp_max_et.to_pydatetime()
    else:
        vp_max_et = datetime.now(timezone.utc)
    x_lo = vp_max_et - timedelta(minutes=_WINDOW_MIN)
    x_hi = vp_max_et
    if not forecast.empty:
        forecast_ts_max = pd.to_datetime(forecast["ts"]).max()
        if forecast_ts_max.tzinfo is None:
            forecast_ts_max = forecast_ts_max.tz_localize("UTC")
        forecast_ts_max = forecast_ts_max.to_pydatetime()
        x_hi = max(x_hi, forecast_ts_max) + timedelta(minutes=2)
    x_scale = alt.Scale(domain=[x_lo, x_hi])
    y_scale = alt.Scale(domain=[40, 300])

    # Safe-range band (70-180). The only band — the pink too-low band was
    # removed to leave the model overlays visible against a quieter backdrop.
    band_df = pd.DataFrame([{"lo": 70, "hi": 180, "kind": "safe range"}])
    band_layer = alt.Chart(band_df).mark_rect(opacity=0.35, color="#DBEDE3").encode(
        y="lo:Q", y2="hi:Q",
    )
    layers = [band_layer]

    # Low line at 70 (dashed red-ish). The 180 and 40 reference lines were
    # removed to keep the chart free of clutter around the forecast overlays;
    # the safe band already encodes the upper edge.
    low_line = alt.Chart(pd.DataFrame([{"y": 70}])).mark_rule(
        color="#9EAAB0", strokeWidth=0.6, strokeDash=[2, 2],
    ).encode(y="y:Q")
    layers.append(low_line)

    # X-axis endpoint labels: two text marks. The right-edge label is
    # "latest reading" rather than "now" because the window is anchored to
    # the vp's own event-time (which may trail wall-clock under lag/pause).
    xlabel_df = pd.DataFrame([
        {"ts": x_lo,      "label": "60 min ago"},
        {"ts": vp_max_et, "label": "latest reading"},
    ])
    xlabels = alt.Chart(xlabel_df).mark_text(
        color="#5C6A70", fontSize=11, dy=155,
    ).encode(
        x=alt.X("ts:T", scale=x_scale),
        text="label:N",
    )
    layers.append(xlabels)

    # ---- Model overlays: thin lines + point markers over the historical
    # window. One series per model so they stack independently. Each row of
    # `preds` has (model, ts, cgm). We split by model so each gets its own
    # fixed colour (not an Altair legend colour-encoding) — the labelled
    # legend is handled by the caption + the right-column prediction card.
    if not preds.empty:
        for model_name, color in (
            ("persistence",   _COLOR_PERSISTENCE),
            ("linear_trend",  _COLOR_LINEAR_TREND),
        ):
            m = preds[preds["model"] == model_name]
            if m.empty:
                continue
            overlay_line = alt.Chart(m).mark_line(
                color=color, strokeWidth=1.0, opacity=0.85,
            ).encode(x="ts:T", y="cgm:Q")
            overlay_pts = alt.Chart(m).mark_point(
                shape="circle", size=18, filled=True, color=color, opacity=0.85,
            ).encode(x="ts:T", y="cgm:Q")
            layers.extend([overlay_line, overlay_pts])

    # ---- Actual CGM line (dark, heaviest weight) — drawn after the overlays
    # so it sits on top of them.
    if not cgm.empty:
        line_layer = alt.Chart(cgm).mark_line(
            color=TEXT_PRIMARY, strokeWidth=2.4,
        ).encode(
            x=alt.X("ts:T", title=None, scale=x_scale,
                    axis=alt.Axis(labels=False, ticks=False,
                                  domainColor="#D7DEDC", grid=False)),
            y=alt.Y("cgm:Q", title=None, scale=y_scale,
                    axis=alt.Axis(values=[40, 70, 180], labelColor="#5C6A70",
                                  tickColor="#D7DEDC", domainColor="#D7DEDC",
                                  grid=False)),
        )
        layers.append(line_layer)

        # First 70-cross (falling) marker — retained from the previous chart.
        below = cgm[cgm["cgm"] < 70].copy()
        above = cgm[cgm["cgm"] >= 70].copy()
        cross = None
        if not below.empty and not above.empty:
            for idx in below.index:
                prev = cgm.loc[cgm.index < idx].tail(1)
                if not prev.empty and prev.iloc[0]["cgm"] >= 70:
                    cross = cgm.loc[idx]
                    break
        if cross is not None:
            cross_df = pd.DataFrame([{"ts": cross["ts"], "cgm": cross["cgm"]}])
            cross_marker = alt.Chart(cross_df).mark_point(
                shape="circle", size=200, filled=False,
                color=WATCH, strokeWidth=2,
            ).encode(x="ts:T", y="cgm:Q")
            cross_label = alt.Chart(cross_df).mark_text(
                text="dropped below safe here", align="left", dx=10, dy=-6,
                color=WATCH_TEXT, fontSize=11,
            ).encode(x="ts:T", y="cgm:Q")
            layers.extend([cross_marker, cross_label])

        # Latest reading (filled dot) — the anchor the forward forecast line
        # extends from.
        latest = cgm.iloc[[-1]].copy()
        latest_marker = alt.Chart(latest).mark_point(
            shape="circle", size=120, filled=True, color=TEXT_PRIMARY,
        ).encode(x="ts:T", y="cgm:Q")
        layers.append(latest_marker)

    # ---- Forward prediction (dashed extension past the last reading).
    # Same semantics as before: two rows (anchor_reading, predicted_point)
    # plus an optional typical-error band.
    if not forecast.empty:
        fdash = alt.Chart(forecast).mark_line(
            color=EMERGENCY, strokeDash=[4, 3], strokeWidth=2,
        ).encode(x="ts:T", y="cgm:Q")
        if "lo" in forecast.columns and "hi" in forecast.columns \
                and forecast["lo"].notna().any():
            band = alt.Chart(forecast).mark_area(color=EMERGENCY, opacity=0.14).encode(
                x="ts:T", y="lo:Q", y2="hi:Q",
            )
            layers.append(band)
        layers.append(fdash)
        tip = forecast.iloc[[-1]].copy()
        tip_marker = alt.Chart(tip).mark_point(
            shape="circle", size=90, filled=False, color=EMERGENCY, strokeWidth=2,
        ).encode(x="ts:T", y="cgm:Q")
        layers.append(tip_marker)

    return alt.layer(*layers).properties(height=330).configure_view(strokeOpacity=0)


def _build_forecast_frame(cgm: pd.DataFrame, fx: pd.DataFrame,
                          typical_err: float | None) -> pd.DataFrame:
    """Shape the forecast rows the chart wants.

    The forecast is anchored to the reading it was made from
    (input_window_end), NOT to 'the latest reading'. Under lag those can
    be different — some readings may arrive before the ML pipeline has
    scored them. Drawing from the latest reading would show a line going
    sideways-or-backward, which is wrong.

    Returns two rows: (input_window_end, cgm_at_that_reading) → (predicted_ts_utc, predicted_cgm).
    If the actual CGM at input_window_end couldn't be read (very rare), we
    fall back to the latest cgm in the series so the chart still has an
    anchor to draw from.
    """
    if fx.empty:
        return pd.DataFrame(columns=["ts", "cgm", "lo", "hi"])
    f = fx.iloc[0]
    pred_cgm = float(f["cgm"]) if f["cgm"] is not None else None
    if pred_cgm is None:
        return pd.DataFrame(columns=["ts", "cgm", "lo", "hi"])
    # Anchor point: (from_ts, from_cgm) if the actual reading is available,
    # otherwise the latest cgm series point.
    from_ts = f["from_ts"]
    from_cgm = f.get("from_cgm")
    if from_cgm is None or pd.isna(from_cgm):
        if cgm.empty:
            return pd.DataFrame(columns=["ts", "cgm", "lo", "hi"])
        tail = cgm.iloc[-1]
        from_ts = tail["ts"]
        from_cgm = float(tail["cgm"])
    else:
        from_cgm = float(from_cgm)
    err = float(typical_err) if typical_err is not None else None
    pred_lo = pred_cgm - err if err is not None else None
    pred_hi = pred_cgm + err if err is not None else None
    return pd.DataFrame([
        {"ts": from_ts, "cgm": from_cgm, "lo": from_cgm, "hi": from_cgm},
        {"ts": f["ts"], "cgm": pred_cgm, "lo": pred_lo,  "hi": pred_hi},
    ])


@st.fragment(run_every="5s", parallel=True)
def chart_fragment() -> None:
    cgm   = _fetch_cgm_series(selected_vp, minutes=_WINDOW_MIN)
    if cgm.empty:
        st.markdown(
            '<div class="card"><h3>Waiting for the first readings</h3>'
            '<p>The chart draws itself as readings arrive.</p></div>',
            unsafe_allow_html=True,
        )
        return
    preds = _fetch_recent_predictions(selected_vp, minutes=_WINDOW_MIN)
    fx    = _fetch_latest_forecast(selected_vp)
    err_df = _fetch_recent_error(selected_vp)
    typical_err = None
    if not err_df.empty:
        r = err_df.iloc[0]
        if (r.get("n_mine") or 0) >= 20 and r.get("p50_mine") is not None:
            typical_err = float(r["p50_mine"])
        elif (r.get("n_global") or 0) >= 50 and r.get("p50_global") is not None:
            typical_err = float(r["p50_global"])
    forecast_df = _build_forecast_frame(cgm, fx, typical_err)

    # Chart card + axis label + plain-language caption. The axis label makes
    # the time-base explicit (event_time is the replay's own clock; under N×
    # replay the ticks are wall-clock-aligned at ingest but do NOT correspond
    # to the source patient's local day). The caption spells out what the
    # thin overlay lines actually mean.
    st.markdown('<div class="card" style="padding:12px 12px 8px 12px;">',
                unsafe_allow_html=True)
    st.altair_chart(_build_cgm_chart(cgm, preds, forecast_df), width='stretch')
    st.markdown(
        '<div style="font-size:11px; color:#8B9499; margin:2px 0 6px 0;">'
        'event time (UTC) — the replay\'s clock, not wall-clock'
        '</div>'
        '<div style="font-size:12px; color:#5C6A70; line-height:1.4;">'
        f'<span style="display:inline-block; width:10px; height:10px; '
        f'background:{_COLOR_PERSISTENCE}; border-radius:50%; margin-right:4px;"></span>'
        'persistence &nbsp;&nbsp;'
        f'<span style="display:inline-block; width:10px; height:10px; '
        f'background:{_COLOR_LINEAR_TREND}; border-radius:50%; margin-right:4px;"></span>'
        'linear_trend'
        '<br/>'
        'The thin lines show what each model expected for that moment, '
        '15 minutes earlier, so the gap between a thin line and the dark line '
        'is that model\'s error.'
        '</div>',
        unsafe_allow_html=True,
    )
    st.markdown('</div>', unsafe_allow_html=True)


chart_fragment()


# --------------------------------------------------------------------------
# HR / BP / O2 small cards
# --------------------------------------------------------------------------
def _sparkline(data: pd.DataFrame, x: str, y: str, color: str = TEXT_PRIMARY) -> alt.Chart:
    return alt.Chart(data).mark_line(color=color, strokeWidth=1.5).encode(
        x=alt.X(f"{x}:T", axis=None),
        y=alt.Y(f"{y}:Q", axis=None, scale=alt.Scale(zero=False)),
    ).properties(height=32).configure_view(strokeOpacity=0)


def _pulse_status(hr: float | None) -> tuple[str, str]:
    if hr is None:
        return ("no reading", TEXT_MUTED)
    if hr < 55:  return ("resting slow", WATCH_TEXT)
    if hr > 100: return ("running fast", EMERGENCY)
    return ("in a normal range", RECOVERING)


def _bp_status(sbp: float | None, dbp: float | None) -> tuple[str, str]:
    if sbp is None:
        return ("no reading", TEXT_MUTED)
    if sbp > 140 or (dbp is not None and dbp > 90):
        return ("higher than usual", WATCH_TEXT)
    if sbp < 90:
        return ("lower than usual", EMERGENCY)
    return ("in a normal range", RECOVERING)


def _spo2_status(v: float | None) -> tuple[str, str]:
    if v is None:
        return ("no reading", TEXT_MUTED)
    if v < 92: return ("low, keep watching", EMERGENCY)
    if v < 95: return ("a touch low", WATCH_TEXT)
    return ("plenty of oxygen", RECOVERING)


@st.fragment(run_every="5s", parallel=True)
def smallcards_fragment() -> None:
    df = _fetch_synth_series(selected_vp)
    if df.empty:
        st.markdown('<div class="card"><h4>heart, pressure, oxygen</h4>'
                    '<p>Nothing recorded in the last hour.</p></div>',
                    unsafe_allow_html=True)
        return
    latest = df.iloc[-1]
    hr = latest.get("hr");   sbp = latest.get("sbp");   dbp = latest.get("dbp");   spo2 = latest.get("spo2")
    hr_txt = f"{int(round(hr))}" if pd.notna(hr) else "—"
    bp_txt = (f"{int(round(sbp))}/{int(round(dbp))}" if pd.notna(sbp) and pd.notna(dbp)
              else "—")
    sp_txt = f"{spo2:.1f}" if pd.notna(spo2) else "—"

    hr_status, hr_color = _pulse_status(None if pd.isna(hr) else float(hr))
    bp_status, bp_color = _bp_status(None if pd.isna(sbp) else float(sbp),
                                     None if pd.isna(dbp) else float(dbp))
    sp_status, sp_color = _spo2_status(None if pd.isna(spo2) else float(spo2))

    c1, c2, c3 = st.columns(3, gap="small")
    with c1:
        st.markdown(
            '<div class="card" style="padding:14px;">'
            f'<div style="display:flex; gap:8px; align-items:center;">'
            f'  {icons.heart(18, TEXT_PRIMARY)}'
            f'  <span class="small-caps">heart rate</span></div>'
            f'<div style="margin-top:6px;"><span class="mono" '
            f'style="font-size:26px; font-weight:500;">{hr_txt}</span> '
            f'<span class="small-caps">bpm</span></div>'
            f'<div style="font-size:12px; color:{hr_color}; margin-bottom:6px;">{hr_status}</div>',
            unsafe_allow_html=True,
        )
        st.altair_chart(_sparkline(df.dropna(subset=["hr"]), "ts", "hr"), width='stretch')
        st.markdown('</div>', unsafe_allow_html=True)
    with c2:
        st.markdown(
            '<div class="card" style="padding:14px;">'
            f'<div style="display:flex; gap:8px; align-items:center;">'
            f'  {icons.gauge(18, TEXT_PRIMARY)}'
            f'  <span class="small-caps">blood pressure</span></div>'
            f'<div style="margin-top:6px;"><span class="mono" '
            f'style="font-size:26px; font-weight:500;">{bp_txt}</span> '
            f'<span class="small-caps">mmHg</span></div>'
            f'<div style="font-size:12px; color:{bp_color}; margin-bottom:6px;">{bp_status}</div>',
            unsafe_allow_html=True,
        )
        st.altair_chart(_sparkline(df.dropna(subset=["sbp"]), "ts", "sbp"), width='stretch')
        st.markdown('</div>', unsafe_allow_html=True)
    with c3:
        st.markdown(
            '<div class="card" style="padding:14px;">'
            f'<div style="display:flex; gap:8px; align-items:center;">'
            f'  {icons.lungs(18, TEXT_PRIMARY)}'
            f'  <span class="small-caps">oxygen in the blood</span></div>'
            f'<div style="margin-top:6px;"><span class="mono" '
            f'style="font-size:26px; font-weight:500;">{sp_txt}</span> '
            f'<span class="small-caps">%</span></div>'
            f'<div style="font-size:12px; color:{sp_color}; margin-bottom:6px;">{sp_status}</div>',
            unsafe_allow_html=True,
        )
        st.altair_chart(_sparkline(df.dropna(subset=["spo2"]), "ts", "spo2"), width='stretch')
        st.markdown('</div>', unsafe_allow_html=True)


# --------------------------------------------------------------------------
# Right column cards
# --------------------------------------------------------------------------
def _payload(row: pd.Series) -> dict:
    """Coerce payload_json to a plain dict for safe .get() access."""
    p = row.get("payload_json")
    if p is None:
        return {}
    if isinstance(p, str):
        import json
        try:
            p = json.loads(p)
        except Exception:
            return {}
    return p if isinstance(p, dict) else {}


def _verdict_reasons(row_df: pd.DataFrame) -> tuple[str, list[str]]:
    """Return (verdict headline, list of plain-language reasons).

    Sensor-verdict reasons include the actual payload numbers that triggered
    the decision — the operator should see the receipts, not a generic line.
    """
    if row_df.empty:
        return ("Not enough readings yet", ["Waiting for the first minute of data."])
    row = row_df.iloc[0]
    s = build_situation(row)
    rule = row["rule_id"]
    p = _payload(row)

    def _f(key, default=None):
        v = p.get(key, default)
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    if s.verdict_label == "Yes, real":
        reasons = []
        cgm = None if pd.isna(row.get("cgm_now")) else float(row["cgm_now"])
        hr  = None if pd.isna(row.get("hr_now"))  else float(row["hr_now"])
        if rule == "cross_signal.hypo_confirmed_by_hr" and cgm is not None and hr is not None:
            reasons.append(f"Blood sugar at {cgm:.0f} mg/dL with heart rate at "
                           f"{hr:.0f} bpm — the two agree.")
        elif rule == "rules.hypo_threshold" and cgm is not None:
            reasons.append(f"Blood sugar is {cgm:.0f} mg/dL, below the safe floor of 70.")
            if hr is not None and hr >= 100:
                reasons.append(f"Heart rate is {hr:.0f} bpm — the body's stress response.")
        elif rule == "rules.hyper_threshold" and cgm is not None:
            reasons.append(f"Blood sugar is {cgm:.0f} mg/dL, above the safe ceiling of 180.")
        if row.get("had_recent_hypo") and rule != "cross_signal.hypo_confirmed_by_hr":
            reasons.append("A low reading has been building for the last few minutes.")
        if not reasons:
            reasons.append("Cross-checks all point the same way.")
        return ("Yes, this reading is real", reasons)

    if s.verdict_label == "No, bad sensor":
        reasons = []
        if rule == "rules.cgm_null":
            reasons.append("The monitor sent nothing at all — an empty reading.")
        elif rule == "rules.cgm_stuck":
            v = _f("cgm_mgdl"); n = p.get("consecutive")
            v_txt = f"{v:.0f} mg/dL" if v is not None else "the same value"
            if isinstance(n, (int, float)):
                reasons.append(f"Held at {v_txt} for {int(n)} readings in a row "
                               f"without any wobble.")
            else:
                reasons.append(f"Held at {v_txt} without any wobble.")
        elif rule == "rules.cgm_roc_spike":
            prior = _f("prior_cgm"); spike = _f("spike_cgm"); after = _f("next_cgm")
            jump  = _f("jump_mgdl")
            if prior is not None and spike is not None and after is not None:
                jump_i = int(round(abs(jump))) if jump is not None else int(round(spike - prior))
                reasons.append(f"Jumped {jump_i} points in one reading "
                               f"(from {prior:.0f} to {spike:.0f}), then dropped back to "
                               f"{after:.0f} — real blood sugar cannot move that fast.")
            else:
                reasons.append("The value jumped, then the next reading came straight back — "
                               "real blood sugar doesn't do that.")
        elif rule == "rules.cgm_out_of_range":
            v = _f("cgm_mgdl"); rng = p.get("range")
            if v is not None and isinstance(rng, list) and len(rng) == 2:
                reasons.append(f"Reported {v:.0f} mg/dL — outside the physical range "
                               f"of {int(rng[0])} to {int(rng[1])} that a real reading can fall in.")
            else:
                reasons.append("The value is beyond what the human body can reach.")
        elif rule == "cross_signal.mad_hr_sensor_error":
            cgm = _f("cgm"); hr = _f("hr"); z = _f("mad_z")
            if cgm is not None and hr is not None and z is not None:
                reasons.append(f"Blood sugar swung {abs(z):.1f}× its usual jitter "
                               f"(to {cgm:.0f} mg/dL) while the pulse stayed at "
                               f"{hr:.0f} bpm.")
            else:
                reasons.append("The pulse stayed calm — a real drop or spike would show in it.")
        elif rule == "batch_analysis.firmware_offset" or row.get("detector") == "batch_analysis":
            dev = _f("deviation_mgdl"); n_batch = p.get("n_readings_in_batch")
            pts = int(round(abs(dev))) if dev is not None else None
            direction = "high" if (dev or 0) > 0 else "low"
            if pts is not None and isinstance(n_batch, (int, float)):
                reasons.append(f"Other monitors in the same batch (about {int(n_batch)} "
                               f"recent readings) are reading {pts} mg/dL too {direction}.")
            elif pts is not None:
                reasons.append(f"Other monitors in the same batch are reading {pts} "
                               f"mg/dL too {direction}.")
            else:
                reasons.append("Other monitors in the same batch are drifting the same way.")
        if not reasons:
            reasons.append("Multiple checks agree this looks like a sensor issue.")
        return ("No, this is the sensor", reasons)

    if s.verdict_label == "Checking device":
        return ("Still checking the device",
                ["This person's monitor is under review. "
                 "We need a two-minute window before we can compare batches confidently."])
    return ("Not sure yet",
            ["The reading is outside the safe range.",
             "Cross-checks are still gathering evidence."])


@st.fragment(run_every="5s", parallel=True)
def verdict_card() -> None:
    df = _fetch_top(selected_vp)
    headline, reasons = _verdict_reasons(df)
    items = "".join(
        f'<li>{icons.check(14, RECOVERING)} '
        f'<span style="font-size:13px; color:{TEXT_SECONDARY};">{r}</span></li>'
        for r in reasons
    )
    st.markdown(
        '<div class="card">'
        '<h4>can we believe this reading</h4>'
        f'<h3>{headline}</h3>'
        f'<ul class="checklist">{items}</ul>'
        '</div>',
        unsafe_allow_html=True,
    )


@st.fragment(run_every="30s", parallel=True)
def events_card() -> None:
    ev = _fetch_events(selected_vp)
    if ev.empty:
        st.markdown(
            '<div class="card">'
            '<h4>what happened today</h4>'
            '<p>No insulin doses or meals recorded in the source trace '
            'for the visible window.</p>'
            '</div>', unsafe_allow_html=True,
        )
        return
    rows = []
    for _, r in ev.iterrows():
        ts = r["ts"]
        try:
            hhmm = ts.strftime("%-I:%M %p")
        except (ValueError, TypeError):
            hhmm = ts.strftime("%H:%M")
        if pd.notna(r.get("bolus_u")) and r["bolus_u"] and float(r["bolus_u"]) > 0:
            rows.append(f'<div style="display:flex; gap:12px;">'
                        f'<span class="mono" style="min-width:60px; color:{TEXT_MUTED};">{hhmm}</span>'
                        f'<span style="font-size:13px;">Insulin dose, {float(r["bolus_u"]):.1f} units</span></div>')
        if pd.notna(r.get("carb_g")) and r["carb_g"] and float(r["carb_g"]) > 0:
            rows.append(f'<div style="display:flex; gap:12px;">'
                        f'<span class="mono" style="min-width:60px; color:{TEXT_MUTED};">{hhmm}</span>'
                        f'<span style="font-size:13px;">Ate {int(round(float(r["carb_g"])))} grams of carbohydrate</span></div>')
    if not rows:
        st.markdown(
            '<div class="card">'
            '<h4>what happened today</h4>'
            '<p>No insulin doses or meals recorded in the source trace '
            'for the visible window.</p>'
            '</div>', unsafe_allow_html=True,
        )
        return
    st.markdown(
        '<div class="card">'
        '<h4>what happened today</h4>'
        + '<div style="display:flex; flex-direction:column; gap:8px;">'
        + "".join(rows)
        + '</div></div>',
        unsafe_allow_html=True,
    )


def _val_or_pending(text: str, muted: bool = False) -> str:
    """Right-aligned grid cell with mono font."""
    cls = "mono t-muted" if muted else "mono"
    return f'<span class="{cls}" style="text-align:right;">{text}</span>'


@st.fragment(run_every="10s", parallel=True)
def prediction_card() -> None:
    """Fill the three rows with real numbers in plain English.

    Row 1 — typical error.  "About N mg/dL off, half the time" using the
            person's own resolved predictions (or the fleet's recent ones
            if the person hasn't accumulated enough).
    Row 2 — how it compares with the simple last-value guess.
            The dashboard uses persistence, which IS the last-value guess.
            Row shows how much the next-best model would cost per guess.
    Row 3 — readings on file (what the system has gathered for this person).
    """
    err_df = _fetch_recent_error(selected_vp)
    board  = _fetch_model_leaderboard()
    nread  = _fetch_vp_reading_count(selected_vp)

    # ---- row 1
    row1 = "<em>still gathering readings</em>"
    err_scope = ""
    if not err_df.empty:
        r = err_df.iloc[0]
        if (r.get("n_mine") or 0) >= 20 and r.get("p50_mine") is not None:
            row1 = f"about {int(round(float(r['p50_mine'])))} mg/dL off, half the time"
            err_scope = f"from this person's last {int(r['n_mine'])} resolved guesses"
        elif (r.get("n_global") or 0) >= 50 and r.get("p50_global") is not None:
            row1 = f"about {int(round(float(r['p50_global'])))} mg/dL off, half the time"
            err_scope = f"fleet-wide ({int(r['n_global'])} recent resolved guesses)"

    # ---- row 2: compare persistence (what we show) with the next-best other model
    row2 = "<em>no comparison yet</em>"
    if not board.empty:
        persistence_rows = board[board["model_name"] == MODEL_NAME]
        others           = board[board["model_name"] != MODEL_NAME]
        if not persistence_rows.empty and not others.empty:
            p_err = float(persistence_rows.iloc[0]["p50"])
            o_err = float(others.iloc[0]["p50"])      # others is already sorted asc
            o_name = str(others.iloc[0]["model_name"])
            name_plain = {
                "linear_trend": "a straight-line trend",
                "river_linreg": "an online linear model",
                "river_ht":     "an online decision tree",
            }.get(o_name, o_name)
            diff = o_err - p_err
            if diff > 0.5:
                row2 = (f"this simple guess beats {name_plain} by "
                        f"{int(round(diff))} mg/dL per prediction")
            elif diff < -0.5:
                row2 = (f"{name_plain} is {int(round(-diff))} mg/dL per prediction "
                        f"better; the simple guess is still what's shown")
            else:
                row2 = (f"{name_plain} is within 1 mg/dL of this simple guess — "
                        f"close call")

    # ---- row 3: readings on file
    n_readings = int(nread.iloc[0]["n"]) if not nread.empty else 0
    row3 = f"{n_readings:,} readings on file" if n_readings else "no readings yet"

    st.markdown(
        '<div class="card">'
        '<h4>the prediction</h4>'
        '<p>The forecast uses the simplest honest rule: "the next reading will '
        'be the same as this one." It looks 15 minutes ahead of the reading it '
        'was made from, not 15 minutes from the wall clock. Three other models '
        'run in parallel and the system shows whichever is winning on recent '
        'accuracy.</p>'
        '<div style="display:grid; grid-template-columns:1fr auto; gap:8px 12px; '
        'margin-top:10px; font-size:13px; color:#5C6A70;">'
        f'<span>Typical error, lately</span>{_val_or_pending(row1)}'
        f'<span>vs. a fancier model</span>{_val_or_pending(row2)}'
        f'<span>What the system has gathered</span>{_val_or_pending(row3)}'
        '</div>'
        + (f'<div style="font-size:11px; color:#8B9499; margin-top:8px;">'
           f'Typical-error source: {err_scope}.</div>' if err_scope else '')
        + '</div>',
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------
# Layout: main column has chart + small cards, side has 3 cards
# --------------------------------------------------------------------------
main_col, side_col = st.columns([3.3, 1], gap="medium")

with main_col:
    smallcards_fragment()

with side_col:
    verdict_card()
    events_card()
    prediction_card()
