from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from app.store import Store
from app.users import Users
from app.tools.whatsapp import WindowClosedError
from tests.fakes import FakeSender

NOW = datetime(2026, 10, 9, 1, tzinfo=timezone.utc)


def setup(tmp_path=None, sender=None):
    from app.followups import FollowUps
    store = Store(str(tmp_path / 'followups.db') if tmp_path else ':memory:')
    users = Users(store)
    users.ensure_admin('Son', '6591234567', NOW)
    clock = [NOW]
    service = FollowUps(store, users, sender or FakeSender(), lambda: clock[0])
    return service, store, clock


def create(service, **kwargs):
    args = dict(user_id=1, action='Follow up with Rishi', context='MSD role after leave', timing_text='next week', due_at=(NOW + timedelta(days=3)).isoformat(), source='my note', needs_confirmation=False, run_no=1)
    args.update(kwargs)
    return service.prepare(**args)


def row(store):
    return store.conn.execute('SELECT * FROM followups ORDER BY id DESC').fetchone()


def test_explicit_reminder_is_durable_deduplicated_and_user_scoped(tmp_path):
    service, store, clock = setup(tmp_path)
    first = create(service)
    assert 'scheduled' in first.lower()
    assert create(service) == first
    assert store.conn.execute('SELECT COUNT(*) FROM followups').fetchone()[0] == 1
    create(service, action='Follow up with CEO')
    assert store.conn.execute('SELECT COUNT(*) FROM followups').fetchone()[0] == 2
    assert 'Rishi' in service.list(1)
    with pytest.raises(ValueError):
        service.list(99)
    with pytest.raises(ValueError):
        create(service, user_id=99)
    from app.followups import FollowUps
    restarted = FollowUps(store, Users(store), FakeSender(), lambda: clock[0])
    assert 'Rishi' in restarted.list(1)


def test_draft_needs_later_run_and_cannot_mutate_proposal():
    service, store, _ = setup()
    create(service, needs_confirmation=True)
    ident = row(store)['id']
    with pytest.raises(ValueError):
        service.confirm(1, ident, 1)
    with pytest.raises(ValueError):
        service.confirm(99, ident, 2)
    assert 'scheduled' in service.confirm(1, ident, 2).lower()
    assert row(store)['state'] == 'pending'
    create(service, due_at=None, needs_confirmation=True, action='CEO availability')
    assert 'unscheduled' in service.confirm(1, row(store)['id'], 2).lower()
    assert row(store)['state'] == 'draft'


@pytest.mark.parametrize('due', ['2026-10-12T09:00:00', NOW.isoformat(), 'junk'])
def test_invalid_or_past_due_never_saved(due):
    service, store, _ = setup()
    with pytest.raises(ValueError):
        create(service, due_at=due)
    assert store.conn.execute('SELECT COUNT(*) FROM followups').fetchone()[0] == 0


def test_due_send_once_done_cancel_and_snooze():
    sender = FakeSender()
    service, store, clock = setup(sender=sender)
    create(service)
    assert service.tick() == 0
    clock[0] += timedelta(days=4)
    assert service.tick() == 1
    assert service.tick() == 0
    assert len(sender.sent) == 1
    assert 'MSD role' in sender.sent[0][1] and 'snooze' in sender.sent[0][1]
    assert row(store)['delivery'] == 'accepted'
    ident = row(store)['id']
    with pytest.raises(ValueError):
        service.update(99, ident, 'done')
    service.update(1, ident, 'snooze', (clock[0] + timedelta(hours=1)).isoformat())
    assert row(store)['delivery'] == 'waiting'
    clock[0] += timedelta(hours=2)
    assert service.tick() == 1
    service.update(1, ident, 'done')
    assert row(store)['state'] == 'done'
    create(service, action='Cancel this', due_at=(clock[0] + timedelta(hours=1)).isoformat())
    service.update(1, row(store)['id'], 'cancel')
    clock[0] += timedelta(days=1)
    assert service.tick() == 0


def test_window_closed_queues_and_current_user_reply_delivers():
    sender = FakeSender(exc=WindowClosedError('closed'))
    service, store, clock = setup(sender=sender)
    create(service)
    clock[0] += timedelta(days=4)
    assert service.tick() == 0
    assert row(store)['delivery'] == 'queued'
    sender.exc = None
    assert service.deliver_queued('6588888888') == 0
    assert service.deliver_queued('6591234567') == 1
    assert service.deliver_queued('6591234567') == 0


def test_uncertain_send_and_interrupted_send_never_retry():
    service, store, clock = setup(sender=FakeSender(exc=RuntimeError('unknown outcome')))
    create(service)
    clock[0] += timedelta(days=4)
    assert service.tick() == 0
    assert row(store)['delivery'] == 'uncertain'
    assert service.tick() == 0
    store.conn.execute("UPDATE followups SET delivery='sending'")
    store.conn.commit()
    from app.followups import FollowUps
    restarted = FollowUps(store, Users(store), FakeSender(), lambda: clock[0])
    assert row(store)['delivery'] == 'uncertain'
    assert restarted.tick() == 0


@pytest.mark.parametrize('change', ["status='disabled',is_admin=0", 'event_only=1'])
def test_ineligible_user_not_sent(change):
    service, store, clock = setup()
    create(service)
    store.conn.execute(f'UPDATE users SET {change} WHERE id=1')
    store.conn.commit()
    clock[0] += timedelta(days=4)
    assert service.tick() == 0
    with pytest.raises(ValueError):
        create(service, action='New action')


def test_atomic_claim_and_updates_rejected_while_sending():
    entered, release = threading.Event(), threading.Event()
    class BlockingSender(FakeSender):
        def send_text(self, to, body):
            entered.set()
            assert release.wait(5)
            return super().send_text(to, body)
    sender = BlockingSender()
    service, store, clock = setup(sender=sender)
    create(service)
    ident = row(store)['id']
    clock[0] += timedelta(days=4)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sending = pool.submit(service.tick)
        assert entered.wait(5)
        assert pool.submit(service.tick).result() == 0
        with pytest.raises(ValueError):
            service.update(1, ident, 'done')
        release.set()
        assert sending.result() == 1
    assert len(sender.sent) == 1


def test_template_fallback_keeps_action_context_and_instructions_bounded():
    class TemplateSender(FakeSender):
        def send_template(self, to, name, params):
            self.template = (to, name, params)
            return 'template-accepted'
    sender = TemplateSender(exc=WindowClosedError('closed'))
    service, store, clock = setup(sender=sender)
    service.template_name = 'follow_up'
    create(service, action='Rishi ' + 'a' * 900, context='MSD role ' + 'c' * 1900)
    clock[0] += timedelta(days=4)
    assert service.tick() == 1
    assert row(store)['delivery'] == 'accepted'
    body = sender.template[2][0]
    assert len(body) <= 1000
    assert 'Rishi' in body and 'MSD role' in body and 'snooze' in body


def test_deleted_user_cannot_create_or_drain_queued_reminder():
    sender = FakeSender(exc=WindowClosedError('closed'))
    service, store, clock = setup(sender=sender)
    create(service)
    clock[0] += timedelta(days=4)
    service.tick()
    store.conn.execute('DELETE FROM users WHERE id=1')
    store.conn.commit()
    sender.exc = None
    assert service.deliver_queued('6591234567') == 0
    with pytest.raises(ValueError):
        create(service, due_at=(clock[0] + timedelta(days=1)).isoformat())


def test_different_timing_and_source_are_not_deduplicated():
    service, store, _ = setup()
    create(service)
    create(service, timing_text='Monday')
    create(service, source='email excerpt')
    assert store.conn.execute('SELECT COUNT(*) FROM followups').fetchone()[0] == 3


def test_cancelled_queue_is_never_drained():
    sender = FakeSender(exc=WindowClosedError('closed'))
    service, store, clock = setup(sender=sender)
    create(service)
    clock[0] += timedelta(days=4)
    service.tick()
    service.update(1, row(store)['id'], 'cancel')
    sender.exc = None
    assert service.deliver_queued('6591234567') == 0


def test_draft_proposal_cannot_be_snoozed_to_bypass_confirmation():
    service, store, _ = setup()
    create(service, needs_confirmation=True)
    with pytest.raises(ValueError):
        service.update(1, row(store)['id'], 'snooze', (NOW + timedelta(days=1)).isoformat())
    assert row(store)['state'] == 'draft'


def test_tool_outcome_prefixes_and_undated_draft_can_be_scheduled():
    service, store, _ = setup()
    assert create(service).startswith('Saved: ')
    assert create(service, action='Dated draft', needs_confirmation=True).startswith('CONFIRM: ')
    ident = row(store)['id']
    assert service.confirm(1, ident, 'later-turn').startswith('Saved: ')
    assert create(service, action='Undated draft', due_at=None).startswith('CLARIFY: ')
    ident = row(store)['id']
    assert service.confirm(1, ident, 'later-turn').startswith('CLARIFY: ')
    assert service.update(1, ident, 'snooze', (NOW + timedelta(days=1)).isoformat()).startswith('Updated: ')
    assert row(store)['state'] == 'pending'


def test_stale_disabled_and_deleted_tool_runs_cannot_read_or_update():
    service, store, _ = setup()
    create(service)
    ident = row(store)['id']
    store.conn.execute("UPDATE users SET status='disabled',is_admin=0 WHERE id=1")
    store.conn.commit()
    with pytest.raises(ValueError):
        service.list(1)
    with pytest.raises(ValueError):
        service.update(1, ident, 'done')
    store.conn.execute('DELETE FROM users WHERE id=1')
    store.conn.commit()
    with pytest.raises(ValueError):
        service.list(1)
    with pytest.raises(ValueError):
        service.update(1, ident, 'cancel')


def test_snooze_cannot_reopen_closed_followup():
    service, store, _ = setup()
    create(service)
    ident = row(store)['id']
    assert service.update(1, ident, 'done').startswith('Updated: ')
    with pytest.raises(ValueError):
        service.update(1, ident, 'snooze', (NOW + timedelta(days=1)).isoformat())


def test_tick_batch_is_bounded():
    service, store, clock = setup()
    for index in range(51):
        create(service, action=f'Action {index}')
    clock[0] += timedelta(days=4)
    assert service.tick() == 50
    assert service.tick() == 1


def test_scheduler_survives_tick_failure_without_logging_private_payload(monkeypatch, caplog):
    import asyncio
    service, _, _ = setup()
    attempts = []
    def tick():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError('private diary contents')
        raise asyncio.CancelledError
    async def no_sleep(seconds):
        assert seconds == 60
    monkeypatch.setattr(service, 'tick', tick)
    monkeypatch.setattr('app.followups.asyncio.sleep', no_sleep)
    async def exercise():
        with pytest.raises(asyncio.CancelledError):
            await service.run()
    asyncio.run(exercise())
    assert len(attempts) == 2
    assert 'private diary contents' not in caplog.text
    assert 'follow-up scheduler' in caplog.text.lower()


def test_queued_drain_batch_is_bounded():
    sender = FakeSender(exc=WindowClosedError('closed'))
    service, store, clock = setup(sender=sender)
    for index in range(51):
        create(service, action=f'Action {index}')
    clock[0] += timedelta(days=4)
    service.tick()
    service.tick()
    sender.exc = None
    assert service.deliver_queued('6591234567') == 50
    assert service.deliver_queued('6591234567') == 1


def test_repeat_prepare_and_confirm_of_closed_item_never_claim_saved():
    service, store, _ = setup()
    create(service)
    ident = row(store)['id']
    service.update(1, ident, 'done')
    assert create(service).startswith('Existing: ')
    assert service.confirm(1, ident, 'later-turn').startswith('Existing: ')


def test_approved_account_without_phone_can_capture_but_cannot_receive():
    service, store, clock = setup()
    store.conn.execute("UPDATE users SET status='approved',is_admin=0,phone=NULL WHERE id=1")
    store.conn.commit()
    assert 'connect WhatsApp' in create(service)
    assert 'connect WhatsApp' in service.list(1)
    ident = row(store)['id']
    service.update(1, ident, 'snooze', (NOW + timedelta(days=2)).isoformat())
    clock[0] += timedelta(days=4)
    assert service.tick() == 0
    assert row(store)['delivery'] == 'waiting'


def test_disabled_admin_cannot_capture_read_update_or_receive():
    service, store, clock = setup()
    create(service)
    ident = row(store)['id']
    store.conn.execute("UPDATE users SET status='disabled' WHERE id=1")
    store.conn.commit()
    with pytest.raises(ValueError):
        create(service, action='Another action')
    with pytest.raises(ValueError):
        service.list(1)
    with pytest.raises(ValueError):
        service.update(1, ident, 'done')
    clock[0] += timedelta(days=4)
    assert service.tick() == 0


def test_list_prioritizes_open_reminders_and_reports_bounded_history():
    service, store, _ = setup()
    create(service, action='Important pending reminder', context='c' * 2000)
    for index in range(50):
        create(service, action=f'Closed {index} ' + 'a' * 900, context='c' * 2000,
               timing_text='t' * 2000, source='s' * 2000)
        service.update(1, row(store)['id'], 'done')
    listing = service.list(1)
    assert 'Important pending reminder' in listing
    assert listing.count('Follow-up #') == 50
    assert 'open: 1' in listing and 'closed: 50' in listing
    assert 'first 50' in listing.lower()
    assert len(listing) < 60000
    assert 'a' * 301 not in listing and 'c' * 301 not in listing
    assert 't' * 151 not in listing and 's' * 151 not in listing
