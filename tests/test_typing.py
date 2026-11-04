"""Plan 2A: read receipts and the "typing…" indicator while the agent works."""

import asyncio
import json
from datetime import datetime

import httpx
import pytest

from app.router import NON_TEXT_REPLY, IncomingMessage, Router
from app.store import Store
from app.timeutil import SGT
from app.tools.whatsapp import WhatsAppClient, WhatsAppError
from tests.fakes import FakeSender

OWNER = "6591234567"
STRANGER = "6580000000"
NOW = datetime(2026, 9, 28, 14, tzinfo=SGT)


def test_mark_read_typing_posts_the_documented_payload():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True})  # no "messages" key in this reply

    client = WhatsAppClient("tok", "PNID", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.mark_read_typing("wamid.in1") is None
    assert seen["url"] == "https://graph.facebook.com/v25.0/PNID/messages"
    assert seen["body"] == {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": "wamid.in1",
        "typing_indicator": {"type": "text"},
    }


def test_mark_read_typing_failure_raises_whatsapp_error():
    client = WhatsAppClient(
        "tok", "PNID",
        http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"error": {"code": 100, "message": "Invalid parameter"}}))),
    )
    with pytest.raises(WhatsAppError, match="error \\(400\\)") as err:
        client.mark_read_typing("wamid.old")
    assert err.value.code == 100


def make_router(sender, events=None):
    async def run_owner(text):
        if events is not None:
            events.append(("run", text))
        return "ok"

    return Router(OWNER, Store(":memory:"), sender, run_owner, lambda: NOW)


def handle(router, sender, text="hi", message_id="wamid.1"):
    asyncio.run(router.handle(IncomingMessage(sender=sender, text=text, message_id=message_id)))


def test_owner_message_shows_typing_before_the_agent_runs():
    events = []

    class RecordingSender(FakeSender):
        def mark_read_typing(self, message_id):
            events.append(("typing", message_id))
            super().mark_read_typing(message_id)

    router = make_router(RecordingSender(), events)
    handle(router, OWNER, "what's next?", "wamid.42")
    assert events == [("typing", "wamid.42"), ("run", "what's next?")]


def test_strangers_get_no_typing_or_read_receipt():
    # A stranger shouldn't see blue ticks or "typing…" from a private assistant.
    sender = FakeSender()
    handle(make_router(sender), STRANGER)
    assert sender.typing == []


def test_unsupported_message_gets_no_typing():
    # Nothing is being worked on, so nothing to show.
    sender = FakeSender()
    handle(make_router(sender), OWNER, text=None)
    assert sender.typing == []
    assert sender.sent == [(OWNER, NON_TEXT_REPLY)]


def test_typing_failure_does_not_stop_the_reply():
    # Review Focus: an expired message id or a Meta hiccup must not cost Pippa his answer.
    sender = FakeSender(typing_exc=WhatsAppError("Invalid parameter"))
    handle(make_router(sender), OWNER)
    assert sender.sent == [(OWNER, "ok")]


def test_unexpected_typing_error_does_not_stop_the_reply():
    # Final review: typing is best-effort (D20); any exception, not just WhatsAppError, must be swallowed.
    sender = FakeSender(typing_exc=ValueError("not JSON"))
    handle(make_router(sender), OWNER)
    assert sender.sent == [(OWNER, "ok")]
