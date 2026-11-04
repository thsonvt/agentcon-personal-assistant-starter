"""Acceptance checks for private, scheduled agendas and numbered meeting details."""
import asyncio
from datetime import timedelta

from app.tools.calendar import Event
from app.tools.whatsapp import WindowClosedError
from tests.fakes import FakeSender
from tests.test_proactive import Env, NOW, PHONE


def at(hour, minute=0):
    return NOW.replace(hour=hour, minute=minute)


def meeting(id, title, hour, minute=0, duration=30):
    start = at(hour, minute)
    return Event(id, title, start, start + timedelta(minutes=duration), False,
                 description='Discuss launch')


def enabled(hour=8, sender=None, path=':memory:'):
    env = Env(path, sender)
    env.time = at(hour)
    env.events = [meeting('first', 'Project review', 10),
                  meeting('second', 'Training discussion', 15),
                  meeting('third', 'Planning session', 18)]
    env.briefs.command(PHONE, 'meeting briefs on')
    return env


def answer(env, text, phone=PHONE):
    return asyncio.run(env.briefs.answer(phone, text))


def messages(env):
    return '\n'.join(body for _, body in env.sender.sent)


def digest_rows(env):
    return env.store.conn.execute(
        "SELECT * FROM proactive_briefs WHERE event_id LIKE '@agenda:%'").fetchall()


def test_morning_agenda_once_even_after_restart(tmp_path):
    path = str(tmp_path / 'agenda.db')
    env = enabled(path=path)
    env.tick()
    assert len(env.sender.sent) == 1
    assert all(event.title in messages(env) for event in env.events)
    assert '3' in messages(env)
    env.time = at(8, 30)
    env.tick()
    assert len(env.sender.sent) == 1
    restarted = Env(path)
    restarted.time = at(8, 45)
    restarted.events = env.events
    restarted.tick()
    assert restarted.sender.sent == []
    assert len(digest_rows(env)) == 1


def test_schedule_does_not_replay_missed_windows():
    env = enabled(hour=7)
    env.tick()
    env.time = at(9)
    env.tick()
    env.time = at(14)
    env.tick()
    assert digest_rows(env) == []


def test_afternoon_shows_ongoing_and_future_with_full_day_count():
    env = enabled(hour=13)
    env.events = [meeting('past', 'Finished review', 10),
                  meeting('ongoing', 'Current discussion', 12, 45, 60),
                  meeting('later', 'Evening planning', 18)]
    env.tick()
    body = messages(env)
    assert 'Finished review' not in body
    assert 'Current discussion' in body and 'Evening planning' in body
    assert '3' in body
    env.tick()
    assert len(digest_rows(env)) == 1


def test_new_local_day_gets_a_new_morning_digest():
    env = enabled()
    env.tick()
    env.time += timedelta(days=1)
    env.events = [Event(e.id, e.title, e.start + timedelta(days=1),
                        e.end + timedelta(days=1), False) for e in env.events]
    env.tick()
    assert len(digest_rows(env)) == 2
    assert len(env.sender.sent) == 2


def test_zero_meetings_and_all_day_events_are_not_timed_meetings():
    env = enabled()
    env.events = [Event('holiday', 'Public holiday', at(0), at(0) + timedelta(days=1), True)]
    body = answer(env, 'agenda')
    assert body and ('no meetings' in body.lower() or '0 meetings' in body.lower())
    assert 'meeting 1' not in body.lower()


def test_voice_agenda_reads_calendar_fresh_and_unrelated_text_is_unhandled():
    env = enabled(hour=10)
    assert answer(env, 'hello') is None
    first = answer(env, '[voice] today agenda')
    assert 'Project review' in first
    env.events = [meeting('new', 'Fresh calendar change', 16)]
    second = answer(env, 'agenda')
    assert 'Fresh calendar change' in second and 'Project review' not in second
    assert len(env.calendar_reads) >= 2


def test_numbered_details_survive_calendar_reorder_and_new_meeting():
    env = enabled()
    answer(env, 'agenda')
    env.events = [meeting('new', 'Inserted early meeting', 9), *reversed(env.events)]
    body = answer(env, 'meeting 2')
    assert 'Training discussion' in body
    assert 'Inserted early meeting' not in body
    assert answer(env, 'details 2')


def test_numbered_details_do_not_return_cancelled_or_rescheduled_content():
    env = enabled()
    answer(env, 'agenda')
    env.events = [env.events[0], env.events[2]]
    assert 'Training discussion' not in answer(env, 'meeting 2')
    env.events[0] = meeting('first', 'Project review', 11)
    assert 'Discuss launch' not in answer(env, 'meeting 1')


def test_stranger_paused_and_event_only_users_do_not_read_calendar():
    env = enabled()
    assert 'Project review' not in (answer(env, 'agenda', '6580000000') or '')
    env.briefs.command(PHONE, 'meeting briefs off')
    assert 'Project review' not in (answer(env, 'agenda') or '')
    env.briefs.command(PHONE, 'meeting briefs on')
    env.users.conn.execute('UPDATE users SET event_only=1 WHERE id=1')
    env.users.conn.commit()
    assert 'Project review' not in (answer(env, 'agenda') or '')
    assert env.calendar_reads == []
    # A second real account gets only its own calendar and numbered snapshot.
    from types import SimpleNamespace
    from app.users import CALENDAR_SCOPE
    friend, _ = env.users.start_signup('Friend', 'friend@example.com', 'sealed', NOW)
    friend_phone = '6581112222'
    env.users.conn.execute('''UPDATE users SET status='active',phone=?,typed_phone=?,joined=1,
        google_scopes=?,calendar_status='connected' WHERE id=?''',
        (friend_phone, friend_phone, '["' + CALENDAR_SCOPE + '"]', friend.id))
    env.users.conn.commit()
    friend_events = [meeting('private', 'Friend private meeting', 16)]
    env.briefs.calendar_for_user = lambda user_id: SimpleNamespace(
        list_events=lambda *args: friend_events if user_id == friend.id else env.events)
    env.briefs.command(friend_phone, 'meeting briefs on')
    body = answer(env, 'agenda', friend_phone)
    assert 'Friend private meeting' in body and 'Project review' not in body
    assert 'Friend private meeting' in answer(env, 'meeting 1', friend_phone)
    assert 'Friend private meeting' not in (answer(env, 'meeting 1') or '')


def test_revoked_permission_blocks_existing_numbered_snapshot():
    env = enabled()
    answer(env, 'agenda')
    reads = len(env.calendar_reads)
    env.users.conn.execute('UPDATE users SET google_disconnected=1 WHERE id=1')
    env.users.conn.commit()
    assert 'Project review' not in (answer(env, 'meeting 1') or '')
    assert len(env.calendar_reads) == reads


def test_short_reminder_keeps_full_detail_for_followup():
    env = enabled(hour=10)
    env.events = [meeting('first', 'Project review', 10, 30)]

    async def generate(event):
        return 'Prepare launch figures. ' * 70

    env.briefs.generate = generate
    env.tick()
    assert len(messages(env)) < 650
    assert 'meeting 1' in messages(env).lower()
    detail = answer(env, 'meeting 1')
    assert detail.count('Prepare launch figures.') > 1


def test_queued_morning_agenda_revalidates_calendar_and_expires_at_midday():
    env = enabled(sender=FakeSender(exc=WindowClosedError('closed')))
    env.tick()
    env.events = [meeting('fresh', 'Changed meeting', 11)]
    env.time = at(8, 30)
    env.briefs.sender = FakeSender()
    env.briefs.deliver_queued(PHONE)
    assert 'Changed meeting' in '\n'.join(b for _, b in env.briefs.sender.sent)
    assert 'Project review' not in '\n'.join(b for _, b in env.briefs.sender.sent)
    late = enabled(sender=FakeSender(exc=WindowClosedError('closed')))
    late.tick()
    late.time = at(13)
    late.briefs.sender = FakeSender()
    late.briefs.deliver_queued(PHONE)
    assert late.briefs.sender.sent == []


def test_pause_and_delete_remove_private_agenda_snapshots():
    env = enabled(sender=FakeSender(exc=WindowClosedError('closed')))
    env.tick()
    assert env.store.conn.execute('SELECT count(*) FROM proactive_agendas').fetchone()[0] == 1
    env.briefs.pause(1)
    assert env.store.conn.execute('SELECT count(*) FROM proactive_agendas').fetchone()[0] == 0
    assert not any(row['body'] for row in digest_rows(env))
    env.briefs.command(PHONE, 'meeting briefs on')
    answer(env, 'agenda')
    env.briefs.remove(1)
    assert env.store.conn.execute('SELECT count(*) FROM proactive_agendas').fetchone()[0] == 0


def test_combined_model_budget_preserves_fallback_agenda():
    env = enabled(hour=10)
    calls = []

    async def generate(event):
        calls.append(event.id)
        return 'Review the calendar agenda.'

    async def generate_agenda(events):
        calls.append('agenda')
        return ['Review the calendar agenda.' for _ in events]

    generate.agenda = generate_agenda
    env.briefs.generate = generate
    env.events = [meeting(str(i), 'Review ' + str(i), 10, 30) for i in range(8)]
    env.tick()
    body = answer(env, 'agenda')
    assert len(calls) <= 5
    assert 'Review 0' in body and body
    assert len(calls) == 5
    env.time = at(13)
    env.events.append(meeting('later', 'Afternoon meeting', 15))
    env.tick()
    assert len(calls) == 5
    assert any('Afternoon meeting' in row['body'] for row in digest_rows(env))


def test_missing_or_failed_agenda_generator_has_honest_source_fallback():
    env = enabled()
    body = answer(env, 'upcoming meetings')
    assert 'Project review' in body
    assert 'calendar' in body.lower()
    assert 'Discuss launch' in body or 'agenda' in body.lower()

    async def failing(events):
        raise ValueError('provider unavailable')

    env.briefs.generate.agenda = failing
    body = answer(env, 'agenda')
    assert 'Project review' in body and 'calendar' in body.lower()


def test_large_agenda_limits_display_and_does_not_print_unsafe_source_url():
    env = enabled()
    env.events = [Event(str(i), 'Session ' + str(i), at(9) + timedelta(minutes=30*i),
                        at(9) + timedelta(minutes=30*i+20), False,
                        source_url='https://attacker.example/private') for i in range(10)]
    body = answer(env, 'agenda')
    assert '10' in body
    assert sum(('Session ' + str(i)) in body for i in range(10)) <= 8
    assert 'attacker.example' not in body
