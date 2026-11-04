from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.store import Store
from app.users import Users

NOW = datetime(2026, 10, 10, 2, 0, tzinfo=timezone.utc)


@pytest.fixture
def setup(request, tmp_path):
    from app.event_store import EventStore
    path = str(tmp_path / 'events.sqlite') if getattr(request, 'param', 'memory') == 'file' else ':memory:'
    users = Users(Store(path), max_users=200)
    # Same shared handle schema as PublicBooking, no provider/network required.
    users.conn.executescript('''CREATE TABLE public_booking_profiles (
        user_id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        display_name TEXT NOT NULL, title TEXT NOT NULL, timezone TEXT NOT NULL,
        location TEXT NOT NULL, work_start TEXT NOT NULL, work_end TEXT NOT NULL,
        weekdays TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0);''')
    clock = [NOW]
    event = EventStore(users, SimpleNamespace(_ready=lambda uid: False), lambda: clock[0], 'https://example.com')
    admin, _ = users.sign_in_identity('admin', 'Admin', 'admin@example.com', NOW)
    users.conn.execute('UPDATE users SET is_admin=1 WHERE id=?', (admin.id,))
    users.conn.commit()
    event.create_event('ai-sg', 'AI Singapore', NOW.isoformat())
    def member(n, consent=True, visibility='event'):
        user, _ = users.sign_in_identity(f'sub{n}', f'Builder {n}', f'person{n}@example.com', NOW)
        event.import_roster('ai-sg', [dict(source_id=str(n), name=user.name, email=user.email)], admin.id)
        event.check_in('ai-sg', str(n), f'check{n}', admin.id)
        event.claim('ai-sg', user)
        event.update_profile('ai-sg', user.id, user.name, 'Agent evaluations', ['coffee'], visibility, consent)
        return user
    return event, users, admin, member, clock


def test_roster_import_is_private_and_receipts_are_idempotent(setup):
    e, users, admin, _, _ = setup
    rows = [dict(source_id='a', name='Alice', email=' ALICE@example.com ')]
    result = e.import_roster('ai-sg', rows, admin.id)
    assert result['imported'] == 1
    assert not e.eligible_for_claim('ai-sg', 'alice@example.com')
    for _ in range(10):
        e.check_in('ai-sg', 'a', 'receipt', admin.id)
    assert e.eligible_for_claim('ai-sg', 'ALICE@example.com')
    assert e.summary('ai-sg', admin.id)['checked_in'] == 1
    with pytest.raises(ValueError):
        e.check_in('ai-sg', 'missing', 'receipt', admin.id)
    assert users.conn.execute('SELECT count(*) FROM event_memberships').fetchone()[0] == 1


def test_claim_exact_identity_and_import_does_not_overwrite_claim(setup):
    e, users, admin, member, _ = setup
    alice = member(1)
    assert e.claim('ai-sg', alice)['user_id'] == alice.id
    assert not e.eligible_for_claim('ai-sg', 'unknown@example.com')
    stranger, _ = users.sign_in_identity('other', 'Other', 'other@example.com', NOW)
    with pytest.raises(ValueError):
        e.claim('ai-sg', stranger)
    result = e.import_roster('ai-sg', [dict(source_id='1', name='Replacement', email='other@example.com')], admin.id)
    assert result['rejected']
    assert e.membership('ai-sg', alice.id)['email'] == alice.email
    users._update(alice.id, NOW, status='disabled')
    with pytest.raises(ValueError):
        e.claim('ai-sg', alice)


def test_private_discovery_and_hidden_profiles(setup):
    e, users, admin, member, _ = setup
    a, b, hidden = member(1), member(2), member(3, False, 'hidden')
    entries = e.directory('ai-sg', a.id)
    assert {p['user_id'] for p in entries} == {a.id, b.id}
    assert all('email' not in p and 'phone' not in p for p in entries)
    assert e.directory('ai-sg', a.id, 'EVALUATIONS')
    with pytest.raises(ValueError):
        e.directory('ai-sg', None)
    with pytest.raises(ValueError):
        e.profile(b.id)
    with pytest.raises(ValueError):
        e.profile(hidden.id, a.id)
    e.update_profile('ai-sg', b.id, b.name, visibility='public')
    assert e.profile(b.id)['stable_url'].endswith(f'/people/{b.id}')
    e.block('ai-sg', a.id, b.id)
    assert b.id not in {p['user_id'] for p in e.directory('ai-sg', a.id)}
    with pytest.raises(ValueError):
        e.profile(b.id, a.id)


def test_handle_namespace_preserves_booking_settings(setup):
    e, users, _, member, _ = setup
    a, b = member(1), member(2)
    assert e.profile(a.id, a.id)['username'] != e.profile(b.id, b.id)['username']
    users.conn.execute("UPDATE public_booking_profiles SET enabled=1,location='saved',timezone='UTC' WHERE user_id=?", (a.id,))
    users.conn.commit()
    e.update_profile('ai-sg', a.id, 'Changed', visibility='event', directory_opt_in=True)
    row = users.conn.execute('SELECT * FROM public_booking_profiles WHERE user_id=?', (a.id,)).fetchone()
    assert row['enabled'] == 1 and row['location'] == 'saved' and row['timezone'] == 'UTC'


def test_followup_idempotency_authorization_blocks_and_daily_limit(setup):
    e, _, _, member, _ = setup
    a, b, c = member(1), member(2), member(3)
    r = e.send_followup('ai-sg', a.id, b.id, 'coffee', 'Great demo', 'one')
    assert e.send_followup('ai-sg', a.id, b.id, 'coffee', 'Great demo', 'one')['id'] == r['id']
    assert e.send_followup('ai-sg', a.id, b.id, 'coffee', 'Great demo', 'two')['id'] == r['id']
    with pytest.raises(ValueError):
        e.send_followup('ai-sg', a.id, b.id, 'work', 'Different', 'one')
    assert e.send_followup('ai-sg', c.id, b.id, 'coffee', '', 'three')['id'] != r['id']
    with pytest.raises(ValueError):
        e.get_followup(r['id'], c.id)
    with pytest.raises(ValueError):
        e.respond(r['id'], a.id, 'accept')
    assert e.respond(r['id'], b.id, 'accept')['state'] == 'accepted'
    for n in range(4, 8):
        target = member(n)
        e.send_followup('ai-sg', a.id, target.id, 'coffee', '', str(n))
    target = member(8)
    with pytest.raises(ValueError, match='five'):
        e.send_followup('ai-sg', a.id, target.id, 'coffee', '', 'limit')
    e.block('ai-sg', b.id, c.id)
    with pytest.raises(ValueError):
        e.send_followup('ai-sg', c.id, b.id, 'coffee', '', 'blocked')


def test_time_agreement_requires_other_party_and_current_version(setup):
    e, _, admin, member, _ = setup
    a, b, c = member(1), member(2), member(3)
    r = e.send_followup('ai-sg', a.id, b.id, 'technical', '', 'r')
    r = e.respond(r['id'], b.id, 'accept')
    slot = (NOW + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError):
        e.propose_times(r['id'], c.id, [slot], 'Asia/Singapore', 'Cafe', r['version'])
    with pytest.raises(ValueError):
        e.propose_times(r['id'], a.id, ['2026-10-11T12:00:00'], 'Asia/Singapore', 'Cafe', r['version'])
    r = e.propose_times(r['id'], a.id, [slot], 'Asia/Singapore', 'Cafe', r['version'])
    with pytest.raises(ValueError):
        e.confirm_time(r['id'], a.id, 0, r['version'])
    with pytest.raises(ValueError):
        e.confirm_time(r['id'], b.id, 0, r['version'] - 1)
    r = e.confirm_time(r['id'], b.id, 0, r['version'])
    assert r['state'] == 'time_agreed'
    assert r['selected_time'] == slot
    counts = e.summary('ai-sg', admin.id)
    assert counts['accepted'] == counts['time_agreed'] == counts['sent'] == 1
    assert counts['calendar_event_created'] is None
    assert counts['provider_attribution'] == 'not_attributed'


def test_expiry_removal_deletion_and_repeated_migrations(setup):
    from app.event_store import EventStore
    e, users, admin, member, clock = setup
    a, b = member(1), member(2)
    r = e.send_followup('ai-sg', a.id, b.id, 'coffee', '', 'r')
    e.remove_member('ai-sg', '2', admin.id)
    with pytest.raises(ValueError):
        e.directory('ai-sg', b.id)
    assert e.get_followup(r['id'], a.id)['state'] == 'expired'
    e.import_roster('ai-sg', [dict(source_id='stub', name='Stub', email='stub@example.com')], admin.id)
    clock[0] += timedelta(days=15)
    assert e.eligible_for_claim('ai-sg', a.email)
    assert e.cleanup(admin.id)['unclaimed_deleted'] == 1
    clock[0] += timedelta(days=16)
    assert e.directory('ai-sg', a.id) == []
    users.delete(b.id)
    assert not users.conn.execute('SELECT 1 FROM event_followups WHERE id=?', (r['id'],)).fetchone()
    EventStore(users, e.public_booking, e.now, e.base_url)
    assert e.membership('ai-sg', a.id)


def test_profile_events_are_visible_only_to_shared_members(setup):
    e, _, _, member, _ = setup
    a, b = member(1), member(2)
    e.update_profile('ai-sg', b.id, b.name, visibility='public', directory_opt_in=True)
    assert e.profile(b.id)['events'] == []
    assert e.profile(b.id, a.id)['events'] == [{'slug': 'ai-sg', 'title': 'AI Singapore'}]


def test_username_rename_reserves_old_handle_and_invalid_change_rolls_back(setup):
    e, users, _, member, _ = setup
    a, b = member(1), member(2)
    old = e.profile(a.id, a.id)['username']
    stable = e.profile(a.id, a.id)['stable_url']
    e.update_profile('ai-sg', a.id, a.name, visibility='event', directory_opt_in=True, username='renamed-builder')
    assert e.profile_by_username('RENAMED-BUILDER', a.id)['stable_url'] == stable
    with pytest.raises(ValueError):
        e.update_profile('ai-sg', b.id, b.name, username=old)
    with pytest.raises(Exception):
        users.conn.execute('UPDATE public_booking_profiles SET username=? WHERE user_id=?', (old, b.id))
    users.conn.rollback()
    for invalid in ('events', 'event-admin', 'inbox', 'people', 'ab', 'Email@example.com'):
        with pytest.raises(ValueError):
            e.update_profile('ai-sg', a.id, 'Should not save', username=invalid)
    assert e.profile(a.id, a.id)['display_name'] == a.name
    with pytest.raises(ValueError):
        e.update_profile('ai-sg', a.id, 'Hidden', visibility='hidden', directory_opt_in=True)
    assert e.membership('ai-sg', a.id)['directory_opt_in'] == 1


def test_cross_event_access_and_removed_mutations_are_rejected(setup):
    e, users, admin, member, _ = setup
    a, b = member(1), member(2)
    e.create_event('other', 'Other', NOW.isoformat())
    outsider, _ = users.sign_in_identity('outsider', 'Outside', 'outside@example.com', NOW)
    e.import_roster('other', [dict(source_id='o', name='Outside', email=outsider.email)], admin.id)
    e.check_in('other', 'o', 'o', admin.id)
    e.claim('other', outsider)
    with pytest.raises(ValueError):
        e.directory('ai-sg', outsider.id)
    with pytest.raises(ValueError):
        e.profile(b.id, outsider.id)
    with pytest.raises(ValueError):
        e.send_followup('ai-sg', outsider.id, b.id, 'coffee', '', 'o')
    r = e.send_followup('ai-sg', a.id, b.id, 'coffee', '', 'r')
    e.remove_member('ai-sg', '2', admin.id)
    with pytest.raises(ValueError):
        e.respond(r['id'], b.id, 'accept')
    with pytest.raises(ValueError):
        e.profile(b.id)
    with pytest.raises(ValueError):
        e.update_profile('ai-sg', b.id, 'Removed')
    users._update(a.id, NOW, status='disabled')
    with pytest.raises(ValueError):
        e.inbox(a.id)


def test_decline_stops_repeat_requests_and_daily_limit_resets(setup):
    e, _, _, member, clock = setup
    a, b = member(1), member(2)
    r = e.send_followup('ai-sg', a.id, b.id, 'coffee', '', 'r')
    e.respond(r['id'], b.id, 'decline')
    with pytest.raises(ValueError):
        e.send_followup('ai-sg', a.id, b.id, 'coffee', '', 'again')
    for n in range(3, 7):
        target = member(n)
        e.send_followup('ai-sg', a.id, target.id, 'coffee', '', str(n))
    c = member(7)
    with pytest.raises(ValueError, match='five'):
        e.send_followup('ai-sg', a.id, c.id, 'coffee', '', 'later')
    clock[0] += timedelta(days=1)
    assert e.send_followup('ai-sg', a.id, c.id, 'coffee', '', 'later')['state'] == 'pending'


def test_slots_normalize_sort_and_validate_optimistic_updates(setup):
    e, _, _, member, _ = setup
    a, b = member(1), member(2)
    r = e.send_followup('ai-sg', a.id, b.id, 'coffee', '', 'r')
    r = e.respond(r['id'], b.id, 'accept')
    invalid_options = [[], ['yesterday'], [NOW.isoformat()], [
        {'start': '2026-10-11T12:00:00+08:00', 'end': '2026-10-11T10:00:00+08:00'}]]
    for options in invalid_options:
        with pytest.raises(ValueError):
            e.propose_times(r['id'], a.id, options, 'Asia/Singapore', 'Cafe', r['version'])
        assert e.get_followup(r['id'], a.id)['version'] == r['version']
    with pytest.raises(ValueError):
        e.propose_times(r['id'], a.id, ['2026-10-11T12:00:00Z'], 'invalid/timezone', '', r['version'])
    r = e.propose_times(r['id'], a.id, ['2026-10-11T06:00:00Z', '2026-10-11T12:00:00+08:00'], 'Asia/Singapore', '', r['version'])
    assert [s['start'] for s in r['slots']] == ['2026-10-11T04:00:00+00:00', '2026-10-11T06:00:00+00:00']
    with pytest.raises(ValueError):
        e.confirm_time(r['id'], b.id, -1, r['version'])
    with pytest.raises(ValueError):
        e.confirm_time(r['id'], b.id, 2, r['version'])
    r = e.confirm_time(r['id'], b.id, 0, r['version'])
    with pytest.raises(ValueError):
        e.propose_times(r['id'], a.id, ['2026-10-11T12:00:00Z'], 'UTC', '', r['version'])


def test_capacity_replay_import_row_rejections_and_organizer_authorization(setup):
    e, users, admin, member, _ = setup
    a = member(1)
    e.create_event('tiny', 'Tiny', NOW.isoformat(), capacity=1)
    rows = [dict(source_id='first', name='A', email='a@example.com'), dict(source_id='second', name='B', email='b@example.com')]
    r = e.import_roster('tiny', rows, admin.id)
    assert r['imported'] == 1 and r['rejected'] == [{'row': 2, 'reason': 'Event capacity reached.'}]
    assert len(e.organizer_roster('tiny', admin.id)) == 1
    for action in (lambda: e.import_roster('tiny', [], a.id), lambda: e.check_in('tiny', 'first', 'x', a.id),
                   lambda: e.summary('tiny', a.id), lambda: e.organizer_roster('tiny', a.id),
                   lambda: e.remove_member('tiny', 'first', a.id), lambda: e.cleanup(a.id)):
        with pytest.raises(ValueError):
            action()
    assert e.organizer_roster('tiny', admin.id)[0]['checked_in_at'] is None
    result = e.import_roster('ai-sg', [dict(source_id='dup', name='Dup', email=a.email), {'source_id': 'bad'}], admin.id)
    assert len(result['rejected']) == 2
    assert e.membership('ai-sg', a.id)['source_id'] == '1'


def test_concurrent_claim_and_request_retries_remain_single_rows(setup):
    from concurrent.futures import ThreadPoolExecutor
    e, users, admin, member, _ = setup
    a, b = member(1), member(2)
    with ThreadPoolExecutor(max_workers=5) as pool:
        claimed = list(pool.map(lambda _: e.claim('ai-sg', a), range(10)))
    assert len({m['id'] for m in claimed}) == 1
    with ThreadPoolExecutor(max_workers=5) as pool:
        requests = list(pool.map(lambda _: e.send_followup('ai-sg', a.id, b.id, 'coffee', 'Hi', 'same'), range(10)))
    assert len({r['id'] for r in requests}) == 1
    assert e.summary('ai-sg', admin.id)['sent'] == 1


def test_failed_mutation_rolls_back_handle_and_profile(setup, monkeypatch):
    e, users, _, member, _ = setup
    a = member(1)
    old = e.profile(a.id, a.id)
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic failure after writes')
    monkeypatch.setattr(e, '_audit', fail)
    with pytest.raises(RuntimeError):
        e.update_profile('ai-sg', a.id, 'Should rollback', visibility='public', username='rollback-new')
    assert e.profile(a.id, a.id)['username'] == old['username']
    assert e.profile(a.id, a.id)['display_name'] == old['display_name']
    assert e.membership('ai-sg', a.id)['directory_opt_in'] == 1


def test_atomic_import_rolls_back_all_rows_on_any_rejection(setup):
    e, _, admin, _, _ = setup
    with pytest.raises(ValueError, match='row 2'):
        e.import_roster('ai-sg', [dict(source_id='valid', name='Valid', email='valid@example.com'), {'name': 'Bad'}], admin.id, atomic=True)
    assert e.organizer_roster('ai-sg', admin.id) == []


def test_atomic_capacity_import_is_serialized_across_threads(setup):
    from concurrent.futures import ThreadPoolExecutor
    e, _, admin, _, _ = setup
    e.create_event('capacity', 'Capacity', NOW.isoformat(), capacity=1)
    def attempt(index):
        try:
            e.import_roster('capacity', [dict(source_id=str(index), name=f'A{index}', email=f'a{index}@example.com')], admin.id, atomic=True)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(attempt, range(5)))
    assert results.count(True) == 1
    assert len(e.organizer_roster('capacity', admin.id)) == 1


def test_acceptance_history_survives_withdrawal_and_retention(setup):
    e, _, admin, member, clock = setup
    a, b = member(1), member(2)
    r = e.send_followup('ai-sg', a.id, b.id, 'coffee', '', 'r')
    with pytest.raises(ValueError):
        e.propose_times(r['id'], a.id, ['2026-10-11T12:00:00Z'], 'UTC', '', r['version'])
    r = e.respond(r['id'], b.id, 'accept')
    r = e.respond(r['id'], a.id, 'withdraw', r['version'])
    assert r['state'] == 'withdrawn'
    clock[0] += timedelta(days=31)
    e.cleanup(admin.id)
    summary = e.summary('ai-sg', admin.id)
    assert summary['claimed'] == 2 and summary['accepted'] == 1 and summary['sent'] == 1
    assert e.membership('ai-sg', a.id) and e.membership('ai-sg', b.id)


def test_username_suggestion_is_private_nonmutating_and_avoids_email(setup):
    e, users, _, member, _ = setup
    a = member(1)
    before = users.conn.execute('SELECT COUNT(*) FROM public_booking_profiles').fetchone()[0]
    suggestion = e.suggest_username(999, 'Builder 1')
    assert suggestion != e.profile(a.id, a.id)['username']
    assert e.suggest_username(999, 'person@example.com') == 'builder'
    assert users.conn.execute('SELECT COUNT(*) FROM public_booking_profiles').fetchone()[0] == before


@pytest.mark.parametrize('days', [15, 31])
def test_claimed_members_keep_identity_access_after_new_claim_expiry(setup, days):
    e, users, admin, member, clock = setup
    a, removed, disabled = member(1), member(2), member(3)
    e.import_roster('ai-sg', [dict(source_id='unclaimed', name='Unclaimed', email='unclaimed@example.com')], admin.id)
    e.check_in('ai-sg', 'unclaimed', 'u', admin.id)
    e.remove_member('ai-sg', '2', admin.id)
    users._update(disabled.id, NOW, status='disabled')
    clock[0] += timedelta(days=days)
    assert e.eligible_for_claim('ai-sg', a.email)
    assert e.claim('ai-sg', a)['user_id'] == a.id
    assert not e.eligible_for_claim('ai-sg', 'unclaimed@example.com')
    assert not e.eligible_for_claim('ai-sg', removed.email)
    assert not e.eligible_for_claim('ai-sg', disabled.email)
    if days == 31:
        assert e.directory('ai-sg', a.id) == []


def test_metrics_distinguish_unattributed_provider_results_current_activation_and_return_use(setup):
    import json
    from app.users import CALENDAR_SCOPE
    e, users, admin, member, clock = setup
    a, b, c, d = [member(n) for n in range(1, 5)]
    users._update(a.id, NOW, google_token_enc='encrypted', google_scopes=json.dumps([CALENDAR_SCOPE]), calendar_status='connected')
    users._update(b.id, NOW, status='active', joined=1, phone='6581234567', typed_phone='6581234567')
    e.send_followup('ai-sg', a.id, b.id, 'coffee', '', 'first')
    e.send_followup('ai-sg', a.id, c.id, 'coffee', '', 'same-day')
    assert e.summary('ai-sg', admin.id)['repeat_senders'] == 0
    clock[0] += timedelta(days=1)
    e.send_followup('ai-sg', a.id, d.id, 'coffee', '', 'later-day')
    users._update(c.id, clock[0], status='disabled', google_token_enc='encrypted', google_scopes=json.dumps([CALENDAR_SCOPE]), calendar_status='connected')
    e.remove_member('ai-sg', '4', admin.id)
    e.import_roster('ai-sg', [dict(source_id='stub', name='Stub', email='stub@example.com')], admin.id)
    result = e.summary('ai-sg', admin.id)
    assert result['roster'] == 5 and result['checked_in'] == result['claimed'] == 4
    assert result['current_participants'] == 2
    assert result['calendar_connected'] == result['assistant_active'] == result['repeat_senders'] == 1
    assert result['sent'] == 3 and result['unique_senders'] == 1
    assert result['calendar_event_created'] is None
    assert result['provider_attribution'] == 'not_attributed'
    assert 'UTC' in result['repeat_use_definition']
    assert 'attributed' in result['calendar_event_created_note']


def test_followup_names_use_chosen_profiles_and_private_fallback(setup):
    e, users, admin, member, _ = setup
    a, b = member(1), member(2)
    users._update(a.id, NOW, name='private-email-name@example.com')
    e.update_profile('ai-sg', a.id, 'Chosen Alias', visibility='event', directory_opt_in=True)
    e.update_profile('ai-sg', b.id, 'Demo Builder', visibility='event', directory_opt_in=True)
    result = e.send_followup('ai-sg', a.id, b.id, 'coffee', '', 'name')
    assert result['sender_name'] == 'Chosen Alias' and result['recipient_name'] == 'Demo Builder'
    # A claimant may send a request before writing any sharing profile.
    c, _ = users.sign_in_identity('profile-less', 'EmailLocalPart', 'private-person@example.com', NOW)
    e.import_roster('ai-sg', [dict(source_id='c', name='Private Registration Name', email=c.email)], admin.id)
    e.check_in('ai-sg', 'c', 'c', admin.id)
    e.claim('ai-sg', c)
    result = e.send_followup('ai-sg', c.id, b.id, 'coffee', '', 'without-profile')
    assert result['sender_name'] == 'Event participant'
    assert c.email not in str(result) and c.name not in str(result)


def test_atomic_reimport_cannot_transfer_checkin_to_replacement_email(setup):
    e, _, admin, _, _ = setup
    e.import_roster('ai-sg', [dict(source_id='attendee', name='Original', email='original@example.com')], admin.id)
    e.check_in('ai-sg', 'attendee', 'check-original', admin.id)
    with pytest.raises(ValueError, match='checked-in'):
        e.import_roster('ai-sg', [dict(source_id='attendee', name='Replacement', email='replacement@example.com')], admin.id, atomic=True)
    assert e.eligible_for_claim('ai-sg', 'original@example.com')
    assert not e.eligible_for_claim('ai-sg', 'replacement@example.com')
    assert e.organizer_roster('ai-sg', admin.id)[0]['name'] == 'Original'


@pytest.mark.parametrize('invalid_version', [None, '1', True, -1])
def test_time_mutations_require_an_explicit_integer_version(setup, invalid_version):
    e, _, _, member, _ = setup
    a, b = member(1), member(2)
    request = e.send_followup('ai-sg', a.id, b.id, 'coffee', '', 'r')
    request = e.respond(request['id'], b.id, 'accept')
    with pytest.raises(ValueError):
        e.propose_times(request['id'], a.id, ['2026-10-11T12:00:00Z'], 'UTC', '', invalid_version)
    request = e.propose_times(request['id'], a.id, ['2026-10-11T12:00:00Z'], 'UTC', '', request['version'])
    with pytest.raises(ValueError):
        e.confirm_time(request['id'], b.id, 0, invalid_version)
    assert e.get_followup(request['id'], a.id)['state'] == 'time_proposed'


@pytest.mark.parametrize('setup', ['memory', 'file'], indirect=True)
def test_oauth_commit_cannot_publish_failed_profile_transaction(setup, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, wait
    from threading import Event
    from starlette.requests import Request
    from app.connections import Connections

    events, users, _, member, _ = setup
    person = member(1, False, 'hidden')
    oauth = SimpleNamespace(connection_auth_url=lambda *args: 'https://accounts.example.com/authorize')
    connections = Connections(oauth, users, None, SimpleNamespace(), '', lambda: NOW)
    request = Request({'type': 'http', 'method': 'GET', 'path': '/signin/google',
                       'headers': [], 'client': ('127.0.0.1', 123),
                       'scheme': 'https', 'server': ('testserver', 443), 'query_string': b''})
    paused, release, reader_started = Event(), Event(), Event()

    def failed_audit(*args):
        paused.set()
        assert release.wait(3), 'test writer was never released'
        raise RuntimeError('synthetic publication failure')

    def read_public():
        reader_started.set()
        return events.profile(person.id)

    monkeypatch.setattr(events, '_audit', failed_audit)
    with ThreadPoolExecutor(max_workers=3) as pool:
        publication = pool.submit(events.update_profile, 'ai-sg', person.id, 'Temporary Public Name',
                                  visibility='public', directory_opt_in=True)
        assert paused.wait(2)
        sign_in = pool.submit(connections._start, request, 'identity')
        reader = pool.submit(read_public)
        assert reader_started.wait(2)
        try:
            early, _ = wait([reader, sign_in], timeout=0.1)
        finally:
            release.set()
        with pytest.raises(RuntimeError, match='publication failure'):
            publication.result(timeout=3)
        assert sign_in.result(timeout=3).status_code == 302
        with pytest.raises(ValueError, match='Profile not found'):
            reader.result(timeout=3)
    assert not early, 'OAuth commits and public readers must wait until the event mutation rolls back'
    assert events.profile(person.id, person.id)['visibility'] == 'hidden'
    assert events.profile(person.id, person.id)['display_name'] == person.name
    assert users.conn.execute('SELECT count(*) FROM connection_oauth').fetchone()[0] == 1
    assert events.conn is not users.conn


def test_store_connections_share_committed_state_but_memory_databases_are_isolated():
    first, second = Store(':memory:'), Store(':memory:')
    clone = first.for_user(7)
    contact = first.add_contact('First', '+6581234567')
    independent = first.new_connection()
    assert independent is not first.conn
    assert clone.conn is first.conn
    assert clone.new_connection().execute('SELECT name FROM contacts WHERE id=?', (contact.id,)).fetchone()[0] == 'First'
    assert independent.execute('SELECT name FROM contacts').fetchone()[0] == 'First'
    assert second.conn.execute('SELECT count(*) FROM contacts').fetchone()[0] == 0
    independent.close()


def test_delete_event_only_account_revokes_auth_and_networking_data(setup):
    from app.connections import Connections
    e, users, admin, member, _ = setup
    a, _ = users.sign_in_event_identity('event-only-delete', 'Attendee', 'delete@example.com', NOW)
    e.import_roster('ai-sg', [dict(source_id='delete', name=a.name, email=a.email)], admin.id)
    e.check_in('ai-sg', 'delete', 'delete-check', admin.id)
    e.claim('ai-sg', a)
    e.update_profile('ai-sg', a.id, a.name, visibility='event', directory_opt_in=True)
    b, c = member(2), member(3)
    request = e.send_followup('ai-sg', a.id, b.id, 'coffee', '', 'delete-request')
    e.block('ai-sg', a.id, c.id)
    Connections(None, users, None, SimpleNamespace(), '', lambda: NOW)
    users.conn.execute('INSERT INTO connection_sessions VALUES(?,?,?,?)', ('deleted-session', a.id, 'csrf', '2027-01-01T00:00:00+00:00'))
    users.conn.execute('INSERT INTO connection_sessions VALUES(?,?,?,?)', ('keep-session', b.id, 'csrf', '2027-01-01T00:00:00+00:00'))
    users.conn.execute('INSERT INTO connection_oauth VALUES(?,?,?,?,?,?,?,?)', ('state', 'browser', 'verifier', 'identity', 'deleted-session', a.id, '2027-01-01T00:00:00+00:00', 'ai-sg'))
    users.conn.commit()
    assert e.delete_networking_data(a.id) == {'account_deleted': True}
    assert users.get(a.id) is None and users.get(b.id) is not None
    assert e.membership('ai-sg', a.id) is None
    assert not e.conn.execute('SELECT 1 FROM event_profiles WHERE user_id=?', (a.id,)).fetchone()
    assert not e.conn.execute('SELECT 1 FROM public_booking_profiles WHERE user_id=?', (a.id,)).fetchone()
    assert not e.conn.execute('SELECT 1 FROM event_followups WHERE id=?', (request['id'],)).fetchone()
    assert not e.conn.execute('SELECT 1 FROM event_followup_receipts WHERE request_id=?', (request['id'],)).fetchone()
    assert not e.conn.execute('SELECT 1 FROM event_blocks WHERE user_id=? OR target_id=?', (a.id, a.id)).fetchone()
    assert not e.conn.execute('SELECT 1 FROM event_checkin_receipts WHERE source_id=?', ('delete',)).fetchone()
    assert users.conn.execute('SELECT count(*) FROM connection_sessions').fetchone()[0] == 1
    assert users.conn.execute('SELECT count(*) FROM connection_oauth').fetchone()[0] == 0


def test_delete_networking_preserves_existing_beta_identity_calendar_and_bookings(setup):
    import json
    from app.users import CALENDAR_SCOPE
    e, users, admin, member, _ = setup
    a, b = member(1), member(2)
    users._update(a.id, NOW, status='active', joined=1, phone='6581234567', typed_phone='6581234567',
                  google_token_enc='encrypted', google_scopes=json.dumps([CALENDAR_SCOPE]), calendar_status='connected')
    users.conn.execute("UPDATE public_booking_profiles SET enabled=1,location='Keep venue' WHERE user_id=?", (a.id,))
    users.conn.execute('CREATE TABLE public_booking_requests(request_id TEXT, user_id INTEGER, result TEXT)')
    users.conn.execute('INSERT INTO public_booking_requests VALUES(?,?,?)', ('booking', a.id, 'saved-provider-event'))
    users.conn.commit()
    original_user = users.get(a.id)
    booking_before = dict(users.conn.execute('SELECT * FROM public_booking_profiles WHERE user_id=?', (a.id,)).fetchone())
    request = e.send_followup('ai-sg', a.id, b.id, 'coffee', '', 'remove-network')
    b_membership_id = e.membership('ai-sg', b.id)['id']
    assert e.delete_networking_data(a.id) == {'account_deleted': False}
    assert users.get(a.id) == original_user
    assert dict(users.conn.execute('SELECT * FROM public_booking_profiles WHERE user_id=?', (a.id,)).fetchone()) == booking_before
    assert users.conn.execute('SELECT result FROM public_booking_requests WHERE user_id=?', (a.id,)).fetchone()[0] == 'saved-provider-event'
    assert e.membership('ai-sg', a.id) is None and e.membership('ai-sg', b.id)
    assert not e.conn.execute('SELECT 1 FROM event_profiles WHERE user_id=?', (a.id,)).fetchone()
    assert not e.conn.execute('SELECT 1 FROM event_followups WHERE id=?', (request['id'],)).fetchone()
    assert not e.conn.execute("SELECT 1 FROM event_audit WHERE actor_id=? OR (action='profile_updated' AND target_id=?)", (a.id, str(a.id))).fetchone()
    assert e.conn.execute("SELECT 1 FROM event_audit WHERE action='checked_in' AND target_id=?", (str(b_membership_id),)).fetchone()
    assert e.delete_networking_data(a.id) == {'account_deleted': False}


def test_networking_deletion_rejects_unknown_disabled_and_admin_accounts(setup):
    e, users, admin, member, _ = setup
    a = member(1)
    users._update(a.id, NOW, status='disabled')
    for user_id in (99999, a.id, admin.id):
        with pytest.raises(ValueError):
            e.delete_networking_data(user_id)
    assert e.membership('ai-sg', a.id)


@pytest.mark.parametrize('setup', ['file'], indirect=True)
@pytest.mark.parametrize('operation', ['proposal', 'capacity'])
def test_database_transaction_serializes_independent_writer_lock_contexts(setup, monkeypatch, operation):
    """Independent connections with no shared process lock model app + operator writers."""
    from concurrent.futures import ThreadPoolExecutor, wait
    from contextlib import nullcontext
    from threading import Event
    import app.event_store as module

    first, users, admin, member, _ = setup
    second = module.EventStore(users, first.public_booking, first.now, first.base_url)
    paused, release = Event(), Event()
    if operation == 'proposal':
        a, b = member(1), member(2)
        request = first.send_followup('ai-sg', a.id, b.id, 'coffee', '', 'race')
        request = first.respond(request['id'], b.id, 'accept')
        original = first._mutable_request
        def pause_after_read(*args, **kwargs):
            result = original(*args, **kwargs)
            paused.set()
            assert release.wait(3)
            return result
        monkeypatch.setattr(first, '_mutable_request', pause_after_read)
        def action(store):
            return store.propose_times(request['id'], a.id, ['2026-10-11T12:00:00Z'], 'UTC', '', request['version'])
    else:
        first.create_event('one-seat', 'One Seat', NOW.isoformat(), capacity=1)
        class PausedCount:
            def __init__(self, cursor): self.cursor = cursor
            def fetchone(self):
                row = self.cursor.fetchone()
                paused.set()
                assert release.wait(3)
                return row
        class ConnectionProxy:
            def __init__(self, conn): self.conn = conn
            def __enter__(self): self.conn.__enter__(); return self
            def __exit__(self, *args): return self.conn.__exit__(*args)
            def execute(self, sql, parameters=()):
                cursor = self.conn.execute(sql, parameters)
                return PausedCount(cursor) if sql.startswith('SELECT COUNT(*) FROM event_memberships') else cursor
        monkeypatch.setattr(first, 'conn', ConnectionProxy(first.conn))
        def action(store):
            suffix = 'first' if store is first else 'second'
            return store.import_roster('one-seat', [dict(source_id=suffix, name=suffix, email=f'{suffix}@example.com')], admin.id, atomic=True)
    # Each process normally owns a different Python lock. Removing this shared
    # test-process lock proves SQLite itself protects the read/modify/write unit.
    monkeypatch.setattr(module, '_WRITE_LOCK', nullcontext())
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_write = pool.submit(action, first)
        assert paused.wait(2)
        second_write = pool.submit(action, second)
        try:
            early, _ = wait([second_write], timeout=0.1)
        finally:
            release.set()
        first_write.result(timeout=3)
        with pytest.raises(ValueError, match='changed|capacity'):
            second_write.result(timeout=3)
    assert not early
    if operation == 'proposal':
        assert second.get_followup(request['id'], a.id)['version'] == request['version'] + 1
    else:
        assert len(second.organizer_roster('one-seat', admin.id)) == 1


def test_user_deletion_audit_cleanup_distinguishes_membership_and_user_ids(setup):
    e, users, _, member, _ = setup
    a, b = member(1), member(2)
    first_id = e.membership('ai-sg', a.id)['id']
    second_id = e.membership('ai-sg', b.id)['id']
    assert second_id == a.id  # Demonstrate the collision between unrelated ID namespaces.
    users.delete(a.id)
    assert not e.conn.execute("SELECT 1 FROM event_audit WHERE action='checked_in' AND target_id=?", (str(first_id),)).fetchone()
    assert e.conn.execute("SELECT 1 FROM event_audit WHERE action='checked_in' AND target_id=?", (str(second_id),)).fetchone()


def test_networking_deletion_rolls_back_as_one_transaction(setup, monkeypatch):
    e, users, _, member, _ = setup
    a, b = member(1), member(2)
    request = e.send_followup('ai-sg', a.id, b.id, 'coffee', '', 'preserve-on-failure')
    original = e._delete_networking_rows
    def fail_after_cleanup(user_id):
        original(user_id)
        raise RuntimeError('synthetic deletion failure')
    monkeypatch.setattr(e, '_delete_networking_rows', fail_after_cleanup)
    with pytest.raises(RuntimeError):
        e.delete_networking_data(a.id)
    assert users.get(a.id) and e.membership('ai-sg', a.id)
    assert e.profile(a.id, a.id)['display_name'] == a.name
    assert e.get_followup(request['id'], a.id)['state'] == 'pending'


def test_event_only_deletion_never_skips_revocation_for_an_inconsistent_granted_account(setup):
    e, users, _, member, _ = setup
    a = member(1)
    users.conn.execute('UPDATE users SET event_only=1,google_token_enc=? WHERE id=?', ('unexpected-grant', a.id))
    users.conn.commit()
    with pytest.raises(ValueError, match='Disconnect integrations'):
        e.delete_networking_data(a.id)
    assert users.get(a.id).google_token_enc == 'unexpected-grant'
    assert e.membership('ai-sg', a.id)
