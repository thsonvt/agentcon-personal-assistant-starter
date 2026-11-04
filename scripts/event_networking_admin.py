"""Provider-free, explicit-database operator commands for the event pilot.

Run from the repository root: python -m scripts.event_networking_admin --help.
The CLI never creates a database, reads environment secrets, or sends messages.
"""
import argparse
import csv
import io
import json
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timezone

from app.event_store import EventError, EventStore, _email, _text
from app.users import Users
from app.store import Store

MAX_CSV_BYTES = 256 * 1024
MAX_CSV_ROWS = 150
REQUIRED_TABLES = {'users', 'public_booking_profiles', 'events', 'event_memberships',
                   'event_checkin_receipts', 'event_profiles', 'event_followups', 'event_audit'}


class ImportRejected(EventError):
    def __init__(self, rejected):
        super().__init__('Import was not applied. Correct the listed CSV rows and preview again.')
        self.rejected = rejected


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--db', required=True, help='Explicit path to an existing, initialized Pippa SQLite database.')
    result.add_argument('--actor', required=True, type=int, help='Existing organizer user ID; is_admin is verified.')
    commands = result.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create-event', help='Create one event; repeating identical details is harmless.')
    create.add_argument('--slug', required=True)
    create.add_argument('--title', required=True)
    create.add_argument('--starts-at', required=True, help='ISO timestamp including offset, e.g. 2026-10-10T09:30:00+08:00')
    create.add_argument('--capacity', type=int, default=150)
    roster_import = commands.add_parser('import-roster', help='Validate CSV without writing unless --apply is present.')
    roster_import.add_argument('--event', required=True)
    roster_import.add_argument('--csv', required=True, help='UTF-8 CSV with exactly source_id,name,email headers.')
    roster_import.add_argument('--apply', action='store_true')
    checkin = commands.add_parser('check-in', help='Record actual staff-verified arrival; keep the receipt ID for retries.')
    checkin.add_argument('--event', required=True)
    checkin.add_argument('--source-id', required=True)
    checkin.add_argument('--receipt-id', required=True)
    roster = commands.add_parser('roster', help='Show membership IDs and states; contact details are omitted.')
    roster.add_argument('--event', required=True)
    roster.add_argument('--show-names', action='store_true', help='Include attendee names and source IDs for trusted staff; sensitive output.')
    summary = commands.add_parser('summary', help='Print aggregate JSON counts only.')
    summary.add_argument('--event', required=True)
    remove = commands.add_parser('remove', help='Remove event access and directory listing; preserve the user account.')
    remove.add_argument('--event', required=True)
    remove.add_argument('--source-id', required=True)
    cleanup = commands.add_parser('cleanup', help='Preview retention cleanup across all events; writes only with --apply.')
    cleanup.add_argument('--apply', action='store_true')
    return result


def _verify_database(conn, actor):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not REQUIRED_TABLES.issubset(tables):
        raise EventError('Database is not initialized for the event pilot. Run the application migration on a backed-up database first.')
    user = conn.execute('SELECT is_admin,status FROM users WHERE id=?', (actor,)).fetchone()
    if not user or not user['is_admin'] or user['status'] in ('disabled', 'rejected'):
        raise EventError('Organizer access is required.')


def _context(store, now):
    # No routes, OAuth settings, Calendar client or application environment needed.
    # Admin EventStore methods do not use its public_booking dependency.
    users = Users(store)
    return EventStore(users, None, now, 'http://localhost')


def _read_csv(path):
    try:
        with Path(path).open('rb') as source:
            payload = source.read(MAX_CSV_BYTES + 1)
    except OSError:
        raise EventError('CSV could not be read.') from None
    if len(payload) > MAX_CSV_BYTES:
        raise EventError('CSV must be no larger than 256 KiB.')
    try:
        reader = csv.DictReader(io.StringIO(payload.decode('utf-8-sig')), strict=True)
        if reader.fieldnames is None or len(reader.fieldnames) != 3 or set(reader.fieldnames) != {'source_id', 'name', 'email'}:
            raise EventError('CSV needs exactly the headers source_id,name,email.')
        rows, rejected, sources, emails = [], [], set(), set()
        for index, row in enumerate(reader, start=2):
            if len(rows) >= MAX_CSV_ROWS:
                raise EventError('CSV must contain no more than 150 attendee rows.')
            rows.append(row)
            try:
                if None in row:
                    raise EventError('Expected three CSV fields.')
                source_id = _text(row.get('source_id'), 120, 'source ID')
                name = _text(row.get('name'), 80, 'name')
                email = _email(row.get('email'))
                if source_id in sources:
                    raise EventError('Duplicate source ID in this CSV.')
                if email in emails:
                    raise EventError('Duplicate email in this CSV.')
                sources.add(source_id)
                emails.add(email)
                rows[-1] = {'source_id': source_id, 'name': name, 'email': email}
            except EventError as exc:
                rejected.append({'row': index, 'reason': str(exc)})
        if rejected:
            raise ImportRejected(rejected)
        if not rows:
            raise EventError('CSV contains no attendees.')
        return rows
    except (UnicodeDecodeError, csv.Error):
        raise EventError('CSV must be valid UTF-8 with correctly quoted fields.') from None


def _execute(events, args, rows=None, preview=False):
    # Recheck the actor on the actual database immediately before all operations;
    # create_event is also called by application setup, so its API is not admin-only.
    events._admin(args.actor)
    if args.command == 'create-event':
        event = events.create_event(args.slug, args.title, args.starts_at, args.capacity)
        return {'event': event['slug'], 'ready': True}
    if args.command == 'import-roster':
        result = events.import_roster(args.event, rows, args.actor, atomic=not preview)
        if result['rejected']:
            # EventStore counts data rows from one; the CSV header occupies row one.
            raise ImportRejected([{'row': row['row'] + 1, 'reason': row['reason']} for row in result['rejected']])
        return {**result, 'dry_run': not args.apply}
    if args.command == 'check-in':
        member = events.check_in(args.event, args.source_id, args.receipt_id, args.actor)
        return {'checked_in': bool(member['checked_in_at'])}
    if args.command == 'roster':
        attendees = []
        for member in events.organizer_roster(args.event, args.actor):
            row = {'membership_id': member['id'], 'checked_in': bool(member['checked_in_at']),
                   'claimed': bool(member['claimed_at']), 'removed': bool(member['removed_at'])}
            if args.show_names:
                row.update(name=member['name'], source_id=member['source_id'])
            attendees.append(row)
        return {'attendees': attendees}
    if args.command == 'summary':
        return events.summary(args.event, args.actor)
    if args.command == 'remove':
        return events.remove_member(args.event, args.source_id, args.actor)
    if args.command == 'cleanup':
        return {**events.cleanup(args.actor), 'dry_run': not args.apply}
    raise EventError('Unknown operator command.')


def main(argv=None, *, now=None):
    args = parser().parse_args(argv)
    now = now or (lambda: datetime.now(timezone.utc))
    readonly = writable = None
    snapshot_store = live_store = events = live_events = None
    try:
        path = Path(args.db).expanduser().resolve(strict=True)
        if not path.is_file():
            raise EventError('Choose an existing SQLite database file.')
        readonly = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        readonly.row_factory = sqlite3.Row
        _verify_database(readonly, args.actor)
        # Validate against an in-memory snapshot. Preview and reporting cannot
        # modify the source file, and database conflict errors precede any writes.
        snapshot_store = Store(':memory:')
        readonly.backup(snapshot_store.conn)
        events = _context(snapshot_store, now)
        rows = _read_csv(args.csv) if args.command == 'import-roster' else None
        result = _execute(events, args, rows, preview=True)
        mutating = args.command in ('create-event', 'check-in', 'remove') or (
            args.command in ('import-roster', 'cleanup') and args.apply)
        if mutating:
            writable = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True)
            writable.row_factory = sqlite3.Row
            _verify_database(writable, args.actor)
            live_store = Store(str(path))
            live_events = _context(live_store, now)
            result = _execute(live_events, args, rows)
        print(json.dumps(result, sort_keys=True))
        return 0
    except ImportRejected as exc:
        print(json.dumps({'error': str(exc), 'rejected': exc.rejected}), file=sys.stderr)
        return 2
    except EventError as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        return 2
    except (OSError, ValueError, sqlite3.Error):
        # Provider credentials, SQL, paths and roster values never enter errors.
        print(json.dumps({'error': 'Database or input is unavailable or incompatible. Check the explicit path, permissions and application version.'}), file=sys.stderr)
        return 2
    finally:
        for event_store in (live_events, events):
            if event_store is not None:
                event_store.conn.close()
        for store in (live_store, snapshot_store):
            if store is not None:
                store.conn.close()
        for connection in (writable, readonly):
            if connection is not None:
                connection.close()


if __name__ == '__main__':
    raise SystemExit(main())
