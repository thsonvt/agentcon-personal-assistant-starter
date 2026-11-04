"""Operator commands fail closed and never mistake imported people for arrivals."""
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from app.event_store import EventStore
from app.public_booking import PublicBooking
from app.store import Store
from app.users import Users

NOW = datetime(2026, 10, 10, 2, tzinfo=timezone.utc)


@pytest.fixture
def operator_db(tmp_path):
    path = tmp_path / 'rehearsal.sqlite'
    users = Users(Store(str(path)))
    users.ensure_admin('Organizer', '6591234567', NOW)
    member, _ = users.sign_in_identity('member', 'Member', 'member@example.com', NOW)
    booking = PublicBooking(None, users, lambda _: None, lambda: NOW, 'http://localhost', 'synthetic-secret')
    events = EventStore(users, booking, lambda: NOW, 'http://localhost')
    events.create_event('ai-sg', 'AI Singapore', NOW.isoformat())
    yield path, events, member
    events.conn.close()
    users.conn.close()


def run_cli(capsys, path, *args, actor=1):
    from scripts.event_networking_admin import main
    code = main(['--db', str(path), '--actor', str(actor), *args], now=lambda: NOW)
    captured = capsys.readouterr()
    output = captured.out or captured.err
    return code, json.loads(output), output


def csv_file(tmp_path, content):
    path = tmp_path/'roster.csv'
    path.write_text('source_id,name,email\n'+content)
    return path


def test_help_smoke_without_database():
    result = subprocess.run([sys.executable, '-m', 'scripts.event_networking_admin', '--help'], capture_output=True, text=True)
    assert result.returncode == 0
    assert 'create-event' in result.stdout and 'cleanup' in result.stdout


def test_missing_database_is_not_created(tmp_path, capsys):
    missing = tmp_path/'missing.sqlite'
    code, output, _ = run_cli(capsys, missing, 'summary', '--event', 'ai-sg')
    assert code == 2 and 'error' in output and not missing.exists()


def test_nonorganizer_cannot_mutate_or_read_roster(operator_db, capsys):
    path, events, member = operator_db
    before = path.read_bytes()
    for args in [('roster', '--event', 'ai-sg'), ('create-event', '--slug', 'other', '--title', 'Other', '--starts-at', NOW.isoformat())]:
        code, output, _ = run_cli(capsys, path, *args, actor=member.id)
        assert code == 2 and 'Organizer' in output['error']
    assert path.read_bytes() == before
    assert events.get_event('other') is None


def test_empty_existing_database_is_not_initialized(tmp_path, capsys):
    path = tmp_path/'empty.sqlite'
    sqlite3.connect(path).close()
    code, output, _ = run_cli(capsys, path, 'summary', '--event', 'ai-sg')
    assert code == 2 and 'error' in output
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT count(*) FROM sqlite_master').fetchone()[0] == 0


def test_import_preview_then_apply_then_idempotent_checkin(operator_db, tmp_path, capsys):
    path, events, _ = operator_db
    roster = csv_file(tmp_path, 'source-1,Alice,alice@example.com\n')
    before_preview = path.read_bytes()
    code, output, raw = run_cli(capsys, path, 'import-roster', '--event', 'ai-sg', '--csv', str(roster))
    assert code == 0 and output['dry_run'] and output['imported'] == 1
    assert path.read_bytes() == before_preview
    assert events.summary('ai-sg', 1)['roster'] == 0
    assert 'Alice' not in raw and 'alice@example.com' not in raw
    code, output, _ = run_cli(capsys, path, 'import-roster', '--event', 'ai-sg', '--csv', str(roster), '--apply')
    assert code == 0 and not output['dry_run'] and output['imported'] == 1
    assert events.summary('ai-sg', 1)['checked_in'] == 0
    for _ in range(2):
        code, output, raw = run_cli(capsys, path, 'check-in', '--event', 'ai-sg', '--source-id', 'source-1', '--receipt-id', 'receipt-1')
        assert code == 0 and output['checked_in'] and 'alice@example.com' not in raw
    assert events.summary('ai-sg', 1)['checked_in'] == 1


@pytest.mark.parametrize('body', [
    'a,Alice,alice@example.com\nb,Bob,invalid\n',
    'a,Alice,alice@example.com\na,Bob,bob@example.com\n',
    'a,Alice,alice@example.com\nb,Bob,ALICE@example.com\n',
])
def test_invalid_or_duplicate_rows_reject_whole_import(operator_db, tmp_path, capsys, body):
    path, events, _ = operator_db
    roster = csv_file(tmp_path, body)
    code, output, raw = run_cli(capsys, path, 'import-roster', '--event', 'ai-sg', '--csv', str(roster), '--apply')
    assert code == 2 and output['rejected']
    assert events.summary('ai-sg', 1)['roster'] == 0
    assert 'alice@example.com' not in raw and 'Alice' not in raw


def test_existing_email_conflict_is_atomic(operator_db, tmp_path, capsys):
    path, events, _ = operator_db
    events.import_roster('ai-sg', [{'source_id': 'original', 'name': 'Private Name', 'email': 'private@example.com'}], 1)
    roster = csv_file(tmp_path, 'new,New Person,new@example.com\nconflict,Private,private@example.com\n')
    code, output, raw = run_cli(capsys, path, 'import-roster', '--event', 'ai-sg', '--csv', str(roster), '--apply')
    assert code == 2 and output['rejected'][0]['row'] == 3
    assert events.summary('ai-sg', 1)['roster'] == 1
    assert 'private@example.com' not in raw and 'Private Name' not in raw


def test_oversized_csv_is_refused(operator_db, tmp_path, capsys):
    path, events, _ = operator_db
    roster = csv_file(tmp_path, 'x'*262145)
    assert run_cli(capsys, path, 'import-roster', '--event', 'ai-sg', '--csv', str(roster), '--apply')[0] == 2
    roster = csv_file(tmp_path, ''.join(f'{i},Person {i},p{i}@example.com\n' for i in range(151)))
    assert run_cli(capsys, path, 'import-roster', '--event', 'ai-sg', '--csv', str(roster), '--apply')[0] == 2
    assert events.summary('ai-sg', 1)['roster'] == 0


def test_summary_and_default_roster_hide_contact_data(operator_db, capsys):
    path, events, _ = operator_db
    events.import_roster('ai-sg', [{'source_id': 'original', 'name': 'Private Name', 'email': 'private@example.com'}], 1)
    code, output, raw = run_cli(capsys, path, 'summary', '--event', 'ai-sg')
    assert code == 0 and output['roster'] == 1 and output['claimed'] == 0
    code, output, raw = run_cli(capsys, path, 'roster', '--event', 'ai-sg')
    assert code == 0 and len(output['attendees']) == 1
    assert 'Private Name' not in raw and 'private@example.com' not in raw and 'original' not in raw
    code, output, raw = run_cli(capsys, path, 'roster', '--event', 'ai-sg', '--show-names')
    assert output['attendees'][0]['name'] == 'Private Name'
    assert 'private@example.com' not in raw


def test_cleanup_is_preview_by_default_and_preserves_claimed(operator_db, capsys):
    from scripts.event_networking_admin import main
    path, events, member = operator_db
    events.import_roster('ai-sg', [dict(source_id='one', name=member.name, email=member.email),
                                   dict(source_id='two', name='Unclaimed', email='unclaimed@example.com')], 1)
    events.check_in('ai-sg', 'one', 'one-check', 1)
    events.claim('ai-sg', member)
    later = lambda: datetime(2026, 11, 11, tzinfo=timezone.utc)
    args = ['--db', str(path), '--actor', '1', 'cleanup']
    assert main(args, now=later) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['dry_run'] and result['unclaimed_deleted'] == 1
    assert events.summary('ai-sg', 1)['roster'] == 2
    assert main(args+['--apply'], now=later) == 0
    result = json.loads(capsys.readouterr().out)
    assert not result['dry_run'] and result['unclaimed_deleted'] == 1
    assert events.summary('ai-sg', 1)['roster'] == 1
    assert events.membership('ai-sg', member.id)


def test_create_event_requires_aware_time_and_remove_is_idempotent(operator_db, capsys):
    path, events, _ = operator_db
    assert run_cli(capsys, path, 'create-event', '--slug', 'new', '--title', 'New', '--starts-at', '2026-10-10T09:30:00')[0] == 2
    assert events.get_event('new') is None
    assert run_cli(capsys, path, 'create-event', '--slug', 'new', '--title', 'New', '--starts-at', '2026-10-10T09:30:00+08:00')[0] == 0
    events.import_roster('ai-sg', [dict(source_id='one', name='Alice', email='alice@example.com')], 1)
    events.check_in('ai-sg', 'one', 'receipt', 1)
    assert run_cli(capsys, path, 'remove', '--event', 'ai-sg', '--source-id', 'one')[0] == 0
    assert not events.eligible_for_claim('ai-sg', 'alice@example.com')
