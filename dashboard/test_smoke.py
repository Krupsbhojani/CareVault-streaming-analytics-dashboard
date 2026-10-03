"""Smoke test: render each page via Streamlit AppTest, flag any exception."""
from __future__ import annotations

import time
from pathlib import Path

from streamlit.testing.v1 import AppTest

HERE = Path(__file__).resolve().parent
PAGES = [
    ("Attention",   HERE / "app.py"),
    ("One person",  HERE / "pages" / "2_One_person.py"),
    ("All monitors",HERE / "pages" / "3_All_monitors.py"),
    ("System",      HERE / "pages" / "4_System.py"),
]


def main() -> int:
    fail = 0
    for name, path in PAGES:
        t0 = time.perf_counter()
        at = AppTest.from_file(str(path), default_timeout=30)
        try:
            at.run()
            excs = list(at.exception)
        except Exception as e:
            excs = [e]
        dt = time.perf_counter() - t0
        ok = not excs
        print(f"{'OK  ' if ok else 'FAIL'} {name:14s} {dt*1000:6.0f} ms  {path.name}")
        if not ok:
            fail += 1
            for e in excs[:3]:
                msg = e.message if hasattr(e, "message") else str(e)
                for L in str(msg).splitlines()[:6]:
                    print(f"        {L}")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
