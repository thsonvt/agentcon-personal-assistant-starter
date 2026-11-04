"""Bounded conference capture persistence on an independent SQLite connection."""

import json
import threading
import uuid
from contextlib import contextmanager
from datetime import timedelta, timezone


SESSION_COLUMNS = 'id, user_id, phone, title, state, created_at, closed_at, expires_at, summary_json, error, link_version'
ITEM_COLUMNS = ('id, user_id, session_id, message_id, kind, original_text, media_id, '
                'mime_type, media, extracted_text, category, status, error, received_at, source_timestamp, included')
RETENTION = timedelta(days=7)
IMAGE_LIMIT = 5 * 1024 * 1024
AUDIO_LIMIT = 16 * 1024 * 1024
SESSION_MEDIA_LIMIT = 64 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS capture_sessions (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, phone TEXT NOT NULL,
    title TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'collecting'
        CHECK(state IN ('collecting', 'closing', 'ready', 'failed')),
    created_at TEXT NOT NULL, closed_at TEXT, expires_at TEXT NOT NULL,
    summary_json TEXT, error TEXT, link_version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(user_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS capture_one_collecting
    ON capture_sessions(user_id) WHERE state = 'collecting';
CREATE TABLE IF NOT EXISTS capture_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    session_id TEXT NOT NULL, message_id TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('text', 'image', 'audio')),
    original_text TEXT NOT NULL DEFAULT '', media_id TEXT, mime_type TEXT,
    media BLOB, extracted_text TEXT, category TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'ready', 'failed')),
    error TEXT, received_at TEXT NOT NULL, source_timestamp TEXT,
    included INTEGER NOT NULL DEFAULT 1, included_override INTEGER,
    FOREIGN KEY(user_id, session_id) REFERENCES capture_sessions(user_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS capture_items_session ON capture_items(user_id, session_id, id);
CREATE TABLE IF NOT EXISTS capture_receipts (
    message_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capture_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    phone TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending', 'sending', 'sent', 'failed')),
    created_at TEXT NOT NULL, session_id TEXT
);
"""


class CaptureStore:
    """Synchronous intake transactions; never hold a transaction across network I/O.

    Workers may replay provider work after a crash. All writes target existing
    rows, so results arriving after deletion cannot recreate private content.
    """

    def __init__(self, store, now):
        self.conn = store.new_connection()
        self.conn.execute('PRAGMA foreign_keys = ON')
        self.conn.executescript(SCHEMA)
        if 'session_id' not in {row['name'] for row in self.conn.execute('PRAGMA table_info(capture_notifications)')}:
            self.conn.execute('ALTER TABLE capture_notifications ADD COLUMN session_id TEXT')
            self.conn.commit()
        self.now = now
        self._lock = threading.RLock()
        self._depth = 0

    def _time(self):
        now = self.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    @contextmanager
    def transaction(self):
        """Nest safely without allowing individual methods to commit outer intake."""
        with self._lock:
            savepoint = f'capture_{self._depth}'
            outer = self._depth == 0
            self.conn.execute('BEGIN IMMEDIATE' if outer else f'SAVEPOINT {savepoint}')
            self._depth += 1
            try:
                yield self
                if outer:
                    self.conn.commit()
                else:
                    self.conn.execute(f'RELEASE {savepoint}')
            except BaseException:
                if outer:
                    self.conn.rollback()
                else:
                    self.conn.execute(f'ROLLBACK TO {savepoint}')
                    self.conn.execute(f'RELEASE {savepoint}')
                raise
            finally:
                self._depth -= 1

    def _one(self, sql, args=()):
        with self._lock:
            row = self.conn.execute(sql, args).fetchone()
        return self._dict(row) if row else None

    def _many(self, sql, args=()):
        with self._lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [self._dict(row) for row in rows]

    @staticmethod
    def _dict(row):
        result = dict(row)
        if 'included' in result:
            result['included'] = bool(result['included'])
        if 'has_media' in result:
            result['has_media'] = bool(result['has_media'])
        return result

    def get(self, user_id, session_id):
        return self._one(f'SELECT {SESSION_COLUMNS} FROM capture_sessions '
                         'WHERE user_id = ? AND id = ? AND expires_at > ?',
                         (user_id, session_id, self._time().isoformat()))

    def current(self, user_id):
        return self._one(f'SELECT {SESSION_COLUMNS} FROM capture_sessions '
                         "WHERE user_id = ? AND state = 'collecting' AND expires_at > ?",
                         (user_id, self._time().isoformat()))

    def latest(self, user_id):
        return self._one(f'SELECT {SESSION_COLUMNS} FROM capture_sessions '
                         'WHERE user_id = ? AND expires_at > ? ORDER BY rowid DESC LIMIT 1',
                         (user_id, self._time().isoformat()))

    def start(self, user_id, phone, title):
        with self.transaction():
            self.expire()
            current = self.current(user_id)
            if current:
                return current
            count = self.conn.execute('SELECT count(*) FROM capture_sessions WHERE user_id = ?', (user_id,)).fetchone()[0]
            if count >= 20:
                raise ValueError('You have 20 retained captures. Delete a capture before starting another.')
            now = self._time()
            session_id = uuid.uuid4().hex
            self.conn.execute('INSERT INTO capture_sessions (id, user_id, phone, title, created_at, expires_at) '
                              'VALUES (?, ?, ?, ?, ?, ?)',
                              (session_id, user_id, phone, title, now.isoformat(), (now + RETENTION).isoformat()))
            return self.get(user_id, session_id)

    def items(self, user_id, session_id, include_media=True):
        if not self.get(user_id, session_id):
            return []
        columns = ITEM_COLUMNS
        if not include_media:
            columns = ITEM_COLUMNS.replace(', media,', ', NULL AS media,')
            columns += ', coalesce(length(media), 0) > 0 AS has_media'
        return self._many(f'SELECT {columns} FROM capture_items WHERE user_id = ? AND session_id = ? ORDER BY id',
                          (user_id, session_id))

    def item(self, user_id, item_id):
        row = self._one(f'SELECT {ITEM_COLUMNS} FROM capture_items WHERE user_id = ? AND id = ?', (user_id, item_id))
        return row if row and self.get(user_id, row['session_id']) else None

    def add_item(self, user_id, session_id, message_id, kind, text='', media_id=None, source_timestamp=None):
        with self.transaction():
            existing = self._one(f'SELECT {ITEM_COLUMNS} FROM capture_items WHERE message_id = ?', (message_id,))
            if existing:
                if existing['user_id'] == user_id and existing['session_id'] == session_id:
                    return existing
                raise ValueError('This message was already captured.')
            session = self.get(user_id, session_id)
            if not session or session['state'] != 'collecting':
                raise ValueError('This capture is closed. Start a new capture to add notes.')
            if kind not in ('text', 'image', 'audio'):
                raise ValueError('Capture supports text, images and audio only.')
            if len(text) > 10000:
                raise ValueError('Each note can contain at most 10,000 characters.')
            count = self.conn.execute('SELECT count(*) FROM capture_items WHERE user_id = ? AND session_id = ?',
                                      (user_id, session_id)).fetchone()[0]
            if count >= 100:
                raise ValueError('This capture has reached its 100 item limit.')
            cursor = self.conn.execute('INSERT INTO capture_items '
                                      '(user_id, session_id, message_id, kind, original_text, media_id, received_at, source_timestamp) '
                                      'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                                      (user_id, session_id, message_id, kind, text, media_id, self._time().isoformat(), source_timestamp))
            return self.item(user_id, cursor.lastrowid)

    def save_media(self, user_id, item_id, data, mime_type):
        with self.transaction():
            item = self.item(user_id, item_id)
            if not item or item['status'] != 'pending':
                return None
            limit = IMAGE_LIMIT if item['kind'] == 'image' else AUDIO_LIMIT
            if item['kind'] == 'text':
                raise ValueError('Text notes cannot contain media.')
            if len(data) > limit:
                raise ValueError(f'This {item["kind"]} exceeds the {limit // (1024 * 1024)} MiB limit.')
            total = self.conn.execute('SELECT coalesce(sum(length(media)), 0) FROM capture_items '
                                      'WHERE user_id = ? AND session_id = ? AND id != ?',
                                      (user_id, item['session_id'], item_id)).fetchone()[0]
            if total + len(data) > SESSION_MEDIA_LIMIT:
                raise ValueError('This capture exceeds its 64 MiB media limit.')
            self.conn.execute('UPDATE capture_items SET media = ?, mime_type = ? WHERE user_id = ? AND id = ?',
                              (data, mime_type, user_id, item_id))
            return self.item(user_id, item_id)

    def complete_item(self, user_id, item_id, text, category, included=True):
        with self.transaction():
            if not self.item(user_id, item_id):
                return None
            if category not in ('slide', 'observation', 'question', 'review'):
                raise ValueError('Unknown capture category.')
            self.conn.execute("UPDATE capture_items SET extracted_text = ?, category = ?, status = 'ready', "
                              'error = NULL, included = coalesce(included_override, ?) '
                              "WHERE user_id = ? AND id = ? AND status = 'pending'",
                              (text, category, int(included), user_id, item_id))
            return self.item(user_id, item_id)

    def fail_item(self, user_id, item_id, error):
        with self.transaction():
            if not self.item(user_id, item_id):
                return None
            self.conn.execute("UPDATE capture_items SET status = 'failed', error = ? WHERE user_id = ? AND id = ? AND status = 'pending'",
                              (error, user_id, item_id))
            return self.item(user_id, item_id)

    def finish(self, user_id, session_id):
        with self.transaction():
            if not self.get(user_id, session_id):
                return None
            self.conn.execute("UPDATE capture_sessions SET state = 'closing', closed_at = ? "
                              "WHERE user_id = ? AND id = ? AND state = 'collecting'",
                              (self._time().isoformat(), user_id, session_id))
            return self.get(user_id, session_id)

    def set_included(self, user_id, session_id, item_id, included):
        with self.transaction():
            item = self.item(user_id, item_id)
            if not item or item['session_id'] != session_id:
                return None
            self.conn.execute('UPDATE capture_items SET included = ?, included_override = ? WHERE user_id = ? AND id = ?',
                              (int(included), int(included), user_id, item_id))
            self.conn.execute('UPDATE capture_sessions SET summary_json = NULL, error = NULL, '
                              "state = CASE WHEN state = 'collecting' THEN state ELSE 'closing' END WHERE user_id = ? AND id = ?",
                              (user_id, session_id))
            return self.item(user_id, item_id)

    def save_summary(self, user_id, session_id, summary):
        with self.transaction():
            session = self.get(user_id, session_id)
            if not session:
                return None
            if session['state'] != 'closing':
                raise ValueError('Finish this capture before creating its summary.')
            if self.conn.execute("SELECT 1 FROM capture_items WHERE user_id = ? AND session_id = ? AND status = 'pending' LIMIT 1",
                                 (user_id, session_id)).fetchone():
                raise ValueError('This capture still has pending items.')
            self.conn.execute("UPDATE capture_sessions SET summary_json = ?, state = 'ready', error = NULL WHERE user_id = ? AND id = ?",
                              (json.dumps(summary, ensure_ascii=False, allow_nan=False), user_id, session_id))
            return self.get(user_id, session_id)

    def fail_summary(self, user_id, session_id, error):
        with self.transaction():
            if not self.get(user_id, session_id):
                return None
            self.conn.execute("UPDATE capture_sessions SET state = 'failed', error = ?, summary_json = NULL "
                              "WHERE user_id = ? AND id = ? AND state = 'closing'", (error, user_id, session_id))
            return self.get(user_id, session_id)

    def retry(self, user_id, session_id):
        with self.transaction():
            session = self.get(user_id, session_id)
            if not session:
                return None
            cursor = self.conn.execute("UPDATE capture_items SET status = 'pending', error = NULL "
                                      "WHERE user_id = ? AND session_id = ? AND status = 'failed'", (user_id, session_id))
            if cursor.rowcount or session['state'] == 'failed':
                self.conn.execute('UPDATE capture_sessions SET summary_json = NULL, error = NULL, '
                                  "state = CASE WHEN state = 'collecting' THEN state ELSE 'closing' END WHERE user_id = ? AND id = ?",
                                  (user_id, session_id))
            return self.get(user_id, session_id)

    def delete(self, user_id, session_id):
        with self.transaction():
            self.conn.execute('DELETE FROM capture_notifications WHERE user_id = ? AND session_id = ?', (user_id, session_id))
            self.conn.execute('DELETE FROM capture_items WHERE user_id = ? AND session_id = ?', (user_id, session_id))
            cursor = self.conn.execute('DELETE FROM capture_sessions WHERE user_id = ? AND id = ?', (user_id, session_id))
            return cursor.rowcount == 1

    def pending_items(self, user_ids=None):
        """One bounded metadata batch; fetch original bytes with item() when processing."""
        args = [self._time().isoformat()]
        eligible = ''
        if user_ids is not None:
            user_ids = list(user_ids)
            if not user_ids:
                return []
            eligible = ' AND i.user_id IN (' + ','.join('?' for _ in user_ids) + ')'
            args.extend(user_ids)
        columns = ', '.join('NULL AS media' if col.strip() == 'media' else 'i.' + col.strip()
                            for col in ITEM_COLUMNS.split(','))
        return self._many('SELECT ' + columns +
                          ' FROM capture_items i JOIN capture_sessions s ON i.session_id = s.id AND i.user_id = s.user_id '
                          "WHERE i.status = 'pending' AND s.expires_at > ?" + eligible + ' ORDER BY i.id LIMIT 100', args)

    def closing_sessions(self):
        return self._many(f'SELECT {SESSION_COLUMNS} FROM capture_sessions '
                          "WHERE state = 'closing' AND expires_at > ? ORDER BY rowid", (self._time().isoformat(),))

    def expire(self):
        with self.transaction():
            now = self._time()
            # Explicit deletion also works with the account-deletion connection's FK setting.
            self.conn.execute('DELETE FROM capture_notifications WHERE session_id IN '
                              '(SELECT id FROM capture_sessions WHERE expires_at <= ?)', (now.isoformat(),))
            self.conn.execute('DELETE FROM capture_items WHERE session_id IN (SELECT id FROM capture_sessions WHERE expires_at <= ?)',
                              (now.isoformat(),))
            self.conn.execute('DELETE FROM capture_sessions WHERE expires_at <= ?', (now.isoformat(),))
            cutoff = (now - RETENTION).isoformat()
            self.conn.execute('DELETE FROM capture_receipts WHERE created_at <= ?', (cutoff,))
            self.conn.execute('DELETE FROM capture_notifications WHERE created_at <= ?', (cutoff,))

    def seen(self, message_id):
        return self._one('SELECT message_id FROM capture_receipts WHERE message_id = ?', (message_id,)) is not None

    def mark_seen(self, user_id, message_id):
        with self.transaction():
            cursor = self.conn.execute('INSERT OR IGNORE INTO capture_receipts (message_id, user_id, created_at) VALUES (?, ?, ?)',
                                      (message_id, user_id, self._time().isoformat()))
            return cursor.rowcount == 1

    def notify(self, user_id, phone, body, session_id=None):
        with self.transaction():
            if session_id is not None and not self.get(user_id, session_id):
                return None
            cursor = self.conn.execute('INSERT INTO capture_notifications (user_id, phone, body, created_at, session_id) '
                                      'VALUES (?, ?, ?, ?, ?)',
                                      (user_id, phone, body, self._time().isoformat(), session_id))
            return self._one('SELECT * FROM capture_notifications WHERE id = ?', (cursor.lastrowid,))

    def pending_notifications(self):
        return self._many("SELECT * FROM capture_notifications WHERE state = 'pending' AND created_at > ? ORDER BY id",
                          ((self._time() - RETENTION).isoformat(),))

    def notification_state(self, id, state):
        transitions = {'sending': 'pending', 'sent': 'sending', 'failed': 'sending'}
        if state not in transitions:
            raise ValueError('Notifications cannot be automatically retried.')
        with self.transaction():
            changed = self.conn.execute('UPDATE capture_notifications SET state = ? WHERE id = ? AND state = ?',
                                        (state, id, transitions[state]))
            if changed.rowcount != 1:
                return None
            return self._one('SELECT * FROM capture_notifications WHERE id = ?', (id,))
