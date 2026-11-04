import asyncio
import json
from dataclasses import replace

from fastapi.testclient import TestClient

from app import wiring
from app.users import CALENDAR_SCOPE
from app.webhook import create_app
from tests.fakes import FakeSender
from tests.test_multiuser_wiring import NOW, settings


def test_web_runtime_reuses_backend_but_isolates_phone_sessions_and_outreach(tmp_path, monkeypatch):
    monkeypatch.setattr(wiring, 'build_service', lambda *args: object())
    runs = []
    async def run(agent, context, session, text):
        runs.append((agent, context, session.session_id, text))
        return 'ok'
    monkeypatch.setattr(wiring, 'run_agent', run)
    runtime = wiring.build_runtime(settings(tmp_path), sender=FakeSender())
    assert runtime.web_assistant is not None
    user, _ = runtime.users.sign_in_identity('web-user', 'Web User', 'web@example.com', NOW)
    runtime.users._update(user.id, NOW, status='approved',
        google_token_enc=runtime.box.encrypt('refresh'), google_scopes=json.dumps([CALENDAR_SCOPE]),
        calendar_status='connected')
    user = runtime.users.get(user.id)
    assert not user.phone
    assert asyncio.run(runtime.web_assistant.run(user, 'hello')) == 'ok'
    agent, context, sid, text = runs[0]
    assert sid == f'{user.id}:web'
    assert context.store.user_id == user.id
    assert context.whatsapp is None
    assert context is not runtime.app_ctx
    names = {t.name for t in agent.tools}
    assert {'list_events', 'create_event', 'search_email'} <= names
    assert not {'start_loop_in', 'cancel_loop_in'} & names
    runtime.users._update(user.id, NOW, status='disabled')
    from app.tools.calendar import CalendarAuthError
    import pytest
    with pytest.raises(CalendarAuthError):
        context.calendar.before_request()


def test_web_routes_and_assets_are_registered_with_signup_only(tmp_path, monkeypatch):
    monkeypatch.setattr(wiring, 'build_service', lambda *args: object())
    runtime = wiring.build_runtime(settings(tmp_path), sender=FakeSender())
    app = create_app('v', 'secret', runtime.store, runtime.router.handle, lambda: NOW, routes=runtime.routes)
    client = TestClient(app, follow_redirects=False)
    assert client.get('/app').headers['location'] == '/signin'
    assert client.get('/api/web/state').status_code == 401
    assert client.get('/pippa-web-assets/app.css').status_code == 200
    assert client.get('/connections/chat').status_code == 303
    assert client.get('/health').json() == {'ok': True}


def test_voice_configuration_is_optional_and_not_exposed_in_page(tmp_path, monkeypatch):
    monkeypatch.setattr(wiring, 'build_service', lambda *args: object())
    config = replace(settings(tmp_path), elevenlabs_api_key='secret-key', elevenlabs_voice_id='voice-id')
    runtime = wiring.build_runtime(config, sender=FakeSender())
    assert runtime.web_assistant.voice.stt_enabled
    assert runtime.web_assistant.voice.tts_enabled


def test_late_store_writes_after_account_deletion_are_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(wiring, 'build_service', lambda *args: object())
    async def late_run(agent, context, session, text):
        runtime.users.delete(context.store.user_id)
        context.store.add_fact('late sensitive fact', NOW)
        context.store.add_contact('Late contact', '6581112222')
        await session.add_items([{'role': 'assistant', 'content': 'late private reply'}])
        return 'late private reply'
    monkeypatch.setattr(wiring, 'run_agent', late_run)
    runtime = wiring.build_runtime(settings(tmp_path), sender=FakeSender())
    user, _ = runtime.users.sign_in_identity('late-user', 'Late User', 'late@example.com', NOW)
    runtime.users._update(user.id, NOW, status='approved', google_token_enc=runtime.box.encrypt('refresh'),
                         google_scopes=json.dumps([CALENDAR_SCOPE]), calendar_status='connected')
    asyncio.run(runtime.web_assistant.run(runtime.users.get(user.id), 'hello'))
    assert runtime.store.for_user(user.id).list_facts() == []
    assert runtime.store.for_user(user.id).list_contacts() == []
    assert runtime.users.conn.execute('SELECT count(*) FROM agent_messages WHERE session_id=?',
                                     (f'{user.id}:web',)).fetchone()[0] == 0


def test_web_tools_recheck_account_before_saving_facts(tmp_path, monkeypatch):
    from agents import RunContextWrapper
    monkeypatch.setattr(wiring, 'build_service', lambda *args: object())
    results = []
    async def run(agent, context, session, text):
        runtime.users._update(context.store.user_id, NOW, status='disabled')
        remember = next(tool for tool in agent.tools if tool.name == 'remember')
        results.append(await remember.on_invoke_tool(RunContextWrapper(context=context),
                                                      json.dumps({'fact': 'private fact'})))
        return 'blocked'
    monkeypatch.setattr(wiring, 'run_agent', run)
    runtime = wiring.build_runtime(settings(tmp_path), sender=FakeSender())
    user, _ = runtime.users.sign_in_identity('guard-user', 'Guard User', 'guard@example.com', NOW)
    runtime.users._update(user.id, NOW, status='approved', google_token_enc=runtime.box.encrypt('refresh'),
                         google_scopes=json.dumps([CALENDAR_SCOPE]), calendar_status='connected')
    asyncio.run(runtime.web_assistant.run(runtime.users.get(user.id), 'remember a fact'))
    assert results[0].startswith('ERROR:')
    assert not runtime.store.for_user(user.id).list_facts()
