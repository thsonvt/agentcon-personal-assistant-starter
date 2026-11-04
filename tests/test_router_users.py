"""Plan 2B: who gets which agent, now that friends can sign up (spec §5)."""

import asyncio
from datetime import datetime, timedelta

from app.commands import WELCOME, Commands
from app.router import CLOSED_TASK_REPLY, JOIN_HINT, PENDING_REPLY, UNKNOWN_REPLY, IncomingMessage, Router
from app.store import Store
from app.timeutil import SGT
from app.users import Users
from tests.fakes import FakeSender

NOW = datetime(2026, 10, 1, 10, tzinfo=SGT)
Pippa, EDMUND, AKASH, STRANGER = "6591234567", "6581112222", "6583330000", "6580000000"


class World:
    def __init__(self):
        self.store = Store(":memory:")
        self.users = Users(self.store)
        self.users.ensure_admin("Pippa", Pippa, NOW)
        self.sender = FakeSender()
        self.calls: list[tuple] = []
        self.commands = Commands(self.users, self.sender, lambda: NOW, decrypt=str, revoke=lambda t: None)

        async def run_owner(text):
            self.calls.append(("Pippa", text))
            return "Pippa's assistant"

        async def run_user(user, text):
            self.calls.append((user.first_name.lower(), text))
            return f"{user.first_name}'s assistant"

        async def run_contact(contact, task, text):
            self.calls.append(("delegate", contact.user_id, task.id, text))
            return "delegate"

        self.router = Router(
            Pippa, self.store, self.sender, run_owner, lambda: NOW,
            run_contact=run_contact, transcribe=lambda media_id: "voice words",
            commands=self.commands, users=self.users, run_user=run_user,
        )

    def active_user(self, name, email, phone):
        user, _ = self.users.start_signup(name, email, "tok", NOW)
        user = self.users.set_details(user.id, phone, None, NOW)
        self.users.approve(user.join_code, NOW)
        return self.users.join(user.join_code, phone, NOW)

    def send(self, phone, text="hi", audio_id=None, mid="m1"):
        asyncio.run(self.router.handle(IncomingMessage(sender=phone, text=text, message_id=mid, audio_id=audio_id)))


def test_active_user_gets_their_own_assistant():
    w = World()
    w.active_user("Edmund Lim", "e@gmail.com", EDMUND)
    w.send(EDMUND, "what's my next meeting?")
    assert w.calls == [("edmund", "what's my next meeting?")]
    assert w.sender.sent == [(EDMUND, "Edmund's assistant")]
    assert w.sender.typing == ["m1"]


def test_owner_still_gets_the_owner_assistant():
    w = World()
    w.send(Pippa, "hi")
    assert w.calls == [("Pippa", "hi")]


def test_active_users_voice_notes_are_transcribed():
    w = World()
    w.active_user("Edmund Lim", "e@gmail.com", EDMUND)
    w.send(EDMUND, text=None, audio_id="MEDIA1")
    assert w.calls == [("edmund", "[voice] voice words")]
    assert w.sender.sent[0][1].startswith("🎙️ Heard: voice words")


def test_commands_run_before_any_agent():
    w = World()
    user, _ = w.users.start_signup("Edmund Lim", "e@gmail.com", "tok", NOW)
    user = w.users.set_details(user.id, EDMUND, None, NOW)
    w.send(Pippa, f"approve {user.join_code}")
    w.send(EDMUND, f"join {user.join_code}", mid="m2")
    assert w.calls == []  # no model run for either
    assert any(to == EDMUND and body.startswith(WELCOME) and "Gmail" in body for to, body in w.sender.sent)


def test_signed_up_but_not_yet_active_gets_the_waiting_reply():
    w = World()
    user, _ = w.users.start_signup("Edmund Lim", "e@gmail.com", "tok", NOW)
    user = w.users.set_details(user.id, EDMUND, None, NOW)
    w.send(EDMUND, f"join {user.join_code}")  # joined, not approved
    w.send(EDMUND, "hello?", mid="m2")
    assert w.calls == []
    assert w.sender.sent[-1] == (EDMUND, PENDING_REPLY.format(admin="Pippa"))
    assert w.sender.typing == []


def test_disabled_user_is_treated_as_a_stranger():
    w = World()
    w.active_user("Edmund Lim", "e@gmail.com", EDMUND)
    w.users.disable("2222", NOW)
    w.send(EDMUND, "hi")
    assert w.calls == []
    assert w.sender.sent == [(EDMUND, UNKNOWN_REPLY)]


def test_open_invite_outranks_the_senders_own_assistant():
    # D25: the demo friend is also a user, but their reply must reach Pippa's Delegate.
    w = World()
    w.active_user("Edmund Lim", "e@gmail.com", EDMUND)
    Pippa = w.store.for_user(1)
    c = Pippa.add_contact("Edmund", EDMUND)
    task = Pippa.create_loop_in_task(c.id, "a call", 30, NOW, NOW + timedelta(days=2), NOW)
    w.send(EDMUND, "Friday 3pm works")
    assert w.calls == [("delegate", 1, task.id, "Friday 3pm works")]
    Pippa.set_task_status(task.id, "done", NOW)
    w.send(EDMUND, "what's next for me?", mid="m2")
    assert w.calls[-1] == ("edmund", "what's next for me?")


def test_invites_sent_by_friends_reach_their_delegate():
    w = World()
    edmund = w.active_user("Edmund Lim", "e@gmail.com", EDMUND)
    mine = w.store.for_user(edmund.id)
    c = mine.add_contact("Akash", AKASH)
    task = mine.create_loop_in_task(c.id, "coffee", 30, NOW, NOW + timedelta(days=2), NOW)
    w.send(AKASH, "sure, when?")
    assert w.calls == [("delegate", edmund.id, task.id, "sure, when?")]


def test_strangers_are_unchanged():
    w = World()
    w.send(STRANGER, "hello")
    assert w.calls == []
    assert w.sender.sent == [(STRANGER, UNKNOWN_REPLY)]


def test_signed_up_but_not_joined_is_told_to_send_the_code():
    # Review Focus: a friend who says "hi" before sending "join CODE" shouldn't hear "private assistant".
    w = World()
    user, _ = w.users.start_signup("Edmund Lim", "e@gmail.com", "tok", NOW)
    w.users.set_details(user.id, EDMUND, None, NOW)
    w.send(EDMUND, "hi")
    assert w.calls == []
    assert w.sender.sent == [(EDMUND, JOIN_HINT)]


def test_thanks_after_a_friends_booking_reaches_that_friend():
    # Plan 2 review fix, multi-user: the post-booking reply names and notifies the inviter, not Pippa.
    w = World()
    edmund = w.active_user("Edmund Lim", "e@gmail.com", EDMUND)
    mine = w.store.for_user(edmund.id)
    c = mine.add_contact("Akash", AKASH)
    task = mine.create_loop_in_task(c.id, "coffee", 30, NOW, NOW + timedelta(days=2), NOW)
    mine.set_task_status(task.id, "done", NOW, event_id="evt")
    w.send(AKASH, "Thanks!")
    assert w.calls == []
    assert (AKASH, CLOSED_TASK_REPLY.format(owner="Edmund")) in w.sender.sent
    assert any(to == EDMUND and "Thanks!" in body for to, body in w.sender.sent)
    assert not any(to == Pippa for to, _ in w.sender.sent)


def test_notices_kept_for_a_friend_are_delivered_on_their_next_message():
    w = World()
    w.active_user("Edmund Lim", "e@gmail.com", EDMUND)
    w.store.queue_notice(EDMUND, "Booked ✅ Coffee with Akash, Fri 2 Oct, 3:00pm–3:30pm.", NOW)
    w.send(EDMUND, "hi")
    assert "While you were away" in w.sender.sent[0][1] and w.sender.sent[0][0] == EDMUND
    assert w.store.take_notices(EDMUND) == []
