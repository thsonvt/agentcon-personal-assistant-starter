"""Shared final booking checks for the single-process deployment.

All Pippa host/delegate check-and-write paths use this lock. It does not lock
Google Calendar against external writers, or serialize separate workers/replicas.
"""
import functools
import threading
from datetime import datetime

from app.timeutil import fmt_dt, fmt_time

BOOKING_LOCK = threading.RLock()


def serialized_booking(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        with BOOKING_LOCK:
            return fn(*args, **kwargs)
    return wrapped


def require_available(calendar, start: datetime, end: datetime, *, exclude_event_id: str | None = None) -> None:
    busy = (calendar.busy_periods_excluding(start, end, exclude_event_id)
            if exclude_event_id else calendar.busy_periods(start, end))
    if any(left < end and right > start for left, right in busy):
        raise ValueError("That time is no longer free. Nothing changed; ask for other free slots.")


def meeting_details(start: datetime, end: datetime, location: str | None) -> str:
    minutes = (end - start).total_seconds() / 60
    begin = f"{fmt_dt(start)} ({start.year})"
    finish = fmt_time(end) if start.date() == end.date() else f"{fmt_dt(end)} ({end.year})"
    return (f"{begin}–{finish} "
            f"(Asia/Singapore, UTC+08:00; {minutes:g} min); "
            f"{location or 'location not specified'}")
