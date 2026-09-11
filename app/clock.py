"""Local-time helpers. The process runs with TZ set to the sites' zone."""

from datetime import datetime, timedelta

from app.config import Window


def local_hhmm(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def in_window(window: Window | None, ts: int) -> bool:
    """True inside start..end, also when the window crosses midnight."""
    if window is None:
        return True
    now = local_hhmm(ts)
    if window.start <= window.end:
        return window.start <= now < window.end
    return now >= window.start or now < window.end


def window_start(window: Window, ts: int) -> int:
    """The most recent moment the window opened, at or before ts."""
    hh, mm = (int(x) for x in window.start.split(":"))
    start = datetime.fromtimestamp(ts).replace(hour=hh, minute=mm, second=0, microsecond=0)
    if start.timestamp() > ts:
        start -= timedelta(days=1)
    return int(start.timestamp())


def window_state(window: Window | None, ts: int) -> dict | None:
    if window is None:
        return None
    on = in_window(window, ts)
    return {
        "on": on,
        "start": window.start,
        "end": window.end,
        "next": window.end if on else window.start,
    }
