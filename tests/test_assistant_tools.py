from datetime import datetime

import httpx
import pytest
from google.auth.exceptions import RefreshError

from app import assistant_tools as t
from app.store import Store
from app.timeutil import SGT
from app.tools.calendar import GoogleCalendar
from app.tools.calendly import CalendlyClient
from tests.fakes import FakeCalendarService

NOW = datetime(2026, 9, 28, 14, 0, tzinfo=SGT)

EVENT = {
    "id": "e1",
    "summary": "Physics revision",
    "start": {"dateTime": "2026-09-28T16:30:00+08:00"},
    "end": {"dateTime": "2026-09-28T17:30:00+08:00"},
    "hangoutLink": "https://meet.google.com/abc",
}


def make_app(svc=None, calendly_handler=None):
    handler = calendly_handler or (
        lambda r: httpx.Response(200, json={"resource": {"scheduling_url": "https://calendly.com/Pippa/30min"}})
    )
    return t.AppContext(
        calendar=GoogleCalendar(svc or FakeCalendarService()),
        calendly=CalendlyClient("tok", "https://api.calendly.com/event_types/X", http=httpx.Client(transport=httpx.MockTransport(handler))),
        store=Store(":memory:"),
        now=lambda: NOW,
    )


def test_list_events_describes_title_time_countdown_link_and_id():
    out = t.list_events(make_app(FakeCalendarService(items=[EVENT])), "2026-09-28T14:00", "2026-10-05T00:00")
    assert "Physics revision" in out
    assert "Mon 28 Sep, 4:30pm–5:30pm" in out
    assert "in 2h 30m" in out
    assert "https://meet.google.com/abc" in out
    assert "[id: e1]" in out


def test_list_events_empty():
    assert t.list_events(make_app(), "2026-09-28T14:00", "2026-10-05T00:00") == "No events in that range."


def test_bad_time_returns_error_string_not_exception():
    out = t.list_events(make_app(), "tomorrow", "2026-10-05T00:00")
    assert out.startswith("ERROR:")


def test_find_conflicts_reports_overlap():
    out = t.find_conflicts(make_app(FakeCalendarService(items=[EVENT])), "2026-09-28T16:00", "2026-09-28T17:00")
    assert out.startswith("Conflicts:")
    assert "Physics revision" in out


def test_find_conflicts_free():
    assert t.find_conflicts(make_app(), "2026-09-28T16:00", "2026-09-28T17:00").startswith("No conflicts")


def test_free_slots_skip_busy_and_past():
    busy = [{"start": "2026-09-28T15:00:00+08:00", "end": "2026-09-28T16:00:00+08:00"}]
    out = t.get_free_slots(make_app(FakeCalendarService(busy=busy)), "2026-09-28T09:00", "2026-09-28T18:00", 60)
    lines = out.splitlines()
    assert lines[0] == "Free slots:"
    # 9am-2pm is in the past (NOW is 2pm); 3-4pm is busy.
    assert lines[1] == "- Mon 28 Sep, 2:00pm–3:00pm"
    assert lines[2] == "- Mon 28 Sep, 4:00pm–5:00pm"


def test_free_slots_reject_silly_duration():
    assert t.get_free_slots(make_app(), "2026-09-28T09:00", "2026-09-28T18:00", 0).startswith("ERROR:")


def test_create_event_confirms_details():
    svc = FakeCalendarService()
    out = t.create_event(make_app(svc), "Study session", "2026-09-29T15:00", "2026-09-29T17:00", booking_mode="personal")
    assert out == "Created: Study session, Tue 29 Sep, 3:00pm (2026)–5:00pm (Asia/Singapore, UTC+08:00; 120 min); location not specified, personal calendar entry; no invitation sent [id: evt_new]"
    assert [name for name, _ in svc.calls] == ["freebusy", "insert"]


def test_create_event_rejects_end_before_start():
    svc = FakeCalendarService()
    out = t.create_event(make_app(svc), "Study", "2026-09-29T17:00", "2026-09-29T15:00", booking_mode="personal")
    assert out.startswith("ERROR:")
    assert svc.calls == []


def test_create_event_rejects_past_time():
    svc = FakeCalendarService()
    out = t.create_event(make_app(svc), "Study", "2026-09-27T15:00", "2026-09-27T17:00", booking_mode="personal")
    assert "past" in out
    assert svc.calls == []


def test_create_event_rejects_blank_title():
    assert t.create_event(make_app(), "  ", "2026-09-29T15:00", "2026-09-29T17:00", booking_mode="personal").startswith("ERROR:")


def test_expired_google_access_gives_reauth_message():
    out = t.list_events(make_app(FakeCalendarService(exc=RefreshError("invalid_grant"))), "2026-09-28T14:00", "2026-09-29T00:00")
    assert out == t.REAUTH_MESSAGE


def test_calendly_link():
    assert t.get_calendly_link(make_app()) == "https://calendly.com/Pippa/30min"


def test_calendly_failure_is_error_string():
    out = t.get_calendly_link(make_app(calendly_handler=lambda r: httpx.Response(500)))
    assert out.startswith("ERROR:")


def test_successful_writes_are_recorded_on_the_context():
    app = make_app(FakeCalendarService(items=[dict(EVENT)]))
    t.create_event(app, "Study", "2026-09-29T15:00", "2026-09-29T17:00", booking_mode="personal")
    t.update_event(app, "e1", new_start="2026-09-29T16:00", new_end="2026-09-29T18:00")
    assert [w.split(":")[0] for w in app.completed_writes] == ["Created", "Updated"]


def test_failed_writes_are_not_recorded():
    app = make_app()
    t.create_event(app, "Study", "2026-09-27T15:00", "2026-09-27T17:00", booking_mode="personal")  # in the past
    assert app.completed_writes == []


def test_free_slots_follow_saved_working_hours():
    from app import profile

    app = make_app()
    profile.set_working_hours(app.store, "16:00", "18:00", NOW)
    out = t.get_free_slots(app, "2026-09-28T09:00", "2026-09-28T21:00", 60)
    assert out.splitlines()[1:] == ["- Mon 28 Sep, 4:00pm–5:00pm", "- Mon 28 Sep, 5:00pm–6:00pm"]
