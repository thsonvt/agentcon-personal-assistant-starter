"""Regression seams for SON-79/80/81/82 integration, without live accounts."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from agents import SQLiteSession

from app import agents as agent_module, wiring
from app.assistant_tools import AppContext
from app.config import REQUIRED, ConfigError, load_settings
from app.router import IncomingMessage, Router
from app.store import Store
from app.timeutil import SGT
from app.tools.calendar import GoogleCalendar
from tests.fakes import FakeCalendarService, FakeSender
from tests.test_google_oauth import google
from tests.test_signup import FakeOAuth, Site

NOW = datetime(2026, 10, 1, 10, tzinfo=SGT)
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def app_context():
    return AppContext(GoogleCalendar(FakeCalendarService()), None, Store(":memory:"), lambda: NOW)


def test_owner_agent_exposes_email_and_full_event_details_but_delegate_does_not():
    names = {t.name for t in agent_module.build_assistant("m").tools}
    assert {"search_email", "read_email", "get_event_details"} <= names
    assert not {"search_email", "read_email", "get_event_details"} & {
        t.name for t in agent_module.build_delegate("m").tools
    }


def test_event_details_preserve_embedded_maps_link_even_when_location_empty():
    from app import gmail_tools

    app = app_context()
    app.calendar = GoogleCalendar(FakeCalendarService(items=[{
        "id": "dinner", "summary": "Dinner with Chris", "location": "",
        "start": {"dateTime": "2026-10-01T18:00:00+08:00"},
        "end": {"dateTime": "2026-10-01T19:00:00+08:00"},
        "description": '<a href="https://maps.app.goo.gl/example">Dinner venue</a>',
        "htmlLink": "https://calendar.google.com/calendar/event?eid=example",
    }]))
    out = gmail_tools.get_event_details(app, "dinner")
    assert "https://maps.app.goo.gl/example" in out
    assert "UNTRUSTED" in out
    assert "No location exists" not in out
    assert app.untrusted_read_this_run is True


def test_normal_oauth_does_not_request_gmail_but_explicit_option_does():
    oauth, _ = google()
    assert GMAIL_SCOPE not in parse_qs(urlparse(oauth.auth_url("s")).query)["scope"][0]
    scopes = parse_qs(urlparse(oauth.auth_url("s", include_gmail=True)).query)["scope"][0]
    assert GMAIL_SCOPE in scopes
    assert "gmail.modify" not in scopes and "gmail.send" not in scopes


def test_oauth_reports_actual_grant_without_requiring_gmail_for_calendar():
    oauth, _ = google()
    assert oauth.exchange("c").gmail_enabled is False
    oauth, _ = google(token_json={
        "access_token": "a", "refresh_token": "r",
        "scope": f"https://www.googleapis.com/auth/calendar {GMAIL_SCOPE}",
    })
    assert oauth.exchange("c").gmail_enabled is True


def test_optional_gmail_connection_explains_readonly_and_does_not_claim_success_if_declined():
    class OAuth(FakeOAuth):
        def auth_url(self, state, include_gmail=False):
            self.include_gmail = include_gmail
            return super().auth_url(state)

    oauth = OAuth()
    site = Site(oauth=oauth)
    page = site.client.get("/signup/gmail")
    assert page.status_code == 200
    assert "read-only" in page.text.lower()
    assert "OpenAI" in page.text
    start = site.client.get("/signup/google?gmail=1")
    assert oauth.include_gmail is True
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    response = site.client.get("/signup/callback", params={"code": "c", "state": state})
    assert response.status_code == 303
    assert "Gmail not connected" in site.client.get("/signup/status").text
    assert site.users.by_email("edmund@example.com") is not None


def test_owner_voice_language_config_is_separate_and_validated():
    env = {key: "x" for key in REQUIRED} | {"OWNER_WHATSAPP": "6591234567"}
    assert load_settings(env).owner_voice_language == "en"
    assert load_settings(env | {"OWNER_VOICE_LANGUAGE": "auto"}).owner_voice_language == "auto"
    with pytest.raises(ConfigError):
        load_settings(env | {"OWNER_VOICE_LANGUAGE": "english please"})


def test_router_passes_sender_to_transcriber_only_after_authorizing_them():
    calls, runs = [], []

    async def run(text):
        runs.append(text)
        return "ok"

    router = Router("6591234567", Store(":memory:"), FakeSender(), run, lambda: NOW,
                    transcribe_for_sender=lambda media, phone: calls.append((media, phone)) or "Meet Quan")
    asyncio.run(router.handle(IncomingMessage("6580000000", None, "unknown", "audio0")))
    assert calls == []
    asyncio.run(router.handle(IncomingMessage("6591234567", None, "owner", "audio1")))
    assert calls == [("audio1", "6591234567")]
    assert runs == ["[voice] Meet Quan"]


def test_run_agent_sets_voice_origin_and_resets_untrusted_read_flag(monkeypatch):
    app = app_context()
    states = []

    async def fake_run(agent, text, **kwargs):
        states.append((app.current_input_is_voice, app.untrusted_read_this_run))
        app.untrusted_read_this_run = True
        return SimpleNamespace(final_output="ok")

    monkeypatch.setattr(agent_module.Runner, "run", fake_run)
    session = SQLiteSession("voice-metadata")
    asyncio.run(agent_module.run_agent(agent_module.build_assistant("m"), app, session, "[voice] meet Quan"))
    asyncio.run(agent_module.run_agent(agent_module.build_assistant("m"), app, session, "no, Quan"))
    assert states == [(True, False), (False, False)]


def test_runtime_gmail_and_voice_are_scoped_per_user(tmp_path, monkeypatch):
    from app.crypto import generate_key

    calls, runs, transcriptions = [], [], []
    monkeypatch.setattr(wiring, "build_service", lambda *args: object())
    monkeypatch.setattr(wiring, "build_gmail_service", lambda *args: calls.append(args) or object())

    class Voice:
        def transcribe(self, *args, **kwargs):
            transcriptions.append(kwargs)
            return "hello"

    monkeypatch.setattr(wiring, "make_transcriber", lambda settings: Voice())
    monkeypatch.setattr(wiring.WhatsAppClient, "download_media", lambda self, mid: (b"audio", "audio/ogg"))

    async def run(agent, app, session, text, **kwargs):
        runs.append(app)
        return "ok"

    monkeypatch.setattr(wiring, "run_agent", run)
    env = {key: "x" for key in REQUIRED} | {
        "OWNER_WHATSAPP": "6591234567", "DB_PATH": str(tmp_path / "app.db"),
        "GOOGLE_WEB_CLIENT_ID": "web", "GOOGLE_WEB_CLIENT_SECRET": "secret",
        "BASE_URL": "https://bot.example.com", "BOT_WHATSAPP": "15550001234",
        "TOKEN_KEY": generate_key(), "SESSION_SECRET": "s" * 32,
    }
    runtime = wiring.build_runtime(load_settings(env), sender=FakeSender())
    runtime.store.add_contact("OwnerOnlyName", "6589000000")
    user, _ = runtime.users.start_signup("Edmund", "edmund@example.com", runtime.box.encrypt("friend-token"), NOW)
    user = runtime.users.set_details(user.id, "6581112222", None, NOW)
    runtime.users.approve(user.join_code, NOW)
    runtime.users.join(user.join_code, "6581112222", NOW)
    for phone in ("6591234567", "6581112222"):
        asyncio.run(runtime.router.handle(IncomingMessage(phone, None, phone, "audio")))
    assert runs[0].gmail is not runs[1].gmail
    assert ("web", "secret", "friend-token") in calls
    assert transcriptions[0]["language"] == "en"
    assert "OwnerOnlyName" in transcriptions[0]["prompt"]
    assert transcriptions[1]["language"] == "auto"
    assert "OwnerOnlyName" not in transcriptions[1]["prompt"]
    assert "/connections/gmail" in runs[1].gmail_reauth_message


def test_unconfirmed_booking_cannot_be_reported_as_booked_by_model(monkeypatch):
    from agents.tool_context import ToolContext
    import json

    app = app_context()
    async def fake_run(agent, text, **kwargs):
        tool = next(t for t in agent.tools if t.name == "create_event")
        args = json.dumps({"title": "Meeting with Quan", "start": "2026-10-02T14:00:00+08:00",
                           "end": "2026-10-02T15:00:00+08:00", "booking_mode": "invitation",
                           "attendee_name": "Quan", "attendee_email": "quan@example.com", "confirmed": False})
        await tool.on_invoke_tool(ToolContext(app, tool_name="create_event", tool_call_id="book", tool_arguments=args), args)
        return SimpleNamespace(final_output="Booked Meeting with Quan tomorrow!")
    monkeypatch.setattr(agent_module.Runner, "run", fake_run)
    reply = asyncio.run(agent_module.run_agent(agent_module.build_assistant("m"), app,
                        SQLiteSession("confirm-output"), "[voice] meet Quan tomorrow"))
    assert "Please confirm" in reply
    assert "quan@example.com" in reply
    assert not reply.startswith("Booked")
    assert app.calendar.service.calls == []


def test_gmail_oauth_error_retry_keeps_explicit_gmail_intent():
    from app.tools.google_oauth import GoogleOAuthError

    class OAuth(FakeOAuth):
        def auth_url(self, state, include_gmail=False):
            return super().auth_url(state)
    site = Site(oauth=OAuth(error=GoogleOAuthError("Please retry")))
    start = site.client.get("/signup/google?gmail=1")
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    reply = site.client.get("/signup/callback", params={"state": state, "code": "bad"})
    assert 'href="/signup/google?gmail=1"' in reply.text


def test_voice_loop_in_cannot_message_a_contact_until_exact_identity_confirmed():
    import json
    from agents.tool_context import ToolContext

    app = app_context()
    app.whatsapp = FakeSender()
    app.store.add_contact("Wong", "6589990000")
    app.current_input_is_voice = True
    app.run_no = 1
    args = {"contact_name": "Wong", "purpose": "coffee", "duration_min": 30,
            "window_start": "2026-10-02T14:00:00+08:00", "window_end": "2026-10-02T18:00:00+08:00",
            "confirmed": False}
    tool = next(t for t in agent_module.build_assistant("m").tools if t.name == "start_loop_in")
    def invoke():
        raw = json.dumps(args)
        return asyncio.run(tool.on_invoke_tool(ToolContext(app, tool_name="start_loop_in", tool_call_id="loop",
                                                          tool_arguments=raw), raw))
    assert invoke().startswith("CONFIRM:")
    assert "Wong" in app.booking_reply
    assert app.whatsapp.templates == []
    args["confirmed"] = True
    assert invoke().startswith("CONFIRM:")  # model cannot answer its own question
    assert app.whatsapp.templates == []
    app.run_no = 2
    app.current_input_is_voice = False
    assert invoke().startswith("Invite sent")
    assert len(app.whatsapp.templates) == 1
