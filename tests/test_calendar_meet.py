from copy import deepcopy
from datetime import datetime

import pytest

from app.timeutil import SGT
from app.tools.calendar import GoogleCalendar
from tests.fakes import FakeCalendarService


START = datetime(2026, 10, 22, 11, tzinfo=SGT)
END = datetime(2026, 10, 22, 11, 30, tzinfo=SGT)


@pytest.mark.parametrize("guests", [
    {"attendee_email": "guest@example.com"},
    {"attendee_emails": ["first@example.com", "second@example.com"]},
])
def test_guest_invites_request_unique_google_meet_conferences(guests):
    service = FakeCalendarService()
    calendar = GoogleCalendar(service)
    for _ in range(2):
        calendar.create_event("Call", START, END, **guests)
    requests = [params for name, params in service.calls if name == "insert"]
    conferences = [params["body"]["conferenceData"]["createRequest"] for params in requests]
    assert all(params["conferenceDataVersion"] == 1 and params["sendUpdates"] == "all" for params in requests)
    assert all(conference["conferenceSolutionKey"] == {"type": "hangoutsMeet"} for conference in conferences)
    assert conferences[0]["requestId"] and conferences[0]["requestId"] != conferences[1]["requestId"]


@pytest.mark.parametrize("details", [
    {},
    {"attendee_email": "guest@example.com", "location": "Library"},
    {"attendee_email": "guest@example.com", "location": "https://zoom.us/j/123"},
    {"attendee_email": "guest@example.com", "location": "https://meet.google.com/existing"},
])
def test_solo_entries_and_supplied_locations_do_not_generate_meet(details):
    service = FakeCalendarService()
    GoogleCalendar(service).create_event("Meeting", START, END, **details)
    params = service.calls[0][1]
    assert "conferenceData" not in params["body"]
    assert params["body"].get("location") == details.get("location")


@pytest.mark.parametrize("link_field", ["hangoutLink", "conferenceData"])
def test_readback_returns_google_generated_video_link(link_field):
    event = {"id": "event", "start": {"dateTime": START.isoformat()}, "end": {"dateTime": END.isoformat()}}
    link = "https://meet.google.com/abc-defg-hij"
    event[link_field] = link if link_field == "hangoutLink" else {
        "entryPoints": [{"entryPointType": "phone", "uri": "tel:+123"},
                        {"entryPointType": "video", "uri": link}]
    }
    calendar = GoogleCalendar(FakeCalendarService(items=[event]))
    assert calendar.get_event("event").meet_link == link
    assert calendar.list_events(START, END)[0].meet_link == link


def test_rescheduling_preserves_conferencing_support_without_requesting_a_new_room():
    event = {"id": "event", "start": {"dateTime": START.isoformat()}, "end": {"dateTime": END.isoformat()},
             "hangoutLink": "https://meet.google.com/abc-defg-hij"}
    service = FakeCalendarService(items=[deepcopy(event)])
    updated = GoogleCalendar(service).update_event("event", title="Renamed")
    params = service.calls[0][1]
    assert params["conferenceDataVersion"] == 1
    assert params["body"] == {"summary": "Renamed"}
    assert updated.meet_link == event["hangoutLink"]
