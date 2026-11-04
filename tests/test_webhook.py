import hashlib
import hmac
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.store import Store
from app.timeutil import SGT
from app.webhook import create_app, extract_messages, verify_signature

SECRET = "app-secret"
NOW = datetime(2026, 9, 28, 14, tzinfo=SGT)


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def text_payload(msg_id="wamid.1", sender="6591234567", body="hi"):
    return {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {"messages": [
            {"from": sender, "id": msg_id, "type": "text", "text": {"body": body}}
        ]}}]}],
    }


def make_client():
    handled = []

    async def handle(msg):
        handled.append(msg)

    app = create_app("verify-me", SECRET, Store(":memory:"), handle, lambda: NOW)
    return TestClient(app), handled


def post(client, payload, signature=None):
    body = json.dumps(payload).encode()
    return client.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": signature if signature is not None else sign(body), "Content-Type": "application/json"},
    )


def test_verification_handshake():
    client, _ = make_client()
    ok = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
    assert ok.status_code == 200
    assert ok.text == "42"
    bad = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "42"})
    assert bad.status_code == 403


def test_bad_signature_rejected_and_not_handled():
    client, handled = make_client()
    assert post(client, text_payload(), signature="sha256=deadbeef").status_code == 403
    assert post(client, text_payload(), signature="").status_code == 403
    assert handled == []


def test_valid_message_is_handled():
    client, handled = make_client()
    assert post(client, text_payload(body="what's next?")).status_code == 200
    assert len(handled) == 1
    assert handled[0].text == "what's next?"
    assert handled[0].sender == "6591234567"


def test_duplicate_delivery_handled_once():
    client, handled = make_client()
    post(client, text_payload("wamid.same"))
    post(client, text_payload("wamid.same"))
    assert len(handled) == 1


def test_status_updates_are_ignored():
    client, handled = make_client()
    payload = {"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.1", "status": "read"}]}}]}]}
    assert post(client, payload).status_code == 200
    assert handled == []


def test_non_text_message_arrives_with_no_text():
    payload = text_payload()
    payload["entry"][0]["changes"][0]["value"]["messages"][0] = {"from": "65", "id": "m", "type": "audio", "audio": {}}
    assert extract_messages(payload)[0].text is None


def test_bad_json_rejected():
    client, _ = make_client()
    body = b"not json"
    response = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body)})
    assert response.status_code == 400


def test_verify_signature_helper():
    assert verify_signature(b"x", sign(b"x"), SECRET)
    assert not verify_signature(b"x", None, SECRET)
    assert not verify_signature(b"x", sign(b"y"), SECRET)


def test_health():
    client, _ = make_client()
    assert client.get("/health").json() == {"ok": True}


@pytest.mark.parametrize("kind", ["reaction", "system", "unsupported"])
def test_reactions_and_system_messages_are_ignored(kind):
    # Review fix: a thumbs-up reaction must not trigger "I can only read text messages".
    payload = text_payload()
    payload["entry"][0]["changes"][0]["value"]["messages"][0] = {"from": "65", "id": "m", "type": kind, kind: {}}
    assert extract_messages(payload) == []


def test_webhook_logs_what_it_received(caplog):
    caplog.set_level("INFO", logger="app.webhook")
    client, _ = make_client()
    post(client, text_payload("wamid.log1"))
    post(client, {"entry": [{"changes": [{"value": {"statuses": [{"id": "x", "status": "read"}]}}]}]})
    post(client, text_payload("wamid.log1"))
    assert "1 message(s), 0 status update(s)" in caplog.text
    assert "0 message(s), 1 status update(s)" in caplog.text
    assert "duplicate" in caplog.text


def test_background_jobs_start_with_the_app():
    started = []

    async def job():
        started.append(True)

    async def handle(msg):
        pass

    app = create_app("v", SECRET, Store(":memory:"), handle, lambda: NOW, background_jobs=[job])
    with TestClient(app) as client:
        client.get("/health")
    assert started == [True]


@pytest.mark.parametrize("payload", [None, [], True, {"entry": 1}, {"entry": [None]},
    {"entry": [{"changes": [None]}]}, {"entry": [{"changes": 1}]},
    {"entry": [{"changes": [{"value": {"statuses": 1, "messages": 1}}]}]}])
def test_signed_malformed_envelopes_are_ignored(payload):
    client, handled = make_client()
    assert post(client, payload).status_code == 200
    assert handled == []


def test_signed_mixed_envelope_preserves_valid_message_and_status_siblings():
    handled, statuses = [], []

    async def handle(msg):
        handled.append(msg)

    client = TestClient(create_app("verify", SECRET, Store(":memory:"), handle, lambda: NOW,
                                   status_handler=statuses.append))
    valid_message = {"from": "6591234567", "id": "valid.message", "type": "text", "text": {"body": "hello"}}
    valid_status = {"id": "valid.status", "recipient_id": "6591234567", "status": "delivered"}
    payload = {"entry": [None, {"changes": [None, {"value": 1}, {"value": {
        "statuses": [None, 1, {}, valid_status],
        "messages": [None, 1, {}, {"from": [], "id": "x", "type": "text"},
                     {"from": "65", "id": "bad.type", "type": []},
                     {"from": "65", "id": "bad.text", "type": "text", "text": []}, valid_message],
    }}]}, {"changes": [{"value": {"statuses": 1, "messages": 1}}]}]}
    assert post(client, payload).status_code == 200
    assert [msg.message_id for msg in handled] == ["valid.message"]
    assert [status["id"] for status in statuses] == ["valid.status"]


def test_malformed_contact_members_do_not_drop_valid_message_siblings():
    client, handled = make_client()
    payload = text_payload(msg_id="valid.text")
    messages = payload["entry"][0]["changes"][0]["value"]["messages"]
    messages.insert(0, {"from": "6591234567", "id": "contact.card", "type": "contacts", "contacts": [
        None, {"name": [], "phones": [None, {"wa_id": []}, {"phone": 123}], "emails": 1},
        {"name": {"formatted_name": "Valid"}, "phones": [{"wa_id": "6581112222"}]},
    ]})
    assert post(client, payload).status_code == 200
    assert handled[-1].message_id == "valid.text"
    assert "Valid, phone +6581112222" in handled[0].text
