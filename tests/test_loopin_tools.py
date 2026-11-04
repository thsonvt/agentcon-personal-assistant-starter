from datetime import datetime

import httpx

from app import loopin_tools as lt
from app.assistant_tools import AppContext
from app.store import Store
from app.timeutil import SGT
from app.tools.calendar import GoogleCalendar
from app.tools.calendly import CalendlyClient
from app.tools.whatsapp import WhatsAppError
from tests.fakes import FakeCalendarService, FakeSender

NOW = datetime(2026, 10, 1, 10, tzinfo=SGT)


def make_app(sender=None):
    return AppContext(
        calendar=GoogleCalendar(FakeCalendarService()),
        calendly=CalendlyClient("t", "https://api.calendly.com/event_types/X", http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))),
        store=Store(":memory:"),
        now=lambda: NOW,
        whatsapp=sender or FakeSender(),
        owner_phone="6591234567",
    )


def test_save_and_list_contacts_hide_full_numbers():
    app = make_app()
    assert lt.save_contact(app, "Edmund Lim", "+65 8123 4567", "e@x.com") == "Saved contact: Edmund Lim (+65 …4567), e@x.com"
    out = lt.list_contacts(app)
    assert "Edmund Lim (…4567)" in out
    assert "81234567" not in out


def test_save_contact_rejects_short_number_and_owner():
    app = make_app()
    assert lt.save_contact(app, "Ed", "1234", None).startswith("ERROR:")
    assert lt.save_contact(app, "Me", "+65 9123 4567", None).startswith("ERROR:")


def test_start_loop_in_creates_task_and_sends_template():
    sender = FakeSender()
    app = make_app(sender)
    lt.save_contact(app, "Edmund Lim", "6581234567", None)
    out = lt.start_loop_in(app, "Edmund", "a 30-minute call", 30, "2026-10-05T09:00", "2026-10-09T18:00")
    assert out.startswith("Invite sent to Edmund Lim")
    assert sender.templates == [("6581234567", "loop_in_intro", ["Edmund", "a 30-minute call"])]
    task = app.store.get_open_task(app.store.find_contact("Edmund").id)
    assert task.status == "waiting"
    assert task.duration_min == 30


def test_start_loop_in_unknown_contact_asks_for_number():
    out = lt.start_loop_in(make_app(), "Akash", "a call", 30, "2026-10-05T09:00", "2026-10-09T18:00")
    assert out.startswith("ERROR:")
    assert "number" in out


def test_second_open_invite_refused():
    app = make_app()
    lt.save_contact(app, "Edmund", "6581234567", None)
    lt.start_loop_in(app, "Edmund", "a call", 30, "2026-10-05T09:00", "2026-10-09T18:00")
    out = lt.start_loop_in(app, "Edmund", "another call", 30, "2026-10-05T09:00", "2026-10-09T18:00")
    assert "already has an open invite" in out


def test_window_in_past_refused():
    app = make_app()
    lt.save_contact(app, "Edmund", "6581234567", None)
    assert lt.start_loop_in(app, "Edmund", "a call", 30, "2026-09-01T09:00", "2026-09-02T18:00").startswith("ERROR:")


def test_template_failure_cancels_task():
    # Review Focus: an unapproved template or unverified recipient must not leave a dangling open task.
    app = make_app(FakeSender(exc=WhatsAppError("(#131030) Recipient phone number not in allowed list")))
    lt.save_contact(app, "Edmund", "6581234567", None)
    out = lt.start_loop_in(app, "Edmund", "a call", 30, "2026-10-05T09:00", "2026-10-09T18:00")
    assert out.startswith("ERROR:")
    assert "allowed list" in out
    assert app.store.get_open_task(app.store.find_contact("Edmund").id) is None


def test_cancel_loop_in():
    app = make_app()
    lt.save_contact(app, "Edmund", "6581234567", None)
    lt.start_loop_in(app, "Edmund", "a call", 30, "2026-10-05T09:00", "2026-10-09T18:00")
    assert lt.cancel_loop_in(app, "edmund") == "Cancelled the invite to Edmund (a call)."
    assert lt.cancel_loop_in(app, "edmund") == "Edmund has no open invite."


def test_ambiguous_name_asks_which_one():
    app = make_app()
    lt.save_contact(app, "Edmund Lim", "6581234567", None)
    lt.save_contact(app, "Edmund Tan", "6589999999", None)
    out = lt.cancel_loop_in(app, "Edmund")
    assert out.startswith("ERROR:")
    assert "Edmund Lim" in out and "Edmund Tan" in out
