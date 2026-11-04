import asyncio
import json
from types import SimpleNamespace

import pytest

from app.web_assistant import WebAssistant
from app.users import CALENDAR_SCOPE
from app.router import PartialRunError
from tests.test_connections_routes import site


class Voice:
    stt_enabled = True
    tts_enabled = True

    def __init__(self):
        self.calls = []

    async def transcribe(self, audio, mime):
        self.calls.append(('stt', audio, mime))
        return 'Meet Ana tomorrow.'

    async def speak(self, text):
        self.calls.append(('tts', text))
        return b'fake-mp3'

    async def realtime_token(self):
        self.calls.append(('token',))
        return 'short-lived-token'


@pytest.fixture
def web(site):
    runs = []

    async def run(user, text):
        runs.append((user.id, text))
        return 'Please confirm the exact date and attendee.'

    voice = Voice()
    service = WebAssistant(site.obj, run, voice, 'https://testserver')
    site.client.app.include_router(service.router)
    site.flow()
    user = site.db.by_email('a@example.com')
    site.db._update(user.id, site.clock[0], status='approved',
                    google_token_enc=site.box.encrypt('refresh'),
                    google_scopes=json.dumps([CALENDAR_SCOPE]), calendar_status='connected')
    return SimpleNamespace(**vars(site), service=service, voice=voice, runs=runs, user_id=user.id)


def post(web, path, payload=None, **kwargs):
    return web.client.post('/api/web/' + path, json=payload, headers={
        'X-CSRF-Token': web.csrf(), 'Origin': 'https://testserver'}, **kwargs)


def message(web, **changes):
    data = dict(message='What is on tomorrow?', voice=False, revision=0, request_id='request-0001')
    data.update(changes)
    return post(web, 'message', data)


def test_approved_web_user_can_chat_without_whatsapp(web):
    assert not web.db.get(web.user_id).phone
    assert web.client.get('/app').status_code == 200
    response = message(web)
    assert response.status_code == 200
    assert [m['role'] for m in response.json()['messages']] == ['user', 'assistant']
    assert web.runs == [(web.user_id, 'What is on tomorrow?')]
    assert response.headers['cache-control'] == 'no-store'


def test_voice_origin_is_preserved_and_speech_uses_saved_reply(web):
    response = message(web, voice=True)
    assert web.runs[0][1].startswith('[voice] ')
    reply_id = response.json()['messages'][-1]['id']
    audio = post(web, 'speech', {'message_id': reply_id, 'text': 'arbitrary speech'})
    assert audio.status_code == 200 and audio.content == b'fake-mp3'
    assert web.voice.calls[-1] == ('tts', 'Please confirm the exact date and attendee.')
    assert audio.headers['content-type'] == 'audio/mpeg'
    assert post(web, 'speech', {'message_id': response.json()['messages'][0]['id']}).status_code == 404


def test_transcription_is_draft_only(web):
    r = web.client.post('/api/web/transcribe', content=b'audio', headers={
        'Content-Type': 'audio/webm', 'X-CSRF-Token': web.csrf(), 'Origin': 'https://testserver'})
    assert r.json() == {'text': 'Meet Ana tomorrow.'}
    assert web.runs == []
    assert web.client.get('/api/web/state').json()['messages'] == []


@pytest.mark.parametrize('path', ['message', 'transcribe', 'speech'])
def test_mutations_reject_missing_csrf_cross_origin_and_unicode(web, path):
    assert web.client.post('/api/web/' + path, json={}).status_code == 403
    assert web.client.post('/api/web/' + path, json={}, headers={
        'X-CSRF-Token': web.csrf(), 'Origin': 'https://evil.example'}).status_code == 403
    # ASCII header with a mismatching token must not execute a provider call.
    assert web.client.post('/api/web/' + path, json={}, headers={
        'X-CSRF-Token': 'bad'}).status_code == 403
    assert not web.runs and not web.voice.calls


def test_replayed_request_does_not_run_agent_twice_and_stale_revision_rejected(web):
    r = message(web)
    assert message(web).json() == r.json()
    assert len(web.runs) == 1
    assert message(web, request_id='request-0002').status_code == 409
    assert message(web, message='changed', revision=1).status_code == 409


@pytest.mark.parametrize('changes', [dict(message=''), dict(message='x' * 4001),
                                    dict(voice='false'), dict(revision=True), dict(request_id='')])
def test_invalid_message_never_calls_agent(web, changes):
    assert message(web, **changes).status_code == 400
    assert web.runs == []


@pytest.mark.parametrize('fields', [dict(status='disabled'), dict(status='pending'),
    dict(event_only=True), dict(calendar_status='not_connected'), dict(google_disconnected=True)])
def test_ineligible_account_cannot_call_paid_endpoints(web, fields):
    token = web.csrf()
    web.db._update(web.user_id, web.clock[0], **fields)
    assert web.client.post('/api/web/message', json={}, headers={'X-CSRF-Token': token}).status_code in (401, 403)
    assert not web.runs and not web.voice.calls


def test_signed_out_routes_and_history_isolation(web):
    message(web)
    b, _ = web.db.sign_in_identity('sub-b', 'B', 'b@example.com', web.clock[0])
    web.db._update(b.id, web.clock[0], status='approved', google_token_enc=web.box.encrypt('refresh-b'),
                    google_scopes=json.dumps([CALENDAR_SCOPE]), calendar_status='connected')
    web.oauth.account = type(web.oauth.account)('b@example.com', 'B', '',
        granted_scopes=('openid','email'), subject='sub-b', access_token='access')
    web.flow()
    assert web.client.get('/api/web/state').json()['messages'] == []
    web.client.cookies.clear()
    assert web.client.get('/api/web/state').status_code == 401
    assert web.client.get('/app').headers['location'] == '/signin'


def test_partial_write_is_reported_and_not_retried(web):
    async def fail(user, text):
        raise PartialRunError(['Created: dentist appointment [id: known-id]'])
    web.service.run = fail
    r = message(web)
    assert r.status_code == 200
    assert 'known-id' in r.json()['messages'][-1]['content']
    assert message(web).json() == r.json()


def test_deleted_account_cannot_be_recreated_by_late_reply(web):
    async def delete(user, text):
        web.db.delete(user.id)
        return 'late reply'
    web.service.run = delete
    r = message(web)
    assert r.status_code == 401
    assert web.db.conn.execute('SELECT count(*) FROM web_conversations').fetchone()[0] == 0


def test_request_and_audio_size_limits_before_provider(web):
    assert web.client.post('/api/web/message', content=b'x' * 20000, headers={
        'X-CSRF-Token': web.csrf(), 'Content-Type': 'application/json'}).status_code == 413
    assert web.client.post('/api/web/transcribe', content=b'x' * (5 * 1024 * 1024 + 1), headers={
        'X-CSRF-Token': web.csrf(), 'Content-Type': 'audio/webm'}).status_code == 413
    assert not web.runs and not web.voice.calls


def test_asset_allowlist_blocks_arbitrary_files(web):
    assert web.client.get('/pippa-web-assets/app.js').status_code == 200
    assert web.client.get('/pippa-web-assets/secrets.txt').status_code == 404


@pytest.mark.parametrize('path', ['transcribe', 'speech'])
def test_revocation_during_body_read_prevents_paid_provider_call(web, path):
    if path == 'speech':
        message(web)
    original = web.service._body
    async def revoked(request, limit):
        body = await original(request, limit)
        web.db._update(web.user_id, web.clock[0], google_disconnected=True)
        return body
    web.service._body = revoked
    if path == 'transcribe':
        response = web.client.post('/api/web/transcribe', content=b'audio', headers={
            'X-CSRF-Token': web.csrf(), 'Content-Type': 'audio/webm'})
    else:
        response = post(web, 'speech', {'message_id': web.service._state(web.user_id)['messages'][-1]['id']})
    assert response.status_code == 403
    assert not web.voice.calls


def test_logout_during_agent_run_finishes_private_receipt_without_leaking_reply(web):
    async def logout(user, text):
        web.db.conn.execute('DELETE FROM connection_sessions WHERE user_id=?', (user.id,))
        web.db.conn.commit()
        return 'private reply after logout'
    web.service.run = logout
    response = message(web)
    assert response.status_code == 401
    assert 'private reply' not in response.text
    assert not web.service._load(web.user_id)['pending']


def test_parallel_message_is_rejected_without_second_agent_run(web):
    from starlette.requests import Request
    token = web.client.cookies.get('pippa_session')
    csrf = web.csrf()
    def request(request_id):
        body = json.dumps(dict(message='hello', revision=0, voice=False, request_id=request_id)).encode()
        async def receive():
            return {'type': 'http.request', 'body': body, 'more_body': False}
        return Request({'type': 'http', 'method': 'POST', 'path': '/api/web/message', 'headers': [
            (b'cookie', f'pippa_session={token}'.encode()), (b'x-csrf-token', csrf.encode()),
            (b'content-type', b'application/json')], 'query_string': b''}, receive)
    async def check():
        entered, finish = asyncio.Event(), asyncio.Event()
        calls = []
        async def slow(user, text):
            calls.append(text)
            entered.set()
            await finish.wait()
            return 'done'
        web.service.run = slow
        user = web.db.get(web.user_id)
        first = asyncio.create_task(web.service._message(request('request-first'), user))
        await entered.wait()
        second = await web.service._message(request('request-second'), user)
        assert second.status_code == 409
        finish.set()
        assert (await first).status_code == 200
        assert calls == ['hello']
    asyncio.run(check())


def test_realtime_token_is_authenticated_csrf_protected_and_delegated_only(web):
    assert web.client.post('/api/web/realtime-token', json={}).status_code == 403
    response = post(web, 'realtime-token', {})
    assert response.status_code == 200
    assert response.json() == {'token': 'short-lived-token'}
    assert response.headers['cache-control'] == 'no-store'
    assert web.voice.calls == [('token',)]


def test_realtime_token_refuses_revoked_account_after_provider_await(web):
    async def token():
        web.db._update(web.user_id, web.clock[0], google_disconnected=True)
        return 'must-not-leak-token'
    web.voice.realtime_token = token
    response = post(web, 'realtime-token', {})
    assert response.status_code == 403
    assert 'must-not-leak-token' not in response.text


def test_realtime_token_rate_limit_and_bounded_request(web):
    assert web.client.post('/api/web/realtime-token', content=b'x' * 20000,
        headers={'X-CSRF-Token': web.csrf(), 'Content-Type': 'application/json'}).status_code == 413
    for _ in range(10):
        assert post(web, 'realtime-token', {}).status_code == 200
    assert post(web, 'realtime-token', {}).status_code == 429
