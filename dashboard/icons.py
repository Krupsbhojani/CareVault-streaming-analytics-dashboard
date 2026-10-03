"""Inline stroke SVG icons. currentColor for stroke, 1.8 stroke width.

Every icon takes a size (px) and an optional colour override. Rendered as
HTML strings that go directly into st.markdown(unsafe_allow_html=True).

No emoji, no decorative art, no external assets.
"""
from __future__ import annotations


def _wrap(inner: str, size: int, color: str | None = None) -> str:
    style = f'display:inline-block; vertical-align:middle;'
    if color:
        style += f' color:{color};'
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
        f'stroke-linecap="round" stroke-linejoin="round" style="{style}">'
        f'{inner}</svg>'
    )


def pulse(size: int = 22, color: str | None = None) -> str:
    return _wrap(
        '<path d="M2 12h4l2-6 4 12 2-6h8"/>',
        size, color,
    )


def drop(size: int = 20, color: str | None = None) -> str:
    """Blood drop."""
    return _wrap(
        '<path d="M12 3c-3 4-6 7-6 11a6 6 0 0 0 12 0c0-4-3-7-6-11z"/>',
        size, color,
    )


def arrow_down(size: int = 18, color: str | None = None) -> str:
    return _wrap('<path d="M12 5v14M5 12l7 7 7-7"/>', size, color)


def arrow_up(size: int = 18, color: str | None = None) -> str:
    return _wrap('<path d="M12 19V5M5 12l7-7 7 7"/>', size, color)


def arrow_right(size: int = 18, color: str | None = None) -> str:
    return _wrap('<path d="M5 12h14M12 5l7 7-7 7"/>', size, color)


def arrow_flat(size: int = 18, color: str | None = None) -> str:
    return _wrap('<path d="M4 12h16"/>', size, color)


def heart(size: int = 18, color: str | None = None) -> str:
    return _wrap(
        '<path d="M20.8 8.6a4.5 4.5 0 0 0-7.8-3.1L12 6.6l-1-1a4.5 4.5 0 1 0-6.4 6.3L12 19l7.4-7.1a4.5 4.5 0 0 0 1.4-3.3z"/>',
        size, color,
    )


def gauge(size: int = 18, color: str | None = None) -> str:
    """Blood-pressure gauge."""
    return _wrap(
        '<path d="M12 21a9 9 0 0 1-9-9 9 9 0 0 1 18 0 9 9 0 0 1-9 9z"/>'
        '<path d="M12 12l4-4"/>'
        '<circle cx="12" cy="12" r="1.2" fill="currentColor"/>',
        size, color,
    )


def lungs(size: int = 18, color: str | None = None) -> str:
    return _wrap(
        '<path d="M12 3v10"/>'
        '<path d="M6 14V8a3 3 0 0 1 6 0"/>'
        '<path d="M18 14V8a3 3 0 0 0-6 0"/>'
        '<path d="M6 14c-2 0-3 2-3 4 0 2 1 3 3 3s3-1 3-3v-4"/>'
        '<path d="M18 14c2 0 3 2 3 4 0 2-1 3-3 3s-3-1-3-3v-4"/>',
        size, color,
    )


def monitor(size: int = 20, color: str | None = None) -> str:
    """Device / monitor rectangle."""
    return _wrap(
        '<rect x="3" y="4" width="18" height="12" rx="1"/>'
        '<path d="M8 20h8M12 16v4"/>',
        size, color,
    )


def warning(size: int = 18, color: str | None = None) -> str:
    """Warning triangle."""
    return _wrap(
        '<path d="M12 3 2 21h20L12 3z"/>'
        '<path d="M12 10v5"/>'
        '<circle cx="12" cy="18" r="0.6" fill="currentColor"/>',
        size, color,
    )


def check(size: int = 16, color: str | None = None) -> str:
    return _wrap('<path d="M5 12l5 5 9-11"/>', size, color)


def arrow_left(size: int = 16, color: str | None = None) -> str:
    return _wrap('<path d="M19 12H5M12 19l-7-7 7-7"/>', size, color)
