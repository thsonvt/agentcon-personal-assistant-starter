"""Plan 2B: each user's assistant speaks for them — their name, their link, their re-sign-in message."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
from google.auth.exceptions import RefreshError

from app import assistant_tools as t
from app import delegate_tools as dt
from app import loopin_tools as lt
from app.agents import assistant_instructions, build_assistant, build_delegate, delegate_instructions
from app.assistant_tools import AppContext
from app.store import Store
from app.timeutil import SGT
from app.tools.calendar import GoogleCalendar
from app.tools.calendly import BookingLink
from tests.fakes import FakeCalendarService, FakeSender

NOW = datetime(2026, 10, 1, 10, tzinfo=SGT)
EDMUND_REAUTH = "ERROR: Edmund's Google access has expired. Ask him to sign in again at https://bot.example.com"


def edmund_app(svc=None, link=None, store=None):
    return AppContext(
        calendar=GoogleCalendar(svc or FakeCalendarService()),
        calendly=BookingLink(link),
        store=(store or Store(":memory:")).for_user(2),
        now=lambda: NOW,
        whatsapp=FakeSender(),
        owner_phone="6581234567",
        owner_name="Edmund",
        reauth_message=EDMUND_REAUTH,
        intro_template="loop_in_intro_named",
        intro_with_owner_name=True,
    )


def test_instructions_name_the_user_not_Pippa():
    app = edmund_app()
    text = assistant_instructions(SimpleNamespace(context=app), build_assistant("m"))
    assert "You are Edmund's personal scheduling assistant" in text
    assert "Pippa" not in text


def test_tool_descriptions_never_name_Pippa():
    # Docstrings are shared by every user's agent, so they must be neutral.
    for agent in (build_assistant("m"), build_delegate("m")):
        for tool in agent.tools:
            assert "Pippa" not in tool.description, tool.name


def test_delegate_speaks_for_the_inviter():
    store = Store(":memory:")
    app = edmund_app(store=store)
    c = app.store.add_contact("Akash", "6583330000")
    task = app.store.create_loop_in_task(c.id, "a call", 30, NOW, NOW + timedelta(days=2), NOW)
    d = dt.DelegateContext(app=app, task_id=task.id, contact=c)
    text = delegate_instructions(SimpleNamespace(context=d), build_delegate("m"))
    assert "You are Edmund's scheduling assistant" in text
    assert "Pippa" not in text
    assert dt.report_to_owner(d, "Zoom?") == "Passed on to Edmund."


def test_expired_google_access_tells_this_user_how_to_fix_it():
    app = edmund_app(FakeCalendarService(exc=RefreshError("invalid_grant")))
    assert t.list_events(app, "2026-10-01T10:00", "2026-10-02T10:00") == EDMUND_REAUTH


def test_booking_link_for_users_without_calendly_api():
    assert t.get_calendly_link(edmund_app(link="https://calendly.com/edmund/30min")) == "https://calendly.com/edmund/30min"
    assert t.get_calendly_link(edmund_app()).startswith("ERROR: No booking link")


def test_friends_invites_use_the_named_template():
    # loop_in_intro says "I'm Pippa's scheduling assistant", so friends need loop_in_intro_named.
    app = edmund_app()
    app.store.add_contact("Akash Tan", "6583330000")
    out = lt.start_loop_in(app, "Akash", "a 30-minute call", 30, "2026-10-02T09:00", "2026-10-03T18:00")
    assert out.startswith("Invite sent")
    assert app.whatsapp.templates == [("6583330000", "loop_in_intro_named", ["Akash", "Edmund", "a 30-minute call"])]


def test_one_open_invite_per_phone_across_all_users():
    # Otherwise the router couldn't tell whose Delegate should answer (D25).
    store = Store(":memory:")
    Pippa = store.for_user(1)
    c = Pippa.add_contact("Akash", "6583330000")
    Pippa.create_loop_in_task(c.id, "a call", 30, NOW, NOW + timedelta(days=2), NOW)
    app = edmund_app(store=store)
    app.store.add_contact("Akash", "6583330000")
    out = lt.start_loop_in(app, "Akash", "coffee", 30, "2026-10-02T09:00", "2026-10-03T18:00")
    assert out.startswith("ERROR:") and "already has an open invite" in out
    assert app.whatsapp.templates == []
