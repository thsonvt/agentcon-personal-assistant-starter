"""The Delegate's tools: act for the inviting user on ONE loop-in task, seeing only free/busy time.

The task id comes from DelegateContext (set by the router), never from the model,
so a contact can't steer the Delegate onto another task.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.assistant_tools import AppContext, tool_errors
from app.booking import BOOKING_LOCK, meeting_details, require_available
from app.profile import working_hours
from app.store import OPEN_STATUSES, Contact, LoopInTask
from app.timeutil import compute_free_slots, fmt_dt, fmt_time, parse_dt
from app.tools.whatsapp import WhatsAppError, WindowClosedError

MAX_OFFERED_SLOTS = 6
# The SDK runs tools in threads: "check the task is open → create the event → mark it done" must not interleave,
# or two quick "yes"es (or two contacts) could both book.
_BOOKING_LOCK = BOOKING_LOCK


@dataclass
class DelegateContext:
    app: AppContext
    task_id: int
    contact: Contact


def current_task(d: DelegateContext) -> LoopInTask:
    task = d.app.store.get_task(d.task_id)
    if task is None or task.status not in OPEN_STATUSES:
        raise ValueError("This meeting request is already closed. Nothing more to arrange.")
    return task


def _window(task: LoopInTask) -> tuple[datetime, datetime]:
    return parse_dt(task.window_start), parse_dt(task.window_end)


def _notify_owner(d: DelegateContext, text: str) -> bool:
    """Tell the owner now, or keep it for their next message if their 24h window is closed."""
    try:
        d.app.whatsapp.send_text(d.app.owner_phone, text)
        return True
    except WindowClosedError:
        d.app.store.queue_notice(d.app.owner_phone, text, d.app.now())
        return True
    except WhatsAppError:
        return False


@tool_errors
def free_slots(d: DelegateContext, day_start: str | None = None, day_end: str | None = None) -> str:
    task = current_task(d)
    win_start, win_end = _window(task)
    start = max(win_start, d.app.now(), parse_dt(day_start) if day_start else win_start)
    end = min(win_end, parse_dt(day_end) if day_end else win_end)
    if end <= start:
        return "No time left in the agreed window."
    work_start, work_end = working_hours(d.app.store)
    busy = d.app.calendar.busy_periods(start, end)
    slots = compute_free_slots(busy, start, end, task.duration_min, work_start, work_end, max_slots=MAX_OFFERED_SLOTS)
    if not slots:
        return f"{d.app.owner_name} has no free slots in that range."
    return "Free slots (Singapore time):\n" + "\n".join(f"- {fmt_dt(s)}–{fmt_time(e)}" for s, e in slots)


@tool_errors
def calendly_link(d: DelegateContext) -> str:
    current_task(d)
    return d.app.calendly.scheduling_url()


@tool_errors
def book_agreed_time(d: DelegateContext, start: str) -> str:
    with _BOOKING_LOCK:
        return _book(d, start)


def _book(d: DelegateContext, start: str) -> str:
    task = current_task(d)
    begin = parse_dt(start)
    end = begin + timedelta(minutes=task.duration_min)
    win_start, win_end = _window(task)
    if begin < max(win_start, d.app.now()) or end > win_end:
        raise ValueError(f"That time is outside the agreed window ({fmt_dt(win_start)} to {fmt_dt(win_end)}).")
    work_start, work_end = working_hours(d.app.store)
    if begin.time() < work_start or end.time() > work_end or end.date() != begin.date():
        raise ValueError(f"That's outside {d.app.owner_name}'s hours ({work_start:%H:%M}–{work_end:%H:%M}).")
    email = (d.contact.email or "").strip()
    if not re.fullmatch(r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+", email):
        return "CLARIFY: Not booked. The host must confirm a valid attendee email before an invitation can be created."
    require_available(d.app.calendar, begin, end)
    title = f"{task.purpose[0].upper()}{task.purpose[1:]} with {d.contact.name}"
    event = d.app.calendar.create_event(title, begin, end, email)
    d.app.store.set_task_status(task.id, "done", d.app.now(), event_id=event.id)
    when = (f"{meeting_details(event.start, event.end, event.location or event.meet_link)}; "
            f"invitation requested for {d.contact.name} <{email}> (delivery and acceptance unconfirmed)")
    d.app.completed_writes.append(f"Booked: {title}, {when}")
    notified = _notify_owner(d, f"Booked ✅ {title}, {when}.")
    note = "" if notified else f" (Couldn't message {d.app.owner_name} about it; it's in their calendar.)"
    return f"Booked: {title}, {when}.{note}"


@tool_errors
def mark_booked_via_link(d: DelegateContext) -> str:
    with _BOOKING_LOCK:
        task = current_task(d)
        d.app.store.set_task_status(task.id, "done", d.app.now())
    _notify_owner(d, f"📅 {d.contact.name} says they booked {task.purpose} through your Calendly link.")
    return f"Noted. {d.app.owner_name} has been told you booked through the link."


@tool_errors
def report_to_owner(d: DelegateContext, message: str) -> str:
    task = current_task(d)
    if not message.strip():
        raise ValueError("Nothing to pass on.")
    ok = _notify_owner(d, f"💬 {d.contact.name} (about {task.purpose}): {message.strip()}")
    return f"Passed on to {d.app.owner_name}." if ok else f"ERROR: Couldn't reach {d.app.owner_name} right now."
