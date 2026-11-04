import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from agents.tool_context import ToolContext

from app.assistant_tools import AppContext
from app.timeutil import SGT
from app.tools.calendar import GoogleCalendar
from app.tools.calendly import BookingLink
from tests.fakes import FakeCalendarService
from tests.test_workshop import setup, login, connect, ORIGIN


class FakeLive:
    def __init__(self):
        self.created, self.stopped = [], []

    async def create(self, user_id, sdp):
        self.created.append((user_id, sdp))
        return {'session': {'id': 'live-test'}, 'transport': {'type': 'webrtc', 'sdp': 'v=0\r\nanswer'}}

    async def stop(self, user_id, session_id):
        self.stopped.append((user_id, session_id))


def test_live_routes_require_login_google_csrf_origin_and_valid_sdp(tmp_path, monkeypatch):
    _, app, client, _, _ = setup(tmp_path, monkeypatch)
    live = FakeLive()
    app.state.workshop.web.live = live
    assert client.post('/api/web/live/session', json={'sdp': 'v=0\r\noffer'}).status_code == 401
    csrf = login(client)
    headers = {'Origin': ORIGIN, 'X-CSRF-Token': csrf}
    assert client.post('/api/web/live/session', json={'sdp': 'v=0\r\noffer'}, headers=headers).status_code == 403
    connect(client, csrf)
    for bad in ({'Origin': ORIGIN}, {'X-CSRF-Token': csrf}, headers | {'Origin': 'https://evil.example'}):
        assert client.post('/api/web/live/session', json={'sdp': 'v=0\r\noffer'}, headers=bad).status_code == 403
    for bad in ({'sdp': ''}, {'sdp': 123}, {'sdp': 'x' * 70000}):
        assert client.post('/api/web/live/session', json=bad, headers=headers).status_code in (400, 413)
    assert live.created == []
    response = client.post('/api/web/live/session', json={'sdp': 'v=0\r\noffer'}, headers=headers)
    assert response.status_code == 201
    assert live.created == [(1, 'v=0\r\noffer')]
    assert response.json()['session']['id'] == 'live-test'
    state = client.get('/api/web/state').json()
    assert state['voice']['live'] == 'openai' and not state['voice']['stt']
    response = client.post('/api/web/live/stop', json={'session_id': 'live-test'}, headers=headers)
    assert response.status_code == 200 and live.stopped == [(1, 'live-test')]


def test_live_provider_failure_is_safe_and_disabled_service_is_clear(tmp_path, monkeypatch):
    from app.tools.openai_live import LiveVoiceError
    _, app, client, _, _ = setup(tmp_path, monkeypatch)
    csrf = login(client)
    connect(client, csrf)
    headers = {'Origin': ORIGIN, 'X-CSRF-Token': csrf}
    result = client.post('/api/web/live/session', json={'sdp': 'v=0\r\noffer'}, headers=headers)
    assert result.status_code == 503
    class Broken(FakeLive):
        async def create(self, *args):
            raise LiveVoiceError('Voice session could not start.', 502)
    app.state.workshop.web.live = Broken()
    result = client.post('/api/web/live/session', json={'sdp': 'v=0\r\noffer'}, headers=headers)
    assert result.status_code == 502
    assert result.json()['error'] == 'Voice session could not start.'


def test_live_voice_cannot_confirm_calendar_writes_but_typed_confirmation_can(tmp_path, monkeypatch):
    _, app, _, _, _ = setup(tmp_path, monkeypatch, OPENAI_API_KEY='test-key', OPENAI_MODEL='test-model')
    tools = {tool.name: tool for tool in app.state.workshop.agent.tools}
    now = datetime(2026, 1, 1, 9, tzinfo=SGT)
    service = FakeCalendarService()
    ctx = AppContext(GoogleCalendar(service), BookingLink(None), app.state.workshop.store, lambda: now)
    wrapper = ToolContext(context=ctx, tool_name='create_event', tool_call_id='call-test', tool_arguments='{}')
    args = dict(title='Voice draft', start=(now + timedelta(days=1)).isoformat(),
                end=(now + timedelta(days=1,minutes=30)).isoformat(), booking_mode='personal',
                attendee_email=None, attendee_name=None, confirmed=False, location='Workshop')
    async def exercise():
        ctx.run_no = 1
        ctx.current_input_is_voice = True
        await tools['create_event'].on_invoke_tool(wrapper, json.dumps(args))
        ctx.run_no = 2
        args['confirmed'] = True
        result = await tools['create_event'].on_invoke_tool(wrapper, json.dumps(args))
        assert not any(name == 'insert' for name, _ in service.calls)
        assert 'typ' in result.lower() and 'typ' in ctx.booking_reply.lower()
        ctx.run_no = 3
        ctx.current_input_is_voice = False
        result = await tools['create_event'].on_invoke_tool(wrapper, json.dumps(args))
        assert result.startswith('Created:')
        assert sum(name == 'insert' for name, _ in service.calls) == 1
        ctx.current_input_is_voice = True
        result = await tools['update_event'].on_invoke_tool(wrapper, json.dumps(dict(event_id='evt_new',title='Changed',new_start=None,new_end=None)))
        assert 'typ' in result.lower() and not any(name == 'patch' for name, _ in service.calls)
    asyncio.run(exercise())


def test_live_voice_never_executes_delete_even_after_spoken_yes(tmp_path, monkeypatch):
    _, app, _, _, _ = setup(tmp_path, monkeypatch, OPENAI_API_KEY='test-key', OPENAI_MODEL='test-model')
    tool = next(tool for tool in app.state.workshop.agent.tools if tool.name == 'delete_event')
    now = datetime(2026, 1, 1, 9, tzinfo=SGT)
    item = {'id':'evt_test','summary':'Workshop', 'start':{'dateTime':(now+timedelta(days=1)).isoformat()}, 'end':{'dateTime':(now+timedelta(days=1,minutes=30)).isoformat()}}
    service = FakeCalendarService(items=[item])
    ctx = AppContext(GoogleCalendar(service), BookingLink(None), app.state.workshop.store, lambda: now)
    wrapper = ToolContext(context=ctx,tool_name='delete_event',tool_call_id='delete-test',tool_arguments='{}')
    async def exercise():
        ctx.current_input_is_voice = True
        for run in (1,2):
            ctx.run_no = run
            result=await tool.on_invoke_tool(wrapper,json.dumps({'event_id':'evt_test','confirmed':True}))
            assert result.startswith('CONFIRM:') and 'typ' in result.lower()
            assert not service.deleted
        ctx.run_no = 3
        ctx.current_input_is_voice = False
        result=await tool.on_invoke_tool(wrapper,json.dumps({'event_id':'evt_test','confirmed':True}))
        assert result.startswith('Deleted:') and service.deleted == {'evt_test'}
    asyncio.run(exercise())
