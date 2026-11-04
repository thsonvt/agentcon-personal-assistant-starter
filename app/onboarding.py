"""Minimal join receipts: number proof and reply delivery are independent facts.

Only hashes, bounded provider IDs, timestamps and enumerated outcomes are stored.
A receipt belongs to one code generation; an old callback cannot affect a new one.
"""
import hashlib
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timezone

from app.config import normalize_phone
from app.users import User, Users


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _generation(user: User) -> str:
    return _hash(f"{user.id}:{user.join_code}:{user.join_code_expires_at}:{user.typed_phone}")


def _error_code(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value <= 999999999:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 9:
        return int(value)
    return None


class OnboardingReceipts:
    def __init__(self, store_or_conn, now: Callable[[], datetime] | None = None):
        self.conn = store_or_conn if isinstance(store_or_conn, sqlite3.Connection) else store_or_conn.conn
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS onboarding_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                generation TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                sender_hash TEXT NOT NULL,
                attempted_at TEXT NOT NULL,
                attempt_status TEXT NOT NULL,
                reply_status TEXT NOT NULL DEFAULT 'not_sent',
                reply_error_code INTEGER,
                message_id TEXT UNIQUE
            );
            CREATE INDEX IF NOT EXISTS onboarding_attempt_user ON onboarding_attempts(user_id, id);
            CREATE TABLE IF NOT EXISTS onboarding_announcements (
                user_id INTEGER PRIMARY KEY,
                generation TEXT NOT NULL
            );
        """)
        self.conn.commit()

    def begin(self, user: User, phone: str, now: datetime, *, generation: str | None = None,
              code_hash: str | None = None) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO onboarding_attempts (user_id,generation,code_hash,sender_hash,attempted_at,attempt_status) "
                "VALUES (?,?,?,?,?,'waiting')",
                (user.id, generation or _generation(user), code_hash or _hash((user.join_code or '').upper()),
                 _hash(normalize_phone(phone)), now.isoformat()),
            )
            attempt_id = cur.lastrowid
            # Bound retained attempts per account; discarded callbacks are safely ignored.
            self.conn.execute("DELETE FROM onboarding_attempts WHERE user_id=? AND id NOT IN "
                              "(SELECT id FROM onboarding_attempts WHERE user_id=? ORDER BY id DESC LIMIT 20)",
                              (user.id, user.id))
            self.conn.commit()
            return attempt_id

    def finish(self, attempt_id: int, status: str) -> None:
        if status not in {"waiting", "number_mismatch", "code_expired", "verified"}:
            return
        with self._lock:
            self.conn.execute("UPDATE onboarding_attempts SET attempt_status=? WHERE id=?", (status, attempt_id))
            self.conn.commit()

    def replay(self, code: str, phone: str, users: Users) -> tuple[User, int] | None:
        """An already-proved sender may request status; this never establishes new proof."""
        row = self.conn.execute(
            "SELECT user_id,generation,code_hash FROM onboarding_attempts "
            "WHERE code_hash=? AND sender_hash=? AND attempt_status='verified' ORDER BY id DESC LIMIT 1",
            (_hash(code.strip().upper()), _hash(normalize_phone(phone))),
        ).fetchone()
        if row is None:
            return None
        user = users.get(row["user_id"])
        if (user is None or not user.verified_join or user.phone != normalize_phone(phone)
                or user.status not in {"pending", "approved", "active"}):
            return None
        # A newly issued code supersedes earlier proof, even if legacy joined state remains.
        if user.join_code and _generation(user) != row["generation"]:
            return None
        attempt_id = self.begin(user, phone, self.now(), generation=row["generation"], code_hash=row["code_hash"])
        self.finish(attempt_id, "verified")
        return user, attempt_id

    def record_reply(self, attempt_id: int, phone: str, *, message_id: str | None = None,
                     error_code: int | None = None, failed: bool = False) -> None:
        valid_id = message_id if isinstance(message_id, str) and 0 < len(message_id) <= 512 else None
        status = "failed" if failed or error_code is not None else "accepted"
        with self._lock:
            self.conn.execute(
                "UPDATE onboarding_attempts SET reply_status=?,reply_error_code=?,message_id=? "
                "WHERE id=? AND sender_hash=?",
                (status, _error_code(error_code), valid_id, attempt_id, _hash(normalize_phone(phone))),
            )
            self.conn.commit()

    def record_status(self, status: dict) -> None:
        """Receive a sanitized provider status only after the webhook signature check."""
        if not isinstance(status, dict):
            return
        message_id, recipient, state = status.get("id"), status.get("recipient_id"), status.get("status")
        if (not isinstance(message_id, str) or not 0 < len(message_id) <= 512
                or not isinstance(recipient, str) or not 0 < len(recipient) <= 32
                or state not in ("sent", "delivered", "read", "failed")):
            return
        recipient = normalize_phone(recipient)
        if not recipient:
            return
        with self._lock:
            row = self.conn.execute("SELECT id,reply_status FROM onboarding_attempts WHERE message_id=? AND sender_hash=?",
                                    (message_id, _hash(recipient))).fetchone()
            if row is None:
                return
            current = row["reply_status"]
            next_status = "accepted" if state == "sent" else state
            rank = {"not_sent": 0, "accepted": 1, "failed": 2, "delivered": 3, "read": 4}
            if rank[next_status] <= rank[current]:
                return
            errors = status.get("errors")
            first_error = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
            code = _error_code(first_error.get("code")) if next_status == "failed" else None
            self.conn.execute("UPDATE onboarding_attempts SET reply_status=?,reply_error_code=? WHERE id=?",
                              (next_status, code, row["id"]))
            self.conn.commit()

    def view(self, user: User) -> dict:
        result = {"attempt_status": "verified" if user.verified_join else "waiting", "attempted_at": None,
                  "reply_status": "not_sent", "reply_error_code": None}
        if user.join_code:
            row = self.conn.execute(
                "SELECT * FROM onboarding_attempts WHERE user_id=? AND generation=? "
                + ("AND attempt_status='verified' AND sender_hash=? " if user.verified_join else "")
                + "ORDER BY id DESC LIMIT 1",
                (user.id, _generation(user), *([_hash(user.phone)] if user.verified_join else [])),
            ).fetchone()
        elif user.verified_join:
            row = self.conn.execute("SELECT * FROM onboarding_attempts WHERE user_id=? "
                                    "AND attempt_status='verified' AND sender_hash=? ORDER BY id DESC LIMIT 1",
                                    (user.id, _hash(user.phone))).fetchone()
        else:
            row = None
        if row:
            result.update({key: row[key] for key in result})
        return result

    def claim_announcement(self, user: User) -> bool:
        if not user.join_code or user.verified_join:
            return False
        generation = _generation(user)
        with self._lock:
            cur = self.conn.execute("INSERT INTO onboarding_announcements(user_id,generation) VALUES (?,?) "
                                    "ON CONFLICT(user_id) DO UPDATE SET generation=excluded.generation "
                                    "WHERE onboarding_announcements.generation != excluded.generation", (user.id, generation))
            self.conn.commit()
            return bool(cur.rowcount)
