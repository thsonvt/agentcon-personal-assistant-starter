"""Plan 2B: the running app keeps every user apart (spec §4, §8 "isolation, the core property")."""

import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import wiring
from app.config import REQUIRED, load_settings
from app.crypto import generate_key
from app.nudge import run_nudges
from app.store import Store
from app.timeutil import SGT
from app.webhook import create_app
from app.wiring import build_runtime
from tests.fakes import FakeSender

NOW = datetime(2026, 10, 1, 10, tzinfo=SGT)
Pippa, EDMUND, AKASH = "6591234567", "6581112222", "6583330000"


def settings(tmp_path, signup=True):
    env = {key: "x" for key in REQUIRED} | {"OWNER_WHATSAPP": Pippa, "DB_PATH": str(tmp_path / "agent.db")}
    if signup:
        env |= {
            "GOOGLE_WEB_CLIENT_ID": "web-id", "GOOGLE_WEB_CLIENT_SECRET": "web-secret",
            "BASE_URL": "https://bot.example.com", "BOT_WHATSAPP": "15550001234",
            "TOKEN_KEY": generate_key(), "SESSION_SECRET": "s" * 32,
        }
    return load_settings(env)


@pytest.fixture
def world(tmp_path, monkeypatch):
    built = []  # (client_id, client_secret, refresh_token) for every calendar built

    def fake_build_service(client_id, client_secret, refresh_token):
        built.append((client_id, client_secret, refresh_token))
        return object()

    monkeypatch.setattr(wiring, "build_service", fake_build_service)
    runs = []

    async def fake_run_agent(agent, app, session, text, keep=30, context=None):
        runs.append({"agent": agent.name, "app": app, "session": session.session_id, "context": context})
        return "ok"

    monkeypatch.setattr(wiring, "run_agent", fake_run_agent)
    runtime = build_runtime(settings(tmp_path), sender=FakeSender())
    user, _ = runtime.users.start_signup("Edmund Lim", "e@gmail.com", runtime.box.encrypt("1//edmund"), NOW)
    user = runtime.users.set_details(user.id, EDMUND, "https://calendly.com/edmund", NOW)
    runtime.users.approve(user.join_code, NOW)
    edmund = runtime.users.join(user.join_code, EDMUND, NOW)
    return runtime, edmund, built, runs


def send(runtime, phone, text, mid):
    from app.router import IncomingMessage

    asyncio.run(runtime.router.handle(IncomingMessage(sender=phone, text=text, message_id=mid)))


def test_friend_gets_their_own_calendar_facts_session_and_counter(world):
    runtime, edmund, built, runs = world
    runtime.store.add_fact("Pippa prefers Zoom", NOW)
    send(runtime, EDMUND, "what's my next meeting?", "m1")
    send(runtime, Pippa, "and mine?", "m2")
    mine, Pippas = runs[0], runs[1]
    # Edmund's calendar uses the web client with his decrypted token; Pippa's uses the desktop client.
    assert ("web-id", "web-secret", "1//edmund") in built
    assert mine["app"] is not Pippas["app"]
    assert mine["app"].calendar is not Pippas["app"].calendar
    assert (mine["app"].store.user_id, mine["app"].owner_name, mine["app"].owner_phone) == (edmund.id, "Edmund", EDMUND)
    assert mine["app"].store.list_facts() == []  # Pippa's fact is invisible to Edmund
    assert mine["session"] == EDMUND and Pippas["session"] == Pippa
    assert mine["app"].calendly.scheduling_url() == "https://calendly.com/edmund"
    assert "bot.example.com" in mine["app"].reauth_message
    assert mine["app"].intro_template == "loop_in_intro_named"


def test_friend_context_is_built_once_and_reused(world):
    runtime, _, built, runs = world
    send(runtime, EDMUND, "one", "m1")
    send(runtime, EDMUND, "two", "m2")
    assert runs[0]["app"] is runs[1]["app"]
    assert sum(1 for b in built if b[0] == "web-id") == 1


def test_friends_invites_run_on_a_copy_of_their_context_with_a_scoped_session(world):
    runtime, edmund, _, runs = world
    mine = runtime.store.for_user(edmund.id)
    c = mine.add_contact("Akash", AKASH)
    task = mine.create_loop_in_task(c.id, "coffee", 30, NOW, NOW + timedelta(days=2), NOW)
    send(runtime, AKASH, "sure", "m1")
    run = runs[-1]
    assert run["agent"] == "Delegate"
    assert run["app"].store.user_id == edmund.id and run["app"].owner_name == "Edmund"
    assert run["session"] == f"{edmund.id}:{AKASH}"
    assert run["context"].task_id == task.id


def test_removed_users_context_is_forgotten(world):
    runtime, edmund, _, runs = world
    send(runtime, EDMUND, "hi", "m1")
    send(runtime, Pippa, "disable 2222", "m2")
    assert runtime.users.get(edmund.id).status == "disabled"
    assert edmund.id not in runtime.user_contexts


def test_nudges_go_to_the_user_who_sent_the_invite(tmp_path):
    store = Store(str(tmp_path / "a.db"))
    old = NOW - timedelta(hours=49)
    for uid, phone in ((1, "6580001111"), (2, "6580002222")):
        s = store.for_user(uid)
        c = s.add_contact(f"C{uid}", phone)
        s.create_loop_in_task(c.id, "a call", 30, old, NOW + timedelta(days=2), old)
    sender = FakeSender()
    phones = {1: Pippa, 2: EDMUND}
    assert run_nudges(store, sender, Pippa, NOW, phone_for_user=phones.get) == 2
    assert sorted(to for to, _ in sender.sent) == sorted([Pippa, EDMUND])
    assert all(t.status == "nudged" for uid in (1, 2) for t in [store.for_user(uid).get_task(uid)])


def test_signup_pages_are_served_only_when_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(wiring, "build_service", lambda *a: object())
    on = build_runtime(settings(tmp_path / "on"), sender=FakeSender())
    app = create_app("v", "secret", on.store, on.router.handle, lambda: NOW, routes=on.routes)
    page = TestClient(app).get("/signup")
    assert page.url.path == "/signin" and 'href="/signin/google"' in page.text
    off = build_runtime(settings(tmp_path / "off", signup=False), sender=FakeSender())
    assert off.users.get(1).name == "Pippa"
    app = create_app("v", "secret", off.store, off.router.handle, lambda: NOW, routes=off.routes)
    home = TestClient(app).get("/")  # a plain home page (SON-73), but no way to sign up
    assert home.status_code == 200 and "Sign in with Google" not in home.text
    assert TestClient(app).get("/signup/google").status_code == 404
    assert TestClient(app).get("/signin").status_code == 200
    assert 'href="/signin/google"' not in TestClient(app).get("/signin").text
