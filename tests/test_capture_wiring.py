from dataclasses import replace
import asyncio
import logging

import pytest

from app.config import REQUIRED, ConfigError, load_settings
from app.crypto import generate_key
from app.router import IncomingMessage
from app.wiring import build_runtime
from app.webhook import create_app, extract_messages
from fastapi.testclient import TestClient
from tests.fakes import FakeSender
from tests.test_webhook import post, text_payload, SECRET


def env(tmp_path):
    settings = {k: 'x' for k in REQUIRED}
    settings.update(OWNER_WHATSAPP='6590000001', DB_PATH=str(tmp_path / 'agent.db'))
    return settings


def enabled_env(tmp_path):
    return {**env(tmp_path), 'CONFERENCE_CAPTURE_ENABLED':'true', 'GOOGLE_WEB_CLIENT_ID':'id',
            'GOOGLE_WEB_CLIENT_SECRET':'secret', 'BASE_URL':'https://pippa.example',
            'BOT_WHATSAPP':'6590000009', 'TOKEN_KEY':generate_key(), 'SESSION_SECRET':'s'*40}


def test_feature_disabled_preserves_runtime(tmp_path):
    runtime = build_runtime(load_settings(env(tmp_path)), sender=FakeSender())
    assert runtime.capture_ingest is None
    assert runtime.conference_capture is None
    assert len(runtime.jobs) == 1


def test_feature_requires_private_link_configuration(tmp_path):
    with pytest.raises(ConfigError, match='capture|Capture|CONFERENCE_CAPTURE'):
        load_settings({**env(tmp_path), 'CONFERENCE_CAPTURE_ENABLED':'true'})


def test_enabled_runtime_wires_ingress_and_private_routes(tmp_path):
    runtime = build_runtime(load_settings(enabled_env(tmp_path)), sender=FakeSender())
    assert runtime.conference_capture is not None
    assert runtime.capture_ingest is not None
    assert len(runtime.jobs) == 2
    assert runtime.capture_ingest(IncomingMessage('6590000001', 'start collecting notes for Demo', 'start'))
    session = runtime.conference_capture.captures.current(1)
    assert session and session['title'] == 'Demo'
    paths = [r.path for router in runtime.routes for r in router.routes if hasattr(r, 'path')]
    assert '/captures/{session_id}' in paths
    assert paths.index('/captures/{session_id}') < paths.index('/{username}')


def test_image_payload_retains_id_caption_and_timestamp():
    payload = text_payload()
    message = payload['entry'][0]['changes'][0]['value']['messages'][0]
    message.update(type='image', timestamp='1791021600', image={'id':'slide-1','caption':'My thought'})
    incoming = extract_messages(payload)[0]
    assert incoming.image_id == 'slide-1'
    assert incoming.caption == 'My thought'
    assert incoming.kind == 'image'
    assert incoming.source_timestamp == '1791021600'
    message['image']['id'] = ['invalid']
    assert extract_messages(payload) == []


def test_capture_is_durable_before_normal_background_dispatch(tmp_path):
    runtime = build_runtime(load_settings(enabled_env(tmp_path)), sender=FakeSender())
    handled = []
    async def handle(msg):
        handled.append(msg)
    app = create_app('token', SECRET, runtime.store, handle, runtime.conference_capture.now,
                     capture_ingest=runtime.capture_ingest)
    client = TestClient(app)
    assert post(client, text_payload('start', '6590000001', 'start collecting notes for Demo')).status_code == 200
    payload = text_payload('note', '6590000001', 'A durable note')
    assert post(client, payload).status_code == 200
    assert post(client, payload).status_code == 200
    records = runtime.conference_capture.captures
    assert len(records.items(1, records.current(1)['id'])) == 1
    assert handled == []
    assert post(client, text_payload('ordinary', '6590000001', 'assistant: what is my next meeting?')).status_code == 200
    assert handled[0].text == 'assistant: what is my next meeting?'


def test_webhook_does_not_acknowledge_failed_capture_persistence(tmp_path):
    from app.store import Store
    def fail(msg):
        raise RuntimeError('database unavailable')
    app = create_app('token', SECRET, Store(':memory:'), lambda msg: None,
        lambda: None, capture_ingest=fail)
    client = TestClient(app, raise_server_exceptions=False)
    assert post(client, text_payload()).status_code == 500


@pytest.mark.parametrize('path', ['/captures/session/items/1?token=secret', '/%63aptures/session?token=secret'])
def test_capture_urls_are_redacted_in_application_access_logs(path):
    from app.group_privacy import PrivateMeetingLogFilter
    record = logging.LogRecord('uvicorn.access', logging.INFO, '', 0, '%s %s %s %s %s',
        ('ip', 'GET', path, '1.1', 200), None)
    PrivateMeetingLogFilter().filter(record)
    assert 'secret' not in str(record.args)


def test_capture_namespace_is_reserved():
    from app.public_booking import RESERVED
    assert 'captures' in RESERVED


def test_account_deletion_removes_capture_tables(tmp_path):
    runtime = build_runtime(load_settings(enabled_env(tmp_path)), sender=FakeSender())
    users = runtime.users
    user = users.start_signup('Alice', 'alice@example.com', 'encrypted-token', runtime.conference_capture.now())[0]
    records = runtime.conference_capture.captures
    session = records.start(user.id, '6590000002', 'Private talk')
    records.add_item(user.id, session['id'], 'private-note', 'text', text='private')
    records.notify(user.id, '6590000002', 'private receipt')
    records.mark_seen(user.id, 'private-note')
    users.delete(user.id)
    for table in ('capture_sessions','capture_items','capture_notifications','capture_receipts'):
        assert records.conn.execute(f'SELECT count(*) FROM {table} WHERE user_id=?', (user.id,)).fetchone()[0] == 0


def test_previously_handled_ordinary_message_cannot_be_recaptured(tmp_path):
    runtime = build_runtime(load_settings(enabled_env(tmp_path)), sender=FakeSender())
    handled = []
    async def handle(msg):
        handled.append(msg)
    app = create_app('token', SECRET, runtime.store, handle, runtime.conference_capture.now,
                     capture_ingest=runtime.capture_ingest)
    client = TestClient(app)
    note = text_payload('old-message', '6590000001', 'Ordinary earlier conversation')
    post(client, note)
    post(client, text_payload('start', '6590000001', 'start collecting notes for New talk'))
    post(client, note)
    records = runtime.conference_capture.captures
    assert records.items(1, records.current(1)['id']) == []
