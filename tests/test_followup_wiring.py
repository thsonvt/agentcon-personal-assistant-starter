import asyncio
from datetime import datetime, timedelta

from app.config import REQUIRED, load_settings
from app.router import IncomingMessage
from app.timeutil import SGT
from app.tools.whatsapp import WindowClosedError
from app.wiring import build_runtime
from tests.fakes import FakeSender

NOW = datetime(2026, 10, 9, 10, tzinfo=SGT)


def settings(tmp_path, enabled='true', briefs='false'):
    env = {key: 'x' for key in REQUIRED}
    env.update(OWNER_WHATSAPP='6591234567', DB_PATH=str(tmp_path / 'app.db'),
               FOLLOWUPS_ENABLED=enabled, PROACTIVE_BRIEFS_ENABLED=briefs,
               FOLLOWUP_REMINDER_TEMPLATE='followup_reminder')
    return load_settings(env)


def test_disabled_flag_preserves_jobs_and_no_extra_service(tmp_path):
    runtime = build_runtime(settings(tmp_path, 'false'), sender=FakeSender())
    assert runtime.followups is None
    assert len(runtime.jobs) == 1
    assert runtime.app_ctx.followups is None


def test_enabled_service_wires_owner_context_and_job(tmp_path):
    runtime = build_runtime(settings(tmp_path), sender=FakeSender())
    assert runtime.app_ctx.followups is runtime.followups
    assert len(runtime.jobs) == 2
    assert runtime.followups.template_name == 'followup_reminder'


def test_brief_and_followup_queues_share_hook(tmp_path):
    runtime = build_runtime(settings(tmp_path, briefs='true'), sender=FakeSender())
    calls = []
    runtime.proactive_briefs.deliver_queued = lambda phone: calls.append(('brief', phone))
    runtime.followups.deliver_queued = lambda phone: calls.append(('followup', phone))
    runtime.router.before_notices('6591234567')
    assert calls == [('brief', '6591234567'), ('followup', '6591234567')]


def test_account_deletion_erases_followups_and_late_tool_cannot_write(tmp_path):
    runtime = build_runtime(settings(tmp_path), sender=FakeSender())
    user, _ = runtime.users.start_signup('Friend', 'friend@example.com', 'sealed', NOW)
    runtime.store.conn.execute("UPDATE users SET status='active',phone='6581112222' WHERE id=?", (user.id,))
    runtime.store.conn.commit()
    runtime.followups.prepare(user.id, 'Check proposal', 'Private note', 'soon', None, '', True, 'one')
    runtime.users.delete(user.id)
    assert runtime.store.conn.execute('SELECT count(*) FROM followups WHERE user_id=?', (user.id,)).fetchone()[0] == 0
    import pytest
    with pytest.raises(ValueError):
        runtime.followups.prepare(user.id, 'Check proposal', 'Private note', 'soon', None, '', True, 'two')


def test_queued_reminder_drains_after_owner_command(tmp_path, monkeypatch):
    sender = FakeSender()
    monkeypatch.setattr('app.wiring.sgt_now', lambda: NOW)
    runtime = build_runtime(settings(tmp_path), sender=sender)
    runtime.followups.template_name = ''
    runtime.followups.prepare(1, 'Follow up with recruiter', 'MSD role', 'today at 11am',
                             (NOW + timedelta(hours=1)).isoformat(), '', False, 'one')
    runtime.followups.now = lambda: NOW + timedelta(hours=2)
    sender.exc = WindowClosedError('closed')
    runtime.followups.tick()
    sender.exc = None
    asyncio.run(runtime.router.handle(IncomingMessage(sender='6591234567', text='help', message_id='followup-help')))
    assert any('MSD role' in body for _, body in sender.sent)
    assert 'accepted' in runtime.followups.list(1)


def test_text_and_voice_share_tools_and_turn_confirmation(tmp_path, monkeypatch):
    from app import followup_tools as ft
    sender = FakeSender()
    monkeypatch.setattr('app.wiring.sgt_now', lambda: NOW)
    runtime = build_runtime(settings(tmp_path), sender=sender)
    seen = []

    async def run(agent, app, session, text, **kwargs):
        app.followup_turn_id = text
        app.current_input_is_voice = text.startswith('[voice]')
        seen.append({tool.name for tool in agent.tools})
        if text == 'yes':
            return ft.confirm_followup(app, 1)
        return ft.save_followup(app, 'Check with recruiter', 'MSD next week', 'next week', None)

    monkeypatch.setattr('app.wiring.run_agent', run)
    runtime.router.transcribe_for_sender = lambda media, phone: 'remind me next week'
    asyncio.run(runtime.router.handle(IncomingMessage(sender='6591234567', text=None, message_id='followup-voice', audio_id='audio')))
    assert 'draft' in runtime.followups.list(1)
    asyncio.run(runtime.router.handle(IncomingMessage(sender='6591234567', text='yes', message_id='followup-yes')))
    assert 'scheduled' in runtime.followups.list(1)
    assert all('save_followup' in names for names in seen)


def test_explicit_reminder_does_not_inject_unrelated_meeting_sources(tmp_path, monkeypatch):
    runtime = build_runtime(settings(tmp_path, briefs='true'), sender=FakeSender())

    async def unexpected(user):
        raise AssertionError('An explicit follow-up should not inject unrelated meeting evidence')

    async def run(agent, app, session, text):
        assert app.meeting_preparation_context == ''
        return 'Saved reminder'

    runtime.proactive_briefs.conversation_context = unexpected
    monkeypatch.setattr('app.wiring.run_agent', run)
    assert asyncio.run(runtime.router.run_owner('help me remind myself to contact the recruiter')) == 'Saved reminder'


def test_web_account_captures_without_phone_and_retains_private_service(tmp_path, monkeypatch):
    import json
    from dataclasses import replace
    from app import followup_tools as ft
    from app.users import CALENDAR_SCOPE
    from tests.test_multiuser_wiring import settings as signup_settings
    monkeypatch.setattr('app.wiring.sgt_now', lambda: NOW)
    monkeypatch.setattr('app.wiring.build_service', lambda *args: object())
    runtime = build_runtime(replace(signup_settings(tmp_path), followups_enabled=True), sender=FakeSender())
    user, _ = runtime.users.sign_in_identity('followup-web-user', 'Web User', 'web@example.com', NOW)
    runtime.users._update(user.id, NOW, status='approved',
        google_token_enc=runtime.box.encrypt('refresh'), google_scopes=json.dumps([CALENDAR_SCOPE]),
        calendar_status='connected')

    async def run(agent, app, session, text):
        assert app.followups is runtime.followups
        assert app.store.user_id == user.id
        assert {'save_followup', 'confirm_followup', 'list_followups', 'update_followup'} <= {t.name for t in agent.tools}
        assert app.whatsapp is None
        app.followup_turn_id = 'web-turn'
        return ft.save_followup(app, 'Check a proposal', 'Private web note', 'today at 11am',
                                (NOW + timedelta(hours=1)).isoformat())

    monkeypatch.setattr('app.wiring.run_agent', run)
    result = asyncio.run(runtime.web_assistant.run(runtime.users.get(user.id), 'remind me'))
    assert result.startswith('Saved:')
    assert 'Private web note' in runtime.followups.list(user.id)
    assert 'Private web note' not in runtime.followups.list(1)
