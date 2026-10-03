"""Pytest fixtures for the loader tests.

Runs the loader once per session against the local Postgres (see docker-compose.yml).
Skips the whole test module if Postgres is unreachable.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loader.load_raw import CONNINFO_DEFAULT, main as load_main  # noqa: E402


@pytest.fixture(scope="session")
def conninfo() -> str:
    return os.environ.get("CAREVAULT_PG", CONNINFO_DEFAULT)


@pytest.fixture(scope="session")
def db(conninfo: str):
    import psycopg
    try:
        conn = psycopg.connect(conninfo, connect_timeout=3)
    except Exception as e:
        pytest.skip(f"Postgres unreachable at {conninfo!r}: {e}")
    conn.autocommit = True
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def loaded(db, conninfo: str):
    """Run the loader once for the whole session. Returns the post-load row count."""
    rc = load_main(["--conninfo", conninfo])
    assert rc == 0
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw.readings_raw")
        return cur.fetchone()[0]
