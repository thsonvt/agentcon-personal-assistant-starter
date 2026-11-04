"""Private event membership and explicit, provider-free follow-up agreements.

All identities use the existing Users table; handles use PublicBooking's namespace.
Roster input never signs anybody up, publishes a profile, or sends a message.
"""
import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.public_booking import RESERVED
from app.users import _WRITE_LOCK

PURPOSES = frozenset(('coffee', 'technical', 'collaboration', 'work'))
OPEN_STATES = ('pending', 'accepted', 'time_proposed')


class EventError(ValueError):
    """A safe, participant-facing validation or permission error."""


def _read(fn):
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        with _WRITE_LOCK:
            return fn(self, *args, **kwargs)
    return wrapped


def _write(fn):
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        with _WRITE_LOCK, self.conn:
            # SQLite, not just this process's lock, must serialize validation and
            # writes when an operator CLI and app use the same persistent file.
            self.conn.execute('BEGIN IMMEDIATE')
            return fn(self, *args, **kwargs)
    return wrapped


def _text(value, maximum, label, required=True):
    if not isinstance(value, str):
        raise EventError(f'Enter a valid {label}.')
    value = value.strip()
    if (required and not value) or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise EventError(f'Enter a valid {label}.')
    return value


def _email(value):
    value = _text(value, 254, 'email').lower()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
        raise EventError('Enter a valid email.')
    return value


def _time(value):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise EventError('Choose a valid time including its timezone.') from None


class EventStore:
    def __init__(self, users, public_booking, now, base_url):
        self.users, self.public_booking, self.now = users, public_booking, now
        # OAuth/session commits on Users' connection must never publish this
        # module's uncommitted consent/profile changes.
        self.conn, self.base_url = users.store.new_connection(), base_url.rstrip('/')
        with _WRITE_LOCK, self.conn:
            self.conn.executescript('''
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
                starts_at TEXT NOT NULL, capacity INTEGER NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS event_memberships (
                id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, source_id TEXT NOT NULL,
                name TEXT NOT NULL, email TEXT NOT NULL COLLATE NOCASE, user_id INTEGER,
                checked_in_at TEXT, claimed_at TEXT, directory_opt_in INTEGER NOT NULL DEFAULT 0,
                consent_at TEXT, consent_version TEXT, removed_at TEXT,
                UNIQUE(event_id,source_id), UNIQUE(event_id,email), UNIQUE(event_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS event_checkin_receipts (
                event_id INTEGER NOT NULL, receipt_id TEXT NOT NULL, source_id TEXT NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(event_id,receipt_id)
            );
            CREATE TABLE IF NOT EXISTS event_profiles (
                user_id INTEGER PRIMARY KEY, display_name TEXT NOT NULL, building TEXT NOT NULL DEFAULT '',
                intents TEXT NOT NULL DEFAULT '[]', visibility TEXT NOT NULL DEFAULT 'hidden', updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS event_handle_reservations (
                username TEXT PRIMARY KEY COLLATE NOCASE, user_id INTEGER NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS event_handle_insert BEFORE INSERT ON public_booking_profiles
            WHEN EXISTS(SELECT 1 FROM event_handle_reservations WHERE username=NEW.username AND user_id!=NEW.user_id)
            BEGIN SELECT RAISE(ABORT,'Handle reserved'); END;
            CREATE TRIGGER IF NOT EXISTS event_handle_update BEFORE UPDATE OF username ON public_booking_profiles
            WHEN EXISTS(SELECT 1 FROM event_handle_reservations WHERE username=NEW.username AND user_id!=NEW.user_id)
            BEGIN SELECT RAISE(ABORT,'Handle reserved'); END;
            CREATE TRIGGER IF NOT EXISTS event_handle_remember AFTER UPDATE OF username ON public_booking_profiles
            WHEN OLD.username!=NEW.username COLLATE NOCASE
            BEGIN INSERT OR IGNORE INTO event_handle_reservations VALUES (OLD.username,OLD.user_id); END;
            CREATE TABLE IF NOT EXISTS event_followups (
                id TEXT PRIMARY KEY, event_id INTEGER NOT NULL, sender_id INTEGER NOT NULL,
                recipient_id INTEGER NOT NULL, purpose TEXT NOT NULL, note TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', version INTEGER NOT NULL DEFAULT 0,
                slots TEXT NOT NULL DEFAULT '[]', timezone TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '',
                proposer_id INTEGER, selected_index INTEGER, selected_time TEXT,
                accepted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS event_one_open_request ON event_followups(event_id,sender_id,recipient_id)
                WHERE state IN ('pending','accepted','time_proposed');
            CREATE TABLE IF NOT EXISTS event_followup_receipts (
                sender_id INTEGER NOT NULL, idempotency_key TEXT NOT NULL, payload_hash TEXT NOT NULL,
                request_id TEXT NOT NULL, PRIMARY KEY(sender_id,idempotency_key)
            );
            CREATE TABLE IF NOT EXISTS event_blocks (
                user_id INTEGER NOT NULL, target_id INTEGER NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(user_id,target_id)
            );
            CREATE TABLE IF NOT EXISTS event_audit (
                id INTEGER PRIMARY KEY, event_id INTEGER, actor_id INTEGER,
                action TEXT NOT NULL, target_id TEXT, created_at TEXT NOT NULL
            );
            DROP TRIGGER IF EXISTS event_delete_user;
            CREATE TRIGGER event_delete_user AFTER DELETE ON users BEGIN
                DELETE FROM event_followup_receipts WHERE request_id IN
                    (SELECT id FROM event_followups WHERE sender_id=OLD.id OR recipient_id=OLD.id);
                DELETE FROM event_followups WHERE sender_id=OLD.id OR recipient_id=OLD.id;
                DELETE FROM event_checkin_receipts WHERE EXISTS
                    (SELECT 1 FROM event_memberships m WHERE m.user_id=OLD.id AND m.event_id=event_checkin_receipts.event_id
                     AND m.source_id=event_checkin_receipts.source_id);
                DELETE FROM event_audit WHERE action='checked_in' AND EXISTS
                    (SELECT 1 FROM event_memberships m WHERE m.user_id=OLD.id
                     AND m.event_id=event_audit.event_id AND CAST(m.id AS TEXT)=event_audit.target_id);
                DELETE FROM event_memberships WHERE user_id=OLD.id;
                DELETE FROM event_profiles WHERE user_id=OLD.id;
                DELETE FROM event_blocks WHERE user_id=OLD.id OR target_id=OLD.id;
                UPDATE event_audit SET actor_id=NULL WHERE actor_id=OLD.id;
                DELETE FROM event_audit WHERE target_id=CAST(OLD.id AS TEXT)
                    AND action IN ('claimed','profile_updated','removed');
                DELETE FROM event_handle_reservations WHERE user_id=OLD.id;
            END;
            COMMIT;
            ''')
            self.conn.commit()

    def _stamp(self):
        return _time(self.now()).isoformat()

    def _user(self, user_id):
        user = self.users.get(user_id) if user_id is not None else None
        if user is None or user.status in ('disabled', 'rejected'):
            raise EventError('This account is not available.')
        return user

    def _admin(self, actor_id):
        user = self._user(actor_id)
        if not user.is_admin:
            raise EventError('Organizer access is required.')

    def _audit(self, event_id, actor_id, action, target_id=None):
        self.conn.execute('INSERT INTO event_audit(event_id,actor_id,action,target_id,created_at) VALUES(?,?,?,?,?)',
                          (event_id, actor_id, action, str(target_id) if target_id is not None else None, self._stamp()))

    @_read
    def get_event(self, slug):
        row = self.conn.execute('SELECT * FROM events WHERE slug=?', (slug,)).fetchone()
        return dict(row) if row else None

    @_read
    def list_events(self):
        return [dict(row) for row in self.conn.execute('SELECT * FROM events ORDER BY starts_at DESC')]

    def _event(self, slug):
        event = self.get_event(slug)
        if not event:
            raise EventError('Event not found.')
        return event

    def _window_open(self, event, days):
        return _time(self.now()) < _time(event['starts_at']) + timedelta(days=days)

    @_write
    def create_event(self, slug, title, starts_at, capacity=150):
        slug = _text(slug, 80, 'event slug')
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug):
            raise EventError('Use a lowercase event slug.')
        title = _text(title, 120, 'event title')
        start = _time(starts_at).isoformat()
        if not isinstance(capacity, int) or not 1 <= capacity <= 150:
            raise EventError('Event capacity must be between 1 and 150.')
        existing = self.get_event(slug)
        if existing:
            if (existing['title'], existing['starts_at'], existing['capacity']) != (title, start, capacity):
                raise EventError('This event already exists with different details.')
            return existing
        self.conn.execute('INSERT INTO events(slug,title,starts_at,capacity,created_at) VALUES(?,?,?,?,?)',
                          (slug, title, start, capacity, self._stamp()))
        return self._event(slug)

    @_write
    def import_roster(self, slug, rows, actor_id, *, atomic=False):
        self._admin(actor_id)
        event = self._event(slug)
        result = {'imported': 0, 'unchanged': 0, 'rejected': []}
        if not isinstance(rows, list) or len(rows) > 500:
            raise EventError('Import up to 500 roster rows at a time.')
        for index, row in enumerate(rows, start=1):
            try:
                if not isinstance(row, dict):
                    raise EventError('Each row needs source_id, name and email.')
                source_id = _text(row.get('source_id'), 120, 'source ID')
                name = _text(row.get('name'), 80, 'name')
                email = _email(row.get('email'))
                old = self.conn.execute('SELECT * FROM event_memberships WHERE event_id=? AND source_id=?',
                                        (event['id'], source_id)).fetchone()
                if old and (old['claimed_at'] or old['checked_in_at']) and old['email'] != email:
                    raise EventError('A claimed or checked-in identity cannot be replaced by an import.')
                if old and old['claimed_at']:
                    result['unchanged'] += 1
                    continue
                if not old and self.conn.execute('SELECT COUNT(*) FROM event_memberships WHERE event_id=?',
                                                 (event['id'],)).fetchone()[0] >= event['capacity']:
                    raise EventError('Event capacity reached.')
                self.conn.execute('''INSERT INTO event_memberships(event_id,source_id,name,email) VALUES(?,?,?,?)
                    ON CONFLICT(event_id,source_id) DO UPDATE SET name=excluded.name,email=excluded.email''',
                                  (event['id'], source_id, name, email))
                result['imported'] += 1
            except (EventError, sqlite3.IntegrityError) as error:
                reason = str(error) if isinstance(error, EventError) else 'Email already belongs to another roster entry.'
                result['rejected'].append({'row': index, 'reason': reason})
        if atomic and result['rejected']:
            reasons = '; '.join(f"row {item['row']}: {item['reason']}" for item in result['rejected'])
            raise EventError('Import was not applied: ' + reasons)
        self._audit(event['id'], actor_id, 'roster_import')
        return result

    @_write
    def check_in(self, slug, source_id, receipt_id, actor_id):
        self._admin(actor_id)
        event = self._event(slug)
        receipt_id = _text(receipt_id, 120, 'check-in receipt')
        row = self.conn.execute('SELECT * FROM event_memberships WHERE event_id=? AND source_id=?',
                                (event['id'], source_id)).fetchone()
        if row is None or row['removed_at']:
            raise EventError('Attendee is not available for check-in.')
        receipt = self.conn.execute('SELECT * FROM event_checkin_receipts WHERE event_id=? AND receipt_id=?',
                                    (event['id'], receipt_id)).fetchone()
        if receipt and receipt['source_id'] != source_id:
            raise EventError('Check-in receipt was already used for another attendee.')
        if not receipt:
            self.conn.execute('INSERT INTO event_checkin_receipts VALUES(?,?,?,?)',
                              (event['id'], receipt_id, source_id, self._stamp()))
            self.conn.execute('UPDATE event_memberships SET checked_in_at=COALESCE(checked_in_at,?) WHERE id=?',
                              (self._stamp(), row['id']))
            self._audit(event['id'], actor_id, 'checked_in', row['id'])
        return dict(self.conn.execute('SELECT * FROM event_memberships WHERE id=?', (row['id'],)).fetchone())

    @_read
    def eligible_for_claim(self, slug, email):
        event = self.get_event(slug)
        if not event:
            return False
        try:
            email = _email(email)
        except EventError:
            return False
        row = self.conn.execute('''SELECT user_id,claimed_at FROM event_memberships WHERE event_id=? AND email=?
            AND checked_in_at IS NOT NULL AND removed_at IS NULL''', (event['id'], email)).fetchone()
        if not row or (not row['claimed_at'] and not self._window_open(event, 14)):
            return False
        if row['user_id'] is not None:
            try:
                self._user(row['user_id'])
            except EventError:
                return False
        return True

    @_read
    def membership(self, slug, user_id):
        row = self.conn.execute('''SELECT m.* FROM event_memberships m JOIN events e ON e.id=m.event_id
            WHERE e.slug=? AND m.user_id=?''', (slug, user_id)).fetchone()
        return dict(row) if row else None

    def _member(self, slug, user_id):
        self._user(user_id)
        row = self.membership(slug, user_id)
        if not row or not row['claimed_at'] or not row['checked_in_at'] or row['removed_at']:
            raise EventError('Claim your checked-in event profile first.')
        return row

    @_write
    def claim(self, slug, user):
        current = self._user(user.id)
        if not current.google_subject or not current.email or current.email.lower() != (user.email or '').lower():
            raise EventError('Sign in with the verified email on your registration.')
        event = self._event(slug)
        if not self.eligible_for_claim(slug, current.email):
            raise EventError('No eligible checked-in registration matches this account.')
        row = self.conn.execute('SELECT * FROM event_memberships WHERE event_id=? AND email=?',
                                (event['id'], _email(current.email))).fetchone()
        if row['user_id'] is not None and row['user_id'] != user.id:
            raise EventError('This registration is already claimed.')
        if not row['claimed_at']:
            self.conn.execute('UPDATE event_memberships SET user_id=?,claimed_at=? WHERE id=?',
                              (user.id, self._stamp(), row['id']))
            self._audit(event['id'], user.id, 'claimed', user.id)
        return self.membership(slug, user.id)

    def _blocked(self, left, right):
        if left is None or right is None:
            return False
        return bool(self.conn.execute('''SELECT 1 FROM event_blocks WHERE
            (user_id=? AND target_id=?) OR (user_id=? AND target_id=?)''', (left, right, right, left)).fetchone())

    def _handle_available(self, name, user_id):
        return not self.conn.execute('''SELECT 1 FROM public_booking_profiles WHERE username=? COLLATE NOCASE AND user_id!=?
            UNION ALL SELECT 1 FROM event_handle_reservations WHERE username=? COLLATE NOCASE AND user_id!=?''',
                                     (name, user_id, name, user_id)).fetchone()

    @_read
    def suggest_username(self, user_id, display_name):
        base = re.sub('[^a-z0-9]+', '-', display_name.lower()).strip('-')[:24]
        reserved = RESERVED | {'events', 'event-admin', 'inbox', 'people', 'network'}
        if '@' in display_name or len(base) < 3 or not base[0].isalpha() or base in reserved:
            base = 'builder'
        name, suffix = base, 1
        while not self._handle_available(name, user_id):
            suffix += 1
            name = f'{base}-{suffix}'
        return name

    def _handle(self, user_id, display_name, requested=None):
        current = self.conn.execute('SELECT * FROM public_booking_profiles WHERE user_id=?', (user_id,)).fetchone()
        reserved = RESERVED | {'events', 'event-admin', 'inbox', 'people', 'network'}
        if requested is None and current:
            return current['username']
        if requested is not None:
            name = _text(requested, 32, 'username').lower()
            if not re.fullmatch(r'[a-z][a-z0-9-]{2,31}', name) or name.endswith('-') or name in reserved:
                raise EventError('Choose a username of 3–32 letters, numbers or hyphens; that name may be reserved.')
            if not self._handle_available(name, user_id):
                raise EventError('That username is already in use.')
        else:
            name = self.suggest_username(user_id, display_name)
        try:
            if current:
                self.conn.execute('UPDATE public_booking_profiles SET username=? WHERE user_id=?', (name, user_id))
            else:
                self.conn.execute('''INSERT INTO public_booking_profiles VALUES(?,?,?,?,?,?,?,?,?,?)''',
                                  (user_id, name, display_name, '30-minute meeting', 'Asia/Singapore', '', '09:00', '18:00', '[0,1,2,3,4]', 0))
        except sqlite3.IntegrityError:
            raise EventError('That username is already in use.') from None
        return name

    @_write
    def update_profile(self, slug, user_id, display_name, building='', intents=None, visibility='hidden', directory_opt_in=False, username=None):
        member = self._member(slug, user_id)
        name = _text(display_name, 80, 'display name')
        building = _text(building, 280, 'project description', False)
        intents = [] if intents is None else intents
        if not isinstance(intents, (list, tuple)) or any(p not in PURPOSES for p in intents):
            raise EventError('Choose valid follow-up interests.')
        if visibility not in ('hidden', 'event', 'public'):
            raise EventError('Choose a valid profile visibility.')
        if not isinstance(directory_opt_in, bool):
            raise EventError('Confirm whether to join the directory.')
        if directory_opt_in and visibility == 'hidden':
            raise EventError('Choose event or public visibility to join the directory.')
        self._handle(user_id, name, username)
        self.conn.execute('''INSERT INTO event_profiles VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
            display_name=excluded.display_name,building=excluded.building,intents=excluded.intents,
            visibility=excluded.visibility,updated_at=excluded.updated_at''',
                          (user_id, name, building, json.dumps(sorted(set(intents))), visibility, self._stamp()))
        self.conn.execute('UPDATE event_memberships SET directory_opt_in=?,consent_at=?,consent_version=? WHERE id=?',
                          (int(directory_opt_in), self._stamp(), 'event-pilot-v1', member['id']))
        if visibility == 'hidden':
            self.conn.execute('UPDATE event_memberships SET directory_opt_in=0 WHERE user_id=?', (user_id,))
        self._audit(member['event_id'], user_id, 'profile_updated', user_id)
        result = self._safe_profile(user_id)
        result['directory_opt_in'] = directory_opt_in
        return result

    def _safe_profile(self, user_id):
        row = self.conn.execute('''SELECT p.*,b.username FROM event_profiles p
            JOIN public_booking_profiles b ON b.user_id=p.user_id WHERE p.user_id=?''', (user_id,)).fetchone()
        if not row:
            raise EventError('Profile not found.')
        result = dict(row)
        result['intents'] = json.loads(result['intents'])
        result['public_id'] = str(user_id)
        result['stable_url'] = f'{self.base_url}/people/{user_id}'
        result['profile_url'] = f"{self.base_url}/{result['username']}"
        result['booking_url'] = None
        booking = self.conn.execute('SELECT enabled FROM public_booking_profiles WHERE user_id=?', (user_id,)).fetchone()
        if booking['enabled'] and self.public_booking._ready(user_id):
            result['booking_url'] = f"{self.base_url}/{result['username']}/30min"
        return result

    @_read
    def profile_by_username(self, username, viewer_id=None):
        row = self.conn.execute('SELECT user_id FROM public_booking_profiles WHERE username=? COLLATE NOCASE', (username,)).fetchone()
        if row is None:
            raise EventError('Profile not found.')
        return self.profile(row['user_id'], viewer_id)

    @_read
    def profile(self, user_id, viewer_id=None):
        self._user(user_id)
        if viewer_id is not None:
            self._user(viewer_id)
        if self._blocked(user_id, viewer_id):
            raise EventError('Profile not found.')
        result = self._safe_profile(user_id)
        memberships = self.conn.execute('''SELECT e.* FROM events e JOIN event_memberships m ON e.id=m.event_id
            WHERE m.user_id=? AND m.removed_at IS NULL AND m.checked_in_at IS NOT NULL AND m.claimed_at IS NOT NULL''', (user_id,)).fetchall()
        if not memberships:
            raise EventError('Profile not found.')
        result['events'] = []
        for event in memberships:
            if viewer_id is not None and self._window_open(event, 30):
                other = self.membership(event['slug'], viewer_id)
                if other and other['claimed_at'] and other['checked_in_at'] and not other['removed_at']:
                    result['events'].append({'slug': event['slug'], 'title': event['title']})
        if viewer_id == user_id:
            return result
        if result['visibility'] == 'public':
            return result
        if result['visibility'] == 'event' and viewer_id is not None:
            for event in memberships:
                if self._window_open(event, 30):
                    other = self.membership(event['slug'], viewer_id)
                    if other and other['claimed_at'] and other['checked_in_at'] and not other['removed_at']:
                        return result
        raise EventError('Profile not found.')

    @_read
    def directory(self, slug, viewer_id, search=''):
        self._member(slug, viewer_id)
        event = self._event(slug)
        if not self._window_open(event, 30):
            return []
        search = _text(search, 100, 'search', False).casefold()
        rows = self.conn.execute('''SELECT m.user_id FROM event_memberships m JOIN users u ON u.id=m.user_id
            JOIN event_profiles p ON p.user_id=m.user_id WHERE m.event_id=? AND m.claimed_at IS NOT NULL
            AND m.checked_in_at IS NOT NULL AND m.directory_opt_in=1 AND m.removed_at IS NULL
            AND u.status NOT IN ('disabled','rejected') AND p.visibility IN ('event','public')''', (event['id'],)).fetchall()
        result = []
        for row in rows:
            if self._blocked(viewer_id, row['user_id']):
                continue
            profile = self._safe_profile(row['user_id'])
            if search in (profile['display_name'] + ' ' + profile['building']).casefold():
                result.append(profile)
        return sorted(result, key=lambda p: p['display_name'].casefold())

    def _request(self, request_id):
        row = self.conn.execute('''SELECT f.*,COALESCE(s.display_name,'Event participant') AS sender_name,
            COALESCE(r.display_name,'Event participant') AS recipient_name,e.slug AS event_slug
            FROM event_followups f LEFT JOIN event_profiles s ON s.user_id=f.sender_id
            LEFT JOIN event_profiles r ON r.user_id=f.recipient_id
            JOIN events e ON e.id=f.event_id WHERE f.id=?''', (request_id,)).fetchone()
        if not row:
            raise EventError('Follow-up not found.')
        result = dict(row)
        result['slots'] = json.loads(result['slots'])
        return result

    @_read
    def get_followup(self, request_id, user_id):
        self._user(user_id)
        result = self._request(request_id)
        if user_id not in (result['sender_id'], result['recipient_id']):
            raise EventError('Follow-up not found.')
        return result

    @_read
    def inbox(self, user_id):
        self._user(user_id)
        return [self._request(row['id']) for row in self.conn.execute(
            'SELECT id FROM event_followups WHERE sender_id=? OR recipient_id=? ORDER BY updated_at DESC', (user_id, user_id)).fetchall()]

    @_write
    def send_followup(self, slug, sender_id, recipient_id, purpose, note, idempotency_key):
        self._member(slug, sender_id)
        recipient = self._member(slug, recipient_id)
        event = self._event(slug)
        if not self._window_open(event, 30):
            raise EventError('Event discovery has closed.')
        if sender_id == recipient_id or self._blocked(sender_id, recipient_id):
            raise EventError('This follow-up is not available.')
        if not recipient['directory_opt_in'] or self._safe_profile(recipient_id)['visibility'] == 'hidden':
            raise EventError('This attendee is not receiving follow-ups.')
        if purpose not in PURPOSES:
            raise EventError('Choose a valid follow-up purpose.')
        note = _text(note, 500, 'note', False)
        key = _text(idempotency_key, 120, 'request key')
        payload = hashlib.sha256(json.dumps([event['id'], sender_id, recipient_id, purpose, note]).encode()).hexdigest()
        receipt = self.conn.execute('SELECT * FROM event_followup_receipts WHERE sender_id=? AND idempotency_key=?', (sender_id, key)).fetchone()
        if receipt:
            if receipt['payload_hash'] != payload:
                raise EventError('This request key was already used with different details.')
            return self._request(receipt['request_id'])
        prior = self.conn.execute('''SELECT id FROM event_followups WHERE event_id=? AND sender_id=? AND recipient_id=?
            AND state IN ('pending','accepted','time_proposed')''', (event['id'], sender_id, recipient_id)).fetchone()
        if prior:
            request_id = prior['id']
        else:
            # Declining is also a durable stop for this sender at this event.
            if self.conn.execute("SELECT 1 FROM event_followups WHERE event_id=? AND sender_id=? AND recipient_id=? AND state='declined'",
                                 (event['id'], sender_id, recipient_id)).fetchone():
                raise EventError('This attendee has declined the follow-up.')
            day = _time(self.now()).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            count = self.conn.execute('SELECT COUNT(*) FROM event_followups WHERE sender_id=? AND created_at>=?', (sender_id, day)).fetchone()[0]
            if count >= 5:
                raise EventError('You can send up to five new requests per day.')
            request_id = uuid.uuid4().hex
            self.conn.execute('''INSERT INTO event_followups(id,event_id,sender_id,recipient_id,purpose,note,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)''', (request_id, event['id'], sender_id, recipient_id, purpose, note, self._stamp(), self._stamp()))
        self.conn.execute('INSERT INTO event_followup_receipts VALUES(?,?,?,?)', (sender_id, key, payload, request_id))
        return self._request(request_id)

    def _mutable_request(self, request_id, user_id, version=None):
        request = self.get_followup(request_id, user_id)
        self._member(request['event_slug'], request['sender_id'])
        self._member(request['event_slug'], request['recipient_id'])
        if self._blocked(request['sender_id'], request['recipient_id']):
            raise EventError('This follow-up is no longer available.')
        if version is not None and request['version'] != version:
            raise EventError('This follow-up changed. Refresh before trying again.')
        return request

    @_write
    def respond(self, request_id, user_id, action, version=None):
        request = self._mutable_request(request_id, user_id, version)
        if action in ('accept', 'decline'):
            if user_id != request['recipient_id'] or request['state'] != 'pending':
                raise EventError('Only the recipient can respond to a pending request.')
            state = 'accepted' if action == 'accept' else 'declined'
        elif action == 'withdraw':
            if user_id != request['sender_id'] or request['state'] not in OPEN_STATES:
                raise EventError('Only the sender can withdraw an open request.')
            state = 'withdrawn'
        else:
            raise EventError('Choose a valid request action.')
        self.conn.execute('''UPDATE event_followups SET state=?,version=version+1,updated_at=?,
            accepted_at=CASE WHEN ?='accepted' THEN ? ELSE accepted_at END WHERE id=?''',
                          (state, self._stamp(), state, self._stamp(), request_id))
        return self._request(request_id)

    @_write
    def block(self, slug, user_id, target_id):
        self._member(slug, user_id)
        self._member(slug, target_id)
        if user_id == target_id:
            raise EventError('You cannot block yourself.')
        self.conn.execute('INSERT OR IGNORE INTO event_blocks VALUES(?,?,?)', (user_id, target_id, self._stamp()))
        self.conn.execute('''UPDATE event_followups SET state='expired',version=version+1,updated_at=?
            WHERE ((sender_id=? AND recipient_id=?) OR (sender_id=? AND recipient_id=?))
            AND state IN ('pending','accepted','time_proposed')''', (self._stamp(), user_id, target_id, target_id, user_id))
        return {'blocked': True}

    @_write
    def propose_times(self, request_id, user_id, slots, timezone, location, version):
        if type(version) is not int or version < 0:
            raise EventError('Refresh this follow-up before choosing a time.')
        request = self._mutable_request(request_id, user_id, version)
        if request['state'] not in ('accepted', 'time_proposed'):
            raise EventError('Accept the follow-up before proposing times.')
        try:
            ZoneInfo(timezone)
        except (ValueError, TypeError, ZoneInfoNotFoundError):
            raise EventError('Choose a valid timezone.') from None
        location = _text(location, 200, 'location', False)
        if not isinstance(slots, list) or not 1 <= len(slots) <= 3:
            raise EventError('Propose one to three times.')
        normalized = []
        for slot in slots:
            start = _time(slot.get('start') if isinstance(slot, dict) else slot)
            end = _time(slot.get('end')) if isinstance(slot, dict) else start + timedelta(minutes=30)
            if start <= _time(self.now()) or end <= start or end - start > timedelta(hours=12):
                raise EventError('Choose future times ending within twelve hours of their start.')
            normalized.append({'start': start.isoformat(), 'end': end.isoformat()})
        if len({slot['start'] for slot in normalized}) != len(normalized):
            raise EventError('Choose different time options.')
        normalized.sort(key=lambda slot: slot['start'])
        self.conn.execute('''UPDATE event_followups SET slots=?,timezone=?,location=?,proposer_id=?,
            state='time_proposed',version=version+1,updated_at=?,selected_index=NULL,selected_time=NULL WHERE id=?''',
                          (json.dumps(normalized), timezone, location, user_id, self._stamp(), request_id))
        return self._request(request_id)

    @_write
    def confirm_time(self, request_id, user_id, slot_index, version):
        if type(version) is not int or version < 0:
            raise EventError('Refresh this follow-up before choosing a time.')
        request = self._mutable_request(request_id, user_id, version)
        if request['state'] != 'time_proposed' or user_id == request['proposer_id']:
            raise EventError('The other participant must confirm a proposed time.')
        if not isinstance(slot_index, int) or not 0 <= slot_index < len(request['slots']):
            raise EventError('Choose one of the proposed times.')
        selected = request['slots'][slot_index]['start']
        if _time(selected) <= _time(self.now()):
            raise EventError('That proposed time has passed.')
        self.conn.execute('''UPDATE event_followups SET state='time_agreed',version=version+1,
            selected_index=?,selected_time=?,updated_at=? WHERE id=?''', (slot_index, selected, self._stamp(), request_id))
        return self._request(request_id)

    @_read
    def organizer_roster(self, slug, actor_id):
        self._admin(actor_id)
        event = self._event(slug)
        return [dict(row) for row in self.conn.execute('SELECT * FROM event_memberships WHERE event_id=? ORDER BY name', (event['id'],))]

    @_read
    def summary(self, slug, actor_id):
        self._admin(actor_id)
        event = self._event(slug)
        row = self.conn.execute('''SELECT COUNT(*) AS roster,
            COALESCE(SUM(checked_in_at IS NOT NULL),0) AS checked_in,
            COALESCE(SUM(claimed_at IS NOT NULL),0) AS claimed,
            COALESCE(SUM(directory_opt_in=1 AND removed_at IS NULL),0) AS directory_opted_in
            FROM event_memberships WHERE event_id=?''', (event['id'],)).fetchone()
        result = dict(row)
        request_counts = self.conn.execute('''SELECT COUNT(*) AS sent,COUNT(DISTINCT sender_id) AS unique_senders,
            COALESCE(SUM(accepted_at IS NOT NULL),0) AS accepted,
            COALESCE(SUM(state='time_agreed'),0) AS time_agreed FROM event_followups WHERE event_id=?''', (event['id'],)).fetchone()
        result.update(dict(request_counts))
        # Public booking rows currently have no event/follow-up attribution. Missing
        # evidence must not be presented as measured zero conversions.
        result['calendar_event_created'] = None
        result['provider_attribution'] = 'not_attributed'
        result['calendar_event_created_note'] = 'Public calendar bookings are not attributed to this event; manual agreements do not create calendar invitations.'
        cohort = [self.users.get(row['user_id']) for row in self.conn.execute('''
            SELECT m.user_id FROM event_memberships m JOIN users u ON u.id=m.user_id
            WHERE m.event_id=? AND m.claimed_at IS NOT NULL AND m.checked_in_at IS NOT NULL
            AND m.removed_at IS NULL AND u.status NOT IN ('disabled','rejected')''', (event['id'],))]
        result['current_participants'] = len(cohort)
        result['calendar_connected'] = sum(bool(user.calendar_ready and user.google_revocation_status != 'pending') for user in cohort)
        result['assistant_active'] = sum(bool(user.status == 'active' and user.verified_join and not user.event_only) for user in cohort)
        result['activation_definition'] = 'Current nonremoved, enabled claimed cohort: Calendar has verified access; assistant is active with a verified phone join.'
        result['repeat_senders'] = self.conn.execute('''SELECT COUNT(*) FROM (
            SELECT sender_id FROM event_followups WHERE event_id=? GROUP BY sender_id
            HAVING COUNT(DISTINCT substr(created_at,1,10))>=2)''', (event['id'],)).fetchone()[0]
        result['repeat_use_definition'] = 'Unique senders creating distinct follow-up requests on at least two UTC calendar dates; same-day requests and retries do not count.'
        return result

    @_write
    def remove_member(self, slug, source_id, actor_id):
        self._admin(actor_id)
        event = self._event(slug)
        member = self.conn.execute('SELECT * FROM event_memberships WHERE event_id=? AND source_id=?', (event['id'], source_id)).fetchone()
        if not member:
            raise EventError('Attendee not found.')
        self.conn.execute('UPDATE event_memberships SET removed_at=?,directory_opt_in=0 WHERE id=?', (self._stamp(), member['id']))
        if member['user_id']:
            self.conn.execute('''UPDATE event_followups SET state='expired',version=version+1,updated_at=?
                WHERE event_id=? AND (sender_id=? OR recipient_id=?) AND state IN ('pending','accepted','time_proposed')''',
                              (self._stamp(), event['id'], member['user_id'], member['user_id']))
        self._audit(event['id'], actor_id, 'removed', member['user_id'])
        return {'removed': True}

    def _delete_networking_rows(self, user_id):
        self.conn.execute('''DELETE FROM event_followup_receipts WHERE request_id IN
            (SELECT id FROM event_followups WHERE sender_id=? OR recipient_id=?)''', (user_id, user_id))
        self.conn.execute('DELETE FROM event_followups WHERE sender_id=? OR recipient_id=?', (user_id, user_id))
        # Check-in audit targets are membership IDs; other targets are user IDs.
        # Match their action as well as the number to avoid touching another person.
        self.conn.execute('''DELETE FROM event_audit WHERE actor_id=? OR
            (action IN ('claimed','profile_updated','removed') AND target_id=?) OR
            (action='checked_in' AND EXISTS (SELECT 1 FROM event_memberships m
                WHERE m.user_id=? AND m.event_id=event_audit.event_id
                AND CAST(m.id AS TEXT)=event_audit.target_id))''', (user_id, str(user_id), user_id))
        self.conn.execute('''DELETE FROM event_checkin_receipts WHERE EXISTS
            (SELECT 1 FROM event_memberships m WHERE m.user_id=?
             AND m.event_id=event_checkin_receipts.event_id AND m.source_id=event_checkin_receipts.source_id)''', (user_id,))
        self.conn.execute('DELETE FROM event_memberships WHERE user_id=?', (user_id,))
        self.conn.execute('DELETE FROM event_profiles WHERE user_id=?', (user_id,))
        self.conn.execute('DELETE FROM event_blocks WHERE user_id=? OR target_id=?', (user_id, user_id))

    @_write
    def delete_networking_data(self, user_id):
        """Erase event data; remove a credential-free event-only identity as well.

        The route must require a fresh authenticated session, CSRF and explicit
        confirmation. Existing beta accounts keep booking handles and provider
        grants. Never invoke Users.delete here: it commits a different connection.
        """
        user = self._user(user_id)
        if user.is_admin:
            raise EventError('Organizer accounts cannot use attendee deletion.')
        if user.event_only and (user.google_token_enc or user.phone or user.joined):
            raise EventError('Disconnect integrations through account settings before deleting this account.')
        self._delete_networking_rows(user_id)
        if not user.event_only:
            # Handle reservations belong to the surviving booking namespace.
            return {'account_deleted': False}
        tables = {row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ('loop_in_tasks', 'contacts', 'profile_facts', 'connection_sessions', 'connection_oauth',
                      'onboarding_attempts', 'onboarding_announcements', 'public_booking_requests', 'public_booking_profiles'):
            if table in tables:
                self.conn.execute(f'DELETE FROM {table} WHERE user_id=?', (user_id,))
        if 'agent_sessions' in tables and 'agent_messages' in tables:
            ids = [row[0] for row in self.conn.execute('SELECT session_id FROM agent_sessions WHERE session_id LIKE ?', (f'{user_id}:%',))]
            for session_id in ids:
                self.conn.execute('DELETE FROM agent_messages WHERE session_id=?', (session_id,))
                self.conn.execute('DELETE FROM agent_sessions WHERE session_id=?', (session_id,))
        self.conn.execute('DELETE FROM users WHERE id=? AND event_only=1', (user_id,))
        return {'account_deleted': True}

    @_write
    def cleanup(self, actor_id):
        self._admin(actor_id)
        count = 0
        for event in self.list_events():
            if not self._window_open(event, 14):
                self.conn.execute('''DELETE FROM event_checkin_receipts WHERE event_id=? AND source_id IN
                    (SELECT source_id FROM event_memberships WHERE event_id=? AND claimed_at IS NULL)''', (event['id'], event['id']))
                count += self.conn.execute('DELETE FROM event_memberships WHERE event_id=? AND claimed_at IS NULL', (event['id'],)).rowcount
            if not self._window_open(event, 30):
                self.conn.execute("UPDATE event_followups SET state='expired',version=version+1,updated_at=? WHERE event_id=? AND state='pending'", (self._stamp(), event['id']))
        self._audit(None, actor_id, 'retention_cleanup')
        return {'unclaimed_deleted': count}
