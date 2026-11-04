from datetime import datetime, timedelta

from app.nudge import run_nudges
from app.store import Store
from app.timeutil import SGT
from app.tools.whatsapp import WhatsAppError
from tests.fakes import FakeSender

OWNER = "6591234567"
NOW = datetime(2026, 10, 3, 10, tzinfo=SGT)


def store_with_task(age_hours, status="waiting"):
    store = Store(":memory:")
    c = store.add_contact("Edmund", "6581234567")
    created = NOW - timedelta(hours=age_hours)
    task = store.create_loop_in_task(c.id, "a call", 30, created, NOW + timedelta(days=3), created)
    if status != "waiting":
        store.set_task_status(task.id, status, created)
    return store, task


def test_old_unanswered_invite_nudges_owner_once():
    store, task = store_with_task(49)
    sender = FakeSender()
    assert run_nudges(store, sender, OWNER, NOW) == 1
    assert sender.sent[0][0] == OWNER
    assert "Edmund hasn't replied" in sender.sent[0][1]
    assert store.get_task(task.id).status == "nudged"
    assert run_nudges(store, sender, OWNER, NOW + timedelta(hours=5)) == 0
    assert len(sender.sent) == 1


def test_recent_invite_not_nudged():
    store, _ = store_with_task(20)
    sender = FakeSender()
    assert run_nudges(store, sender, OWNER, NOW) == 0
    assert sender.sent == []


def test_answered_invite_not_nudged():
    store, _ = store_with_task(72, status="negotiating")
    assert run_nudges(store, FakeSender(), OWNER, NOW) == 0


def test_contact_is_never_messaged():
    store, _ = store_with_task(49)
    sender = FakeSender()
    run_nudges(store, sender, OWNER, NOW)
    assert all(to == OWNER for to, _ in sender.sent)


def test_failed_send_retries_later():
    store, task = store_with_task(49)
    # A closed 24h window is kept for later instead (tests/test_loopin_review.py); other errors retry hourly.
    assert run_nudges(store, FakeSender(exc=WhatsAppError("Meta down")), OWNER, NOW) == 0
    assert store.get_task(task.id).status == "waiting"
