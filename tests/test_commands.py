"""Plan 2B: join codes, the admin's commands and "delete my data" are plain code, never the model."""

from datetime import datetime, timedelta

import pytest

from app.commands import WELCOME, Commands
from app.store import Store
from app.timeutil import SGT
from app.users import Users
from tests.fakes import FakeSender

NOW = datetime(2026, 10, 1, 10, tzinfo=SGT)
Pippa = "6591234567"
EDMUND = "6581112222"


class Env:
    def __init__(self):
        self.users = Users(Store(":memory:"), max_users=5)
        self.users.ensure_admin("Pippa", Pippa, NOW)
        self.sender = FakeSender()
        self.revoked: list[str] = []
        self.removed: list[int] = []
        self.commands = Commands(
            self.users,
            self.sender,
            now=lambda: NOW,
            decrypt=lambda sealed: f"plain:{sealed}",
            revoke=self.revoked.append,
            on_user_removed=self.removed.append,
        )

    def signup(self, name="Edmund Lim", email="edmund@example.com", phone=EDMUND):
        user, _ = self.users.start_signup(name, email, "sealed-token", NOW)
        return self.users.set_details(user.id, phone, None, NOW)

    def say(self, phone, text):
        return self.commands.handle(phone, text)


@pytest.fixture
def env():
    return Env()


def test_ordinary_messages_are_not_commands(env):
    assert env.say(Pippa, "what's my next meeting?") is None
    assert env.say(EDMUND, "hi") is None
    assert env.say(Pippa, "please approve my leave") is None  # "approve" needs a code on its own


def test_join_before_approval_waits_and_tells_the_admin(env):
    user = env.signup()
    reply = env.say(EDMUND, f"join {user.join_code}")
    assert "still needs to approve" in reply
    assert any(to == Pippa and "Edmund" in body and "…2222" in body for to, body in env.sender.sent)


def test_approve_after_join_welcomes_the_user(env):
    user = env.signup()
    env.say(EDMUND, f"join {user.join_code}")
    reply = env.say(Pippa, f"approve {user.join_code.lower()}")
    assert "Edmund" in reply and "in" in reply
    assert any(to == EDMUND and body.startswith(WELCOME) and "Gmail" in body for to, body in env.sender.sent)


def test_approve_before_join_then_join_welcomes(env):
    user = env.signup()
    assert "once they send" in env.say(Pippa, f"APPROVE {user.join_code}")
    assert env.say(EDMUND, f"Join {user.join_code}").startswith(WELCOME)


def test_join_from_a_different_number_than_typed_is_rejected(env):
    user = env.signup(phone="6581112222")
    env.say(Pippa, f"approve {user.join_code}")
    reply = env.say("6589990000", f"join {user.join_code}")
    assert "exact WhatsApp number" in reply
    assert env.users.get(user.id).phone is None
    assert env.sender.sent == []


def test_bad_codes_get_a_clear_reply(env):
    assert "isn't right" in env.say(EDMUND, "join ZZZZ")
    assert "No sign-up" in env.say(Pippa, "approve ZZZZ")


def test_admin_commands_are_ignored_from_anyone_else(env):
    # Review Focus: a friend typing "approve XXXX" must not approve anyone.
    user = env.signup()
    assert env.say(EDMUND, f"approve {user.join_code}") is None
    assert env.say(EDMUND, "users") is None
    assert env.users.get(user.id).status == "pending"


def test_reject(env):
    user = env.signup()
    assert "Rejected Edmund" in env.say(Pippa, f"reject {user.join_code}")
    assert "not approved" in env.say(EDMUND, f"join {user.join_code}")


def test_users_lists_everyone_without_full_numbers_or_emails(env):
    user = env.signup()
    out = env.say(Pippa, "users")
    assert "Pippa" in out and "Edmund Lim" in out and "pending" in out and user.join_code in out
    assert EDMUND not in out and "edmund@example.com" not in out


def test_disable(env):
    user = env.signup()
    env.say(Pippa, f"approve {user.join_code}")
    env.say(EDMUND, f"join {user.join_code}")
    assert "Disabled Edmund" in env.say(Pippa, "disable 2222")
    assert env.users.get(user.id).status == "disabled"
    assert env.removed == [user.id]  # cached assistant is dropped


def active_edmund(env):
    user = env.signup()
    env.say(Pippa, f"approve {user.join_code}")
    env.say(EDMUND, f"join {user.join_code}")
    return user


def test_delete_my_data_asks_first_then_deletes_and_revokes(env):
    user = active_edmund(env)
    assert "Reply yes" in env.say(EDMUND, "Delete my data")
    assert env.users.get(user.id) is not None
    reply = env.say(EDMUND, "yes")
    assert "deleted" in reply
    assert env.users.get(user.id) is None
    assert env.revoked == ["plain:sealed-token"]
    assert env.removed == [user.id]


def test_anything_but_yes_keeps_everything(env):
    # Review Focus: "delete my data" → "wait, no" must not delete.
    user = active_edmund(env)
    env.say(EDMUND, "delete my data")
    assert "nothing was deleted" in env.say(EDMUND, "no wait")
    assert env.users.get(user.id) is not None
    assert env.say(EDMUND, "yes") is None  # the question has been answered; "yes" is an ordinary message now
    assert env.revoked == []


def test_delete_still_happens_if_google_revoke_fails(env):
    user = active_edmund(env)

    def broken(token):
        raise RuntimeError("Google down")

    env.commands.revoke = broken
    env.say(EDMUND, "delete my data")
    reply = env.say(EDMUND, "yes")
    assert env.users.get(user.id) is None
    assert "myaccount.google.com/permissions" in reply


def test_admin_cannot_delete_himself_this_way(env):
    assert "can't be deleted" in env.say(Pippa, "delete my data")
    assert env.users.get(1) is not None


def test_strangers_saying_delete_my_data_are_ignored(env):
    assert env.say("6580000000", "delete my data") is None
