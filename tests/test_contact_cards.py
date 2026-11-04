"""Shared WhatsApp contact cards become text the agent can use (SON-77, item 1)."""

import asyncio
from datetime import datetime

from app.router import NON_TEXT_REPLY, IncomingMessage, Router
from app.store import Store
from app.timeutil import SGT
from app.webhook import extract_messages
from tests.fakes import FakeSender

OWNER = "6591234567"
NOW = datetime(2026, 10, 1, 10, tzinfo=SGT)


def payload(*contacts):
    msg = {"from": OWNER, "id": "wamid.c", "type": "contacts", "contacts": list(contacts)}
    return {"entry": [{"changes": [{"value": {"messages": [msg]}}]}]}


CHRIS = {
    "name": {"formatted_name": "Chris Example", "first_name": "Chris"},
    "phones": [{"phone": "+65 9123 4567", "wa_id": "6591234567", "type": "CELL"}],
    "emails": [{"email": "chris@example.com", "type": "WORK"}],
}


def test_contact_card_becomes_text_with_name_number_and_email():
    text = extract_messages(payload(CHRIS))[0].text
    assert text == "[contact card] Chris Example, phone +6591234567, email chris@example.com"


def test_card_without_whatsapp_id_uses_the_listed_phone():
    card = {"name": {"formatted_name": "Ed"}, "phones": [{"phone": "+65 8111 2222"}]}
    assert extract_messages(payload(card))[0].text == "[contact card] Ed, phone +6581112222"


def test_card_without_a_phone_says_so():
    card = {"name": {"formatted_name": "Ed"}, "emails": [{"email": "ed@x.com"}]}
    assert extract_messages(payload(card))[0].text == "[contact card] Ed, no phone number, email ed@x.com"


def test_several_cards_become_several_lines():
    ed = {"name": {"formatted_name": "Ed"}, "phones": [{"wa_id": "6581112222"}]}
    assert extract_messages(payload(CHRIS, ed))[0].text.splitlines() == [
        "[contact card] Chris Example, phone +6591234567, email chris@example.com",
        "[contact card] Ed, phone +6581112222",
    ]


def test_a_shared_contact_reaches_the_assistant():
    seen = []

    async def run_owner(text):
        seen.append(text)
        return "Saved Chris."

    sender = FakeSender()
    router = Router(OWNER, Store(":memory:"), sender, run_owner, lambda: NOW)
    asyncio.run(router.handle(extract_messages(payload(CHRIS))[0]))
    assert seen == ["[contact card] Chris Example, phone +6591234567, email chris@example.com"]
    assert (OWNER, NON_TEXT_REPLY) not in sender.sent


def test_the_assistant_is_told_what_contact_cards_are():
    import inspect

    from app.agents import assistant_instructions

    assert "[contact card]" in inspect.getsource(assistant_instructions)
