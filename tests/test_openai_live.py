import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.tools.openai_live import LiveVoiceError, OpenAILiveSessions


def provider():
    response = SimpleNamespace(session=SimpleNamespace(id='live_one', secret='never-forward'),
                               transport=SimpleNamespace(type='webrtc', sdp='v=0\r\nanswer'))
    live = SimpleNamespace(create=AsyncMock(return_value=response),
                           sessions=SimpleNamespace(hangup=AsyncMock()))
    return SimpleNamespace(live=live, close=AsyncMock())


def test_creation_restricts_voice_and_returns_only_transport():
    async def scenario():
        client = provider()
        service = OpenAILiveSessions('private-key', client=client)
        result = await service.create(1, 'v=0\r\noffer')
        assert result == {'session': {'id': 'live_one'}, 'transport': {'type': 'webrtc', 'sdp': 'v=0\r\nanswer'}}
        request = client.live.create.call_args.kwargs
        assert request['session']['model'] == 'gpt-live-1'
        assert request['session']['delegation']['type'] == 'client'
        assert request['session']['store'] is False
        permissions = request['session']['client']['data_channel']
        assert 'session.close' in permissions['allowed_client_events']
        assert 'session.update' not in permissions['allowed_client_events']
        assert not any(event.startswith('response.') for event in permissions['allowed_client_events'])
        assert all(isinstance(event, dict) and 'type' in event for event in permissions['allowed_server_events'])
        assert 'typed' in request['session']['instructions']
        assert 'private-key' not in str(result) + str(request)
        assert 'audio' not in request['session']
        await service.shutdown()
        client.live.sessions.hangup.assert_awaited_once_with('live_one')
        client.close.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize('sdp', ['', '  ', None, 12, 'x' * 65537, 'é' * 32769, '\ud800'])
def test_bad_offer_never_reaches_provider(sdp):
    async def scenario():
        client = provider()
        service = OpenAILiveSessions('key', client=client)
        with pytest.raises(LiveVoiceError) as error:
            await service.create(1, sdp)
        assert error.value.status_code == 400
        client.live.create.assert_not_called()
        await service.shutdown()
    asyncio.run(scenario())


def test_missing_key_and_closed_manager_do_not_start_sessions():
    async def scenario():
        client = provider()
        service = OpenAILiveSessions('', client=client)
        with pytest.raises(LiveVoiceError, match='configured'):
            await service.create(1, 'offer')
        await service.shutdown()
        with pytest.raises(LiveVoiceError):
            await service.create(1, 'offer')
        client.live.create.assert_not_called()
    asyncio.run(scenario())


def test_inflight_creation_reserves_user_and_failure_allows_retry():
    async def scenario():
        client = provider()
        gate = asyncio.Event()
        async def fail(**kwargs):
            await gate.wait()
            raise RuntimeError('private-key provider body')
        client.live.create.side_effect = fail
        service = OpenAILiveSessions('private-key', client=client)
        pending = asyncio.create_task(service.create(1, 'offer'))
        await asyncio.sleep(0)
        with pytest.raises(LiveVoiceError) as conflict:
            await service.create(1, 'another offer')
        assert conflict.value.status_code == 409
        gate.set()
        with pytest.raises(LiveVoiceError) as error:
            await pending
        assert 'private-key' not in str(error.value)
        assert 'provider body' not in str(error.value)
        assert error.value.__cause__ is None
        assert client.live.create.await_count == 1
        client.live.create.side_effect = None
        await service.create(1, 'new offer')
        await service.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('broken', [None, '', 12, 'x' * 65537, 'private-key', '\ud800'])
def test_bad_answer_is_rejected_and_known_session_closed(broken):
    async def scenario():
        client = provider()
        client.live.create.return_value.transport.sdp = broken
        service = OpenAILiveSessions('private-key', client=client)
        with pytest.raises(LiveVoiceError):
            await service.create(1, 'offer')
        client.live.sessions.hangup.assert_awaited_once_with('live_one')
        await service.shutdown()
    asyncio.run(scenario())


def test_missing_provider_id_returns_safe_failure():
    async def scenario():
        client = provider()
        client.live.create.return_value.session.id = None
        service = OpenAILiveSessions('key', client=client)
        with pytest.raises(LiveVoiceError):
            await service.create(1, 'offer')
        client.live.sessions.hangup.assert_not_called()
        await service.shutdown()
    asyncio.run(scenario())


def test_stop_checks_ownership_and_failed_hangup_can_be_retried():
    async def scenario():
        client = provider()
        service = OpenAILiveSessions('key', client=client)
        await service.create(1, 'offer')
        for owner, session in [(2, 'live_one'), (1, 'live_other')]:
            with pytest.raises(LiveVoiceError) as error:
                await service.stop(owner, session)
            assert error.value.status_code == 404
        client.live.sessions.hangup.assert_not_called()
        client.live.sessions.hangup.side_effect = RuntimeError('private provider failure')
        with pytest.raises(LiveVoiceError, match='not confirm'):
            await service.stop(1, 'live_one')
        with pytest.raises(LiveVoiceError) as error:
            await service.create(1, 'replacement')
        assert error.value.status_code == 409
        client.live.sessions.hangup.side_effect = None
        await service.stop(1, 'live_one')
        assert client.live.sessions.hangup.await_count == 2
        await service.shutdown()
    asyncio.run(scenario())


def test_concurrent_stop_only_calls_provider_once():
    async def scenario():
        client = provider()
        gate = asyncio.Event()
        async def close(_):
            await gate.wait()
        client.live.sessions.hangup.side_effect = close
        service = OpenAILiveSessions('key', client=client)
        await service.create(1, 'offer')
        stops = [asyncio.create_task(service.stop(1, 'live_one')) for _ in range(2)]
        await asyncio.sleep(0)
        gate.set()
        await asyncio.gather(*stops)
        client.live.sessions.hangup.assert_awaited_once()
        await service.shutdown()
    asyncio.run(scenario())


def test_cancelled_create_closes_late_provider_session():
    async def scenario():
        client = provider()
        gate = asyncio.Event()
        response = client.live.create.return_value
        async def delayed(**kwargs):
            await gate.wait()
            return response
        client.live.create.side_effect = delayed
        service = OpenAILiveSessions('key', client=client)
        pending = asyncio.create_task(service.create(1, 'offer'))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        with pytest.raises(LiveVoiceError) as error:
            await service.create(1, 'second')
        assert error.value.status_code == 409
        gate.set()
        await service.shutdown()
        client.live.sessions.hangup.assert_awaited_once_with('live_one')
        client.close.assert_awaited_once()
    asyncio.run(scenario())


def test_expiry_failure_is_retained_and_can_be_stopped(monkeypatch):
    async def scenario():
        from app.tools import openai_live
        monkeypatch.setattr(openai_live, 'SESSION_SECONDS', 0)
        client = provider()
        client.live.sessions.hangup.side_effect = RuntimeError('private failure')
        service = OpenAILiveSessions('key', client=client)
        await service.create(1, 'offer')
        for _ in range(8):
            await asyncio.sleep(0)
        assert client.live.sessions.hangup.await_count == 1
        assert 'not confirm' in service.last_error
        with pytest.raises(LiveVoiceError):
            await service.create(1, 'second')
        client.live.sessions.hangup.side_effect = None
        await service.stop(1, 'live_one')
        await service.shutdown()
    asyncio.run(scenario())


def test_shutdown_failure_keeps_cleanup_retryable():
    async def scenario():
        client = provider()
        service = OpenAILiveSessions('key', client=client)
        await service.create(1, 'offer')
        client.live.sessions.hangup.side_effect = RuntimeError('secret')
        with pytest.raises(LiveVoiceError):
            await service.shutdown()
        client.close.assert_not_called()
        client.live.sessions.hangup.side_effect = None
        await service.shutdown()
        client.close.assert_awaited_once()
    asyncio.run(scenario())


def test_real_client_disables_retries_and_has_bounded_timeout(monkeypatch):
    from app.tools import openai_live
    made = []
    monkeypatch.setattr(openai_live, 'AsyncOpenAI', lambda **kwargs: made.append(kwargs) or provider())
    service = OpenAILiveSessions('private-key')
    assert made == [{'api_key': 'private-key', 'timeout': 30, 'max_retries': 0}]
    asyncio.run(service.shutdown())


def test_cancelled_stop_continues_until_provider_confirms():
    async def scenario():
        client = provider()
        gate = asyncio.Event()
        async def hangup(_):
            await gate.wait()
        client.live.sessions.hangup.side_effect = hangup
        service = OpenAILiveSessions('key', client=client)
        await service.create(1, 'offer')
        stopping = asyncio.create_task(service.stop(1, 'live_one'))
        await asyncio.sleep(0)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        with pytest.raises(LiveVoiceError) as error:
            await service.create(1, 'replacement')
        assert error.value.status_code == 409
        gate.set()
        await service.shutdown()
        client.live.sessions.hangup.assert_awaited_once_with('live_one')
        client.close.assert_awaited_once()
    asyncio.run(scenario())


def test_invalid_provider_answer_with_failed_cleanup_keeps_ownership():
    async def scenario():
        client = provider()
        client.live.create.return_value.transport.sdp = None
        client.live.sessions.hangup.side_effect = RuntimeError('private response')
        service = OpenAILiveSessions('key', client=client)
        with pytest.raises(LiveVoiceError, match='not confirm'):
            await service.create(1, 'offer')
        with pytest.raises(LiveVoiceError) as error:
            await service.create(1, 'replacement')
        assert error.value.status_code == 409
        client.live.sessions.hangup.side_effect = None
        await service.stop(1, 'live_one')
        await service.shutdown()
    asyncio.run(scenario())


def test_client_close_failure_is_sanitized_and_retryable():
    async def scenario():
        client = provider()
        client.close.side_effect = RuntimeError('private-key client detail')
        service = OpenAILiveSessions('private-key', client=client)
        with pytest.raises(LiveVoiceError) as error:
            await service.shutdown()
        assert 'private-key' not in str(error.value)
        assert error.value.__cause__ is None
        client.close.side_effect = None
        await service.shutdown()
        assert client.close.await_count == 2
    asyncio.run(scenario())


def test_failed_manual_stop_preserves_expiry_cleanup(monkeypatch):
    async def scenario():
        from app.tools import openai_live
        monkeypatch.setattr(openai_live, 'SESSION_SECONDS', 0.01)
        monkeypatch.setattr(openai_live, 'EXPIRY_RETRY_SECONDS', 0, raising=False)
        client = provider()
        ended = asyncio.Event()
        attempts = 0
        async def hangup(_):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise RuntimeError('temporary provider error')
            ended.set()
        client.live.sessions.hangup.side_effect = hangup
        service = OpenAILiveSessions('key', client=client)
        await service.create(1, 'offer')
        with pytest.raises(LiveVoiceError):
            await service.stop(1, 'live_one')
        await asyncio.wait_for(ended.wait(), timeout=1)
        assert attempts == 3, 'manual stop fails, expiry fails once, then succeeds'
        await service.create(1, 'replacement')
        await service.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('status', [404, 410])
def test_provider_already_ended_session_releases_owner(status):
    class Gone(Exception):
        status_code = status
    async def scenario():
        client = provider()
        service = OpenAILiveSessions('key', client=client)
        await service.create(1, 'offer')
        client.live.sessions.hangup.side_effect = Gone('private provider body')
        await service.stop(1, 'live_one')
        await service.create(1, 'replacement')
        client.live.sessions.hangup.side_effect = None
        await service.shutdown()
    asyncio.run(scenario())


def test_expiry_retries_are_bounded_and_failure_remains_visible(monkeypatch):
    async def scenario():
        from app.tools import openai_live
        monkeypatch.setattr(openai_live, 'SESSION_SECONDS', 0)
        monkeypatch.setattr(openai_live, 'EXPIRY_RETRY_SECONDS', 0, raising=False)
        client = provider()
        client.live.sessions.hangup.side_effect = RuntimeError('private provider error')
        service = OpenAILiveSessions('key', client=client)
        await service.create(1, 'offer')
        for _ in range(40):
            await asyncio.sleep(0)
        assert client.live.sessions.hangup.await_count == 3
        assert 'not confirm' in service.last_error
        with pytest.raises(LiveVoiceError) as error:
            await service.create(1, 'replacement')
        assert error.value.status_code == 409
        client.live.sessions.hangup.side_effect = None
        await service.stop(1, 'live_one')
        await service.shutdown()
    asyncio.run(scenario())


def test_invalid_answer_keeps_automatic_cleanup_after_failed_hangup(monkeypatch):
    async def scenario():
        from app.tools import openai_live
        monkeypatch.setattr(openai_live, 'SESSION_SECONDS', 0.01)
        client = provider()
        client.live.create.return_value.transport.sdp = None
        client.live.sessions.hangup.side_effect = [RuntimeError('temporary failure'), None]
        service = OpenAILiveSessions('key', client=client)
        with pytest.raises(LiveVoiceError, match='not confirm'):
            await service.create(1, 'offer')
        await asyncio.sleep(0.03)
        assert client.live.sessions.hangup.await_count == 2
        client.live.create.return_value.transport.sdp = 'answer'
        await service.create(1, 'replacement')
        client.live.sessions.hangup.side_effect = None
        await service.shutdown()
    asyncio.run(scenario())
