"""Shared DB access + per-panel timing. Connects as dashboard_ro only.

Every SELECT the UI runs goes through timed_query(); ms elapsed is stored
in st.session_state so the System page can display the p95 the operator
actually experiences.
"""
from __future__ import annotations

import collections
import os
import time

import pandas as pd
import psycopg
import streamlit as st
from psycopg_pool import ConnectionPool

# Single source of truth for the dashboard's DB connection. On Azure App
# Service this is one App Setting; locally, export the same variable in your
# shell (see .env.example at the repo root). No hardcoded fallback — a missing
# or empty value is reported as a readable error, not a stack trace.
_ENV_VAR = "CAREVAULT_DASHBOARD_PG"

_POOL_MIN = 2
_POOL_MAX = 8            # 4 pages × 3-4 fragments × parallel=True
_PANEL_PERF_MAX = 200

_MISSING_CONN_MSG = (
    f"**Dashboard is not configured.**  \n"
    f"The `{_ENV_VAR}` environment variable is not set.  \n\n"
    f"- On Azure App Service: add it under *Settings → Environment variables*.  \n"
    f"- Locally: copy `.env.example` to `.env` and `export` the line, "
    f"or set it in your shell before `streamlit run`.  \n\n"
    f"Expected format: "
    f"`host=... port=5432 dbname=... user=dashboard_ro password=... sslmode=require`."
)


def assert_configured() -> None:
    """Call once at the top of each page (BEFORE any @st.fragment is defined
    or invoked). Renders one friendly error and halts the page if the required
    env var is missing. Kept outside the cached _pool() so st.stop() propagates
    cleanly instead of being swallowed by @st.cache_resource. timed_query()
    also calls it as a safety net."""
    v = os.environ.get(_ENV_VAR, "").strip()
    if not v:
        st.error(_MISSING_CONN_MSG)
        st.stop()


@st.cache_resource(show_spinner=False)
def _pool() -> ConnectionPool:
    # By the time this runs, assert_configured() has already verified the
    # env var; reading it directly here is safe.
    p = ConnectionPool(
        conninfo=os.environ[_ENV_VAR].strip(),
        min_size=_POOL_MIN,
        max_size=_POOL_MAX,
        kwargs={"autocommit": True, "row_factory": psycopg.rows.dict_row},
        name="carevault-dashboard",
        open=True,
    )
    p.wait(timeout=30.0)
    return p


def _record_perf(panel: str, ms: float) -> None:
    perf = st.session_state.setdefault("panel_perf", {})
    dq = perf.setdefault(panel, collections.deque(maxlen=_PANEL_PERF_MAX))
    dq.append((time.time(), ms))


def timed_query(panel: str, sql: str,
                params: tuple | list | dict | None = None) -> pd.DataFrame:
    assert_configured()
    pool = _pool()
    t0 = time.perf_counter()
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                rows = cur.fetchall()
                cols = [d.name for d in cur.description] if cur.description else []
        df = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
        errs = st.session_state.setdefault("panel_error", {})
        errs.pop(panel, None)
        return df
    except Exception as e:
        errs = st.session_state.setdefault("panel_error", {})
        errs[panel] = str(e)
        return pd.DataFrame()
    finally:
        _record_perf(panel, (time.perf_counter() - t0) * 1000.0)


def panel_error(panel: str) -> str | None:
    return st.session_state.get("panel_error", {}).get(panel)


def panel_perf_summary() -> pd.DataFrame:
    perf = st.session_state.get("panel_perf", {})
    if not perf:
        return pd.DataFrame(columns=["panel", "p50_ms", "p95_ms", "n_calls"])
    rows = []
    for panel, samples in perf.items():
        vals = sorted(v for _, v in samples)
        if not vals:
            continue
        n = len(vals)
        p50 = vals[int(n * 0.50)] if n > 1 else vals[0]
        p95 = vals[min(n - 1, int(n * 0.95))]
        rows.append({"panel": panel, "p50_ms": round(p50, 1),
                     "p95_ms": round(p95, 1), "n_calls": n})
    return pd.DataFrame(rows).sort_values("p95_ms", ascending=False)
