"""Design tokens + font loading + global CSS.

Exact tokens from the approved design brief. Colour encodes severity or
verdict only. No decoration, no gradients, no shadows.
"""
from __future__ import annotations

import streamlit as st

# ---- surface tokens ----
BG_PAGE       = "#EEF1F0"
BG_CARD       = "#FFFFFF"
BG_DARK       = "#10161A"
BORDER        = "#D7DEDC"

# ---- text tokens ----
TEXT_PRIMARY   = "#10161A"
TEXT_SECONDARY = "#3F4E55"
TEXT_MUTED     = "#5C6A70"
TEXT_ON_DARK   = "#FFFFFF"

# ---- semantic tokens (severity + verdict) ----
EMERGENCY      = "#A8102F"   # confirmed clinical emergency
WATCH          = "#C97A12"   # value outside range but not emergency
WATCH_TEXT     = "#8A4B06"   # readable-on-light variant of WATCH for body text
SENSOR         = "#3F3B96"   # confirmed sensor / firmware fault
RECOVERING     = "#1F7A5C"   # returning to safe range

# ---- row tint for emergency rows ----
BG_EMERGENCY_ROW = "#FDF6F7"
BG_SUSPECT_ROW   = "#FDF8EE"    # warning-triangle row on Page 3

# ---- refresh cadences (seconds) — from the brief ----
CHECK_BUDGET_SEC = 1            # "checked within 1 second"

# ---- font stack ----
FONT_SANS = "'IBM Plex Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
FONT_MONO = "'IBM Plex Mono', ui-monospace, 'SF Mono', Menlo, Consolas, monospace"

_CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap');

/* Streamlit chrome ------------------------------------------------------- */
html, body, [class*="css"] {{
    font-family: {FONT_SANS};
    color: {TEXT_PRIMARY};
    background: {BG_PAGE};
}}
[data-testid="stAppViewContainer"] {{ background: {BG_PAGE}; }}
[data-testid="stHeader"] {{ background: {BG_PAGE}; }}
section.main > div.block-container {{
    padding-top: 1rem;
    padding-bottom: 1rem;
    max-width: 1440px;
}}

/* mono utility */
.mono, code {{
    font-family: {FONT_MONO};
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.01em;
}}

/* card primitives -------------------------------------------------------- */
.card {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: 2px;
    padding: 16px 18px;
    margin-bottom: 12px;
}}
.card-dark {{
    background: {BG_DARK};
    color: {TEXT_ON_DARK};
    border: 1px solid {BG_DARK};
    border-radius: 2px;
    padding: 16px 18px;
    margin-bottom: 12px;
}}
.card-dark a, .card-dark a:hover {{ color: #A9D8C7; text-decoration: underline; }}
.card h4, .card-dark h4 {{
    font-size: 11px;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 1.6px;
    color: {TEXT_MUTED};
    margin: 0 0 10px 0;
}}
.card-dark h4 {{ color: #9EAAB0; }}
.card h3, .card-dark h3 {{
    font-size: 18px;
    font-weight: 600;
    margin: 0 0 6px 0;
}}
.card p, .card-dark p {{
    font-size: 14px;
    color: {TEXT_SECONDARY};
    margin: 0 0 4px 0;
    line-height: 1.42;
}}
.card-dark p {{ color: #C7CFD4; }}

/* semantic text colours */
.t-emergency  {{ color: {EMERGENCY}; }}
.t-watch      {{ color: {WATCH_TEXT}; }}
.t-sensor     {{ color: {SENSOR}; }}
.t-recovering {{ color: {RECOVERING}; }}
.t-muted      {{ color: {TEXT_MUTED}; }}
.t-secondary  {{ color: {TEXT_SECONDARY}; }}

/* header strap ----------------------------------------------------------- */
.strap {{
    display: flex; align-items: center; justify-content: space-between;
    padding: 6px 0 14px 0;
    border-bottom: 1px solid {BORDER};
    margin-bottom: 18px;
}}
.strap-title {{ display: flex; align-items: center; gap: 10px; font-size: 15px; }}
.strap-title .brand {{ font-weight: 600; }}
.strap-title .divider {{ color: {TEXT_MUTED}; }}
.strap-status {{
    display: flex; align-items: center; gap: 14px;
    font-family: {FONT_MONO}; font-size: 12px; color: {TEXT_SECONDARY};
}}
.dot-live {{
    display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    background: {RECOVERING};
}}

/* filter buttons --------------------------------------------------------- */
.filter-row {{ display: flex; gap: 8px; margin: 8px 0 14px 0; }}
.filter-btn {{
    display: inline-block;
    padding: 6px 12px;
    border: 1px solid {BORDER};
    background: {BG_CARD};
    color: {TEXT_SECONDARY};
    font-size: 13px;
    cursor: pointer;
    border-radius: 2px;
    text-decoration: none;
}}
.filter-btn.active {{
    background: {BG_DARK};
    color: {TEXT_ON_DARK};
    border-color: {BG_DARK};
}}
.filter-btn .count {{
    margin-left: 6px;
    font-family: {FONT_MONO};
    opacity: 0.7;
}}
.filter-btn.active .count {{ opacity: 1; }}

/* attention table ------------------------------------------------------- */
.attn-table {{
    width: 100%;
    border-collapse: collapse;
    background: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: 2px;
    overflow: hidden;
}}
.attn-table th {{
    text-align: left;
    padding: 10px 12px;
    font-size: 11px;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 1.4px;
    color: {TEXT_MUTED};
    border-bottom: 1px solid {BORDER};
}}
.attn-table td {{
    padding: 10px 12px;
    vertical-align: middle;
    border-bottom: 1px solid #ECEFEE;
    color: {TEXT_PRIMARY};
    font-size: 14px;
}}
.attn-table tr:last-child td {{ border-bottom: none; }}
.attn-table td.sev-bar {{
    padding: 0; width: 5px; height: 100%;
}}
.attn-table td .person-name {{ font-family: {FONT_MONO}; font-size: 14px; color: {TEXT_PRIMARY}; }}
.attn-table td .person-ctx  {{ font-size: 12px; color: {TEXT_MUTED}; }}
.attn-table td.sugar {{ font-family: {FONT_MONO}; font-size: 27px; font-weight: 500; }}
.attn-table td.sugar.strike {{ text-decoration: line-through; text-decoration-thickness: 2px; }}
.attn-table td .headline {{ font-size: 14px; color: {TEXT_PRIMARY}; line-height: 1.35; }}
.attn-table td .evidence {{ font-size: 12px; color: {TEXT_MUTED}; line-height: 1.35; margin-top: 2px; }}
.attn-table tr.row-emergency {{ background: {BG_EMERGENCY_ROW}; }}
.attn-table .going {{ display: inline-flex; align-items: center; gap: 6px; }}
.attn-table .age {{ font-family: {FONT_MONO}; font-size: 13px; color: {TEXT_SECONDARY}; }}

/* verdict chips (Page 1 verdict column) --------------------------------- */
.chip {{
    display: inline-block;
    padding: 3px 10px;
    border-radius: 2px;
    font-size: 12px;
    font-weight: 500;
    line-height: 1.4;
    letter-spacing: 0.02em;
    border: 1px solid transparent;
    white-space: nowrap;
}}
.chip-real     {{ background: {BG_DARK};  color: {TEXT_ON_DARK}; }}
.chip-sensor   {{ background: {SENSOR};   color: {TEXT_ON_DARK}; }}
.chip-unsure   {{ background: {BG_CARD};  color: {TEXT_SECONDARY}; border-color: {BORDER}; }}
.chip-checking {{ background: {BG_CARD};  color: {TEXT_SECONDARY}; border-color: {BORDER}; }}

/* compare bars (device-check card) --------------------------------------- */
.bar-row {{ margin-bottom: 10px; }}
.bar-row .label {{ font-size: 12px; color: #9EAAB0; display: flex; justify-content: space-between; }}
.bar-track {{
    background: #22303A; height: 8px; border-radius: 2px; margin-top: 4px;
    overflow: hidden;
}}
.bar-fill.suspect  {{ background: {WATCH}; height: 8px; border-radius: 2px; }}
.bar-fill.baseline {{ background: #6E8B98; height: 8px; border-radius: 2px; }}

/* hairline separator inside cards */
.hair {{ border-top: 1px solid #ECEFEE; margin: 8px 0; }}
.card-dark .hair {{ border-top-color: #22303A; }}

/* one-person top card values */
.big-value {{ font-family: {FONT_MONO}; font-size: 52px; font-weight: 500; line-height: 1; }}
.mid-value {{ font-family: {FONT_MONO}; font-size: 26px; font-weight: 500; line-height: 1; }}
.small-caps {{ font-size: 11px; font-weight: 500; text-transform: uppercase; letter-spacing: 1.4px; color: {TEXT_MUTED}; }}

/* checklist */
.checklist li {{ list-style: none; padding: 3px 0; display: flex; gap: 8px; align-items: flex-start; }}
.checklist {{ padding-left: 0; }}
.checklist .check {{ color: {RECOVERING}; }}

/* buttons */
.pill-btn {{
    display: inline-block; padding: 8px 14px;
    border: 1px solid {BG_DARK}; background: {BG_DARK}; color: {TEXT_ON_DARK};
    font-size: 13px; text-decoration: none; border-radius: 2px;
    font-family: {FONT_SANS};
}}
.pill-btn.ghost {{ background: transparent; color: {TEXT_PRIMARY}; border-color: {BORDER}; }}

/* Streamlit multipage sidebar: keep tidy */
[data-testid="stSidebarNav"] ul li a {{ font-family: {FONT_SANS}; }}
[data-testid="stSidebarNav"] {{ padding-top: 8px; }}

/* Altair charts container */
[data-testid="stArrowVegaLiteChart"] {{ background: {BG_CARD}; }}
</style>
"""

def page_config(title: str) -> None:
    st.set_page_config(
        page_title=f"CareVault — {title}",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    st.markdown(_CSS, unsafe_allow_html=True)
