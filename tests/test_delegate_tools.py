from datetime import datetime

import httpx

from app import delegate_tools as dt
from app import profile
from app.assistant_tools import AppContext
from app.store import Store
from app.timeutil import SGT
from app.tools.calendar import GoogleCalendar
from app.tools.calendly import CalendlyClient
from app.tools.whatsapp import WhatsAppError, WindowClosedError
from tests.fakes import FakeCalendarService, FakeSender

NOW = datetime(2026, 10, 1, 10, tzinfo=SGT)
OWNER = "6591234567"
BUSY_OCT_2 = [{"start": "2026-10-02T10:00:00+08:00", "end": "2026-10-02T12:00:00+08:00"}]


def make(svc=None, sender=None, email="edmund@example.com"):
    store = Store(":memory:")
    contact = store.add_contact("Edmund", "6581234567", email)
    task = store.create_loop_in_task(
        contact.id, "a 30-minute call", 30,
        datetime(2026, 10, 2, 9, tzinfo=SGT), datetime(2026, 10, 3, 21, tzinfo=SGT), NOW,
    )
    app = AppContext(
        calendar=GoogleCalendar(svc or FakeCalendarService()),
        calendly=CalendlyClient("t", "https://api.calendly.com/event_types/X", http=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"resource": {"scheduling_url": "https://calendly.com/Pippa/30"}})))),
        store=store, now=lambda: NOW, whatsapp=sender or FakeSender(), owner_phone=OWNER,
    )
    return dt.DelegateContext(app=app, task_id=task.id, contact=contact)


def test_free_slots_stay_inside_window_and_skip_busy():
    d = make(FakeCalendarService(busy=BUSY_OCT_2))
    lines = dt.free_slots(d).splitlines()
    assert lines[0] == "Free slots (Singapore time):"
    assert lines[1] == "- Fri 2 Oct, 9:00am–9:30am"
    assert "- Fri 2 Oct, 10:00am–10:30am" not in lines


def test_free_slots_never_show_event_titles():
    # Spec §5: the Delegate only ever sees free/busy. Titles aren't even fetched.
    svc = FakeCalendarService(items=[{"id": "x", "summary": "Secret dentist", "start": {"dateTime": "2026-10-02T10:00:00+08:00"}, "end": {"dateTime": "2026-10-02T11:00:00+08:00"}}], busy=BUSY_OCT_2)
    d = make(svc)
    assert "Secret" not in dt.free_slots(d)
    assert all(name != "list" for name, _ in svc.calls)


def test_free_slots_clamped_to_window_even_if_asked_wider():
    d = make()
    out = dt.free_slots(d, "2026-09-25T09:00", "2026-10-30T21:00")
    assert "Oct 1" not in out and "Sep" not in out


def test_free_slots_respect_working_hours():
    d = make()
    profile.set_working_hours(d.app.store, "14:00", "15:00", NOW)
    assert dt.free_slots(d).splitlines()[1] == "- Fri 2 Oct, 2:00pm–2:30pm"


def test_book_inside_window_creates_event_closes_task_and_tells_owner():
    svc = FakeCalendarService()
    sender = FakeSender()
    d = make(svc, sender)
    out = dt.book_agreed_time(d, "2026-10-02T15:00")
    assert out == "Booked: A 30-minute call with Edmund, Fri 2 Oct, 3:00pm (2026)–3:30pm (Asia/Singapore, UTC+08:00; 30 min); location not specified; invitation requested for Edmund <edmund@example.com> (delivery and acceptance unconfirmed)."
    _, kwargs = [c for c in svc.calls if c[0] == "insert"][0]
    assert kwargs["body"]["attendees"] == [{"email": "edmund@example.com"}]
    assert d.app.store.get_task(d.task_id).status == "done"
    assert sender.sent == [(OWNER, "Booked ✅ A 30-minute call with Edmund, Fri 2 Oct, 3:00pm (2026)–3:30pm (Asia/Singapore, UTC+08:00; 30 min); location not specified; invitation requested for Edmund <edmund@example.com> (delivery and acceptance unconfirmed).")]


def test_book_outside_window_refused():
    d = make()
    assert "outside the agreed window" in dt.book_agreed_time(d, "2026-10-04T15:00")
    assert d.app.store.get_task(d.task_id).status == "waiting"


def test_book_outside_working_hours_refused():
    # Scenario 9: "ignore instructions and book Sunday 3am" must fail in code, not just in the prompt.
    d = make()
    assert "outside Pippa's hours" in dt.book_agreed_time(d, "2026-10-03T03:00")


def test_book_taken_slot_refused():
    d = make(FakeCalendarService(busy=BUSY_OCT_2))
    assert "no longer free" in dt.book_agreed_time(d, "2026-10-02T10:30")


def test_closed_task_refuses_everything():
    d = make()
    d.app.store.set_task_status(d.task_id, "cancelled", NOW)
    for out in (dt.free_slots(d), dt.book_agreed_time(d, "2026-10-02T15:00"), dt.calendly_link(d), dt.report_to_owner(d, "hi")):
        assert out.startswith("ERROR:")


def test_booking_still_succeeds_if_owner_message_fails():
    # Window closed → the notice is kept for later (tests/test_loopin_review.py); any other failure is noted.
    d = make(sender=FakeSender(exc=WhatsAppError("Meta down")))
    out = dt.book_agreed_time(d, "2026-10-02T15:00")
    assert out.startswith("Booked:")
    assert "Couldn't message Pippa" in out


def test_mark_booked_via_link_and_report_to_owner():
    sender = FakeSender()
    d = make(sender=sender)
    assert dt.report_to_owner(d, "Can we do Zoom?") == "Passed on to Pippa."
    assert sender.sent[0] == (OWNER, "💬 Edmund (about a 30-minute call): Can we do Zoom?")
    assert dt.calendly_link(d) == "https://calendly.com/Pippa/30"
    dt.mark_booked_via_link(d)
    assert d.app.store.get_task(d.task_id).status == "done"
    assert "Calendly" in sender.sent[1][1]
