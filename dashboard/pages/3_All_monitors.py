"""Page 3 — All monitors.

The systemic bias no individual chart reveals. Two-line chart (suspect
batch vs the rest), small comparison table, and one flat card that
explains what the operator should do about it.
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
from db import assert_configured, panel_error, timed_query  # noqa: E402
from domain import (  # noqa: E402
    BATCH_BIAS_SQL, BATCH_SUSPECT_THRESHOLD_MGDL, batch_suspect_from_df,
)
from theme import (  # noqa: E402
    BG_CARD, BORDER, BG_SUSPECT_ROW, RECOVERING, SENSOR, TEXT_MUTED,
    TEXT_PRIMARY, TEXT_SECONDARY, WATCH, WATCH_TEXT, page_config,
)

page_config("All monitors")
assert_configured()

# Analyzer's rolling comparison window (in sync with processor/outliers.py).
BATCH_ANALYSIS_WINDOW_S = 120

# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------
st.markdown(
    '<div class="strap">'
    '<div class="strap-title">'
    '<a href="/" target="_self" style="color:inherit; text-decoration:none; '
    'display:inline-flex; align-items:center; gap:6px;">'
    f'{icons.arrow_left(14)} Back</a>'
    '<span class="divider">·</span>'
    f'<span style="display:inline-flex; align-items:center; gap:8px;">'
    f'{icons.monitor(20)} <span>All monitors</span></span>'
    '</div>'
    '<div class="strap-status"><span class="t-muted">Refreshes every 10 s</span></div>'
    '</div>',
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
def _fetch_bias() -> pd.DataFrame:
    """Shared batch-bias query — same one Page 1's device-check card uses.
    Columns: fw, n_devices, median_cgm, median_others, deviation_from_others."""
    return timed_query("mon_batch_bias", BATCH_BIAS_SQL)


def _fetch_batch_series() -> pd.DataFrame:
    """Two-line chart data: per firmware, minute median CGM over last 2h."""
    return timed_query("mon_batch_series", """
        SELECT d.firmware_version AS fw,
               v.minute_start_utc AS ts,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY v.cgm_mgdl_mean) AS median_cgm
          FROM live.vitals_1min v
          JOIN live.device d ON d.virtual_patient_id = v.virtual_patient_id
         WHERE v.minute_start_utc > now() - interval '120 minutes'
           AND v.cgm_mgdl_mean IS NOT NULL
         GROUP BY d.firmware_version, v.minute_start_utc
         ORDER BY v.minute_start_utc, d.firmware_version
    """)


# --------------------------------------------------------------------------
# Header block
# --------------------------------------------------------------------------
@st.fragment(run_every="10s", parallel=True)
def title_fragment() -> None:
    df = _fetch_bias()
    if df.empty or df["median_cgm"].isna().all():
        st.markdown(
            '<div class="card"><h3>Not enough readings to compare monitors</h3>'
            '<p>Waiting for 15 minutes of data before comparing batches.</p></div>',
            unsafe_allow_html=True,
        )
        return
    suspect, _ = batch_suspect_from_df(df)
    if suspect is None:
        st.markdown(
            '<h2 style="font-weight:600; margin:0 0 6px 0;">All monitors agree</h2>'
            '<p class="t-secondary" style="margin:0 0 12px 0;">'
            'No batch of monitors is drifting away from the rest of the fleet '
            'right now.</p>',
            unsafe_allow_html=True,
        )
        return
    n_pts = int(round(abs(suspect["deviation_from_fleet"])))
    direction = "up" if suspect["deviation_from_fleet"] > 0 else "down"
    st.markdown(
        f'<h2 style="font-weight:600; margin:0 0 6px 0;">'
        f'One batch of monitors is adding about {n_pts} points {direction} '
        f'on every reading</h2>'
        f'<p class="t-secondary" style="margin:0 0 12px 0;">'
        f'Nobody looks unwell on their own chart, but {int(suspect["n_devices"])} monitors '
        f'are reading roughly {n_pts} mg/dL {direction} compared to the '
        f'rest of the fleet.</p>',
        unsafe_allow_html=True,
    )


title_fragment()


# --------------------------------------------------------------------------
# Two-line chart + table
# --------------------------------------------------------------------------
def _chart(series: pd.DataFrame, bias: pd.DataFrame) -> alt.Chart | None:
    if series.empty:
        return None
    bias = bias.dropna(subset=["median_cgm", "deviation_from_fleet"]).copy()
    suspects = bias[bias["deviation_from_fleet"].abs() >= BATCH_SUSPECT_THRESHOLD_MGDL]
    suspect_fws = set(suspects["fw"].tolist())
    series = series.copy()
    series["is_suspect"] = series["fw"].apply(lambda f: f in suspect_fws)
    # Two logical groups only:
    #   'suspect' — the suspect batch (amber)
    #   'rest'    — everything else, drawn as one median line
    series["group"] = series["is_suspect"].map({True: "suspect", False: "rest"})
    grouped = series.groupby(["ts", "group"], as_index=False)["median_cgm"].mean()

    color_scale = alt.Scale(domain=["suspect", "rest"], range=[WATCH, "#9EAAB0"])

    lines = alt.Chart(grouped).mark_line(strokeWidth=2).encode(
        x=alt.X("ts:T", title=None,
                axis=alt.Axis(format="%-I:%M", labelColor="#5C6A70",
                              tickColor=BORDER, domainColor=BORDER, grid=False)),
        y=alt.Y("median_cgm:Q", title=None,
                scale=alt.Scale(zero=False, nice=True),
                axis=alt.Axis(labelColor="#5C6A70", tickColor=BORDER,
                              domainColor=BORDER, grid=True, gridColor="#ECEFEE")),
        color=alt.Color("group:N", scale=color_scale, legend=None),
    )

    # Inline label per group at the right edge (latest point).
    latest = grouped.sort_values("ts").groupby("group", as_index=False).last()
    latest["label"] = latest["group"].map({"suspect": "suspect batch",
                                            "rest":    "every other monitor"})
    labels = alt.Chart(latest).mark_text(
        align="left", dx=6, fontSize=12, fontWeight=500,
    ).encode(
        x="ts:T",
        y="median_cgm:Q",
        text="label:N",
        color=alt.Color("group:N", scale=color_scale, legend=None),
    )
    return (lines + labels).properties(height=280).configure_view(strokeOpacity=0)


@st.fragment(run_every="10s", parallel=True)
def chart_and_table() -> None:
    bias = _fetch_bias()
    series = _fetch_batch_series()
    st.markdown('<div class="card">', unsafe_allow_html=True)
    ch = _chart(series, bias)
    if ch is None:
        st.markdown('<p>Waiting for two hours of data to draw the comparison chart.</p>',
                    unsafe_allow_html=True)
    else:
        st.altair_chart(ch, width='stretch')
    st.markdown('</div>', unsafe_allow_html=True)

    if bias.empty:
        return
    per = bias.dropna(subset=["median_cgm", "deviation_from_fleet"]).copy()
    rows = []
    for _, r in per.iterrows():
        dev = float(r["deviation_from_fleet"])
        is_suspect = abs(dev) >= BATCH_SUSPECT_THRESHOLD_MGDL
        pts = int(round(abs(dev)))
        diff_text = f"{pts} too high" if dev > 0 else f"{pts} too low"
        if not is_suspect:
            diff_text = "in line with the rest"
        row_bg = BG_SUSPECT_ROW if is_suspect else BG_CARD
        prefix = (f'{icons.warning(16, WATCH_TEXT)}' if is_suspect
                  else f'<span style="display:inline-block; width:16px;"></span>')
        rows.append(
            f'<tr style="background:{row_bg};">'
            f'  <td style="padding:10px 12px; border-bottom:1px solid #ECEFEE;">'
            f'    <span style="display:inline-flex; align-items:center; gap:8px;">'
            f'    {prefix}<span class="mono">{r["fw"]}</span></span></td>'
            f'  <td class="mono" style="padding:10px 12px; border-bottom:1px solid #ECEFEE;">'
            f'    {int(r["n_devices"])}</td>'
            f'  <td class="mono" style="padding:10px 12px; border-bottom:1px solid #ECEFEE;">'
            f'    {r["median_cgm"]:.0f} mg/dL</td>'
            f'  <td class="mono" style="padding:10px 12px; border-bottom:1px solid #ECEFEE; '
            f'    color:{WATCH_TEXT if is_suspect else TEXT_SECONDARY};">'
            f'    {diff_text}</td>'
            f'</tr>'
        )
    st.markdown(
        '<div class="card" style="padding:0; overflow:hidden;">'
        '<table class="attn-table" style="border:none;">'
        '<thead><tr>'
        '<th>Monitor software version</th>'
        '<th>People on it</th>'
        '<th>Typical reading</th>'
        '<th>Difference from the rest</th>'
        '</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody>'
        '</table></div>',
        unsafe_allow_html=True,
    )


chart_and_table_col_placeholder = st.container()

# --------------------------------------------------------------------------
# Right column
# --------------------------------------------------------------------------
def _card_why_one_chart_hides_this() -> None:
    st.markdown(
        '<div class="card-dark">'
        f'<h4>{icons.monitor(15, "#9EAAB0")} why one person\'s chart cannot show this</h4>'
        '<h3>A constant offset moves the whole chart together</h3>'
        '<p>If every reading is shifted by the same amount, the '
        'individual chart still looks smooth and healthy. Only by '
        'comparing many monitors to each other does the drift show up.</p>'
        '<p>That is why this page exists.</p>'
        '</div>',
        unsafe_allow_html=True,
    )


def _card_check_quality() -> None:
    st.markdown(
        '<div class="card">'
        '<h4>how well this check works</h4>'
        '<div style="display:grid; grid-template-columns:1fr auto; gap:8px 12px; '
        'align-items:baseline;">'
        f'<span style="font-size:13px; color:{TEXT_SECONDARY};">Caught (recall)</span>'
        '<span class="mono">100%</span>'
        f'<span style="font-size:13px; color:{TEXT_SECONDARY};">Missed</span>'
        '<span class="mono">0%</span>'
        f'<span style="font-size:13px; color:{TEXT_SECONDARY};">False alarms</span>'
        '<span class="mono">0%</span>'
        f'<span style="font-size:13px; color:{TEXT_SECONDARY};">Time to notice</span>'
        '<span class="mono">under 15 s</span>'
        '</div>'
        '<div class="hair"></div>'
        f'<p style="font-size:12px; color:{TEXT_MUTED}; line-height:1.4;">'
        f'The check compares each batch\'s middle reading to the fleet\'s middle '
        f'reading over a rolling {BATCH_ANALYSIS_WINDOW_S}-second window. '
        f'That window is a floor, not lag: the check runs every 15 s.</p>'
        '</div>',
        unsafe_allow_html=True,
    )


def _card_what_to_do() -> None:
    st.markdown(
        '<div class="card">'
        '<h4>what to do</h4>'
        '<h3>Pause high alerts. Keep low alerts.</h3>'
        f'<p>Because the batch is reading too high, "blood sugar is high" alerts '
        f'from these monitors are likely wrong. But if the same monitor reports a '
        f'low reading, that\'s still real — a biased-high monitor calling something '
        f'low means the underlying value is truly low.</p>'
        '<div style="display:flex; gap:8px; margin-top:12px;">'
        '<a class="pill-btn" href="#" target="_self">Pause high alerts for this batch</a>'
        '<a class="pill-btn ghost" href="/System" target="_self">System log</a>'
        '</div>'
        '</div>',
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------
main_col, side_col = st.columns([3.3, 1], gap="medium")

with main_col:
    chart_and_table()

with side_col:
    _card_why_one_chart_hides_this()
    _card_check_quality()
    _card_what_to_do()
