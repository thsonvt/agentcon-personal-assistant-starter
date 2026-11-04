"""Plan 2B final-review fixes: lifecycle isolation (delete → re-sign-up, re-sign-in, disable),
the inviter hearing about their own Delegate's errors, a crash-safe migration, and leaving."""

import asyncio
import sqlite3
from datetime import datetime, timedelta

import pytest

from app import wiring
from app.commands import Commands
from app.router import CONTACT_ERROR_REPLY, IncomingMessage, Router
from app.store import Store
from app.timeutil import SGT
from app.users import Users
from app.wiring import build_runtime
from tests.fakes import FakeSender
from tests.test_multiuser_wiring import settings
from tests.test_store_scoping import _old_database

NOW = datetime(2026, 10, 1, 10, tzinfo=SGT)
Pippa, EDMUND, AKASH, EZE = "6591234567", "6581112222", "6583330000", "6584440000"


def make_users():
    users = Users(Store(":memory:"))
    users.ensure_admin("Pippa", Pippa, NOW)
    return users


def activate(users, name, email, phone, token="tok"):
    user, _ = users.start_signup(name, email, token, NOW)
    user = users.set_details(user.id, phone, None, NOW)
    users.approve(user.join_code, NOW)
    return users.join(user.join_code, phone, NOW)


# --- 1. ids are never reused, and commands wait for the sender's running reply ---------

def test_a_deleted_users_id_is_never_given_to_the_next_sign_up():
    users = make_users()
    edmund = activate(users, "Edmund", "e@gmail.com", EDMUND)
    users.delete(edmund.id)
    eze = activate(users, "Eze", "eze@gmail.com", EZE)
    assert eze.id != edmund.id


def test_delete_confirmation_waits_for_the_users_running_reply():
    users = make_users()
    edmund = activate(users, "Edmund", "e@gmail.com", EDMUND)
    order = []

    async def run_user(user, text):
        order.append("run start")
        await asyncio.sleep(0.05)
        users.store.for_user(user.id).add_fact("written by the running reply", NOW)
        order.append("run end")
        return "ok"

    async def run_owner(text):
        return "ok"

    commands = Commands(users, FakeSender(), lambda: NOW, decrypt=str, revoke=lambda t: None,
                        on_user_removed=lambda uid: order.append("deleted"))
    router = Router(Pippa, users.store, commands.sender, run_owner, lambda: NOW,
                    commands=commands, users=users, run_user=run_user)

    async def race():
        # A reply is still running when the friend sends "delete my data" and then "yes".
        running = asyncio.create_task(router.handle(IncomingMessage(EDMUND, "what's next?", "m1")))
        await asyncio.sleep(0.01)
        await router.handle(IncomingMessage(EDMUND, "delete my data", "m2"))
        await router.handle(IncomingMessage(EDMUND, "yes", "m3"))
        await running

    asyncio.run(race())
    assert order.index("deleted") > order.index("run end")
    assert users.store.for_user(edmund.id).list_facts() == []


# --- 2. signing in again takes effect straight away ------------------------------------

def test_signing_in_again_rebuilds_the_calendar_with_the_new_token(tmp_path, monkeypatch):
    built = []
    monkeypatch.setattr(wiring, "build_service", lambda cid, secret, token: built.append(token) or object())

    async def fake_run_agent(agent, app, session, text, keep=30, context=None):
        return "ok"

    monkeypatch.setattr(wiring, "run_agent", fake_run_agent)
    runtime = build_runtime(settings(tmp_path), sender=FakeSender())
    edmund = activate(runtime.users, "Edmund", "e@gmail.com", EDMUND, token=runtime.box.encrypt("1//old"))
    asyncio.run(runtime.router.handle(IncomingMessage(EDMUND, "hi", "m1")))
    # The sign-up page's re-auth path: same email, new token.
    runtime.users.start_signup("Edmund", "e@gmail.com", runtime.box.encrypt("1//new"), NOW)
    runtime.on_user_changed(edmund.id)
    asyncio.run(runtime.router.handle(IncomingMessage(EDMUND, "hi again", "m2")))
    assert built[-1] == "1//new"


def test_sign_up_routes_report_changed_users():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tests.test_signup import FakeOAuth, Site

    site = Site()
    changed = []
    app = FastAPI()
    from app.signup import signup_routes

    app.include_router(signup_routes(
        oauth=FakeOAuth(), users=site.users, box=site.box, commands=Commands(site.users, site.sender, lambda: NOW, str, lambda t: None),
        bot_whatsapp="15550001234", session_secret="s" * 32, now=lambda: NOW, on_user_changed=changed.append,
    ))
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    from urllib.parse import parse_qs, urlparse

    for _ in range(2):  # first sign-up, then signing in again
        start = client.get("/signup/google")
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        client.get("/signup/callback", params={"code": "c", "state": state})
        from tests.test_onboarding_routes import Inputs
        csrf = Inputs(client.get("/signup/number").text).values["csrf_token"]
        response = client.post("/signup/number", data={"phone": "6581112222", "calendly": "", "csrf_token": csrf})
        assert response.status_code == 303
    user = site.users.by_email("edmund@example.com")
    assert user.id in changed


# --- 3. a friend's Delegate failure is reported to that friend, not to Pippa ------------

def test_delegate_errors_go_to_the_inviter():
    users = make_users()
    edmund = activate(users, "Edmund", "e@gmail.com", EDMUND)
    mine = users.store.for_user(edmund.id)
    c = mine.add_contact("Akash", AKASH)
    mine.create_loop_in_task(c.id, "coffee", 30, NOW, NOW + timedelta(days=2), NOW)
    sender = FakeSender()

    async def run_contact(contact, task, text):
        raise RuntimeError("model down")

    async def run_owner(text):
        return "ok"

    router = Router(Pippa, users.store, sender, run_owner, lambda: NOW, run_contact=run_contact, users=users)
    asyncio.run(router.handle(IncomingMessage(AKASH, "hi", "m1")))
    assert (AKASH, CONTACT_ERROR_REPLY) in sender.sent
    assert any(to == EDMUND and "coffee" in body for to, body in sender.sent)
    assert not any(to == Pippa for to, _ in sender.sent)


# --- 4. the migration keeps table references intact ---------------------------------------

def test_upgrade_leaves_loop_in_tasks_pointing_at_contacts(tmp_path):
    path = str(tmp_path / "agent.db")
    _old_database(path)
    Store(path)
    conn = sqlite3.connect(path)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'loop_in_tasks'").fetchone()[0]
    assert "contacts_old" not in sql
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO loop_in_tasks (user_id, contact_id, purpose, duration_min, window_start, window_end, status, created_at, updated_at) "
        "VALUES (1, 1, 'x', 30, 'a', 'b', 'done', 'c', 'd')"
    )  # would fail with "no such table: contacts_old"
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not {"contacts_old", "contacts_new"} & names


# --- 5. anyone can leave; rejected sign-ups don't keep a token -------------------------------

def commands_for(users, revoked):
    return Commands(users, FakeSender(), lambda: NOW, decrypt=lambda s: f"plain:{s}", revoke=revoked.append)


def test_pending_sign_up_can_delete_their_data():
    users = make_users()
    user, _ = users.start_signup("Edmund", "e@gmail.com", "tok", NOW)
    users.set_details(user.id, EDMUND, None, NOW)  # never joined, never approved
    revoked = []
    commands = commands_for(users, revoked)
    assert "Reply yes" in commands.handle(EDMUND, "delete my data")
    commands.handle(EDMUND, "yes")
    assert users.get(user.id) is None and revoked == ["plain:tok"]


def test_disabled_user_can_still_delete_their_data():
    users = make_users()
    edmund = activate(users, "Edmund", "e@gmail.com", EDMUND)
    users.disable("2222", NOW)
    revoked = []
    commands = commands_for(users, revoked)
    assert "Reply yes" in commands.handle(EDMUND, "delete my data")
    commands.handle(EDMUND, "yes")
    assert users.get(edmund.id) is None


def test_rejecting_a_sign_up_revokes_and_forgets_its_token():
    users = make_users()
    user, _ = users.start_signup("Edmund", "e@gmail.com", "tok", NOW)
    user = users.set_details(user.id, EDMUND, None, NOW)
    revoked = []
    commands = commands_for(users, revoked)
    commands.handle(Pippa, f"reject {user.join_code}")
    assert revoked == ["plain:tok"]
    assert users.get(user.id).google_token_enc is None


def test_delete_removes_kept_notices_and_reply_history_for_the_phone():
    users = make_users()
    edmund = activate(users, "Edmund", "e@gmail.com", EDMUND)
    users.store.queue_notice(EDMUND, "Booked ✅ …", NOW)
    users.store.should_send_unknown_reply(EDMUND, NOW)
    users.delete(edmund.id)
    assert users.store.take_notices(EDMUND) == []
    assert users.store.should_send_unknown_reply(EDMUND, NOW) is True  # no history left behind


# --- 6. a disabled friend's invites stop -------------------------------------------------------

def test_disabling_a_user_cancels_their_open_invites():
    users = make_users()
    edmund = activate(users, "Edmund", "e@gmail.com", EDMUND)
    mine = users.store.for_user(edmund.id)
    c = mine.add_contact("Akash", AKASH)
    task = mine.create_loop_in_task(c.id, "coffee", 30, NOW, NOW + timedelta(days=2), NOW)
    users.disable("2222", NOW)
    assert mine.get_task(task.id).status == "cancelled"
    assert users.store.find_open_invite(AKASH) is None


def test_contact_runs_never_fall_back_to_Pippas_context(tmp_path, monkeypatch):
    monkeypatch.setattr(wiring, "build_service", lambda *a: object())
    runs = []

    async def fake_run_agent(agent, app, session, text, keep=30, context=None):
        runs.append(app)
        return "ok"

    monkeypatch.setattr(wiring, "run_agent", fake_run_agent)
    runtime = build_runtime(settings(tmp_path), sender=FakeSender())
    from app.store import Contact, LoopInTask

    ghost = Contact(id=99, name="Akash", phone=AKASH, email=None, user_id=42)  # inviter no longer exists
    task = LoopInTask(99, 99, "coffee", 30, NOW.isoformat(), NOW.isoformat(), "negotiating", None, user_id=42)
    reply = asyncio.run(runtime.router.run_contact(ghost, task, "hi"))
    assert runs == []
    assert "no longer active" in reply


# --- 7. an unreadable token asks the friend to sign in again -----------------------------------

def test_unreadable_token_asks_the_user_to_sign_in_again(tmp_path, monkeypatch):
    monkeypatch.setattr(wiring, "build_service", lambda *a: object())
    runtime = build_runtime(settings(tmp_path), sender=FakeSender())
    activate(runtime.users, "Edmund", "e@gmail.com", EDMUND, token="sealed-with-an-old-key")
    asyncio.run(runtime.router.handle(IncomingMessage(EDMUND, "hi", "m1")))
    body = runtime.router.sender.sent[-1][1]
    assert "sign in again" in body and "https://bot.example.com" in body
