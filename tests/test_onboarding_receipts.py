"""Join proof and WhatsApp delivery are separate, durable facts."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.commands import Commands
from app.onboarding import OnboardingReceipts
from app.router import IncomingMessage, Router
from app.store import Store
from app.tools.whatsapp import WhatsAppError
from app.users import Users
from app.webhook import create_app
from tests.test_commands import NOW, Pippa, EDMUND
from tests.test_webhook import SECRET, post, text_payload


@pytest.fixture
def env():
    store = Store(":memory:")
    users = Users(store)
    users.ensure_admin("Pippa", Pippa, NOW)
    user, _ = users.start_signup("Edmund", "edmund@example.com", "sealed", NOW)
    user = users.set_details(user.id, EDMUND, None, NOW)
    sent = []

    class Sender:
        error = None

        def send_text(self, to, body):
            sent.append((to, body))
            if to != Pippa and self.error:
                raise self.error
            return f"wamid.{len(sent)}"

    sender = Sender()
    receipts = OnboardingReceipts(store, now=lambda: NOW)
    commands = Commands(users, sender, lambda: NOW, lambda token: token, lambda token: None, receipts=receipts)

    async def run(text):
        return "unused"

    router = Router(Pippa, store, sender, run, lambda: NOW, commands=commands, users=users)
    return SimpleNamespace(store=store, users=users, user=user, sender=sender, sent=sent,
                           receipts=receipts, commands=commands, router=router)


def test_received_mismatch_and_failed_reply_survive_restart(env):
    env.sender.error = WhatsAppError("recipient restriction", code=131030)
    asyncio.run(env.router.handle(IncomingMessage("6599990000", f"join {env.user.join_code}", "inbound.1")))
    receipts = OnboardingReceipts(env.store, now=lambda: NOW)
    assert receipts.view(env.user) == {
        "attempt_status": "number_mismatch", "attempted_at": NOW.isoformat(),
        "reply_status": "failed", "reply_error_code": 131030,
    }
    assert env.users.get(env.user.id).phone is None
    rows = str([tuple(row) for row in env.store.conn.execute("SELECT * FROM onboarding_attempts")])
    assert "6599990000" not in rows and env.user.join_code not in rows


def test_accepted_reply_is_not_delivery_and_callbacks_are_bound_and_monotonic(env):
    asyncio.run(env.router.handle(IncomingMessage(EDMUND, f"join {env.user.join_code}", "inbound.1")))
    user = env.users.get(env.user.id)
    assert env.receipts.view(user)["attempt_status"] == "verified"
    assert env.receipts.view(user)["reply_status"] == "accepted"
    status = {"id": "wamid.2", "recipient_id": EDMUND, "status": "failed", "errors": [{"code": 131030}]}
    env.receipts.record_status({**status, "id": "unknown"})
    env.receipts.record_status({**status, "recipient_id": Pippa})
    assert env.receipts.view(user)["reply_status"] == "accepted"
    env.receipts.record_status(status)
    assert env.receipts.view(user)["reply_status"] == "failed"
    assert env.receipts.view(user)["reply_error_code"] == 131030
    env.receipts.record_status({**status, "status": "delivered"})
    env.receipts.record_status(status)
    env.receipts.record_status({**status, "status": "sent"})
    assert env.receipts.view(user)["reply_status"] == "delivered"
    assert env.receipts.view(user)["reply_error_code"] is None
    env.receipts.record_status({**status, "status": "read"})
    env.receipts.record_status({**status, "status": "delivered"})
    assert env.receipts.view(user)["reply_status"] == "read"


def test_current_generation_ignores_old_failure_and_wrong_number_after_verified(env):
    asyncio.run(env.router.handle(IncomingMessage("6599990000", f"join {env.user.join_code}", "inbound.1")))
    old_code = env.user.join_code
    user = env.users.set_details(env.user.id, EDMUND, None, NOW + timedelta(hours=1))
    assert user.join_code != old_code
    assert env.receipts.view(user)["attempted_at"] is None
    env.receipts.record_status({"id": "wamid.1", "recipient_id": "6599990000", "status": "failed", "errors": [{"code": 131030}]})
    assert env.receipts.view(user)["reply_status"] == "not_sent"
    asyncio.run(env.router.handle(IncomingMessage(EDMUND, f"join {user.join_code}", "inbound.2")))
    user = env.users.get(user.id)
    asyncio.run(env.router.handle(IncomingMessage("6599990000", f"join {user.join_code}", "inbound.3")))
    assert env.receipts.view(user)["attempt_status"] == "verified"
    assert env.receipts.view(user)["reply_status"] == "accepted"


def test_verified_sender_can_replay_consumed_code_without_relinking(env):
    env.users.approve(env.user.join_code, NOW)
    asyncio.run(env.router.handle(IncomingMessage(EDMUND, f"join {env.user.join_code}", "inbound.1")))
    assert env.users.get(env.user.id).join_code is None
    asyncio.run(env.router.handle(IncomingMessage(EDMUND, f"join {env.user.join_code}", "inbound.2")))
    assert "Account: active" in env.sent[-1][1]
    assert "isn't right" not in env.sent[-1][1]
    asyncio.run(env.router.handle(IncomingMessage("6599990000", f"join {env.user.join_code}", "inbound.3")))
    assert "isn't right" in env.sent[-1][1]
    assert env.users.get(env.user.id).phone == EDMUND
    assert env.receipts.view(env.users.get(env.user.id))["attempt_status"] == "verified"


def test_expired_code_is_recorded_without_linking(env):
    env.commands.now = lambda: NOW + timedelta(hours=1)
    assert "expired" in env.commands.handle(EDMUND, f"join {env.user.join_code}")
    assert env.receipts.view(env.user)["attempt_status"] == "code_expired"
    assert not env.users.get(env.user.id).joined


def test_signup_notice_is_accurate_and_deduplicated_durably(env):
    env.users.auto_activate = True
    env.commands.announce_signup(env.user)
    again = Commands(env.users, env.sender, lambda: NOW, lambda x: x, lambda x: None)
    again.announce_signup(env.user)
    assert len(env.sent) == 1
    body = env.sent[0][1]
    assert "Setup submitted" in body and "awaiting WhatsApp number verification" in body
    assert "Meta" in body
    assert "Approved automatically" not in body and env.user.join_code not in body


def test_only_signed_valid_status_callbacks_update_receipts(env):
    asyncio.run(env.router.handle(IncomingMessage(EDMUND, f"join {env.user.join_code}", "inbound.1")))
    app = create_app("verify", SECRET, env.store, env.router.handle, lambda: NOW,
                     status_handler=env.receipts.record_status)
    client = TestClient(app)
    status = {"id": "wamid.2", "recipient_id": EDMUND, "status": "failed", "errors": [{"code": 131030}]}
    payload = {"entry": [{"changes": [{"value": {"statuses": [status, {}, None, {"id": "missing"}]}}]}]}
    assert post(client, payload, signature="sha256=wrong").status_code == 403
    assert env.receipts.view(env.users.get(env.user.id))["reply_status"] == "accepted"
    assert post(client, payload).status_code == 200
    assert env.receipts.view(env.users.get(env.user.id))["reply_error_code"] == 131030


def test_delete_my_data_removes_receipts_and_announcement(env):
    env.commands.announce_signup(env.user)
    env.commands.handle(EDMUND, f"join {env.user.join_code}")
    env.users.delete(env.user.id)
    for table in ("onboarding_attempts", "onboarding_announcements"):
        assert env.store.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id=?", (env.user.id,)).fetchone()[0] == 0


def test_new_generation_can_notify_and_manual_approval_remains_available(env):
    env.commands.announce_signup(env.user)
    assert f"approve {env.user.join_code}" in env.sent[-1][1]
    fresh = env.users.set_details(env.user.id, EDMUND, None, NOW + timedelta(hours=1))
    env.commands.announce_signup(fresh)
    assert len(env.sent) == 2
    assert f"approve {fresh.join_code}" in env.sent[-1][1]


def test_unknown_code_and_failed_reply_do_not_change_current_receipt(env):
    env.sender.error = WhatsAppError("recipient restriction", code=131030)
    asyncio.run(env.router.handle(IncomingMessage(EDMUND, "join ZZZZZZ", "inbound.1")))
    assert env.receipts.view(env.user) == {
        "attempt_status": "waiting", "attempted_at": None,
        "reply_status": "not_sent", "reply_error_code": None,
    }


def test_missing_provider_error_still_records_failed_send(env):
    env.sender.error = WhatsAppError("transport error")
    asyncio.run(env.router.handle(IncomingMessage(EDMUND, f"join {env.user.join_code}", "inbound.1")))
    assert env.receipts.view(env.users.get(env.user.id))["reply_status"] == "failed"
    assert env.receipts.view(env.users.get(env.user.id))["reply_error_code"] is None


@pytest.mark.parametrize("response_body", [b"{}", b"not json", b'{"messages":[{"id":null}]}', b"[]"])
def test_malformed_provider_success_is_recorded_as_failure_through_real_client(env, response_body, caplog):
    import httpx
    from app.tools.whatsapp import WhatsAppClient

    client = WhatsAppClient("private-token", "PNID", http=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=response_body)
    )))
    env.router.sender = client
    env.commands.sender = client
    webhook = TestClient(create_app("verify", SECRET, env.store, env.router.handle, lambda: NOW))
    assert post(webhook, text_payload("inbound.1", EDMUND, f"join {env.user.join_code}")).status_code == 200
    assert "private-token" not in caplog.text
    user = env.users.get(env.user.id)
    assert user.verified_join
    assert env.receipts.view(user)["attempt_status"] == "verified"
    assert env.receipts.view(user)["reply_status"] == "failed"
    assert env.receipts.view(user)["reply_error_code"] is None


def test_verified_user_status_reply_recovers_after_recipient_restriction(env):
    env.users.approve(env.user.join_code, NOW)
    env.sender.error = WhatsAppError("recipient restriction", code=131030)
    asyncio.run(env.router.handle(IncomingMessage(EDMUND, f"join {env.user.join_code}", "inbound.1")))
    user = env.users.get(env.user.id)
    assert user.join_code is None and user.verified_join
    assert env.receipts.view(user)["reply_error_code"] == 131030
    env.sender.error = None
    asyncio.run(env.router.handle(IncomingMessage(EDMUND, "status", "inbound.2")))
    assert env.receipts.view(user)["attempt_status"] == "verified"
    assert env.receipts.view(user)["reply_status"] == "accepted"
    assert env.receipts.view(user)["reply_error_code"] is None
    env.receipts.record_status({"id": "wamid.3", "recipient_id": EDMUND, "status": "delivered"})
    assert env.receipts.view(user)["reply_status"] == "delivered"
    assert env.users.get(user.id) == user


def test_unverified_user_status_reply_never_creates_number_proof(env):
    asyncio.run(env.router.handle(IncomingMessage(EDMUND, "status", "inbound.1")))
    assert env.receipts.view(env.user) == {
        "attempt_status": "waiting", "attempted_at": None,
        "reply_status": "not_sent", "reply_error_code": None,
    }
    assert not env.users.get(env.user.id).joined
