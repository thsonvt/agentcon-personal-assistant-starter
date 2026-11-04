"""Plan 2 final-review fixes: stale task snapshots, double booking, lost owner notices,
"thanks!" after booking, local numbers, and the blocking nudge loop."""

import asyncio
import threading
import time as time_mod
from datetime import datetime, timedelta

import pytest

from app import delegate_tools as dt
from app import loopin_tools as lt
from app import nudge
from app.agents import build_delegate
from app.router import CLOSED_TASK_REPLY, IncomingMessage, Router
from app.store import Store
from app.timeutil import SGT
from app.tools.whatsapp import WhatsAppError, WindowClosedError
from tests.fakes import FakeCalendarService, FakeSender
from tests.test_delegate_tools import OWNER, make
from tests.test_loopin_tools import make_app

NOW = datetime(2026, 10, 1, 10, tzinfo=SGT)
EDMUND = "6581234567"


# --- Critical 1: a queued message must not re-open a closed task --------------------

def router_with_task(run_contact):
    store = Store(":memory:")
    c = store.add_contact("Edmund", EDMUND)
    task = store.create_loop_in_task(c.id, "a call", 30, NOW, NOW + timedelta(days=2), NOW)

    async def run_owner(text):
        return "owner"

    router = Router(OWNER, store, FakeSender(), run_owner, lambda: NOW, run_contact=run_contact,
                    transcribe=lambda media_id: (time_mod.sleep(0.05), "Friday 3pm works")[1])
    return router, store, task


def test_message_queued_behind_a_booking_does_not_reopen_the_task():
    calls = []

    async def run_contact(contact, task, text):
        calls.append(text)
        if len(calls) == 1:
            router.store.set_task_status(task.id, "done", NOW, event_id="evt")  # the Delegate booked it
        return "ok"

    router, store, task = router_with_task(run_contact)

    async def both():
        first = asyncio.create_task(router.handle(IncomingMessage(EDMUND, None, "m1", audio_id="MEDIA1")))
        await asyncio.sleep(0.01)  # "thanks!" arrives while the voice note is still being transcribed
        await router.handle(IncomingMessage(EDMUND, "thanks!", "m2"))
        await first

    asyncio.run(both())
    assert store.get_task(task.id).status == "done"  # not flipped back to negotiating
    assert calls == ["[voice] Friday 3pm works"]  # the Delegate isn't run on a closed task
    assert router.sender.sent[-1][1] == CLOSED_TASK_REPLY.format(owner="Pippa")


def test_cancel_during_a_contacts_run_stays_cancelled():
    async def run_contact(contact, task, text):
        return "ok"

    router, store, task = router_with_task(run_contact)

    async def both():
        first = asyncio.create_task(router.handle(IncomingMessage(EDMUND, None, "m1", audio_id="MEDIA1")))
        await asyncio.sleep(0.01)
        store.set_task_status(task.id, "cancelled", NOW)  # Pippa cancels meanwhile
        await first

    asyncio.run(both())
    assert store.get_task(task.id).status == "cancelled"


def test_mark_negotiating_only_moves_open_waiting_tasks():
    store = Store(":memory:")
    c = store.add_contact("Edmund", EDMUND)
    task = store.create_loop_in_task(c.id, "a call", 30, NOW, NOW + timedelta(days=2), NOW)
    assert store.mark_negotiating(task.id, NOW).status == "negotiating"
    store.set_task_status(task.id, "done", NOW)
    assert store.mark_negotiating(task.id, NOW).status == "done"


# --- Important 2: no double booking ---------------------------------------------------

def test_parallel_bookings_for_one_task_book_once():
    class SlowCalendar(FakeCalendarService):
        pass

    svc = SlowCalendar()
    d = make(svc)
    original = d.app.calendar.create_event

    def slow_create(*args, **kwargs):
        time_mod.sleep(0.05)
        return original(*args, **kwargs)

    d.app.calendar.create_event = slow_create
    results = []
    threads = [
        threading.Thread(target=lambda s=s: results.append(dt.book_agreed_time(d, s)))
        for s in ("2026-10-02T15:00", "2026-10-03T15:00")
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sum(r.startswith("Booked:") for r in results) == 1
    assert [c[0] for c in svc.calls].count("insert") == 1


def test_delegate_makes_one_tool_call_at_a_time():
    assert build_delegate("m").model_settings.parallel_tool_calls is False


# --- Important 3: owner notices wait for the 24h window instead of vanishing ------------

def test_booking_notice_is_kept_when_Pippas_window_is_closed():
    d = make(sender=FakeSender(exc=WindowClosedError("closed")))
    out = dt.book_agreed_time(d, "2026-10-02T15:00")
    assert out.startswith("Booked:")
    assert [n.startswith("Booked ✅") for n in d.app.store.take_notices(OWNER)] == [True]


def test_report_to_owner_is_kept_and_counts_as_passed_on():
    d = make(sender=FakeSender(exc=WindowClosedError("closed")))
    assert dt.report_to_owner(d, "Can we do Zoom?") == "Passed on to Pippa."
    assert "Can we do Zoom?" in d.app.store.take_notices(OWNER)[0]


def test_kept_notices_are_delivered_when_Pippa_next_messages():
    store = Store(":memory:")
    store.queue_notice(OWNER, "Booked ✅ A call with Edmund, Fri 2 Oct, 3:00pm–3:30pm.", NOW)
    order = []
    sender = FakeSender()

    async def run_owner(text):
        order.append(("run", list(sender.sent)))
        return "reply"

    router = Router(OWNER, store, sender, run_owner, lambda: NOW)
    asyncio.run(router.handle(IncomingMessage(OWNER, "hi", "m1")))
    assert sender.sent[0][0] == OWNER and "While you were away" in sender.sent[0][1] and "Booked ✅" in sender.sent[0][1]
    assert order[0][1] == sender.sent[:1]  # delivered before the reply
    assert store.take_notices(OWNER) == []  # delivered once


def test_nudge_with_closed_window_is_kept_and_not_retried_hourly():
    store = Store(":memory:")
    c = store.add_contact("Edmund", EDMUND)
    old = NOW - timedelta(hours=49)
    task = store.create_loop_in_task(c.id, "a call", 30, old, NOW + timedelta(days=3), old)
    assert nudge.run_nudges(store, FakeSender(exc=WindowClosedError("closed")), OWNER, NOW) == 1
    assert store.get_task(task.id).status == "nudged"
    assert "hasn't replied" in store.take_notices(OWNER)[0]


def test_nudge_with_other_errors_still_retries():
    store = Store(":memory:")
    c = store.add_contact("Edmund", EDMUND)
    old = NOW - timedelta(hours=49)
    task = store.create_loop_in_task(c.id, "a call", 30, old, NOW + timedelta(days=3), old)
    assert nudge.run_nudges(store, FakeSender(exc=WhatsAppError("Meta down")), OWNER, NOW) == 0
    assert store.get_task(task.id).status == "waiting"


# --- Important 4: "thanks!" after the booking ---------------------------------------------

def test_contact_writing_after_the_booking_gets_a_friendly_reply_and_Pippa_hears_it():
    store = Store(":memory:")
    c = store.add_contact("Edmund", EDMUND)
    task = store.create_loop_in_task(c.id, "a call", 30, NOW, NOW + timedelta(days=2), NOW)
    store.set_task_status(task.id, "done", NOW, event_id="evt")
    sender = FakeSender()

    async def never(*args):
        raise AssertionError("no agent should run")

    router = Router(OWNER, store, sender, never, lambda: NOW, run_contact=never)
    asyncio.run(router.handle(IncomingMessage(EDMUND, "Thanks! Can we make it Zoom?", "m1")))
    assert (EDMUND, CLOSED_TASK_REPLY.format(owner="Pippa")) in sender.sent
    assert any(to == OWNER and "Can we make it Zoom?" in body for to, body in sender.sent)


def test_old_closed_invites_fall_back_to_the_stranger_reply():
    store = Store(":memory:")
    c = store.add_contact("Edmund", EDMUND)
    task = store.create_loop_in_task(c.id, "a call", 30, NOW, NOW + timedelta(days=2), NOW)
    store.set_task_status(task.id, "done", NOW - timedelta(days=8))
    sender = FakeSender()

    async def never(*args):
        raise AssertionError("no agent should run")

    router = Router(OWNER, store, sender, never, lambda: NOW, run_contact=never)
    asyncio.run(router.handle(IncomingMessage(EDMUND, "hi", "m1")))
    assert sender.sent == [(EDMUND, "This is a private assistant.")]


# --- Important 5: local Singapore numbers --------------------------------------------------

@pytest.mark.parametrize("typed", ["8123 4567", "9876-5432", "61234567"])
def test_eight_digit_singapore_numbers_get_the_country_code(typed):
    app = make_app()
    out = lt.save_contact(app, "Edmund", typed, None)
    assert "+65" in out
    assert app.store.find_contact("Edmund").phone.startswith("65")


def test_other_short_numbers_are_refused():
    app = make_app()
    assert "country code" in lt.save_contact(app, "Ed", "71234567", None)  # 8 digits, not a SG mobile/landline
    assert "country code" in lt.save_contact(app, "Ed", "123456789", None)


# --- Important 6: the nudge loop must not block the event loop ------------------------------

def test_nudge_loop_runs_the_check_off_the_event_loop(monkeypatch):
    seen = {}

    def fake_run_nudges(*args, **kwargs):
        seen["thread"] = threading.get_ident()
        raise asyncio.CancelledError  # stop after one pass

    monkeypatch.setattr(nudge, "run_nudges", fake_run_nudges)

    async def go():
        seen["loop_thread"] = threading.get_ident()
        with pytest.raises(asyncio.CancelledError):
            await nudge.nudge_loop(Store(":memory:"), FakeSender(), OWNER, lambda: NOW, interval_s=0)

    asyncio.run(go())
    assert seen["thread"] != seen["loop_thread"]


# --- Promoted minor: logs never hold full numbers --------------------------------------------

def test_failed_send_logs_only_the_last_four_digits(caplog):
    caplog.set_level("INFO", logger="app.router")

    async def run_owner(text):
        return "reply"

    router = Router(OWNER, Store(":memory:"), FakeSender(exc=WhatsAppError("Invalid token")), run_owner, lambda: NOW)
    asyncio.run(router.handle(IncomingMessage(OWNER, "hi", "m1")))
    assert "…4567" in caplog.text and OWNER not in caplog.text
