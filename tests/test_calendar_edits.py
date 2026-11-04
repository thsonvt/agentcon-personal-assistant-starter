"""Plan 2A: reading one event, changing it and deleting it."""

from datetime import datetime

import pytest

from app.timeutil import SGT
from app.tools.calendar import EventNotFoundError, GoogleCalendar
from tests.fakes import FakeCalendarService

TIMED = {
    "id": "e1",
    "summary": "Physics revision",
    "start": {"dateTime": "2026-09-28T16:30:00+08:00"},
    "end": {"dateTime": "2026-09-28T17:30:00+08:00"},
}


def test_get_event_returns_the_event():
    event = GoogleCalendar(FakeCalendarService(items=[TIMED])).get_event("e1")
    assert event.title == "Physics revision"
    assert event.start == datetime(2026, 9, 28, 16, 30, tzinfo=SGT)


def test_update_event_sends_only_the_changed_fields_and_notifies_guests():
    svc = FakeCalendarService(items=[TIMED])
    event = GoogleCalendar(svc).update_event("e1", title="Physics exam prep")
    name, kwargs = svc.calls[0]
    assert name == "patch"
    assert kwargs["body"] == {"summary": "Physics exam prep"}
    assert kwargs["sendUpdates"] == "all"
    assert event.title == "Physics exam prep"
    assert event.start == datetime(2026, 9, 28, 16, 30, tzinfo=SGT)  # unchanged


def test_update_event_times():
    svc = FakeCalendarService(items=[TIMED])
    event = GoogleCalendar(svc).update_event(
        "e1", start=datetime(2026, 9, 29, 10, tzinfo=SGT), end=datetime(2026, 9, 29, 11, tzinfo=SGT)
    )
    assert set(svc.calls[0][1]["body"]) == {"start", "end"}
    assert event.start == datetime(2026, 9, 29, 10, tzinfo=SGT)
    assert event.end == datetime(2026, 9, 29, 11, tzinfo=SGT)


def test_delete_event_cancels_for_guests_too():
    svc = FakeCalendarService(items=[TIMED])
    assert GoogleCalendar(svc).delete_event("e1") is None
    name, kwargs = svc.calls[0]
    assert (name, kwargs["eventId"], kwargs["sendUpdates"]) == ("delete", "e1", "all")


@pytest.mark.parametrize("action", ["get", "update", "delete"])
def test_unknown_or_already_deleted_event_gives_not_found(action):
    # Review Focus: a stale id (event deleted in the Google app, or deleted twice) must say so plainly.
    svc = FakeCalendarService(items=[TIMED])
    cal = GoogleCalendar(svc)
    cal.delete_event("e1")
    calls = {
        "get": lambda: cal.get_event("e1"),
        "update": lambda: cal.update_event("e1", title="x"),
        "delete": lambda: cal.delete_event("e1"),
    }
    with pytest.raises(EventNotFoundError, match="deleted already"):
        calls[action]()


@pytest.mark.parametrize(
    "cancelled",
    [{"id": "e1", "status": "cancelled"}, {**TIMED, "status": "cancelled"}],
    ids=["id-only", "full-payload"],
)
def test_cancelled_event_counts_as_not_found(cancelled):
    # Final review: Google can answer get/patch for a deleted event with status "cancelled"
    # (sometimes with only the id) instead of 404/410.
    cal = GoogleCalendar(FakeCalendarService(items=[cancelled]))
    with pytest.raises(EventNotFoundError):
        cal.get_event("e1")
    with pytest.raises(EventNotFoundError):
        cal.update_event("e1", title="x")
