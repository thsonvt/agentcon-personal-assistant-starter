"""SQLite storage. The only module that writes SQL."""

import copy
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from app.config import normalize_phone

# Plan 1/2 schema, kept so the migration can be tested against a real old database.
OLD_SCHEMA_FOR_TESTS = """
CREATE TABLE contacts (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT UNIQUE NOT NULL, email TEXT
);
CREATE TABLE profile_facts (
  id INTEGER PRIMARY KEY, key TEXT, fact TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE loop_in_tasks (
  id INTEGER PRIMARY KEY, contact_id INTEGER NOT NULL REFERENCES contacts(id),
  purpose TEXT NOT NULL, duration_min INTEGER NOT NULL,
  window_start TEXT NOT NULL, window_end TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('waiting','negotiating','done','nudged','cancelled')),
  event_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX one_open_task_per_contact
  ON loop_in_tasks(contact_id) WHERE status IN ('waiting','negotiating','nudged');
CREATE TABLE seen_messages (wa_message_id TEXT PRIMARY KEY, received_at TEXT NOT NULL);
CREATE TABLE unknown_replies (phone TEXT PRIMARY KEY, last_sent_at TEXT NOT NULL);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL DEFAULT 1,
  name TEXT NOT NULL, phone TEXT NOT NULL, email TEXT,
  UNIQUE (user_id, phone)
);
CREATE TABLE IF NOT EXISTS profile_facts (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL DEFAULT 1,
  key TEXT, fact TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loop_in_tasks (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL DEFAULT 1,
  contact_id INTEGER NOT NULL REFERENCES contacts(id),
  purpose TEXT NOT NULL, duration_min INTEGER NOT NULL,
  window_start TEXT NOT NULL, window_end TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('waiting','negotiating','done','nudged','cancelled')),
  event_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_task_per_contact
  ON loop_in_tasks(contact_id) WHERE status IN ('waiting','negotiating','nudged');
CREATE TABLE IF NOT EXISTS seen_messages (
  wa_message_id TEXT PRIMARY KEY, received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unknown_replies (
  phone TEXT PRIMARY KEY, last_sent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS owner_notices (
  id INTEGER PRIMARY KEY, phone TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrade a Plan 1/2 database in place. Idempotent; existing rows become Pippa's (user 1)."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "contacts" in tables and "user_id" not in _columns(conn, "contacts"):
        # The old UNIQUE(phone) is part of the table definition, so the table must be rebuilt.
        # SQLite's recommended order (new table, copy, drop old, rename new) keeps loop_in_tasks'
        # REFERENCES contacts(id) pointing at the right table, and one transaction makes it all-or-nothing.
        conn.executescript(
            """
            BEGIN;
            DROP TABLE IF EXISTS contacts_new;
            CREATE TABLE contacts_new (
              id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL DEFAULT 1,
              name TEXT NOT NULL, phone TEXT NOT NULL, email TEXT,
              UNIQUE (user_id, phone)
            );
            INSERT INTO contacts_new (id, user_id, name, phone, email)
              SELECT id, 1, name, phone, email FROM contacts;
            DROP TABLE contacts;
            ALTER TABLE contacts_new RENAME TO contacts;
            COMMIT;
            """
        )
    for table in ("profile_facts", "loop_in_tasks"):
        if table in tables and "user_id" not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
    conn.commit()


OPEN_STATUSES = ("waiting", "negotiating", "nudged")
TASK_STATUSES = ("waiting", "negotiating", "done", "nudged", "cancelled")
TASK_COLUMNS = "id, contact_id, purpose, duration_min, window_start, window_end, status, event_id, user_id"
CONTACT_COLUMNS = "id, name, phone, email, user_id"


class OpenTaskExists(Exception):
    """The contact already has an open loop-in task."""


class AmbiguousContact(Exception):
    """More than one saved contact matches a name."""


@dataclass(frozen=True)
class Contact:
    id: int
    name: str
    phone: str
    email: str | None
    user_id: int = 1


@dataclass(frozen=True)
class Fact:
    id: int
    key: str | None
    fact: str


@dataclass(frozen=True)
class LoopInTask:
    id: int
    contact_id: int
    purpose: str
    duration_min: int
    window_start: str
    window_end: str
    status: str
    event_id: str | None
    user_id: int = 1


class _SharedMemoryConnection(sqlite3.Connection):
    """Give shared-memory SQLITE_LOCKED the same bounded wait as disk writes.

    SQLite's busy timeout does not handle shared-cache table locks. Retrying an
    individual failed statement is safe (SQLite aborts that statement atomically),
    unlike retrying a whole transaction or an executescript batch.
    """

    def execute(self, sql, parameters=(), /):
        deadline = time.monotonic() + 5
        while True:
            try:
                return super().execute(sql, parameters)
            except sqlite3.OperationalError as exc:
                if (getattr(exc, 'sqlite_errorcode', 0) & 0xff) != sqlite3.SQLITE_LOCKED or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)


class Store:
    """One database. Contacts, facts and tasks are always read and written for `user_id`.

    `Store(path)` is user 1 (Pippa); `store.for_user(n)` is a view of the same database for user n.
    Everything else (users, webhook bookkeeping, cross-user lookups) is shared.
    """

    def __init__(self, db_path: str):
        self._memory = db_path == ":memory:"
        if self._memory:
            # Unique per Store, shareable only by its explicitly opened connections.
            self._database = f"file:pippa-{uuid.uuid4().hex}?mode=memory&cache=shared"
        else:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            self._database = str(Path(db_path).resolve())
        self.conn = self.new_connection()
        _migrate(self.conn)
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.user_id = 1

    def new_connection(self) -> sqlite3.Connection:
        """Open an independent transaction boundary on this Store's database.

        Callers own the returned connection. User-scoped Store copies retain the
        same database target; independently created in-memory Stores stay isolated.
        """
        factory = _SharedMemoryConnection if self._memory else sqlite3.Connection
        conn = sqlite3.connect(self._database, uri=self._memory, check_same_thread=False, factory=factory)
        conn.row_factory = sqlite3.Row
        return conn

    def for_user(self, user_id: int) -> "Store":
        view = copy.copy(self)
        view.user_id = user_id
        return view

    def table_names(self) -> set[str]:
        rows = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {row["name"] for row in rows}

    # --- contacts -------------------------------------------------------
    def add_contact(self, name: str, phone: str, email: str | None = None) -> Contact:
        digits = normalize_phone(phone)
        if not digits:
            raise ValueError("A contact needs a phone number.")
        self.conn.execute(
            "INSERT INTO contacts (user_id, name, phone, email) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, phone) DO UPDATE SET name = excluded.name, "
            "email = COALESCE(excluded.email, contacts.email)",
            (self.user_id, name.strip(), digits, email),
        )
        self.conn.commit()
        contact = self.get_contact_by_phone(digits)
        assert contact is not None
        return contact

    def get_contact_by_phone(self, phone: str) -> Contact | None:
        row = self.conn.execute(
            f"SELECT {CONTACT_COLUMNS} FROM contacts WHERE user_id = ? AND phone = ?",
            (self.user_id, normalize_phone(phone)),
        ).fetchone()
        return Contact(**dict(row)) if row else None

    def get_contact(self, contact_id: int) -> Contact | None:
        row = self.conn.execute(
            f"SELECT {CONTACT_COLUMNS} FROM contacts WHERE user_id = ? AND id = ?", (self.user_id, contact_id)
        ).fetchone()
        return Contact(**dict(row)) if row else None

    def list_contacts(self) -> list[Contact]:
        rows = self.conn.execute(
            f"SELECT {CONTACT_COLUMNS} FROM contacts WHERE user_id = ? ORDER BY name COLLATE NOCASE", (self.user_id,)
        ).fetchall()
        return [Contact(**dict(r)) for r in rows]

    def find_contact(self, name: str) -> Contact | None:
        """Exact name (any case) wins; otherwise a unique partial match. Raises AmbiguousContact."""
        wanted = name.strip().lower()
        if not wanted:
            return None
        contacts = self.list_contacts()
        exact = [c for c in contacts if c.name.lower() == wanted]
        if len(exact) == 1:
            return exact[0]
        partial = exact or [c for c in contacts if wanted in c.name.lower()]
        if len(partial) > 1:
            raise AmbiguousContact(", ".join(c.name for c in partial))
        return partial[0] if partial else None

    # --- loop-in tasks ----------------------------------------------------
    def create_loop_in_task(
        self,
        contact_id: int,
        purpose: str,
        duration_min: int,
        window_start: datetime,
        window_end: datetime,
        now: datetime,
    ) -> LoopInTask:
        stamp = now.isoformat()
        try:
            cur = self.conn.execute(
                "INSERT INTO loop_in_tasks (user_id, contact_id, purpose, duration_min, window_start, "
                "window_end, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'waiting', ?, ?)",
                (self.user_id, contact_id, purpose, duration_min, window_start.isoformat(), window_end.isoformat(), stamp, stamp),
            )
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            raise OpenTaskExists("This contact already has an open request.") from exc
        self.conn.commit()
        task = self._get_task(cur.lastrowid)
        assert task is not None
        return task

    def get_open_task(self, contact_id: int) -> LoopInTask | None:
        row = self.conn.execute(
            f"SELECT {TASK_COLUMNS} FROM loop_in_tasks WHERE user_id = ? AND contact_id = ? AND status IN (?, ?, ?)",
            (self.user_id, contact_id, *OPEN_STATUSES),
        ).fetchone()
        return LoopInTask(**dict(row)) if row else None

    def get_task(self, task_id: int) -> LoopInTask | None:
        row = self.conn.execute(
            f"SELECT {TASK_COLUMNS} FROM loop_in_tasks WHERE user_id = ? AND id = ?", (self.user_id, task_id)
        ).fetchone()
        return LoopInTask(**dict(row)) if row else None

    _get_task = get_task

    def set_task_status(self, task_id: int, status: str, now: datetime, event_id: str | None = None) -> LoopInTask:
        if status not in TASK_STATUSES:
            raise ValueError(f"Unknown task status {status!r}")
        self.conn.execute(
            "UPDATE loop_in_tasks SET status = ?, event_id = COALESCE(?, event_id), updated_at = ? "
            "WHERE user_id = ? AND id = ?",
            (status, event_id, now.isoformat(), self.user_id, task_id),
        )
        self.conn.commit()
        task = self.get_task(task_id)
        assert task is not None
        return task

    def find_open_invite(self, phone: str) -> tuple[Contact, LoopInTask] | None:
        """The open loop-in invite for this phone, whichever user sent it (at most one exists)."""
        row = self.conn.execute(
            f"SELECT c.id AS c_id, c.name, c.phone, c.email, c.user_id AS c_user, "
            f"{', '.join('t.' + col for col in TASK_COLUMNS.split(', '))} "
            "FROM loop_in_tasks t JOIN contacts c ON c.id = t.contact_id "
            "WHERE c.phone = ? AND t.status IN (?, ?, ?) ORDER BY t.id LIMIT 1",
            (normalize_phone(phone), *OPEN_STATUSES),
        ).fetchone()
        if row is None:
            return None
        contact = Contact(id=row["c_id"], name=row["name"], phone=row["phone"], email=row["email"], user_id=row["c_user"])
        task = LoopInTask(**{col: row[col] for col in TASK_COLUMNS.split(", ")})
        return contact, task

    def mark_negotiating(self, task_id: int, now: datetime) -> LoopInTask | None:
        """waiting/nudged → negotiating, and nothing else: a done or cancelled task is never re-opened."""
        self.conn.execute(
            "UPDATE loop_in_tasks SET status = 'negotiating', updated_at = ? "
            "WHERE user_id = ? AND id = ? AND status IN ('waiting', 'nudged')",
            (now.isoformat(), self.user_id, task_id),
        )
        self.conn.commit()
        return self.get_task(task_id)

    def recent_closed_task(self, contact_id: int, since: datetime) -> LoopInTask | None:
        """The contact's latest booked (done) task, if it closed at or after `since`. Cancelled invites don't count."""
        row = self.conn.execute(
            f"SELECT {TASK_COLUMNS}, updated_at FROM loop_in_tasks WHERE user_id = ? AND contact_id = ? AND status = 'done' "
            "ORDER BY updated_at DESC LIMIT 1",
            (self.user_id, contact_id),
        ).fetchone()
        if row is None or datetime.fromisoformat(row["updated_at"]) < since:
            return None
        return LoopInTask(**{k: row[k] for k in row.keys() if k != "updated_at"})

    def find_recent_booking(self, phone: str, since: datetime) -> tuple[Contact, LoopInTask] | None:
        """This phone's most recent booked invite (any user) that closed at or after `since`."""
        row = self.conn.execute(
            "SELECT c.id AS c_id, c.user_id AS c_user, t.id AS t_id, t.updated_at AS closed_at "
            "FROM loop_in_tasks t JOIN contacts c ON c.id = t.contact_id "
            "WHERE c.phone = ? AND t.status = 'done' ORDER BY t.updated_at DESC LIMIT 1",
            (normalize_phone(phone),),
        ).fetchone()
        if row is None or datetime.fromisoformat(row["closed_at"]) < since:
            return None
        mine = self.for_user(row["c_user"])
        return mine.get_contact(row["c_id"]), mine.get_task(row["t_id"])


    def tasks_waiting_since(self, cutoff: datetime) -> list[LoopInTask]:
        """Tasks still 'waiting' whose last update is at or before `cutoff`, for every user (nudges)."""
        rows = self.conn.execute(
            f"SELECT {TASK_COLUMNS}, updated_at FROM loop_in_tasks WHERE status = 'waiting'"
        ).fetchall()
        due = [r for r in rows if datetime.fromisoformat(r["updated_at"]) <= cutoff]
        return [LoopInTask(**{k: r[k] for k in r.keys() if k != "updated_at"}) for r in due]

    # --- profile facts ------------------------------------------------------
    def add_fact(self, fact: str, now: datetime, key: str | None = None) -> Fact:
        """Save a fact. A keyed fact (e.g. 'working_hours') replaces the previous one with that key."""
        if key:
            self.conn.execute("DELETE FROM profile_facts WHERE user_id = ? AND key = ?", (self.user_id, key))
        cur = self.conn.execute(
            "INSERT INTO profile_facts (user_id, key, fact, created_at) VALUES (?, ?, ?, ?)",
            (self.user_id, key, fact.strip(), now.isoformat()),
        )
        self.conn.commit()
        return Fact(id=cur.lastrowid, key=key, fact=fact.strip())

    def list_facts(self) -> list[Fact]:
        rows = self.conn.execute(
            "SELECT id, key, fact FROM profile_facts WHERE user_id = ? ORDER BY id", (self.user_id,)
        ).fetchall()
        return [Fact(**dict(r)) for r in rows]

    def get_fact(self, key: str) -> Fact | None:
        row = self.conn.execute(
            "SELECT id, key, fact FROM profile_facts WHERE user_id = ? AND key = ?", (self.user_id, key)
        ).fetchone()
        return Fact(**dict(row)) if row else None

    def delete_fact(self, fact_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM profile_facts WHERE user_id = ? AND id = ?", (self.user_id, fact_id))
        self.conn.commit()
        return cur.rowcount == 1

    # --- notices Pippa couldn't be sent (his 24h window was closed) -----------
    def queue_notice(self, phone: str, text: str, now: datetime) -> None:
        self.conn.execute(
            "INSERT INTO owner_notices (phone, text, created_at) VALUES (?, ?, ?)",
            (normalize_phone(phone), text, now.isoformat()),
        )
        self.conn.commit()

    def take_notices(self, phone: str) -> list[str]:
        """All waiting notices for this phone, oldest first, removed as they're returned."""
        digits = normalize_phone(phone)
        rows = self.conn.execute("SELECT id, text FROM owner_notices WHERE phone = ? ORDER BY id", (digits,)).fetchall()
        if rows:
            self.conn.execute(f"DELETE FROM owner_notices WHERE id IN ({','.join('?' * len(rows))})", [r["id"] for r in rows])
            self.conn.commit()
        return [r["text"] for r in rows]

    # --- webhook bookkeeping ------------------------------------------------
    def has_seen_message(self, wa_message_id: str) -> bool:
        """Read receipt before choosing a route that may have changed since delivery."""
        return self.conn.execute('SELECT 1 FROM seen_messages WHERE wa_message_id = ?', (wa_message_id,)).fetchone() is not None

    def mark_message_seen(self, wa_message_id: str, now: datetime) -> bool:
        """True the first time a message ID is seen, False for repeats."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO seen_messages (wa_message_id, received_at) VALUES (?, ?)",
            (wa_message_id, now.isoformat()),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def should_send_unknown_reply(self, phone: str, now: datetime) -> bool:
        """True at most once per 24 hours per number; records the send when True."""
        digits = normalize_phone(phone)
        row = self.conn.execute(
            "SELECT last_sent_at FROM unknown_replies WHERE phone = ?", (digits,)
        ).fetchone()
        if row and now - datetime.fromisoformat(row["last_sent_at"]) < timedelta(hours=24):
            return False
        self.conn.execute(
            "INSERT INTO unknown_replies (phone, last_sent_at) VALUES (?, ?) "
            "ON CONFLICT(phone) DO UPDATE SET last_sent_at = excluded.last_sent_at",
            (digits, now.isoformat()),
        )
        self.conn.commit()
        return True
