import threading
import time as time_mod
from datetime import datetime
from types import SimpleNamespace

import pytest
from google.auth.exceptions import RefreshError

from app.timeutil import SGT
from app.tools.calendar import CalendarAuthError, CalendarError, GoogleCalendar
from tests.fakes import FakeCalendarService

START = datetime(2026, 9, 28, 0, tzinfo=SGT)
END = datetime(2026, 9, 29, 0, tzinfo=SGT)

TIMED = {
    "id": "e1",
    "summary": "Physics revision",
    "start": {"dateTime": "2026-09-28T16:30:00+08:00"},
    "end": {"dateTime": "2026-09-28T17:30:00+08:00"},
    "location": "Library",
    "hangoutLink": "https://meet.google.com/abc",
}
ALL_DAY = {"id": "e2", "summary": "Holiday", "start": {"date": "2026-09-28"}, "end": {"date": "2026-09-29"}}


def test_list_events_parses_timed_and_all_day_events():
    # Review Focus: all-day events use "date", not "dateTime", and must not crash.
    cal = GoogleCalendar(FakeCalendarService(items=[TIMED, ALL_DAY]))
    events = cal.list_events(START, END)
    assert events[0].title == "Physics revision"
    assert events[0].start == datetime(2026, 9, 28, 16, 30, tzinfo=SGT)
    assert events[0].meet_link == "https://meet.google.com/abc"
    assert events[0].all_day is False
    assert events[1].all_day is True
    assert events[1].start == datetime(2026, 9, 28, 0, tzinfo=SGT)


def test_list_events_asks_google_for_single_ordered_events():
    svc = FakeCalendarService()
    GoogleCalendar(svc, calendar_id="test@group").list_events(START, END, limit=3)
    name, kwargs = svc.calls[0]
    assert name == "list"
    assert kwargs["calendarId"] == "test@group"
    assert kwargs["singleEvents"] is True
    assert kwargs["orderBy"] == "startTime"
    assert kwargs["maxResults"] == 3
    assert kwargs["showDeleted"] is False


@pytest.mark.parametrize("excluded", [
    {"id": "deleted", "status": "cancelled"},
    {**TIMED, "status": "cancelled"},
    {**TIMED, "status": "cancelled", "recurringEventId": "series"},
    {**TIMED, "status": "confirmed", "recurringEventId": "series",
     "attendees": [{"self": True, "responseStatus": "declined"}]},
])
def test_day_listing_omits_cancelled_deleted_and_self_declined_events(excluded):
    svc = FakeCalendarService(items=[excluded, ALL_DAY])
    assert [e.id for e in GoogleCalendar(svc).list_events(START, END)] == ["e2"]


@pytest.mark.parametrize("attendees", [
    [], [{"self": True, "responseStatus": "accepted"}],
    [{"self": True, "responseStatus": "tentative"}],
    [{"self": True, "responseStatus": "needsAction"}],
    [{"self": False, "responseStatus": "declined"}],
    [{"self": True, "responseStatus": "accepted"}, {"responseStatus": "declined"}],
])
def test_day_listing_keeps_events_not_declined_by_the_calendar_user(attendees):
    svc = FakeCalendarService(items=[{**TIMED, "attendees": attendees}])
    assert [e.id for e in GoogleCalendar(svc).list_events(START, END)] == ["e1"]


def test_pagination_fills_limit_after_filtered_or_empty_pages():
    calls = []
    pages = {
        None: {"items": [{"id": "deleted", "status": "cancelled"}], "nextPageToken": "second"},
        "second": {"items": [], "nextPageToken": "third"},
        "third": {"items": [TIMED, ALL_DAY, {**TIMED, "id": "extra"}], "nextPageToken": "unused"},
    }

    def listing(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(execute=lambda: pages[kwargs.get("pageToken")])

    svc = SimpleNamespace(events=lambda: SimpleNamespace(list=listing))
    assert [e.id for e in GoogleCalendar(svc).list_events(START, END, limit=2)] == ["e1", "e2"]
    assert [c.get("pageToken") for c in calls] == [None, "second", "third"]
    assert all(c["singleEvents"] and c["showDeleted"] is False for c in calls)


def test_all_filtered_events_return_empty_list():
    svc = FakeCalendarService(items=[{**TIMED, "attendees": [{"self": True, "responseStatus": "declined"}]}])
    assert GoogleCalendar(svc).list_events(START, END) == []


def test_reused_adapter_reads_google_again_after_an_event_is_cancelled():
    svc = FakeCalendarService(items=[TIMED])
    calendar = GoogleCalendar(svc)
    assert calendar.list_events(START, END)
    svc.items = [{**TIMED, "status": "cancelled"}]
    assert calendar.list_events(START, END) == []
    assert len(svc.calls) == 2


def test_repeated_page_token_raises_instead_of_hanging():
    result = {"items": [], "nextPageToken": "same"}
    svc = SimpleNamespace(events=lambda: SimpleNamespace(list=lambda **kw: SimpleNamespace(execute=lambda: result)))
    with pytest.raises(CalendarError, match="repeated"):
        GoogleCalendar(svc).list_events(START, END)


def test_declined_events_do_not_appear_in_schedule_or_conflict_tool_output():
    from app import assistant_tools
    svc = FakeCalendarService(items=[{**TIMED, "attendees": [{"self": True, "responseStatus": "declined"}]}])
    app = SimpleNamespace(calendar=GoogleCalendar(svc), now=lambda: START)
    assert assistant_tools.list_events(app, START.isoformat(), END.isoformat()) == "No events in that range."
    assert assistant_tools.find_conflicts(app, START.isoformat(), END.isoformat()) == "No conflicts: that time is free."


def test_event_without_title_gets_placeholder():
    item = {**TIMED, "summary": ""}
    assert GoogleCalendar(FakeCalendarService(items=[item])).list_events(START, END)[0].title == "(no title)"


def test_busy_periods_converted_to_sgt():
    busy = [{"start": "2026-09-28T02:00:00Z", "end": "2026-09-28T03:00:00Z"}]
    periods = GoogleCalendar(FakeCalendarService(busy=busy)).busy_periods(START, END)
    assert periods == [(datetime(2026, 9, 28, 10, tzinfo=SGT), datetime(2026, 9, 28, 11, tzinfo=SGT))]


LUMA_ID = "luma@import.calendar.google.com"
LUMA_BUSY = {"start": "2026-10-13T13:30:00+08:00", "end": "2026-10-13T17:30:00+08:00"}
BOOKING_START = datetime(2026, 10, 13, 15, 30, tzinfo=SGT)
BOOKING_END = datetime(2026, 10, 13, 16, tzinfo=SGT)


def test_selected_calendars_block_the_luma_booking_without_changing_write_destination():
    svc = FakeCalendarService(calendar_items=[
        {"id": "owner@example.com", "primary": True},
        {"id": LUMA_ID, "selected": True},
        {"id": "unselected"},
        {"id": "hidden", "selected": True, "hidden": True},
        {"id": "deleted", "selected": True, "deleted": True},
        {"id": LUMA_ID, "selected": True},
    ], busy_by_calendar={LUMA_ID: {"busy": [LUMA_BUSY]}})
    cal = GoogleCalendar(svc, calendar_id="destination", check_selected_calendars=True)
    periods = cal.busy_periods(BOOKING_START, BOOKING_END)
    assert periods == [(datetime(2026, 10, 13, 13, 30, tzinfo=SGT), datetime(2026, 10, 13, 17, 30, tzinfo=SGT))]
    freebusy = next(body for name, body in svc.calls if name == "freebusy")
    assert [item["id"] for item in freebusy["items"]] == ["destination", "primary", LUMA_ID]
    cal.create_event("Call", BOOKING_START, BOOKING_END)
    assert svc.calls[-1][1]["calendarId"] == "destination"


def test_default_availability_still_checks_only_destination():
    svc = FakeCalendarService(calendar_items=[{"id": LUMA_ID, "selected": True}])
    assert GoogleCalendar(svc, calendar_id="destination").busy_periods(START, END) == []
    assert svc.calls == [("freebusy", {
        "timeMin": START.isoformat(), "timeMax": END.isoformat(), "timeZone": str(SGT),
        "items": [{"id": "destination"}],
    })]


def test_selected_calendar_discovery_reads_all_pages_and_refreshes_each_check():
    svc = FakeCalendarService(busy_by_calendar={LUMA_ID: {"busy": [LUMA_BUSY]}})
    calls = []
    pages = {
        None: {"items": [], "nextPageToken": "second"},
        "second": {"items": [{"id": LUMA_ID, "selected": True}]},
    }

    def listing(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(execute=lambda: pages[kwargs.get("pageToken")])

    svc.calendarList = lambda: SimpleNamespace(list=listing)
    cal = GoogleCalendar(svc, check_selected_calendars=True)
    assert cal.busy_periods(BOOKING_START, BOOKING_END)
    pages["second"] = {"items": [{"id": LUMA_ID, "selected": False}]}
    assert cal.busy_periods(BOOKING_START, BOOKING_END) == []
    assert [call.get("pageToken") for call in calls] == [None, "second", None, "second"]
    assert all(call["showHidden"] is False and call["showDeleted"] is False for call in calls)


@pytest.mark.parametrize("response", [
    {}, {"items": None}, {"items": [None]}, {"items": [{"selected": True}]},
    {"items": [{"id": "secondary", "selected": "true"}]},
    {"items": [{"id": "secondary", "selected": True, "hidden": "false"}]},
    {"items": [], "nextPageToken": 123},
    {"items": [], "nextPageToken": "same"},
])
def test_incomplete_or_looping_calendar_discovery_fails_closed(response):
    svc = FakeCalendarService()
    svc.calendarList = lambda: SimpleNamespace(list=lambda **kw: SimpleNamespace(execute=lambda: response))
    with pytest.raises(CalendarError):
        GoogleCalendar(svc, check_selected_calendars=True).busy_periods(START, END)
    assert not svc.calls


@pytest.mark.parametrize("calendar", [
    {"errors": [{"reason": "internalError"}]}, {"errors": [{"reason": "unknown"}]},
    {}, {"busy": None}, {"busy": [{"start": "2026-10-13T13:30:00", "end": LUMA_BUSY["end"]}]},
    {"busy": [], "errors": {}},
    {"busy": [{"start": LUMA_BUSY["end"], "end": LUMA_BUSY["start"]}]},
])
def test_selected_calendar_provider_or_incomplete_freebusy_failure_fails_closed(calendar):
    svc = FakeCalendarService(calendar_items=[{"id": LUMA_ID, "selected": True}],
                              busy_by_calendar={LUMA_ID: calendar})
    with pytest.raises(CalendarError):
        GoogleCalendar(svc, check_selected_calendars=True).busy_periods(BOOKING_START, BOOKING_END)
    assert not any(name == "list" for name, _ in svc.calls)


def test_missing_selected_calendar_freebusy_fails_closed():
    svc = FakeCalendarService(calendar_items=[{"id": LUMA_ID, "selected": True}])
    svc.freebusy = lambda: SimpleNamespace(query=lambda **kw: SimpleNamespace(
        execute=lambda: {"calendars": {"primary": {"busy": []}}}))
    with pytest.raises(CalendarError):
        GoogleCalendar(svc, check_selected_calendars=True).busy_periods(START, END)


def test_selected_calendar_checks_batch_more_than_fifty_calendars():
    svc = FakeCalendarService(calendar_items=[{"id": f"secondary-{i}", "selected": True} for i in range(55)])
    assert GoogleCalendar(svc, check_selected_calendars=True).busy_periods(START, END) == []
    bodies = [body for name, body in svc.calls if name == "freebusy"]
    assert [len(body["items"]) for body in bodies] == [50, 6]
    assert {item["id"] for body in bodies for item in body["items"]} == {"primary"} | {
        f"secondary-{i}" for i in range(55)
    }


@pytest.mark.parametrize("reason", ["notFound", "forbidden"])
def test_imported_calendar_fallback_expands_pages_and_filters_free_events(reason):
    luma = {"id": "luma", "transparency": "opaque",
            "start": {"dateTime": LUMA_BUSY["start"]}, "end": {"dateTime": LUMA_BUSY["end"]}}
    svc = FakeCalendarService(calendar_items=[{"id": LUMA_ID, "selected": True}],
                              busy_by_calendar={LUMA_ID: {"errors": [{"reason": reason}]}})
    calls = []
    pages = {
        None: {"items": [
            {**luma, "id": "transparent", "transparency": "transparent"},
            {"id": "cancelled", "status": "cancelled"},
            {**luma, "id": "declined", "attendees": [{"self": True, "responseStatus": "declined"}]},
        ], "nextPageToken": "second", "timeZone": "Asia/Singapore"},
        "second": {"items": [luma], "timeZone": "Asia/Singapore"},
    }

    def listing(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(execute=lambda: pages[kwargs.get("pageToken")])

    svc.events = lambda: SimpleNamespace(list=listing)
    periods = GoogleCalendar(svc, check_selected_calendars=True).busy_periods(BOOKING_START, BOOKING_END)
    assert periods == [(datetime(2026, 10, 13, 13, 30, tzinfo=SGT), datetime(2026, 10, 13, 17, 30, tzinfo=SGT))]
    assert [call.get("pageToken") for call in calls] == [None, "second"]
    assert all(call["calendarId"] == LUMA_ID and call["singleEvents"] and
               call["showDeleted"] is False and call["maxResults"] == 2500 for call in calls)


def test_imported_calendar_fallback_all_day_uses_calendar_timezone():
    svc = FakeCalendarService(items=[ALL_DAY], calendar_items=[{"id": LUMA_ID, "selected": True}],
                              busy_by_calendar={LUMA_ID: {"errors": [{"reason": "notFound"}]}})
    svc.events = lambda: SimpleNamespace(list=lambda **kw: SimpleNamespace(
        execute=lambda: {"items": [ALL_DAY], "timeZone": "America/Los_Angeles"}))
    periods = GoogleCalendar(svc, check_selected_calendars=True).busy_periods(START, END)
    assert periods == [(datetime(2026, 9, 28, 15, tzinfo=SGT), datetime(2026, 9, 29, 15, tzinfo=SGT))]


@pytest.mark.parametrize("response", [
    {}, {"items": None}, {"items": [TIMED], "timeZone": "Invalid/Timezone"},
    {"items": [ALL_DAY]},
    {"items": [{"id": "broken"}]}, {"items": [], "nextPageToken": "same"},
])
def test_incomplete_imported_calendar_event_fallback_fails_closed(response):
    svc = FakeCalendarService(calendar_items=[{"id": LUMA_ID, "selected": True}],
                              busy_by_calendar={LUMA_ID: {"errors": [{"reason": "notFound"}]}})
    svc.events = lambda: SimpleNamespace(list=lambda **kw: SimpleNamespace(execute=lambda: response))
    with pytest.raises(CalendarError):
        GoogleCalendar(svc, check_selected_calendars=True).busy_periods(START, END)


def test_selected_calendar_availability_preserves_account_guard_and_auth_errors():
    svc = FakeCalendarService(calendar_items=[{"id": LUMA_ID, "selected": True}],
                              busy_by_calendar={LUMA_ID: {"errors": [{"reason": "notFound"}]}}, items=[TIMED])
    guarded_calls = []
    cal = GoogleCalendar(svc, check_selected_calendars=True,
                         before_request=lambda: guarded_calls.append(svc.calls[-1][0]))
    assert cal.busy_periods(START, END)
    assert guarded_calls == ["calendarList", "freebusy", "list"]
    svc.exc = RefreshError("invalid_grant")
    with pytest.raises(CalendarAuthError):
        cal.busy_periods(START, END)


def test_create_event_sends_body_and_invites_attendee():
    svc = FakeCalendarService()
    event = GoogleCalendar(svc).create_event(
        "Call with Edmund", datetime(2026, 9, 28, 15, tzinfo=SGT), datetime(2026, 9, 28, 15, 30, tzinfo=SGT),
        attendee_email="edmund@example.com",
    )
    _, kwargs = svc.calls[0]
    assert kwargs["body"]["summary"] == "Call with Edmund"
    assert kwargs["body"]["attendees"] == [{"email": "edmund@example.com"}]
    assert kwargs["sendUpdates"] == "all"
    assert event.id == "evt_new"
    assert event.start == datetime(2026, 9, 28, 15, tzinfo=SGT)


def test_create_event_without_attendee_sends_no_invites():
    svc = FakeCalendarService()
    GoogleCalendar(svc).create_event("Study", datetime(2026, 9, 28, 15, tzinfo=SGT), datetime(2026, 9, 28, 17, tzinfo=SGT))
    _, kwargs = svc.calls[0]
    assert "attendees" not in kwargs["body"]
    assert kwargs["sendUpdates"] == "none"


def test_refresh_error_becomes_auth_error():
    cal = GoogleCalendar(FakeCalendarService(exc=RefreshError("invalid_grant")))
    with pytest.raises(CalendarAuthError):
        cal.list_events(START, END)


def test_network_error_becomes_calendar_error():
    cal = GoogleCalendar(FakeCalendarService(exc=ConnectionError("down")))
    with pytest.raises(CalendarError):
        cal.list_events(START, END)


def test_google_calls_never_overlap_across_threads():
    # Review fix: the SDK runs sync tools in threads, in parallel; googleapiclient is not thread-safe.
    state = {"active": 0, "peak": 0}
    guard = threading.Lock()

    class SlowRequest:
        def execute(self):
            with guard:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time_mod.sleep(0.05)
            with guard:
                state["active"] -= 1
            return {"items": []}

    svc = SimpleNamespace(events=lambda: SimpleNamespace(list=lambda **kw: SlowRequest()))
    cal = GoogleCalendar(svc)
    threads = [threading.Thread(target=cal.list_events, args=(START, END)) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert state["peak"] == 1
