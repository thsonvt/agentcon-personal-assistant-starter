"""The Assistant's tools as plain functions: take AppContext + model arguments, return text.

Every function returns a string the model can read. Failures come back as
"ERROR: ..." strings instead of exceptions, so the model can explain or retry.
"""

import functools
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from datetime import datetime, timedelta

from app.booking import BOOKING_LOCK, meeting_details, require_available, serialized_booking
from app.profile import working_hours
from app.store import Store
from app.timeutil import compute_free_slots, fmt_dt, fmt_time, fmt_until, parse_dt
from app.tools.calendar import CalendarAuthError, CalendarError, Event, GoogleCalendar
from app.tools.calendly import BookingLink, CalendlyClient, CalendlyError

REAUTH_MESSAGE = (
    "ERROR: Google Calendar access has expired. Pippa needs to run "
    "scripts/get_google_token.py and update GOOGLE_REFRESH_TOKEN."
)


@dataclass
class AppContext:
    calendar: GoogleCalendar
    calendly: CalendlyClient | BookingLink
    store: Store
    now: Callable[[], datetime]
    completed_writes: list[str] = field(default_factory=list)  # reset at the start of each run
    whatsapp: Any = None  # Sender with send_text/send_template; needed for loop-ins
    owner_phone: str = ""
    run_no: int = 0  # bumped by run_agent at the start of every run
    pending_deletes: dict[str, int] = field(default_factory=dict)  # event id -> run_no it was asked about in
    # Plan 2B: whose assistant this is. Defaults are Pippa's, so Plan 1/2 behaviour is unchanged.
    owner_name: str = "Pippa"
    reauth_message: str = REAUTH_MESSAGE
    intro_template: str = "loop_in_intro"
    intro_with_owner_name: bool = False  # loop_in_intro_named takes [contact, owner, purpose]
    current_input_is_voice: bool = False
    pending_bookings: dict[tuple[str, ...], int] = field(default_factory=dict)
    pending_loop_ins: dict[tuple[str, ...], int] = field(default_factory=dict)
    gmail: Any = None
    gmail_reauth_message: str = (
        "Reconnect Google with read-only Gmail access using scripts/get_google_token.py --gmail --save-env."
    )
    untrusted_read_this_run: bool = False
    booking_reply: str | None = None  # authoritative user-facing clarification for this run
    group_meetings: Any = None
    pending_groups: dict[str, tuple[int, str]] = field(default_factory=dict)
    meeting_preparation_context: str = ''
    followups: Any = None
    followup_turn_id: str = ''


def tool_errors(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> str:
        try:
            return fn(*args, **kwargs)
        except CalendarAuthError:
            ctx = args[0] if args else None
            ctx = getattr(ctx, "app", ctx)  # the Delegate's tools get a DelegateContext
            return getattr(ctx, "reauth_message", REAUTH_MESSAGE)
        except (CalendarError, CalendlyError, ValueError) as exc:
            return f"ERROR: {exc}"

    return wrapper


def _when(event: Event) -> str:
    if event.all_day:
        return f"{event.start:%a} {event.start.day} {event.start:%b} (all day)"
    return f"{fmt_dt(event.start)}–{fmt_time(event.end)}"


def _describe(event: Event, now: datetime) -> str:
    when = _when(event) if event.all_day else f"{_when(event)} ({fmt_until(event.start, now)})"
    parts = [f"- {event.title} | {when}"]
    if event.location:
        parts.append(f"at {event.location}")
    if event.meet_link:
        parts.append(f"link {event.meet_link}")
    parts.append(f"[id: {event.id}]")
    return " | ".join(parts)


def _check_range(start: datetime, end: datetime) -> None:
    if end <= start:
        raise ValueError("The end time must be after the start time.")


@tool_errors
def list_events(app: AppContext, start: str, end: str, limit: int = 5) -> str:
    start_dt, end_dt = parse_dt(start), parse_dt(end)
    _check_range(start_dt, end_dt)
    events = app.calendar.list_events(start_dt, end_dt, limit=max(1, min(limit, 20)))
    if not events:
        return "No events in that range."
    now = app.now()
    return "\n".join(_describe(e, now) for e in events)


@tool_errors
def find_conflicts(app: AppContext, start: str, end: str) -> str:
    start_dt, end_dt = parse_dt(start), parse_dt(end)
    _check_range(start_dt, end_dt)
    events = app.calendar.list_events(start_dt, end_dt, limit=20)
    if not events:
        return "No conflicts: that time is free."
    now = app.now()
    return "Conflicts:\n" + "\n".join(_describe(e, now) for e in events)


@tool_errors
def get_free_slots(app: AppContext, window_start: str, window_end: str, duration_min: int) -> str:
    start_dt, end_dt = parse_dt(window_start), parse_dt(window_end)
    _check_range(start_dt, end_dt)
    if not 5 <= duration_min <= 480:
        raise ValueError("Duration must be between 5 and 480 minutes.")
    start_dt = max(start_dt, app.now())
    if end_dt <= start_dt:
        return "That window is already in the past."
    work_start, work_end = working_hours(app.store)
    busy = app.calendar.busy_periods(start_dt, end_dt)
    slots = compute_free_slots(busy, start_dt, end_dt, duration_min, work_start, work_end)
    if not slots:
        return f"No free slots in that window within working hours ({work_start:%H:%M}–{work_end:%H:%M})."
    return "Free slots:\n" + "\n".join(f"- {fmt_dt(s)}–{fmt_time(e)}" for s, e in slots)


def _check_new_times(app: AppContext, start_dt: datetime, end_dt: datetime) -> None:
    _check_range(start_dt, end_dt)
    if start_dt < app.now() - timedelta(minutes=5):
        raise ValueError("That start time is in the past.")


def _invitation_identity(app: AppContext, name: str, email: str) -> tuple[str, str] | str:
    """Resolve only exact saved identities; a partial match is a clarification, not consent."""
    if not name:
        return "CLARIFY: Not created. Ask for the attendee's exact name and email; never guess."
    contacts = app.store.list_contacts()
    exact = [c for c in contacts if c.name.casefold() == name.casefold()]
    if len(exact) > 1 and email:
        by_email = [c for c in exact if (c.email or "").strip().casefold() == email.casefold()]
        if len(by_email) == 1:
            exact = by_email
    matches = exact or [c for c in contacts if name.casefold() in c.name.casefold()]
    if len(matches) > 1 or (matches and not exact):
        choices = ", ".join(f"{c.name} <{c.email or 'no saved email'}>" for c in matches)
        return f"CLARIFY: Not created. Confirm the exact saved contact before inviting: {choices}."
    if exact:
        contact = exact[0]
        saved_email = (contact.email or "").strip()
        if saved_email and email and saved_email.casefold() != email.casefold():
            return (
                f"CLARIFY: Not created. {contact.name}'s saved email is {saved_email}, "
                "which differs from the proposed address. Ask the owner to resolve the contact details."
            )
        name, email = contact.name, saved_email or email
    if not email:
        return f"CLARIFY: Not created. Ask for {name}'s email to send an invitation, or whether this is only a personal calendar entry."
    if not re.fullmatch(r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+", email):
        raise ValueError("A single valid attendee email is required. Ask the owner; never guess an address.")
    return name, email


@tool_errors
@serialized_booking
def create_event(
    app: AppContext,
    title: str,
    start: str,
    end: str,
    attendee_email: str | None = None,
    *,
    booking_mode: str | None = None,
    attendee_name: str | None = None,
    confirmed: bool = False,
    location: str | None = None,
) -> str:
    if not title.strip():
        raise ValueError("The event needs a title.")
    start_dt, end_dt = parse_dt(start), parse_dt(end)
    _check_new_times(app, start_dt, end_dt)
    if booking_mode not in ("personal", "invitation"):
        return "CLARIFY: Not created. Is this a personal calendar entry or an invitation to another person?"
    name, email = (attendee_name or "").strip(), (attendee_email or "").strip()
    if booking_mode == "personal" and email:
        raise ValueError("A personal entry cannot include an attendee email. Choose invitation to invite someone.")
    if booking_mode == "invitation":
        identity = _invitation_identity(app, name, email)
        if isinstance(identity, str):
            return identity
        name, email = identity
    location = (location or "").strip()
    key = (booking_mode, title.strip(), start_dt.isoformat(), end_dt.isoformat(), name, email, location)
    asked_in = app.pending_bookings.get(key)
    if booking_mode == "invitation" or app.current_input_is_voice or app.untrusted_read_this_run or app.pending_bookings:
        if not (confirmed and asked_in == app.run_no - 1):
            # Keep one proposal so a changed recipient/time cannot reuse an earlier approval.
            app.pending_bookings = {key: app.run_no}
            recipient = f"invite {name} <{email}>" if booking_mode == "invitation" else "personal entry only, no invitation"
            location_note = (
                "Google Meet will be added to the invitation. " if email and not location else
                "If location is unspecified, ask for a location/link or confirmation to proceed without one. "
            )
            return (
                f"CONFIRM: Not created yet. Ask {app.owner_name}: create {title.strip()}, "
                f"{meeting_details(start_dt, end_dt, location)}, {recipient}? "
                f"Read back the full details and stop. {location_note}"
                "Set confirmed=true only after the owner confirms them in the next message."
            )
        # Consume before the external write, including on errors: do not silently repeat a send.
        app.pending_bookings.clear()
    require_available(app.calendar, start_dt, end_dt)
    event = app.calendar.create_event(title.strip(), start_dt, end_dt, email or None, location=location or None)
    guest = (
        f", invitation requested for {name} <{email}> (delivery and acceptance unconfirmed)"
        if email else ", personal calendar entry; no invitation sent"
    )
    result = f"Created: {event.title}, {meeting_details(event.start, event.end, event.location or event.meet_link)}{guest} [id: {event.id}]"
    app.completed_writes.append(result)
    return result


def _require_id(event_id: str) -> str:
    if not event_id.strip():
        raise ValueError("Which event? Pass the event id from list_events or create_event.")
    return event_id.strip()


@tool_errors
def update_event(
    app: AppContext,
    event_id: str,
    title: str | None = None,
    new_start: str | None = None,
    new_end: str | None = None,
) -> str:
    event_id = _require_id(event_id)
    if title is not None and not title.strip():
        raise ValueError("The new title can't be blank.")
    if title is None and new_start is None and new_end is None:
        raise ValueError("Nothing to change: pass a new title, new_start or new_end.")
    start_dt = parse_dt(new_start) if new_start else None
    end_dt = parse_dt(new_end) if new_end else None
    with BOOKING_LOCK:
        if start_dt is not None or end_dt is not None:
            old = app.calendar.get_event(event_id)
            if old.all_day:
                raise ValueError(
                    f"That's an all-day event. I can only rename those; {app.owner_name} can move it in Google Calendar."
                )
            if start_dt is None:
                # Only the end changes, e.g. "push this meeting to 4pm" while it's already under way:
                # the start may be in the past, so check just the new end, and leave the start alone.
                _check_range(old.start, end_dt)
                if end_dt <= app.now():
                    raise ValueError("That end time is in the past.")
            else:
                if end_dt is None:
                    end_dt = start_dt + (old.end - old.start)  # a new start alone keeps the length
                _check_new_times(app, start_dt, end_dt)
            require_available(app.calendar, start_dt or old.start, end_dt or old.end, exclude_event_id=event_id)
        event = app.calendar.update_event(event_id, title.strip() if title else None, start_dt, end_dt)
    when = _when(event) if event.all_day else meeting_details(event.start, event.end, event.location or event.meet_link)
    result = f"Updated: {event.title}, now {when} [id: {event.id}]"
    app.completed_writes.append(result)
    return result


@tool_errors
def delete_event(app: AppContext, event_id: str, confirmed: bool = False) -> str:
    """Two-step delete, enforced here rather than trusted to the model.

    The first call only records the request and returns CONFIRM. The delete happens on a
    confirmed call in the user's NEXT message (the following run), so the model can never ask
    and answer its own question inside one run.
    """
    event_id = _require_id(event_id)
    asked_in = app.pending_deletes.get(event_id)
    if confirmed and asked_in == app.run_no - 1:
        event = app.calendar.get_event(event_id)
        app.calendar.delete_event(event_id)
        del app.pending_deletes[event_id]
        result = f"Deleted: {event.title}, {_when(event)}"
        app.completed_writes.append(result)
        return result
    event = app.calendar.get_event(event_id)
    app.pending_deletes[event_id] = app.run_no
    return (
        f"CONFIRM: Not deleted yet. Ask {app.owner_name}: delete {event.title}, {_when(event)}? "
        "Then stop and wait. Call delete_event with confirmed=true only after he replies yes."
    )


@tool_errors
def get_calendly_link(app: AppContext) -> str:
    return app.calendly.scheduling_url()
