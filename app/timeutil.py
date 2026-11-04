"""Time parsing, formatting and free-slot maths. Pure functions, no I/O."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

SGT = ZoneInfo("Asia/Singapore")


def parse_dt(value: str, tz: ZoneInfo = SGT) -> datetime:
    """Parse an ISO 8601 string from the model. A time with no offset is taken as Singapore time."""
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise ValueError(
            f"Could not read the time {value!r}. Use ISO 8601, e.g. 2026-09-28T15:00:00+08:00"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def fmt_time(dt: datetime, tz: ZoneInfo = SGT) -> str:
    """'3:00pm'"""
    local = dt.astimezone(tz)
    hour = local.strftime("%I").lstrip("0")
    return f"{hour}:{local:%M}{local.strftime('%p').lower()}"


def fmt_dt(dt: datetime, tz: ZoneInfo = SGT) -> str:
    """'Mon 28 Sep, 3:00pm'"""
    local = dt.astimezone(tz)
    return f"{local:%a} {local.day} {local:%b}, {fmt_time(local, tz)}"


def fmt_until(start: datetime, now: datetime) -> str:
    """'in 2h 15m', 'in 3d 4h', 'started 10m ago'."""
    seconds = int((start - now).total_seconds())
    past = seconds < 0
    minutes = abs(seconds) // 60
    days, rem = divmod(minutes, 24 * 60)
    hours, mins = divmod(rem, 60)
    if days:
        text = f"{days}d {hours}h" if hours else f"{days}d"
    elif hours:
        text = f"{hours}h {mins}m" if mins else f"{hours}h"
    else:
        text = f"{mins}m"
    return f"started {text} ago" if past else f"in {text}"


def _round_up(dt: datetime, step_min: int) -> datetime:
    base = dt.replace(second=0, microsecond=0)
    if base < dt:
        base += timedelta(minutes=1)
    rem = base.minute % step_min
    return base if rem == 0 else base + timedelta(minutes=step_min - rem)


def _overlaps(start: datetime, end: datetime, busy: list[tuple[datetime, datetime]]) -> bool:
    return any(b_start < end and b_end > start for b_start, b_end in busy)


def compute_free_slots(
    busy: list[tuple[datetime, datetime]],
    window_start: datetime,
    window_end: datetime,
    duration_min: int,
    work_start: time,
    work_end: time,
    tz: ZoneInfo = SGT,
    step_min: int = 30,
    max_slots: int = 10,
) -> list[tuple[datetime, datetime]]:
    """Non-overlapping free slots of `duration_min`, inside working hours and the window."""
    if duration_min <= 0:
        raise ValueError("Duration must be more than 0 minutes.")
    if window_end <= window_start:
        raise ValueError("The window end must be after the window start.")
    duration = timedelta(minutes=duration_min)
    slots: list[tuple[datetime, datetime]] = []
    candidate = _round_up(window_start.astimezone(tz), step_min)
    while candidate + duration <= window_end and len(slots) < max_slots:
        day = candidate.date()
        day_start = datetime.combine(day, work_start, tzinfo=tz)
        day_end = datetime.combine(day, work_end, tzinfo=tz)
        if candidate < day_start:
            candidate = day_start
            continue
        if candidate + duration > day_end:
            candidate = datetime.combine(day + timedelta(days=1), work_start, tzinfo=tz)
            continue
        end = candidate + duration
        if _overlaps(candidate, end, busy):
            candidate += timedelta(minutes=step_min)
            continue
        slots.append((candidate, end))
        candidate = end
    return slots
