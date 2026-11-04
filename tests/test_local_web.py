import json
from fastapi.testclient import TestClient
from app import wiring
from app.local_web import build_local_web
from tests.test_multiuser_wiring import settings
from tests.fakes import FakeSender


def test_local_app_runs_real_backend_owner_with_configured_voice(tmp_path, monkeypatch):
    monkeypatch.setattr(wiring, 'build_service', lambda *args: object())
    calls = []
    async def run(agent, context, session, text):
        calls.append((context, session.session_id, text))
        return 'real backend reply'
    monkeypatch.setattr(wiring, 'run_agent', run)
    from dataclasses import replace
    config = replace(settings(tmp_path), elevenlabs_api_key='test-key', elevenlabs_voice_id='voice-id')
    runtime = wiring.build_runtime(config, sender=FakeSender())
    client = TestClient(build_local_web(runtime), base_url='http://127.0.0.1:8016')
    import re
    signin = client.get('/signin')
    assert signin.headers['referrer-policy'] == 'same-origin'
    nonce = re.search(r'name="nonce" value="([^"]+)"', signin.text).group(1)
    page = client.post('/signin', data={'nonce': nonce}, headers={'Origin': 'http://127.0.0.1:8016'})
    bootstrap = json.loads(re.search(r'id="pippa-state" type="application/json">(.*?)</script>', page.text).group(1))
    assert bootstrap['voice'] == {'stt': True, 'tts': True}
    response = client.post('/api/web/message', json=dict(message='hello', voice=False, revision=0,
        request_id='local-first'), headers={'X-CSRF-Token': bootstrap['csrf_token'],
        'Origin': 'http://127.0.0.1:8016'})
    assert response.status_code == 200
    assert response.json()['messages'][-1]['content'] == 'real backend reply'
    context, sid, text = calls[0]
    assert context.store.user_id == 1 and sid == '1:web'
    assert context.calendar is runtime.app_ctx.calendar and context.whatsapp is None
    assert context is not runtime.app_ctx
    assert client.get('/connections/chat').status_code == 200
    assert 'Chat scheduling' in client.get('/connections/chat').text
    assert runtime.group_meetings.calendar_for_user(1) is runtime.app_ctx.calendar


def test_local_session_blocks_cross_site_login_and_rebinding(tmp_path, monkeypatch):
    monkeypatch.setattr(wiring, 'build_service', lambda *args: object())
    runtime = wiring.build_runtime(settings(tmp_path), sender=FakeSender())
    client = TestClient(build_local_web(runtime), base_url='http://127.0.0.1:8016', follow_redirects=False)
    assert client.get('/signin', headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 200
    assert client.get('/signin', headers={'Host': 'evil.example'}).status_code == 400
    assert client.post('/api/web/message', json={}).status_code == 401
    assert client.post('/signin', data={}, headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    assert client.post('/api/web/message', json={}).status_code == 401


def test_public_runtime_still_disallows_owner_admin_web_access(tmp_path, monkeypatch):
    monkeypatch.setattr(wiring, 'build_service', lambda *args: object())
    runtime = wiring.build_runtime(settings(tmp_path), sender=FakeSender())
    assert not runtime.web_assistant._ready(runtime.users.get(1))
