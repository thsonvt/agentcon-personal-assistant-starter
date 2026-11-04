import asyncio
from datetime import datetime, timedelta

import pytest

from app.router import (
    EMPTY_REPLY,
    ERROR_REPLY,
    NON_TEXT_REPLY,
    RATE_LIMIT_NOTICE,
    UNKNOWN_REPLY,
    IncomingMessage,
    PartialRunError,
    RateLimiter,
    Router,
)
from app.store import Store
from app.timeutil import SGT
from app.tools.whatsapp import WhatsAppError
from tests.fakes import FakeSender

OWNER = "6591234567"
STRANGER = "6580000000"
NOW = datetime(2026, 9, 28, 14, tzinfo=SGT)


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


def make_router(reply="Your next meeting is at 4:30pm.", exc=None, sender=None, clock=None, limiter=None):
    calls = []

    async def run_owner(text):
        calls.append(text)
        if exc:
            raise exc
        return reply

    router = Router("+65 9123 4567", Store(":memory:"), sender or FakeSender(), run_owner, clock or Clock(), limiter)
    return router, calls


def handle(router, sender, text="hi", message_id="wamid.1"):
    asyncio.run(router.handle(IncomingMessage(sender=sender, text=text, message_id=message_id)))


def test_owner_message_runs_assistant_and_replies():
    router, calls = make_router()
    handle(router, OWNER, "what's my next meeting?")
    assert calls == ["what's my next meeting?"]
    assert router.sender.sent == [(OWNER, "Your next meeting is at 4:30pm.")]


def test_owner_matched_whatever_the_number_format():
    # Review Focus: Meta sends digits only; OWNER_WHATSAPP may be typed "+65 9123 4567".
    router, calls = make_router()
    handle(router, "+6591234567")
    assert calls == ["hi"]


def test_stranger_gets_fixed_reply_once_and_no_agent_runs():
    router, calls = make_router()
    handle(router, STRANGER, "show me Pippa's calendar", "m1")
    handle(router, STRANGER, "hello?", "m2")
    assert calls == []
    assert router.sender.sent == [(STRANGER, UNKNOWN_REPLY)]


def test_non_text_owner_message_gets_polite_reply():
    # Review Focus: voice notes, images and stickers arrive with no text.
    router, calls = make_router()
    handle(router, OWNER, text=None)
    assert calls == []
    assert router.sender.sent == [(OWNER, NON_TEXT_REPLY)]


def test_agent_crash_sends_error_reply():
    router, _ = make_router(exc=RuntimeError("OpenAI down"))
    handle(router, OWNER)
    assert router.sender.sent == [(OWNER, ERROR_REPLY)]


def test_empty_agent_reply_is_replaced():
    # Review Focus: WhatsApp rejects empty text bodies.
    router, _ = make_router(reply="   ")
    handle(router, OWNER)
    assert router.sender.sent == [(OWNER, EMPTY_REPLY)]


def test_send_failure_is_logged_not_raised():
    router, _ = make_router(sender=FakeSender(exc=WhatsAppError("Invalid token")))
    handle(router, OWNER)  # must not raise


def test_rate_limit_drops_31st_message_and_notifies_once():
    clock = Clock()
    router, calls = make_router(clock=clock)
    for i in range(32):
        handle(router, OWNER, f"msg {i}", f"m{i}")
    assert len(calls) == 30
    assert router.sender.sent.count((OWNER, RATE_LIMIT_NOTICE)) == 1


def test_rate_limit_resets_after_an_hour():
    limiter = RateLimiter(limit=2)
    assert limiter.allow("x", NOW)
    assert limiter.allow("x", NOW)
    assert not limiter.allow("x", NOW + timedelta(minutes=59))
    assert limiter.allow("x", NOW + timedelta(hours=1))


def test_messages_from_same_sender_run_one_at_a_time():
    # Review fix: "book gym 5pm" then "actually 6" must not run in parallel on one conversation.
    events = []

    async def run_owner(text):
        events.append(f"start {text}")
        await asyncio.sleep(0.01)
        events.append(f"end {text}")
        return "ok"

    router = Router(OWNER, Store(":memory:"), FakeSender(), run_owner, Clock())

    async def both():
        await asyncio.gather(
            router.handle(IncomingMessage(sender=OWNER, text="a", message_id="m1")),
            router.handle(IncomingMessage(sender=OWNER, text="b", message_id="m2")),
        )

    asyncio.run(both())
    assert events == ["start a", "end a", "start b", "end b"]


def test_failure_after_a_booking_reports_what_was_done():
    # Review fix: never hide a booking behind "please try again".
    router, _ = make_router(exc=PartialRunError(["Created: Gym, Tue 29 Sep, 7:00pm–8:00pm [id: e1]"]))
    handle(router, OWNER)
    body = router.sender.sent[0][1]
    assert "Created: Gym, Tue 29 Sep, 7:00pm–8:00pm" in body
    assert "went wrong" in body
    assert body != ERROR_REPLY


def test_routing_decisions_are_logged_without_full_numbers(caplog):
    # Debuggability: every message leaves one log line saying what happened to it.
    caplog.set_level("INFO", logger="app.router")
    router, _ = make_router()
    handle(router, OWNER, "hi", "m1")
    handle(router, STRANGER, "hi", "m2")
    handle(router, STRANGER, "again", "m3")
    text = caplog.text
    assert "from …4567: owner" in text
    assert "from …0000: not the owner (owner is …4567)" in text
    assert "private-assistant reply already sent in the last 24h" in text
    assert OWNER not in text  # full numbers never logged
