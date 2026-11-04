import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from app.capture_store import CaptureStore
from app.store import Store


@pytest.fixture
def captures(tmp_path):
    now = [datetime(2026, 10, 3, tzinfo=timezone.utc)]
    store = Store(str(tmp_path / 'capture.sqlite'))
    capture = CaptureStore(store, lambda: now[0])
    yield capture, store, now
    capture.conn.close()
    store.conn.close()


def test_restart_isolation_and_one_collecting_session(captures):
    cap, store, now = captures
    session = cap.start(1, '6590000001', 'Talk')
    assert cap.start(1, '6590000001', 'Replacement')['title'] == 'Talk'
    assert cap.get(2, session['id']) is None
    assert cap.current(2) is None
    other = CaptureStore(store, lambda: now[0])
    assert other.get(1, session['id']) == session
    cap.finish(1, session['id'])
    assert cap.current(1) is None
    assert cap.start(1, '6590000001', 'Next')['id'] != session['id']
    other.conn.close()


def test_public_fields_and_receipt_order(captures):
    cap, _, _ = captures
    session = cap.start(1, 'phone', 'Talk')
    assert set(session) == set('id user_id phone title state created_at closed_at expires_at summary_json error link_version'.split())
    first = cap.add_item(1, session['id'], 'm1', 'text', text='First', source_timestamp='200')
    second = cap.add_item(1, session['id'], 'm2', 'image', media_id='photo', source_timestamp='100')
    assert set(first) == set('id user_id session_id message_id kind original_text media_id mime_type media extracted_text category status error received_at source_timestamp included'.split())
    assert [r['id'] for r in cap.items(1, session['id'])] == [first['id'], second['id']]
    assert cap.item(2, first['id']) is None
    assert cap.items(2, session['id']) == []


def test_duplicate_items_and_global_receipts(captures):
    cap, _, _ = captures
    session = cap.start(1, 'phone', 'Talk')
    item = cap.add_item(1, session['id'], 'm1', 'text', text='First')
    assert cap.add_item(1, session['id'], 'm1', 'text', text='Replacement') == item
    assert cap.mark_seen(1, 'command') is True
    assert cap.mark_seen(2, 'command') is False
    assert cap.seen('command') is True
    assert not cap.seen('unknown')
    second = cap.start(2, 'other', 'Talk')
    with pytest.raises(ValueError):
        cap.add_item(2, second['id'], 'm1', 'text', text='Cannot steal')


def test_transaction_rolls_back_receipt_item_finish_and_notification(captures):
    cap, store, now = captures
    session = cap.start(1, 'phone', 'Talk')
    with pytest.raises(RuntimeError):
        with cap.transaction():
            cap.mark_seen(1, 'finish-command')
            cap.add_item(1, session['id'], 'item', 'text', text='Note')
            cap.finish(1, session['id'])
            cap.notify(1, 'phone', 'Receipt')
            raise RuntimeError('Crash before commit')
    other = CaptureStore(store, lambda: now[0])
    assert not other.seen('finish-command')
    assert other.items(1, session['id']) == []
    assert other.current(1)['id'] == session['id']
    assert other.pending_notifications() == []
    other.conn.close()


def test_independent_connections_do_not_commit_store_changes(captures):
    cap, store, _ = captures
    with cap.transaction():
        cap.start(1, 'phone', 'Talk')
        store.conn.commit()
        assert cap.conn.in_transaction


def test_nested_failure_can_be_caught_without_losing_outer_transaction(captures):
    cap, _, _ = captures
    with cap.transaction():
        session = cap.start(1, 'phone', 'Talk')
        with pytest.raises(ValueError):
            with cap.transaction():
                cap.mark_seen(1, 'rolled-back')
                cap.add_item(1, session['id'], 'too-long', 'text', text='x' * 10001)
        cap.mark_seen(1, 'committed')
    assert cap.get(1, session['id'])
    assert cap.seen('committed')
    assert not cap.seen('rolled-back')


def test_commit_failure_rolls_back_outer_transaction(captures):
    cap, _, _ = captures
    with pytest.raises(sqlite3.IntegrityError):
        with cap.transaction():
            cap.conn.execute('PRAGMA defer_foreign_keys = ON')
            cap.mark_seen(1, 'must-rollback')
            cap.conn.execute('INSERT INTO capture_items (user_id, session_id, message_id, kind, received_at) '
                             "VALUES (1, 'missing-session', 'orphan', 'text', '2026-10-03')")
    assert not cap.conn.in_transaction
    assert not cap.seen('must-rollback')


def test_concurrent_connections_keep_single_active_session(captures):
    cap, store, now = captures
    other = CaptureStore(store, lambda: now[0])
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(cap.start, 1, 'phone', 'First')
        two = pool.submit(other.start, 1, 'phone', 'Second')
        assert one.result()['id'] == two.result()['id']
    other.conn.close()


def test_capture_uses_store_shared_memory_without_leaking_to_other_stores():
    now = lambda: datetime(2026, 10, 3, tzinfo=timezone.utc)
    store, separate = Store(':memory:'), Store(':memory:')
    cap, peer, isolated = CaptureStore(store, now), CaptureStore(store, now), CaptureStore(separate, now)
    try:
        session = cap.start(1, 'phone', 'Talk')
        assert peer.get(1, session['id']) == session
        assert isolated.get(1, session['id']) is None
    finally:
        for connection in (cap.conn, peer.conn, isolated.conn, store.conn, separate.conn):
            connection.close()


def test_finish_waits_for_pending_and_rejects_late_intake(captures):
    cap, _, _ = captures
    session = cap.start(1, 'phone', 'Talk')
    item = cap.add_item(1, session['id'], 'm1', 'text', text='Note')
    cap.finish(1, session['id'])
    assert cap.closing_sessions()[0]['id'] == session['id']
    with pytest.raises(ValueError, match='pending'):
        cap.save_summary(1, session['id'], {'takeaways': []})
    with pytest.raises(ValueError, match='closed'):
        cap.add_item(1, session['id'], 'late', 'text', text='Too late')
    cap.complete_item(1, item['id'], 'Note', 'observation')
    summary = {'takeaways': [{'text': 'Note', 'source_ids': [item['id']]}]}
    cap.save_summary(1, session['id'], summary)
    assert cap.get(1, session['id'])['state'] == 'ready'
    assert json.loads(cap.get(1, session['id'])['summary_json']) == summary
    assert cap.pending_items() == []


def test_corrections_invalidate_summary_and_are_user_scoped(captures):
    cap, _, _ = captures
    session = cap.start(1, 'phone', 'Talk')
    item = cap.add_item(1, session['id'], 'm1', 'text', text='Note')
    cap.complete_item(1, item['id'], 'Note', 'observation')
    cap.finish(1, session['id'])
    cap.save_summary(1, session['id'], {})
    assert cap.set_included(2, session['id'], item['id'], False) is None
    assert cap.get(1, session['id'])['state'] == 'ready'
    cap.set_included(1, session['id'], item['id'], False)
    assert cap.get(1, session['id'])['summary_json'] is None
    assert cap.get(1, session['id'])['state'] == 'closing'
    assert cap.item(1, item['id'])['included'] is False


def test_pending_correction_survives_provider_completion(captures):
    cap, _, _ = captures
    session = cap.start(1, 'phone', 'Talk')
    item = cap.add_item(1, session['id'], 'm1', 'text', text='Note')
    cap.set_included(1, session['id'], item['id'], False)
    cap.complete_item(1, item['id'], 'Note', 'observation', included=True)
    assert cap.item(1, item['id'])['included'] is False


def test_slide_extraction_preserves_distinct_source_category(captures):
    cap, _, _ = captures
    session = cap.start(1, 'phone', 'Talk')
    item = cap.add_item(1, session['id'], 'slide', 'image', text='My caption', media_id='photo')
    cap.complete_item(1, item['id'], 'Claim shown on slide', 'slide')
    saved = cap.item(1, item['id'])
    assert saved['status'] == 'ready'
    assert saved['category'] == 'slide'
    assert saved['original_text'] == 'My caption'
    assert saved['extracted_text'] == 'Claim shown on slide'


def test_failure_retry_preserves_media(captures):
    cap, _, _ = captures
    session = cap.start(1, 'phone', 'Talk')
    item = cap.add_item(1, session['id'], 'm1', 'image', media_id='photo')
    cap.save_media(1, item['id'], b'photo', 'image/jpeg')
    cap.fail_item(1, item['id'], 'Could not extract')
    cap.finish(1, session['id'])
    cap.fail_summary(1, session['id'], 'Could not summarize')
    assert cap.get(1, session['id'])['state'] == 'failed'
    cap.retry(1, session['id'])
    assert cap.get(1, session['id'])['state'] == 'closing'
    assert cap.item(1, item['id'])['status'] == 'pending'
    assert cap.item(1, item['id'])['media'] == b'photo'


def test_deleted_rows_cannot_be_resurrected_by_worker(captures):
    cap, _, _ = captures
    session = cap.start(1, 'phone', 'Talk')
    item = cap.add_item(1, session['id'], 'm1', 'image', media_id='photo')
    assert cap.delete(2, session['id']) is False
    assert cap.delete(1, session['id']) is True
    assert cap.save_media(1, item['id'], b'photo', 'image/jpeg') is None
    assert cap.complete_item(1, item['id'], 'Late', 'observation') is None
    assert cap.fail_item(1, item['id'], 'Late') is None
    assert cap.save_summary(1, session['id'], {}) is None
    assert cap.fail_summary(1, session['id'], 'Late') is None
    assert cap.get(1, session['id']) is None


def test_text_item_and_session_limits(captures):
    cap, _, _ = captures
    session = cap.start(1, 'phone', 'Talk')
    with pytest.raises(ValueError, match='10,000'):
        cap.add_item(1, session['id'], 'oversize', 'text', text='x' * 10001)
    cap.add_item(1, session['id'], 'boundary', 'text', text='x' * 10000)
    for i in range(99):
        cap.add_item(1, session['id'], f'm{i}', 'text', text='Note')
    with pytest.raises(ValueError, match='100'):
        cap.add_item(1, session['id'], 'overcount', 'text', text='Note')
    cap.finish(1, session['id'])
    for i in range(19):
        cap.finish(1, cap.start(1, 'phone', f'Talk {i}')['id'])
    with pytest.raises(ValueError, match='20'):
        cap.start(1, 'phone', 'Too many')


@pytest.mark.parametrize('kind,mime,limit', [('image', 'image/jpeg', 5), ('audio', 'audio/ogg', 16)])
def test_media_size_limits(captures, kind, mime, limit):
    cap, _, _ = captures
    session = cap.start(1, 'phone', 'Talk')
    item = cap.add_item(1, session['id'], 'm1', kind, media_id='media')
    with pytest.raises(ValueError, match=f'{limit} MiB'):
        cap.save_media(1, item['id'], b'x' * (limit * 1024 * 1024 + 1), mime)
    assert cap.item(1, item['id'])['media'] is None


def test_total_media_limit_and_replacement_accounting(captures):
    cap, _, _ = captures
    session = cap.start(1, 'phone', 'Talk')
    for i in range(4):
        item = cap.add_item(1, session['id'], f'm{i}', 'audio', media_id='media')
        cap.save_media(1, item['id'], b'x' * (16 * 1024 * 1024), 'audio/ogg')
    cap.save_media(1, item['id'], b'x' * (16 * 1024 * 1024), 'audio/ogg')
    extra = cap.add_item(1, session['id'], 'extra', 'audio', media_id='media')
    with pytest.raises(ValueError, match='64 MiB'):
        cap.save_media(1, extra['id'], b'x', 'audio/ogg')


def test_expiry_removes_content_receipts_and_notifications(captures):
    cap, _, now = captures
    session = cap.start(1, 'phone', 'Talk')
    cap.add_item(1, session['id'], 'm1', 'text', text='Note')
    cap.mark_seen(1, 'm1')
    cap.notify(1, 'phone', 'Receipt')
    now[0] += timedelta(days=7)
    assert cap.get(1, session['id']) is None
    assert cap.pending_items() == []
    cap.expire()
    assert cap.conn.execute('SELECT count(*) FROM capture_items').fetchone()[0] == 0
    assert cap.conn.execute('SELECT count(*) FROM capture_sessions').fetchone()[0] == 0
    assert not cap.seen('m1')
    assert cap.pending_notifications() == []


def test_outbox_never_retries_uncertain_or_failed_sends(captures):
    cap, store, now = captures
    note = cap.notify(1, 'phone', 'Receipt')
    assert set(note) == set('id user_id phone body state created_at session_id'.split())
    assert cap.pending_notifications() == [note]
    cap.notification_state(note['id'], 'sending')
    restarted = CaptureStore(store, lambda: now[0])
    assert restarted.pending_notifications() == []
    cap.notification_state(note['id'], 'failed')
    assert cap.pending_notifications() == []
    with pytest.raises(ValueError):
        cap.notification_state(note['id'], 'pending')
    second = cap.notify(1, 'phone', 'New receipt')
    cap.notification_state(second['id'], 'sending')
    cap.notification_state(second['id'], 'sent')
    assert cap.pending_notifications() == []
    restarted.conn.close()


def test_delete_removes_session_notifications_in_every_delivery_state(captures):
    cap, _, _ = captures
    session = cap.start(1, 'phone', 'Talk')
    unrelated = cap.notify(1, 'phone', 'Generic receipt')
    for state in ('pending', 'sending', 'sent', 'failed'):
        note = cap.notify(1, 'phone', 'Private notes', session_id=session['id'])
        if state != 'pending':
            cap.notification_state(note['id'], 'sending')
        if state in ('sent', 'failed'):
            cap.notification_state(note['id'], state)
    cap.delete(2, session['id'])
    assert cap.conn.execute('SELECT count(*) FROM capture_notifications').fetchone()[0] == 5
    cap.delete(1, session['id'])
    assert [dict(row) for row in cap.conn.execute('SELECT * FROM capture_notifications')] == [unrelated]
    assert cap.notify(1, 'phone', 'Late summary', session_id=session['id']) is None


def test_expiry_removes_recent_notifications_linked_to_expired_session(captures):
    cap, _, now = captures
    session = cap.start(1, 'phone', 'Talk')
    now[0] += timedelta(days=6)
    cap.notify(1, 'phone', 'Private notes', session_id=session['id'])
    now[0] += timedelta(days=1)
    cap.expire()
    assert cap.conn.execute('SELECT count(*) FROM capture_notifications').fetchone()[0] == 0


def test_old_notification_schema_migrates_without_losing_outbox(captures):
    cap, store, now = captures
    cap.conn.executescript("""
        DROP TABLE capture_notifications;
        CREATE TABLE capture_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            phone TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
        );
        INSERT INTO capture_notifications (user_id, phone, body, created_at)
        VALUES (1, 'phone', 'Old receipt', '2026-10-03T00:00:00+00:00');
    """)
    migrated = CaptureStore(store, lambda: now[0])
    try:
        assert migrated.pending_notifications()[0]['session_id'] is None
        assert migrated.pending_notifications()[0]['body'] == 'Old receipt'
    finally:
        migrated.conn.close()


def test_pending_batch_is_bounded_metadata_and_original_media_remains(captures):
    cap, _, _ = captures
    originals = []
    for user_id in (1, 2):
        session = cap.start(user_id, 'phone', 'Talk')
        for index in range(60):
            item = cap.add_item(user_id, session['id'], f'{user_id}-{index}', 'image', media_id='photo')
            cap.save_media(user_id, item['id'], b'original-photo', 'image/jpeg')
            originals.append(item)
    batch = cap.pending_items()
    assert len(batch) == 100
    assert [item['id'] for item in batch] == [item['id'] for item in originals[:100]]
    assert all(item['media'] is None for item in batch)
    assert cap.item(1, originals[0]['id'])['media'] == b'original-photo'
    for item in batch:
        cap.complete_item(item['user_id'], item['id'], 'Slide text', 'slide')
    assert len(cap.pending_items()) == 20


def test_items_can_read_metadata_without_loading_original_media(captures):
    cap, _, _ = captures
    session = cap.start(1, 'phone', 'Talk')
    item = cap.add_item(1, session['id'], 'photo', 'image', media_id='photo')
    cap.save_media(1, item['id'], b'original-photo', 'image/jpeg')
    cap.add_item(1, session['id'], 'text', 'text', text='An observation')
    metadata = cap.items(1, session['id'], include_media=False)
    assert metadata[0]['media'] is None
    assert metadata[0]['has_media'] is True
    assert metadata[1]['media'] is None
    assert metadata[1]['has_media'] is False
    assert cap.items(1, session['id'])[0]['media'] == b'original-photo'


def test_pending_batch_filters_eligible_users_before_limit(captures):
    cap, _, _ = captures
    disabled = cap.start(1, 'disabled-phone', 'Old queue')
    for index in range(100):
        cap.add_item(1, disabled['id'], f'disabled-{index}', 'text', text='Old note')
    active = cap.start(2, 'active-phone', 'New queue')
    item = cap.add_item(2, active['id'], 'active-image', 'image', media_id='photo')
    cap.save_media(2, item['id'], b'original-photo', 'image/jpeg')
    eligible = cap.pending_items(user_ids=[2])
    assert [row['id'] for row in eligible] == [item['id']]
    assert eligible[0]['media'] is None
    assert cap.pending_items(user_ids=[]) == []
    assert len(cap.pending_items()) == 100
