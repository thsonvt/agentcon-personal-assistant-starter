import asyncio
from datetime import datetime, timedelta

from app.proactive import ProactiveBriefs
from app.store import Store
from app.timeutil import SGT
from app.tools.calendar import Event
from app.tools.whatsapp import WindowClosedError, WhatsAppError
from app.users import Users
from tests.fakes import FakeSender

NOW = datetime(2026, 10, 7, 10, tzinfo=SGT)
PHONE = '6591234567'


class Env:
    def __init__(self, path=':memory:', sender=None):
        self.store = Store(path)
        self.users = Users(self.store)
        self.users.ensure_admin('Son', PHONE, NOW)
        self.time = NOW
        self.events = [Event('event1', 'Project review', NOW + timedelta(minutes=30),
                             NOW + timedelta(minutes=60), False, description='Discuss launch')]
        self.generated = []
        self.sender = sender or FakeSender()
        self.calendar_reads = []
        self.fail = False

        async def generate(event):
            self.generated.append(event.id)
            if self.fail:
                raise ValueError('provider unavailable')
            return 'Prepare a launch update. Suggested question: what remains blocked?'

        def calendar(user_id):
            self.calendar_reads.append(user_id)
            return self

        self.briefs = ProactiveBriefs(self.store, self.users, calendar, generate,
                                     self.sender, lambda: self.time)

    def list_events(self, start, end, limit=25):
        return self.events

    def get_event(self, event_id):
        from app.tools.calendar import EventNotFoundError
        event = next((e for e in self.events if e.id == event_id), None)
        if event is None:
            raise EventNotFoundError('cancelled')
        return event

    def tick(self):
        asyncio.run(self.briefs.tick())


def test_explicit_opt_in_and_restart_dedup(tmp_path):
    env = Env(str(tmp_path / 'briefs.db'))
    env.tick()
    assert env.generated == []
    assert 'enabled' in env.briefs.command(PHONE, 'meeting briefs on')
    env.tick()
    assert len(env.sender.sent) == 1
    restarted = Env(str(tmp_path / 'briefs.db'))
    restarted.tick()
    assert restarted.generated == []
    assert 'enabled' in restarted.briefs.command(PHONE, 'meeting briefs status')


def test_unknown_and_event_only_cannot_opt_in():
    env = Env()
    assert 'active' in env.briefs.command('6580000000', 'meeting briefs on')
    env.users.conn.execute('UPDATE users SET event_only=1 WHERE id=1')
    env.users.conn.commit()
    assert 'active' in env.briefs.command(PHONE, 'meeting briefs on')
    assert env.briefs.command(PHONE, 'hello') is None


def test_skip_all_day_past_and_outside_window():
    env = Env()
    env.events = [Event('all', 'Holiday', NOW, NOW + timedelta(days=1), True),
                  Event('past', 'Past', NOW - timedelta(minutes=10), NOW, False),
                  Event('far', 'Future', NOW + timedelta(hours=2), NOW + timedelta(hours=3), False)]
    env.briefs.command(PHONE, 'meeting briefs on')
    env.tick()
    assert env.generated == []


def test_closed_window_queues_once_and_pause_removes_notice():
    env = Env(sender=FakeSender(exc=WindowClosedError('closed')))
    env.briefs.command(PHONE, 'meeting briefs on')
    env.tick()
    env.tick()
    assert len(env.generated) == 1
    assert env.store.conn.execute("SELECT count(*) FROM proactive_briefs WHERE state='queued'").fetchone()[0] == 1
    assert 'paused' in env.briefs.command(PHONE, 'meeting briefs off')
    assert env.store.take_notices(PHONE) == []


def test_revoked_permission_clears_queue_and_stops_calendar_reads():
    env = Env(sender=FakeSender(exc=WindowClosedError('closed')))
    env.briefs.command(PHONE, 'meeting briefs on')
    env.tick()
    reads_before_revoke = list(env.calendar_reads)
    env.users.conn.execute('UPDATE users SET google_disconnected=1 WHERE id=1')
    env.users.conn.commit()
    env.tick()
    assert env.calendar_reads == reads_before_revoke
    assert env.store.take_notices(PHONE) == []


def test_expired_queued_brief_is_removed():
    env = Env(sender=FakeSender(exc=WindowClosedError('closed')))
    env.briefs.command(PHONE, 'meeting briefs on')
    env.tick()
    env.time += timedelta(hours=1)
    env.tick()
    assert env.store.take_notices(PHONE) == []


def test_uncertain_delivery_is_not_retried_or_regenerated():
    env = Env(sender=FakeSender(exc=WhatsAppError('timeout')))
    env.briefs.command(PHONE, 'meeting briefs on')
    env.tick()
    env.tick()
    assert len(env.generated) == 1
    assert 'uncertain' in env.briefs.command(PHONE, 'meeting briefs status')


def test_generation_cooldown_and_three_attempt_limit():
    env = Env()
    env.events = [Event('e', 'Review', NOW + timedelta(minutes=59), NOW + timedelta(hours=2), False)]
    env.fail = True
    env.briefs.command(PHONE, 'meeting briefs on')
    env.tick()
    env.tick()
    assert len(env.generated) == 1
    for _ in range(3):
        env.time += timedelta(minutes=15)
        env.tick()
    assert len(env.generated) == 3


def test_pause_during_generation_prevents_delivery():
    env = Env()
    env.briefs.command(PHONE, 'meeting briefs on')

    async def generate(event):
        env.briefs.command(PHONE, 'meeting briefs off')
        return 'Do not deliver'

    env.briefs.generate = generate
    env.tick()
    assert env.sender.sent == []


def test_daily_budget_and_per_user_isolation():
    env = Env()
    env.events = [Event(str(i), 'Review', NOW + timedelta(minutes=30), NOW + timedelta(hours=1), False)
                  for i in range(8)]
    env.briefs.command(PHONE, 'meeting briefs on')
    env.tick()
    env.tick()
    assert len(env.generated) == 5
    assert all(phone == PHONE for phone, _ in env.sender.sent)


def test_delete_removes_feature_data():
    env = Env(sender=FakeSender(exc=WindowClosedError('closed')))
    env.briefs.command(PHONE, 'meeting briefs on')
    env.tick()
    env.briefs.remove(1)
    assert env.store.take_notices(PHONE) == []
    assert env.store.conn.execute('SELECT count(*) FROM proactive_goals').fetchone()[0] == 0
    assert env.store.conn.execute('SELECT count(*) FROM proactive_briefs').fetchone()[0] == 0


def test_queue_delivery_revalidates_calendar_and_records_sent():
    env = Env(sender=FakeSender(exc=WindowClosedError('closed')))
    env.briefs.command(PHONE, 'meeting briefs on')
    env.tick()
    env.briefs.sender = FakeSender()
    env.briefs.deliver_queued(PHONE)
    assert len(env.briefs.sender.sent) == 1
    assert env.store.take_notices(PHONE) == []
    assert 'sent: 1' in env.briefs.command(PHONE, 'meeting briefs status')


def test_cancelled_queue_never_delivers_even_before_next_tick():
    env = Env(sender=FakeSender(exc=WindowClosedError('closed')))
    env.briefs.command(PHONE, 'meeting briefs on')
    env.tick()
    env.events = []
    env.briefs.sender = FakeSender()
    env.briefs.deliver_queued(PHONE)
    assert env.briefs.sender.sent == []
    assert env.store.take_notices(PHONE) == []


def test_cancellation_during_generation_does_not_deliver():
    env = Env()
    env.briefs.command(PHONE, 'meeting briefs on')

    async def generate(event):
        env.events = []
        return 'Old brief'

    env.briefs.generate = generate
    env.tick()
    assert env.sender.sent == []


def test_new_goal_revision_is_not_paused_by_old_tick():
    env = Env()
    env.events += [Event('second', 'Another meeting', NOW + timedelta(minutes=45), NOW + timedelta(hours=1), False)]
    env.briefs.command(PHONE, 'meeting briefs on')

    async def generate(event):
        env.briefs.command(PHONE, 'meeting briefs on')
        return 'Prepared'

    env.briefs.generate = generate
    env.tick()
    assert 'enabled' in env.briefs.command(PHONE, 'meeting briefs status')


def test_admin_with_explicitly_missing_calendar_permission_cannot_opt_in():
    env = Env()
    env.users.conn.execute("UPDATE users SET google_scopes='[]' WHERE id=1")
    env.users.conn.commit()
    assert 'active' in env.briefs.command(PHONE, 'meeting briefs on')


def test_queued_uncertain_send_is_not_retried():
    env = Env(sender=FakeSender(exc=WindowClosedError('closed')))
    env.briefs.command(PHONE, 'meeting briefs on')
    env.tick()
    env.briefs.sender = FakeSender(exc=WhatsAppError('timeout'))
    env.briefs.deliver_queued(PHONE)
    env.briefs.sender = FakeSender()
    env.briefs.deliver_queued(PHONE)
    assert env.briefs.sender.sent == []
    assert 'uncertain: 1' in env.briefs.command(PHONE, 'meeting briefs status')


def test_two_users_have_separate_goals_calendars_and_receipts():
    from app.users import CALENDAR_SCOPE
    env = Env()
    friend, _ = env.users.start_signup('Friend', 'friend@example.com', 'sealed', NOW)
    env.users.conn.execute('''UPDATE users SET status='active',phone=?,typed_phone=?,joined=1,
        google_scopes=?,calendar_status='connected' WHERE id=?''',
        ('6581112222', '6581112222', '["' + CALENDAR_SCOPE + '"]', friend.id))
    env.users.conn.commit()
    env.briefs.command('6581112222', 'meeting briefs on')
    env.tick()
    assert set(env.calendar_reads) == {friend.id}
    assert all(phone == '6581112222' for phone, _ in env.sender.sent)
    assert 'no briefs yet' in env.briefs.command(PHONE, 'meeting briefs status')
    env.briefs.command(PHONE, 'meeting briefs on')
    env.tick()
    assert len(env.sender.sent) == 2
    assert {phone for phone, _ in env.sender.sent} == {PHONE, '6581112222'}


def test_interrupted_generation_recovers_after_lease(tmp_path):
    env = Env(str(tmp_path / 'app.db'))
    env.briefs.command(PHONE, 'meeting briefs on')
    event = env.events[0]
    key = (1, event.id, event.start.isoformat())
    assert env.briefs._claim(key, NOW) == ''
    restarted = Env(str(tmp_path / 'app.db'))
    restarted.tick()
    assert restarted.generated == []
    restarted.time += timedelta(minutes=5)
    restarted.tick()
    assert restarted.generated == ['event1']


def test_expired_queue_cleans_up_even_when_calendar_unavailable():
    env = Env(sender=FakeSender(exc=WindowClosedError('closed')))
    env.briefs.command(PHONE, 'meeting briefs on')
    env.tick()
    env.time += timedelta(hours=1)

    def unavailable(*args):
        raise ValueError('calendar unavailable')

    env.briefs.calendar_for_user = unavailable
    env.tick()
    assert 'queued:' not in env.briefs.command(PHONE, 'meeting briefs status')


def test_changed_agenda_does_not_return_old_preparation_or_repeat_notification():
    from dataclasses import replace
    env = Env()
    env.briefs.command(PHONE, 'meeting briefs on')
    env.tick()
    env.events = [replace(env.events[0], title='New meeting purpose', description='Discuss financial budget')]

    async def updated(event):
        return 'Prepare the financial budget.'

    env.briefs.generate = updated
    reply = asyncio.run(env.briefs.answer(PHONE, 'meeting 1'))
    assert 'New meeting purpose' in reply
    assert 'financial budget' in reply
    assert 'launch update' not in reply
    env.tick()
    assert len(env.sender.sent) == 1
