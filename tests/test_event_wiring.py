"""Release switch is default-off and reversible without deleting event identities."""
from datetime import datetime, timedelta, timezone
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import wiring
from app.config import ConfigError, REQUIRED, load_settings
from app.crypto import generate_key
from app.users import CALENDAR_SCOPE
from tests.fakes import FakeSender

NOW = datetime(2026, 10, 3, 2, 0, tzinfo=timezone.utc)


def environment(tmp_path, **extra):
    return {**{key:'x' for key in REQUIRED}, 'OWNER_WHATSAPP':'6591234567',
            'DB_PATH':str(tmp_path/'agent.db'), 'GOOGLE_WEB_CLIENT_ID':'web-id',
            'GOOGLE_WEB_CLIENT_SECRET':'web-secret', 'BASE_URL':'https://testserver',
            'BOT_WHATSAPP':'15550001234', 'TOKEN_KEY':generate_key(),
            'SESSION_SECRET':'s'*32, **extra}


def build(env, monkeypatch):
    monkeypatch.setattr(wiring, 'build_service', lambda *args: object())
    runtime = wiring.build_runtime(load_settings(env), sender=FakeSender())
    app = FastAPI()
    for router in runtime.routes:
        app.include_router(router)
    return runtime, TestClient(app, base_url='https://testserver', follow_redirects=False)


def test_event_release_defaults_off_without_disabling_booking(tmp_path, monkeypatch):
    env = environment(tmp_path)
    settings = load_settings(env)
    assert settings.event_networking_enabled is False
    runtime, client = build(env, monkeypatch)
    assert runtime.event_networking is None
    assert runtime.public_booking is not None
    assert runtime.public_booking.network_profile is None
    assert client.get('/events/builders').status_code == 404
    assert client.get('/network/inbox').status_code == 404
    assert client.get('/people/2').status_code == 404
    assert client.get('/connections/scheduling').headers['location'] == '/signin'
    assert client.get('/signin').status_code == 200
    host, _ = runtime.users.sign_in_identity('host-sub', 'Host', 'host@example.com', NOW)
    runtime.users._update(host.id,NOW,status='approved',google_token_enc='sealed',
                          google_scopes=json.dumps([CALENDAR_SCOPE]),calendar_status='connected')
    runtime.users.conn.execute('INSERT INTO public_booking_profiles VALUES (?,?,?,?,?,?,?,?,?,?)',
        (host.id,'host','Host','30-minute meeting','Asia/Singapore','Cafe','09:00','18:00','[0,1,2,3,4]',1))
    runtime.users.conn.commit()
    assert client.get('/host').headers['location'] == '/host/30min'
    assert client.get('/host/30min').status_code == 200


def test_explicit_release_mounts_event_routes_only_with_signup(tmp_path, monkeypatch):
    env = environment(tmp_path, EVENT_NETWORKING_ENABLED='true')
    runtime, client = build(env, monkeypatch)
    assert runtime.event_networking is not None
    assert runtime.public_booking.connections.event_access is runtime.event_networking.store
    runtime.event_networking.store.create_event('builders', 'Builders', NOW+timedelta(days=1))
    assert client.get('/events/builders').status_code == 200
    assert client.get('/network/inbox').headers['location'] == '/network/signin'
    assert client.get('/__preview').status_code == 404
    assert client.get('/__preview/as/alice').status_code == 404
    for key in ('GOOGLE_WEB_CLIENT_ID','GOOGLE_WEB_CLIENT_SECRET','BASE_URL','BOT_WHATSAPP','TOKEN_KEY','SESSION_SECRET'):
        env.pop(key)
    env['DB_PATH'] = str(tmp_path/'no-signup.db')
    runtime, client = build(env, monkeypatch)
    assert runtime.event_networking is None
    assert client.get('/events/builders').status_code == 404


def test_disabling_release_preserves_event_only_accounts_and_roster(tmp_path, monkeypatch):
    env = environment(tmp_path, EVENT_NETWORKING_ENABLED='true')
    enabled, _ = build(env, monkeypatch)
    store = enabled.event_networking.store
    store.now = lambda: NOW
    store.create_event('builders', 'Builders', NOW+timedelta(days=1))
    store.import_roster('builders', [dict(source_id='one',name='Attendee',email='attendee@example.com')], actor_id=1)
    store.check_in('builders', 'one', 'receipt', actor_id=1)
    user, _ = enabled.users.sign_in_event_identity('event-sub', 'Attendee', 'attendee@example.com', NOW)
    store.claim('builders', user)
    enabled.users.conn.close()
    disabled, client = build({**env,'EVENT_NETWORKING_ENABLED':'false'}, monkeypatch)
    assert disabled.event_networking is None
    assert disabled.public_booking.network_profile is None
    assert disabled.users.get(user.id).event_only is True
    assert disabled.users.conn.execute('SELECT COUNT(*) FROM event_memberships').fetchone()[0] == 1
    assert client.get('/events/builders').status_code == 404
    assert client.get('/network/inbox').status_code == 404
    assert disabled.users.seats_left() == disabled.users.max_users - 1
    disabled.users.conn.close()
    restored, client = build(env, monkeypatch)
    assert restored.users.get(user.id).event_only is True
    assert restored.event_networking.store.membership('builders', user.id)['claimed_at']
    assert client.get('/events/builders').status_code == 200


@pytest.mark.parametrize(('raw','expected'), [('true',True),('1',True),(' YES ',True),('false',False),('0',False),('no',False),('',False)])
def test_release_switch_uses_existing_boolean_conventions(tmp_path, raw, expected):
    assert load_settings(environment(tmp_path, EVENT_NETWORKING_ENABLED=raw)).event_networking_enabled is expected


def test_release_switch_rejects_invalid_boolean(tmp_path):
    with pytest.raises(ConfigError, match='EVENT_NETWORKING_ENABLED must be true or false'):
        load_settings(environment(tmp_path, EVENT_NETWORKING_ENABLED='sometimes'))
