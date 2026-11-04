from app.config import REQUIRED, load_settings
from app.wiring import build_runtime
from tests.fakes import FakeSender


def settings(tmp_path, enabled):
    env = {key: 'x' for key in REQUIRED}
    env.update(OWNER_WHATSAPP='6591234567', DB_PATH=str(tmp_path / 'app.db'),
               PROACTIVE_BRIEFS_ENABLED=enabled)
    return load_settings(env)


def test_disabled_flag_keeps_existing_runtime(tmp_path):
    runtime = build_runtime(settings(tmp_path, 'false'), sender=FakeSender())
    assert runtime.proactive_briefs is None
    assert len(runtime.jobs) == 1
    assert 'not enabled' in runtime.router.commands.handle('6591234567', 'meeting briefs on')


def test_enabled_flag_wires_commands_job_and_cleanup(tmp_path):
    runtime = build_runtime(settings(tmp_path, 'true'), sender=FakeSender())
    assert len(runtime.jobs) == 2
    assert 'enabled' in runtime.router.commands.handle('6591234567', 'meeting briefs on')
    assert 'enabled' in runtime.router.commands.handle('6591234567', 'meeting briefs status')
    runtime.on_user_changed(1)
    assert 'paused' in runtime.router.commands.handle('6591234567', 'meeting briefs status')


def test_account_deletion_erases_proactive_tables(tmp_path):
    runtime = build_runtime(settings(tmp_path, 'true'), sender=FakeSender())
    from tests.test_proactive import NOW
    user, _ = runtime.users.start_signup('Friend', 'friend@example.com', 'sealed-token', NOW)
    conn = runtime.store.conn
    conn.execute('INSERT INTO proactive_goals(user_id,phone,enabled) VALUES(?,?,1)', (user.id, '6581112222'))
    conn.execute('INSERT INTO proactive_attempts VALUES(?,?,1)', (user.id, '2026-10-07'))
    conn.commit()
    runtime.users.delete(user.id)
    assert conn.execute('SELECT count(*) FROM proactive_goals WHERE user_id=?', (user.id,)).fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM proactive_attempts WHERE user_id=?', (user.id,)).fetchone()[0] == 0


def test_status_command_drains_eligible_queued_briefs(tmp_path):
    import asyncio
    from app.router import IncomingMessage
    sender = FakeSender()
    runtime = build_runtime(settings(tmp_path, 'true'), sender=sender)
    calls = []
    runtime.router.before_notices = calls.append
    asyncio.run(runtime.router.handle(IncomingMessage(sender='6591234567', text='meeting briefs status', message_id='brief-status')))
    assert calls == ['6591234567']


def test_text_and_voice_agenda_use_private_workflow_before_general_agent(tmp_path, monkeypatch):
    import asyncio
    from app.router import IncomingMessage
    sender = FakeSender()
    runtime = build_runtime(settings(tmp_path, 'true'), sender=sender)
    seen = []

    async def answer(phone, text):
        seen.append((phone, text))
        return 'Private daily timeline'

    async def unexpected(*args, **kwargs):
        raise AssertionError('General assistant should not run for agenda')

    runtime.proactive_briefs.answer = answer
    monkeypatch.setattr('app.wiring.run_agent', unexpected)
    runtime.router.transcribe_for_sender = lambda media, phone: 'agenda'
    asyncio.run(runtime.router.handle(IncomingMessage(sender='6591234567', text='agenda', message_id='agenda-text')))
    asyncio.run(runtime.router.handle(IncomingMessage(sender='6591234567', text=None, message_id='agenda-voice', audio_id='audio')))
    assert seen == [('6591234567', 'agenda'), ('6591234567', '[voice] agenda')]
    assert all('Private daily timeline' in body for _, body in sender.sent)


def test_private_reply_is_remembered_and_followup_gets_transient_context(tmp_path, monkeypatch):
    import asyncio
    runtime = build_runtime(settings(tmp_path, 'true'), sender=FakeSender())
    runtime.proactive_briefs.answer = _answer
    seen = []

    async def context(user):
        assert user.id == 1
        return 'UNTRUSTED: interview brief with two verified examples'

    async def run(agent, app, session, text, **kwargs):
        seen.append((app.meeting_preparation_context, await session.get_items()))
        return 'Here are the two examples.'

    runtime.proactive_briefs.conversation_context = context
    monkeypatch.setattr('app.wiring.run_agent', run)

    async def converse():
        assert await runtime.router.run_owner('meeting 1') == 'Your interview preparation'
        assert await runtime.router.run_owner('help me prepare those examples') == 'Here are the two examples.'

    asyncio.run(converse())
    assert 'two verified examples' in seen[0][0]
    assert seen[0][1][-1] == {'role': 'assistant', 'content': 'Your interview preparation'}
    assert runtime.app_ctx.meeting_preparation_context == ''


async def _answer(phone, text):
    return 'Your interview preparation' if text == 'meeting 1' else None


def test_explicit_booking_is_not_blocked_by_automatic_preparation_context(tmp_path, monkeypatch):
    import asyncio
    runtime = build_runtime(settings(tmp_path, 'true'), sender=FakeSender())
    runtime.proactive_briefs.answer = _answer

    async def unexpected(user):
        raise AssertionError('Booking should use its direct request, not injected preparation')

    async def run(agent, app, session, text, **kwargs):
        assert app.meeting_preparation_context == ''
        return 'Booking proposal'

    runtime.proactive_briefs.conversation_context = unexpected
    monkeypatch.setattr('app.wiring.run_agent', run)
    assert asyncio.run(runtime.router.run_owner('schedule these interviews')) == 'Booking proposal'
