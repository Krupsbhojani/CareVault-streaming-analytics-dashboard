"""Page 4 — System (technical).

Only the operations team sees this page. Content is deliberately technical;
the layout still uses the shared theme so nothing clashes visually.

Linked from the "how the system is running" card on Page 1.
"""
from __future__ import annotations

import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import icons  # noqa: E402
from db import assert_configured, panel_error, panel_perf_summary, timed_query  # noqa: E402
from theme import (  # noqa: E402
    BORDER, EMERGENCY, RECOVERING, SENSOR, TEXT_MUTED, TEXT_PRIMARY,
    TEXT_SECONDARY, WATCH, page_config,
)

page_config("System")
assert_configured()

LAG_BUDGET_SEC = 15

st.markdown(
    '<div class="strap">'
    '<div class="strap-title">'
    '<a href="/" target="_self" style="color:inherit; text-decoration:none; '
    'display:inline-flex; align-items:center; gap:6px;">'
    f'{icons.arrow_left(14)} Back</a>'
    '<span class="divider">·</span>'
    '<span>System</span>'
    '</div>'
    '<div class="strap-status"><span class="t-muted">operations team only</span></div>'
    '</div>',
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------
# Rows/min area chart
# --------------------------------------------------------------------------
@st.fragment(run_every="5s", parallel=True)
def rows_fragment() -> None:
    st.markdown('<h4 style="font-size:11px; font-weight:500; text-transform:uppercase; '
                'letter-spacing:1.6px; color:#5C6A70; margin:0 0 6px 0;">'
                'rows per minute (last 30 min)</h4>', unsafe_allow_html=True)
    df = timed_query("sys_rows", """
        SELECT minute_start_utc AS ts,
               sum(n_readings)  AS rows_in_minute
          FROM live.vitals_1min
         WHERE minute_start_utc > now() - interval '30 minutes'
         GROUP BY minute_start_utc
         ORDER BY minute_start_utc
    """)
    if df.empty:
        st.markdown('<p>No rows in the last 30 minutes.</p>', unsafe_allow_html=True)
        return
    chart = alt.Chart(df).mark_area(
        color=TEXT_PRIMARY, opacity=0.15,
        line=alt.OverlayMarkDef(color=TEXT_PRIMARY, strokeWidth=1.5),
    ).encode(
        x=alt.X("ts:T", title=None,
                axis=alt.Axis(labelColor="#5C6A70", tickColor=BORDER,
                              domainColor=BORDER, grid=False)),
        y=alt.Y("rows_in_minute:Q", title="rows / minute",
                axis=alt.Axis(labelColor="#5C6A70", tickColor=BORDER,
                              domainColor=BORDER, grid=True, gridColor="#ECEFEE")),
    ).properties(height=180).configure_view(strokeOpacity=0)
    with st.container(border=True):
        st.altair_chart(chart, width='stretch')


# --------------------------------------------------------------------------
# Worker lag + e2e latency
# --------------------------------------------------------------------------
_H4 = ('<h4 style="font-size:11px; font-weight:500; text-transform:uppercase; '
       'letter-spacing:1.6px; color:#5C6A70; margin:0 0 6px 0;">')

@st.fragment(run_every="5s", parallel=True)
def lag_and_latency_fragment() -> None:
    c1, c2 = st.columns(2, gap="medium")

    with c1:
        st.markdown(_H4 + 'worker lag (cursor age)</h4>', unsafe_allow_html=True)
        wc = timed_query("sys_cursor", """
            SELECT worker_id, last_ingested_at,
                   EXTRACT(EPOCH FROM (now() - last_ingested_at))::int AS age_sec
              FROM live.worker_cursor
             ORDER BY worker_id
        """)
        with st.container(border=True):
            if wc.empty:
                st.markdown('<p>No worker has committed a cursor yet.</p>',
                            unsafe_allow_html=True)
            else:
                for _, r in wc.iterrows():
                    age = int(r["age_sec"] or 0)
                    pct = min(100, int(100 * age / LAG_BUDGET_SEC)) if age else 0
                    color = (EMERGENCY if age > LAG_BUDGET_SEC
                             else WATCH if age > 0.66 * LAG_BUDGET_SEC
                             else RECOVERING)
                    st.markdown(
                        f'<div class="mono" style="font-size:13px;">{r["worker_id"]}</div>'
                        f'<div style="display:flex; justify-content:space-between; margin-top:2px;">'
                        f'  <span class="mono">{age}s</span>'
                        f'  <span class="mono t-muted">budget {LAG_BUDGET_SEC}s</span></div>'
                        f'<div style="background:#ECEFEE; height:8px; border-radius:2px; '
                        f'margin:6px 0; overflow:hidden;">'
                        f'  <div style="width:{pct}%; height:100%; background:{color};"></div></div>',
                        unsafe_allow_html=True,
                    )

    with c2:
        st.markdown(_H4 + 'e2e latency: ingested − event, last 5 min</h4>',
                    unsafe_allow_html=True)
        lat = timed_query("sys_latency", """
            WITH tail AS (
              SELECT ingested_at, event_time
                FROM live.vitals_readings
               WHERE event_time > now() - interval '5 minutes'
               LIMIT 50000
            )
            SELECT
              percentile_cont(0.50) WITHIN GROUP
                (ORDER BY extract(epoch from (ingested_at - event_time))) AS p50,
              percentile_cont(0.95) WITHIN GROUP
                (ORDER BY extract(epoch from (ingested_at - event_time))) AS p95,
              percentile_cont(0.99) WITHIN GROUP
                (ORDER BY extract(epoch from (ingested_at - event_time))) AS p99,
              count(*) AS n
              FROM tail
        """)
        err = panel_error("sys_latency")
        with st.container(border=True):
            if err:
                st.markdown(f'<p style="color:{EMERGENCY};">query error: {err}</p>',
                            unsafe_allow_html=True)
            elif lat.empty or lat.iloc[0]["p50"] is None:
                st.markdown('<p>No readings in the window.</p>',
                            unsafe_allow_html=True)
            else:
                r = lat.iloc[0]
                st.markdown(
                    '<div class="mono" style="font-size:15px; line-height:1.8;">'
                    f'p50 <b>{r["p50"]:.2f} s</b> · '
                    f'p95 <b>{r["p95"]:.2f} s</b> · '
                    f'p99 <b>{r["p99"]:.2f} s</b></div>'
                    f'<div class="mono t-muted" style="font-size:12px;">'
                    f'n = {int(r["n"]):,} rows</div>',
                    unsafe_allow_html=True,
                )


# --------------------------------------------------------------------------
# Dead letter + partitions
# --------------------------------------------------------------------------
@st.fragment(run_every="15s", parallel=True)
def dead_and_parts_fragment() -> None:
    c1, c2 = st.columns(2, gap="medium")

    with c1:
        st.markdown(_H4 + 'dead letter (last 15 min)</h4>', unsafe_allow_html=True)
        dl = timed_query("sys_dead", """
            SELECT count(*) AS n_dead
              FROM live.dead_letter
             WHERE received_at > now() - interval '15 minutes'
        """)
        rows15 = timed_query("sys_rows15", """
            SELECT sum(n_readings) AS n FROM live.vitals_1min
             WHERE minute_start_utc > now() - interval '15 minutes'
        """)
        reasons = timed_query("sys_dead_reasons", """
            SELECT reason, count(*) AS n FROM live.dead_letter
             WHERE received_at > now() - interval '15 minutes'
             GROUP BY reason ORDER BY n DESC LIMIT 5
        """)
        n = int(dl.iloc[0]["n_dead"] or 0) if not dl.empty else 0
        total = int(rows15.iloc[0]["n"] or 0) if not rows15.empty else 0
        pct = (n / total * 100.0) if total else 0.0
        reason_txt = (", ".join(f"{r['reason']} ({r['n']})"
                                for _, r in reasons.iterrows())
                      or "—")
        with st.container(border=True):
            st.markdown(
                f'<div class="mono" style="font-size:15px;">{n:,} of {total:,} rows '
                f'(<b>{pct:.3f}%</b>)</div>'
                f'<div class="mono t-muted" style="font-size:12px; margin-top:6px;">'
                f'{reason_txt}</div>',
                unsafe_allow_html=True,
            )

    with c2:
        st.markdown(_H4 + 'partition sizes (live.vitals_readings)</h4>',
                    unsafe_allow_html=True)
        parts = timed_query("sys_partitions", """
            SELECT c.relname                     AS partition,
                   pg_size_pretty(pg_total_relation_size(c.oid)) AS size,
                   s.n_live_tup                  AS n_live_tup
              FROM pg_inherits i
              JOIN pg_class parent ON parent.oid = i.inhparent
              JOIN pg_namespace pn ON pn.oid = parent.relnamespace
              JOIN pg_class c      ON c.oid  = i.inhrelid
              LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
             WHERE pn.nspname = 'live' AND parent.relname = 'vitals_readings'
             ORDER BY c.relname DESC LIMIT 8
        """)
        with st.container(border=True):
            if parts.empty:
                st.markdown('<p>No partitions.</p>', unsafe_allow_html=True)
            else:
                body = "".join(
                    f'<tr><td class="mono" style="padding:6px 8px;">{r["partition"]}</td>'
                    f'<td class="mono" style="padding:6px 8px; text-align:right;">{r["size"]}</td>'
                    f'<td class="mono" style="padding:6px 8px; text-align:right; color:{TEXT_MUTED};">'
                    f'{int(r["n_live_tup"] or 0):,}</td></tr>'
                    for _, r in parts.iterrows()
                )
                st.markdown(
                    f'<table style="width:100%; border-collapse:collapse; font-size:13px;">'
                    f'<thead><tr>'
                    f'<th style="text-align:left; padding:6px 8px; color:{TEXT_MUTED}; '
                    f'font-size:11px; text-transform:uppercase; letter-spacing:1px;">partition</th>'
                    f'<th style="text-align:right; padding:6px 8px; color:{TEXT_MUTED}; '
                    f'font-size:11px; text-transform:uppercase; letter-spacing:1px;">size</th>'
                    f'<th style="text-align:right; padding:6px 8px; color:{TEXT_MUTED}; '
                    f'font-size:11px; text-transform:uppercase; letter-spacing:1px;">live rows</th>'
                    f'</tr></thead><tbody>{body}</tbody></table>',
                    unsafe_allow_html=True,
                )


# --------------------------------------------------------------------------
# Panel perf
# --------------------------------------------------------------------------
@st.fragment(run_every="5s", parallel=True)
def perf_fragment() -> None:
    st.markdown(_H4 + 'slowest dashboard queries (this session)</h4>',
                unsafe_allow_html=True)
    df = panel_perf_summary()
    with st.container(border=True):
        if df.empty:
            st.markdown('<p>No panel timings yet — visit another page and return.</p>',
                        unsafe_allow_html=True)
            return
        body = "".join(
            f'<tr><td class="mono" style="padding:6px 8px;">{r["panel"]}</td>'
            f'<td class="mono" style="padding:6px 8px; text-align:right;">{r["p50_ms"]}</td>'
            f'<td class="mono" style="padding:6px 8px; text-align:right;">{r["p95_ms"]}</td>'
            f'<td class="mono" style="padding:6px 8px; text-align:right; color:{TEXT_MUTED};">'
            f'{int(r["n_calls"])}</td></tr>'
            for _, r in df.head(25).iterrows()
        )
        st.markdown(
            '<table style="width:100%; border-collapse:collapse; font-size:13px;">'
            '<thead><tr>'
            f'<th style="text-align:left; padding:6px 8px; color:{TEXT_MUTED}; '
            'font-size:11px; text-transform:uppercase; letter-spacing:1px;">panel</th>'
            f'<th style="text-align:right; padding:6px 8px; color:{TEXT_MUTED}; '
            'font-size:11px; text-transform:uppercase; letter-spacing:1px;">p50 ms</th>'
            f'<th style="text-align:right; padding:6px 8px; color:{TEXT_MUTED}; '
            'font-size:11px; text-transform:uppercase; letter-spacing:1px;">p95 ms</th>'
            f'<th style="text-align:right; padding:6px 8px; color:{TEXT_MUTED}; '
            'font-size:11px; text-transform:uppercase; letter-spacing:1px;">calls</th>'
            f'</tr></thead><tbody>{body}</tbody></table>',
            unsafe_allow_html=True,
        )


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------
rows_fragment()
lag_and_latency_fragment()
dead_and_parts_fragment()
perf_fragment()
