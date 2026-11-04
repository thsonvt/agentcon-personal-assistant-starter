"""Long-term memory: facts about Pippa, including working hours. Plain functions returning text."""

import re
from datetime import datetime, time

from app.store import Store

DEFAULT_HOURS = (time(9), time(21))
WORKING_HOURS_KEY = "working_hours"
_HOURS_RE = re.compile(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})")


def _parse_hhmm(value: str) -> time:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"Could not read the time {value!r}. Use 24-hour HH:MM, e.g. 09:00")
    return time(int(m.group(1)), int(m.group(2)))


def working_hours(store: Store) -> tuple[time, time]:
    """Pippa's working hours from memory, or 09:00–21:00 if never set."""
    fact = store.get_fact(WORKING_HOURS_KEY)
    m = _HOURS_RE.search(fact.fact) if fact else None
    if not m:
        return DEFAULT_HOURS
    return time(int(m.group(1)), int(m.group(2))), time(int(m.group(3)), int(m.group(4)))


def set_working_hours(store: Store, start: str, end: str, now: datetime) -> str:
    try:
        s, e = _parse_hhmm(start), _parse_hhmm(end)
    except ValueError as exc:
        return f"ERROR: {exc}"
    if e <= s:
        return "ERROR: The end of working hours must be after the start."
    store.add_fact(f"Working hours {s:%H:%M}-{e:%H:%M} (no meetings outside these)", now, key=WORKING_HOURS_KEY)
    return f"Saved: working hours are now {s:%H:%M}-{e:%H:%M}."


def remember(store: Store, fact: str, now: datetime) -> str:
    if not fact.strip():
        return "ERROR: Nothing to remember."
    saved = store.add_fact(fact, now)
    return f"Saved fact #{saved.id}: {saved.fact}"


def recall(store: Store) -> str:
    facts = store.list_facts()
    if not facts:
        return "No saved facts yet."
    return "Saved facts:\n" + "\n".join(f"#{f.id}: {f.fact}" for f in facts)


def forget(store: Store, fact_id: int) -> str:
    return f"Forgot fact #{fact_id}." if store.delete_fact(fact_id) else f"ERROR: There is no fact #{fact_id}."
