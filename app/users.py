"""People who can use the agent: Pippa (user 1, admin) plus friends who sign up (D23, D24).

Sign-up states: pending → approved → active (or rejected / disabled). "approve" (by the admin)
and "join" (from the friend's WhatsApp, proving they own the number) can happen in either
order; the user becomes active once both have.
"""

import secrets
import re
import sqlite3
import json
import threading
from collections.abc import Collection
from functools import wraps
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from app.config import normalize_phone
from app.store import Store

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I look-alikes
CODE_LENGTH = 6
CODE_LIFETIME = timedelta(minutes=30)
ADMIN_ID = 1
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
GMAIL_SCOPES = frozenset({"https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.modify", "https://mail.google.com/"})
# All Users instances in this process share this lock, including those using the same SQLite connection.
_WRITE_LOCK = threading.RLock()


def _serialized_write(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with _WRITE_LOCK:
            return fn(*args, **kwargs)
    return wrapped

USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,  -- never reuse a deleted user's id (rows keyed by it must not pass on)
  name TEXT NOT NULL,
  email TEXT UNIQUE,
  phone TEXT UNIQUE,
  typed_phone TEXT,
  status TEXT NOT NULL CHECK (status IN ('pending','approved','active','rejected','disabled')),
  joined INTEGER NOT NULL DEFAULT 0,
  join_code TEXT UNIQUE,
  join_code_expires_at TEXT,
  google_token_enc TEXT,
  google_scopes TEXT,
  calendly_url TEXT,
  is_admin INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""
COLUMNS = (
    "id, name, email, phone, typed_phone, status, joined, join_code, join_code_expires_at, "
    "google_token_enc, calendly_url, is_admin, google_scopes, google_subject, google_disconnected, "
    "calendar_status, calendar_detail, gmail_status, gmail_detail, google_checked_at, google_revocation_status, google_connection_version, event_only"
)
SEATED = ("pending", "approved", "active", "disabled")  # everyone except rejected takes a seat


class SignupError(ValueError):
    """A sign-up step that can't go ahead; the message is safe to show the person."""


class SignupsFull(SignupError):
    pass


class UnknownCode(SignupError):
    pass


@dataclass(frozen=True)
class User:
    id: int
    name: str
    email: str | None
    phone: str | None
    typed_phone: str | None
    status: str
    joined: bool
    join_code: str | None
    join_code_expires_at: str | None
    google_token_enc: str | None
    calendly_url: str | None
    is_admin: bool
    google_scopes: tuple[str, ...] | None = None

    google_subject: str | None = None
    google_disconnected: bool = False
    calendar_status: str = "unknown"
    calendar_detail: str = "Access has not been checked yet."
    gmail_status: str = "unknown"
    gmail_detail: str = "Access has not been checked yet."
    google_checked_at: str | None = None
    google_revocation_status: str | None = None
    google_connection_version: int = 0
    event_only: bool = False

    @property
    def first_name(self) -> str:
        return self.name.split()[0] if self.name.strip() else "there"

    @property
    def calendar_authorized(self) -> bool | None:
        return False if self.google_disconnected else (None if self.google_scopes is None else CALENDAR_SCOPE in self.google_scopes)

    @property
    def verified_join(self) -> bool:
        return bool(self.joined and self.typed_phone and self.phone == self.typed_phone)

    @property
    def calendar_ready(self) -> bool:
        return bool(self.google_token_enc and self.calendar_authorized is True and self.calendar_status == "connected")

    @property
    def gmail_authorized(self) -> bool | None:
        return False if self.google_disconnected else (None if self.google_scopes is None else bool(GMAIL_SCOPES.intersection(self.google_scopes)))


def _row(row: sqlite3.Row | None) -> User | None:
    if row is None:
        return None
    data = dict(row)
    data["joined"] = bool(data["joined"])
    data["is_admin"] = bool(data["is_admin"])
    data["event_only"] = bool(data["event_only"])
    data["google_scopes"] = tuple(json.loads(data["google_scopes"])) if data["google_scopes"] is not None else None
    return User(**data)


def _clean_code(code: str) -> str:
    return code.strip().upper()


def canonical_signup_phone(phone: str, country_code: str = "") -> str:
    """Validate explicit signup input; never guess a missing country prefix."""
    raw = phone.strip(" ")
    if not re.fullmatch(r"\+?[0-9 ()-]+", raw):
        raise SignupError("Use digits and a country code, e.g. +65 8123 4567.")
    digits = re.sub(r"[ ()-]", "", raw)
    if digits.startswith("+"):
        digits = digits[1:]
    elif country_code:
        prefix = country_code.strip(" ")
        if not re.fullmatch(r"\+[1-9][0-9]{0,2}", prefix):
            raise SignupError("Enter a country code beginning with +, e.g. +65 for Singapore.")
        digits = prefix[1:] + digits
    if not re.fullmatch(r"[1-9][0-9]{9,14}", digits):
        raise SignupError("Include the country code, e.g. select +65 and enter 8123 4567.")
    if digits.startswith("65") and len(digits) != 10:
        raise SignupError("Singapore numbers need country code +65 followed by exactly 8 digits.")
    return digits


class Users:
    def __init__(
        self, store: Store, max_users: int = 5,
        allowed_emails: Collection[str] | None = None, auto_activate: bool = False,
    ):
        self.store = store
        self.conn = store.conn
        self.max_users = max_users
        self.allowed_emails = frozenset(email.strip().lower() for email in (allowed_emails or ()) if email.strip())
        self.auto_activate = auto_activate
        if auto_activate and not self.allowed_emails:
            raise ValueError("Automatic activation requires a nonempty email allowlist.")
        self.conn.executescript(USERS_SCHEMA)
        if "google_scopes" not in {row[1] for row in self.conn.execute("PRAGMA table_info(users)")}:
            self.conn.execute("ALTER TABLE users ADD COLUMN google_scopes TEXT")
        additions = {
            "event_only": "INTEGER NOT NULL DEFAULT 0",
            "google_connection_version": "INTEGER NOT NULL DEFAULT 0",
            "google_subject": "TEXT", "google_disconnected": "INTEGER NOT NULL DEFAULT 0",
            "calendar_status": "TEXT NOT NULL DEFAULT 'unknown'",
            "calendar_detail": "TEXT NOT NULL DEFAULT 'Access has not been checked yet.'",
            "gmail_status": "TEXT NOT NULL DEFAULT 'unknown'",
            "gmail_detail": "TEXT NOT NULL DEFAULT 'Access has not been checked yet.'",
            "google_checked_at": "TEXT", "google_revocation_status": "TEXT",
        }
        existing_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(users)")}
        for column, definition in additions.items():
            if column not in existing_columns:
                self.conn.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_google_subject ON users(google_subject)")
        self.conn.commit()

    def is_eligible(self, email: str) -> bool:
        return not self.auto_activate or email.strip().lower() in self.allowed_emails

    def _check_automatic_eligibility(self, email: str, scopes: Collection[str] | None) -> None:
        if not self.auto_activate:
            return
        if not self.is_eligible(email):
            raise SignupError("This email is not in the current early-adopter group.")
        if scopes is None or CALENDAR_SCOPE not in scopes:
            raise SignupError("Google Calendar permission is required. Reconnect Google and allow Calendar access.")

    # --- reading ------------------------------------------------------------
    def get(self, user_id: int) -> User | None:
        return _row(self.conn.execute(f"SELECT {COLUMNS} FROM users WHERE id = ?", (user_id,)).fetchone())

    def by_phone(self, phone: str) -> User | None:
        digits = normalize_phone(phone)
        return _row(self.conn.execute(f"SELECT {COLUMNS} FROM users WHERE phone = ?", (digits,)).fetchone())

    def by_typed_phone(self, phone: str) -> User | None:
        """A sign-up that typed this number but hasn't sent its join code yet."""
        digits = normalize_phone(phone)
        return _row(
            self.conn.execute(
                f"SELECT {COLUMNS} FROM users WHERE typed_phone = ? AND joined = 0 ORDER BY id DESC", (digits,)
            ).fetchone()
        )

    def by_email(self, email: str) -> User | None:
        return _row(
            self.conn.execute(f"SELECT {COLUMNS} FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
        )

    def by_code(self, code: str) -> User | None:
        return _row(
            self.conn.execute(f"SELECT {COLUMNS} FROM users WHERE join_code = ?", (_clean_code(code),)).fetchone()
        )

    def list(self) -> list[User]:
        return [_row(r) for r in self.conn.execute(f"SELECT {COLUMNS} FROM users ORDER BY id").fetchall()]

    def seats_left(self) -> int:
        taken = self.conn.execute(
            f"SELECT COUNT(*) FROM users WHERE event_only = 0 AND status IN ({','.join('?' * len(SEATED))})", SEATED
        ).fetchone()[0]
        return max(0, self.max_users - taken)

    def check_event_upgrade(self, user: User) -> None:
        """Event admission never grants a seat in the assistant beta implicitly."""
        if not user.event_only:
            return
        if (user.is_admin or user.status in ("disabled", "rejected")
                or (user.email or "").strip().lower() not in self.allowed_emails):
            raise SignupError("Assistant access requires an invitation to the beta. Your event profile remains available.")
        if self.seats_left() <= 0:
            raise SignupsFull("Assistant sign-ups are full. Your event profile remains available.")

    # --- writing ------------------------------------------------------------
    @_serialized_write
    def _update(self, user_id: int, now: datetime, **fields) -> User:
        current = self.get(user_id)
        if current and current.event_only and (
            fields.get("status") in ("approved", "active") or fields.get("google_token_enc")
            or fields.get("joined") or fields.get("phone") or fields.get("event_only") == 0
        ):
            self.check_event_upgrade(current)
            fields["event_only"] = 0
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE users SET {sets}, updated_at = ? WHERE id = ?", (*fields.values(), now.isoformat(), user_id)
        )
        self.conn.commit()
        user = self.get(user_id)
        assert user is not None
        return user

    @_serialized_write
    def ensure_admin(self, name: str, phone: str, now: datetime) -> User:
        """Pippa is user 1, active and admin. Runs at every startup from his settings."""
        stamp = now.isoformat()
        self.conn.execute(
            "INSERT INTO users (id, name, phone, status, joined, is_admin, created_at, updated_at) "
            "VALUES (?, ?, ?, 'active', 1, 1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, phone = excluded.phone, "
            "status = 'active', joined = 1, is_admin = 1, updated_at = excluded.updated_at",
            (ADMIN_ID, name, normalize_phone(phone), stamp, stamp),
        )
        self.conn.commit()
        return self.get(ADMIN_ID)

    @_serialized_write
    def start_signup(
        self, name: str, email: str, token_enc: str, now: datetime,
        granted_scopes: Collection[str] | None = None,
    ) -> tuple[User, bool]:
        """After Google sign-in. Returns (user, is_new). The same email again just gets the new token."""
        email = email.strip().lower()
        scopes = tuple(sorted(set(granted_scopes))) if granted_scopes is not None else None
        self._check_automatic_eligibility(email, scopes)
        encoded_scopes = json.dumps(scopes) if scopes is not None else None
        existing = self.by_email(email)
        if existing:
            if existing.google_subject or existing.google_disconnected:
                raise SignupError("Manage this account through Connections. Start a fresh sign-in.")
            if existing.status in ("disabled", "rejected"):
                raise SignupError("This account is not enabled. Contact the organizer before signing in again.")
            status = existing.status
            activation_fields = {}
            verified_join = existing.joined and bool(existing.typed_phone) and existing.phone == existing.typed_phone
            if status in ("pending", "approved") and existing.joined and not verified_join:
                # Older versions accepted a join from a different phone. Only a fresh
                # Google sign-in may reset that unverified link; require new phone proof.
                activation_fields = {"phone": None, "joined": 0, "join_code": None, "join_code_expires_at": None}
            if self.auto_activate and status in ("pending", "approved"):
                status = "active" if verified_join else "approved"
                if verified_join:
                    activation_fields = {"join_code": None, "join_code_expires_at": None}
            return self._update(existing.id, now, google_token_enc=token_enc, google_scopes=encoded_scopes,
                                name=name.strip() or existing.name, status=status, **activation_fields), False
        if self.seats_left() <= 0:
            raise SignupsFull("Sign-ups are full for now.")
        stamp = now.isoformat()
        cur = self.conn.execute(
            "INSERT INTO users (name, email, status, google_token_enc, google_scopes, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name.strip() or email.split("@")[0], email, "approved" if self.auto_activate else "pending",
             token_enc, encoded_scopes, stamp, stamp),
        )
        self.conn.commit()
        return self.get(cur.lastrowid), True

    @_serialized_write
    def set_details(self, user_id: int, typed_phone: str, calendly_url: str | None, now: datetime, *, country_code: str = "") -> User:
        """Save an exact international number, retaining its unexpired proof code."""
        user = self.get(user_id)
        if user is None or user.status in ("disabled", "rejected"):
            raise SignupError("This account is not enabled.")
        recoverable_join = (user.google_subject and user.status in ("pending", "approved")
                            and user.joined and not user.verified_join)
        if (user.joined and not recoverable_join) or user.status == "active":
            raise SignupError("This account is already linked to WhatsApp. Contact the organizer to change its number.")
        if user.google_subject and not user.calendar_ready:
            raise SignupError("Connect and check Google Calendar before setting up WhatsApp.")
        digits = canonical_signup_phone(typed_phone, country_code)
        taken = self.conn.execute(
            "SELECT id FROM users WHERE id != ? AND status != 'rejected' AND (phone = ? OR typed_phone = ?)",
            (user_id, digits, digits),
        ).fetchone()
        if taken:
            raise SignupError("That number is already signed up.")
        link = (calendly_url or "").strip() or None
        if link and not link.startswith("https://"):
            raise SignupError("The booking link should start with https://")
        reuse_code = bool(not recoverable_join and user.typed_phone == digits and user.join_code
                          and user.join_code_expires_at and now < datetime.fromisoformat(user.join_code_expires_at))
        return self._update(
            user_id,
            now,
            **({"phone": None, "joined": 0} if recoverable_join else {}),
            typed_phone=digits,
            calendly_url=link,
            join_code=user.join_code if reuse_code else self._new_code(),
            join_code_expires_at=user.join_code_expires_at if reuse_code else (now + CODE_LIFETIME).isoformat(),
        )

    def _new_code(self) -> str:
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            if self.by_code(code) is None:
                return code

    def _ready(self, user: User, now: datetime) -> User:
        """Both approved and joined → active; the code has done its job."""
        if user.status == "approved" and user.joined:
            if not user.typed_phone or user.phone != user.typed_phone:
                raise SignupError("The joined WhatsApp number does not match the entered number. Contact the organizer.")
            self._check_automatic_eligibility(user.email or "", user.google_scopes)
            return self._update(user.id, now, status="active", join_code=None, join_code_expires_at=None)
        return user

    @_serialized_write
    def approve(self, code: str, now: datetime) -> User:
        user = self.by_code(code)
        if user is None:
            raise UnknownCode("No sign-up has that code.")
        if user.status not in ("pending", "approved"):
            return user
        # Validate before committing any status change, including legacy joined sign-ups.
        self._check_automatic_eligibility(user.email or "", user.google_scopes)
        if user.joined:
            if not user.typed_phone or user.phone != user.typed_phone:
                raise SignupError("The joined WhatsApp number does not match the entered number. Contact the organizer.")
            return self._update(user.id, now, status="active", join_code=None, join_code_expires_at=None)
        return self._update(user.id, now, status="approved")

    @_serialized_write
    def approve_beta(self, user_id: int, now: datetime) -> User:
        """Approve an invited beta account without inventing Calendar or phone proof."""
        user = self.get(user_id)
        if (user is None or user.is_admin or user.status not in ("pending", "approved", "active")
                or (user.email or "").strip().lower() not in self.allowed_emails):
            raise SignupError("This account is not eligible for beta approval.")
        if user.status == "active":
            return user
        ready = user.verified_join and user.calendar_ready
        return self._update(user.id, now, status="active" if ready else "approved",
                            **({"join_code": None, "join_code_expires_at": None} if ready else {}))

    @_serialized_write
    def reject(self, code: str, now: datetime) -> User:
        user = self.by_code(code)
        if user is None:
            raise UnknownCode("No sign-up has that code.")
        # The code stays, so a later "join" gets "not approved" rather than "code isn't right".
        return self._update(user.id, now, status="rejected", phone=None)

    @_serialized_write
    def join(self, code: str, phone: str, now: datetime) -> User:
        """A `join CODE` message: links the sender's number to the sign-up."""
        user = self.by_code(code)
        if user is None:
            raise UnknownCode("That code isn't right. Check it on the sign-up page.")
        if user.status in ("rejected", "disabled"):
            raise SignupError("This sign-up was not approved.")
        if user.joined:
            raise SignupError("That code has already been used.")
        if user.join_code_expires_at and now >= datetime.fromisoformat(user.join_code_expires_at):
            raise SignupError("That code has expired. Sign up again to get a new one.")
        digits = normalize_phone(phone)
        if not user.typed_phone or digits != user.typed_phone:
            raise SignupError("Send this code from the exact WhatsApp number entered on the sign-up page.")
        self._check_automatic_eligibility(user.email or "", user.google_scopes)
        if user.google_subject and not user.calendar_ready:
            raise SignupError("Connect and check Google Calendar before setting up WhatsApp.")
        other = self.by_phone(digits)
        if other and other.id != user.id:
            raise SignupError("This WhatsApp number is already linked to another account.")
        user = self._update(user.id, now, phone=digits, joined=1)
        return self._ready(user, now)

    @_serialized_write
    def sign_in_identity(self, subject: str, name: str, email: str, now: datetime) -> tuple[User, bool]:
        """Bind a verified Google identity without modifying integration tokens or grants."""
        email = email.strip().lower()
        if not subject or not email:
            raise SignupError("A verified Google identity is required.")
        if not self.is_eligible(email):
            raise SignupError("This email is not in the current early-adopter group.")
        existing = _row(self.conn.execute(f"SELECT {COLUMNS} FROM users WHERE google_subject = ?", (subject,)).fetchone())
        existing = existing or self.by_email(email)
        if existing:
            if existing.is_admin or existing.status in ("disabled", "rejected"):
                raise SignupError("This account is not enabled for web sign-in. Contact the organizer.")
            if existing.google_subject and existing.google_subject != subject:
                raise SignupError("This email is already bound to a different Google identity.")
            # Stable subject is authoritative; do not reassign an existing email identity.
            if existing.email != email:
                raise SignupError("Your Google email has changed. Contact the organizer to update your account.")
            status = "approved" if self.auto_activate and existing.status == "pending" else existing.status
            return self._update(existing.id, now, google_subject=subject, status=status,
                                **({"event_only": 0} if existing.event_only else {})), False
        if self.seats_left() <= 0:
            raise SignupsFull("Sign-ups are full for now.")
        stamp = now.isoformat()
        cur = self.conn.execute(
            "INSERT INTO users (name,email,status,google_subject,google_scopes,calendar_status,gmail_status,created_at,updated_at) "
            "VALUES (?,?,?,?, '[]','not_connected','not_connected',?,?)",
            (name.strip() or email.split("@")[0], email, "approved" if self.auto_activate else "pending", subject, stamp, stamp),
        )
        self.conn.commit()
        return self.get(cur.lastrowid), True

    @_serialized_write
    def sign_in_event_identity(self, subject: str, name: str, email: str, now: datetime,
                               capacity: int = 150) -> tuple[User, bool]:
        """Bind identity after the caller verifies checked-in roster eligibility.

        This does not claim membership or opt anyone into a directory. Existing
        accounts retain their entitlement and integration state.
        """
        email = email.strip().lower()
        if not subject or not email:
            raise SignupError("A verified Google identity is required.")
        # Admission has its own transaction boundary: OAuth/session commits on
        # self.conn must not publish a half-completed admission. BEGIN IMMEDIATE
        # also serializes the capacity check across independent app processes.
        conn = self.store.new_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = _row(conn.execute(
                f"SELECT {COLUMNS} FROM users WHERE google_subject = ?", (subject,)).fetchone())
            existing = existing or _row(conn.execute(
                f"SELECT {COLUMNS} FROM users WHERE email = ?", (email,)).fetchone())
            if existing:
                if existing.is_admin or existing.status in ("disabled", "rejected"):
                    raise SignupError("This account is not enabled for web sign-in. Contact the organizer.")
                if existing.google_subject and existing.google_subject != subject:
                    raise SignupError("This email is already bound to a different Google identity.")
                if existing.email != email:
                    raise SignupError("Your Google email has changed. Contact the organizer to update your account.")
                conn.execute("UPDATE users SET google_subject=?,updated_at=? WHERE id=?",
                             (subject, now.isoformat(), existing.id))
                user_id, is_new = existing.id, False
            else:
                count = conn.execute("SELECT COUNT(*) FROM users WHERE event_only=1").fetchone()[0]
                if count >= min(capacity, 150):
                    raise SignupsFull("Event profile capacity has been reached. Contact the organizer.")
                stamp = now.isoformat()
                cur = conn.execute(
                    "INSERT INTO users (name,email,status,google_subject,google_scopes,calendar_status,gmail_status,"
                    "event_only,created_at,updated_at) VALUES (?,?,'pending',?,'[]','not_connected','not_connected',1,?,?)",
                    (name.strip() or "Attendee", email, subject, stamp, stamp),
                )
                user_id, is_new = cur.lastrowid, True
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.get(user_id), is_new

    def set_booking(self, user_id: int, value: str, now: datetime) -> User:
        link = value.strip()
        if link:
            try:
                parsed = urlsplit(link)
                valid = (parsed.scheme == "https" and parsed.netloc.lower() == "calendly.com"
                         and bool(parsed.path.strip("/")) and not any(c.isspace() for c in link)
                         and "\\" not in link)
            except ValueError:
                valid = False
            if not valid:
                raise SignupError("Use an HTTPS calendly.com booking URL with a booking path.")
        return self._update(user_id, now, calendly_url=link or None)

    @_serialized_write
    def commit_connection(self, user_id: int, version: int, now: datetime, **fields) -> User:
        user = self.get(user_id)
        if not user or user.status in ("disabled", "rejected") or user.google_connection_version != version:
            raise SignupError("This connection changed while Google was checking access. Please try again.")
        if user.google_revocation_status == "pending":
            raise SignupError("Google revocation is still in progress. Try reconnecting shortly. If this persists, contact the organizer and review Pippa in your Google account permissions.")
        return self._update(user_id, now, google_connection_version=version + 1, **fields)

    @_serialized_write
    def disconnect_google(self, user_id: int, now: datetime) -> User:
        user = self.get(user_id)
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "connection_oauth" in tables:
            self.conn.execute("DELETE FROM connection_oauth WHERE user_id=?", (user_id,))
        return self._update(user_id, now, google_connection_version=user.google_connection_version + 1, google_token_enc=None, google_scopes="[]", google_disconnected=1,
                            calendar_status="not_connected", gmail_status="not_connected",
                            calendar_detail="Disconnected locally.", gmail_detail="Disconnected locally.",
                            google_checked_at=now.isoformat(), google_revocation_status="pending")

    @_serialized_write
    def finish_google_disconnect(self, user_id: int, version: int, result: str, now: datetime) -> None:
        user = self.get(user_id)
        if user and user.google_connection_version == version and user.google_revocation_status == "pending":
            self._update(user_id, now, google_revocation_status=result)

    def clear_token(self, user_id: int, now: datetime) -> None:
        self.disconnect_google(user_id, now)

    def _cancel_open_tasks(self, user_id: int, now: datetime) -> None:
        self.conn.execute(
            "UPDATE loop_in_tasks SET status = 'cancelled', updated_at = ? "
            "WHERE user_id = ? AND status IN ('waiting', 'negotiating', 'nudged')",
            (now.isoformat(), user_id),
        )
        self.conn.commit()

    @_serialized_write
    def disable(self, last4: str, now: datetime) -> User:
        matches = [u for u in self.list() if u.phone and u.phone.endswith(last4.strip()) and u.status == "active"]
        if len(matches) != 1:
            raise SignupError(f"No single active user's number ends in {last4.strip()}.")
        if matches[0].is_admin:
            raise SignupError("The admin can't be disabled.")
        self._cancel_open_tasks(matches[0].id, now)  # the bot stops acting for them
        return self._update(matches[0].id, now, status="disabled")

    @_serialized_write
    def delete(self, user_id: int) -> None:
        """Remove the user and every row that belongs to them. Google revocation is done by the caller."""
        user = self.get(user_id)
        if user is None:
            return
        if user.is_admin:
            raise SignupError("The admin account can't be deleted this way.")
        c = self.conn
        c.execute("DELETE FROM loop_in_tasks WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM contacts WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM profile_facts WHERE user_id = ?", (user_id,))
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("connection_sessions", "connection_oauth", "onboarding_attempts", "onboarding_announcements",
                      "capture_items", "capture_sessions", "capture_receipts", "capture_notifications",
                      "proactive_goals", "proactive_briefs", "proactive_attempts", "proactive_agendas", "followups"):
            if table in tables:
                c.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        if "agent_messages" in tables and "agent_sessions" in tables:
            # Their own chat (session id = phone) and their Delegates' chats ("<user_id>:<phone>").
            ids = [user.phone] if user.phone else []
            ids += [r[0] for r in c.execute("SELECT session_id FROM agent_sessions WHERE session_id LIKE ?", (f"{user_id}:%",))]
            for sid in ids:
                c.execute("DELETE FROM agent_messages WHERE session_id = ?", (sid,))
                c.execute("DELETE FROM agent_sessions WHERE session_id = ?", (sid,))
        for phone in {p for p in (user.phone, user.typed_phone) if p}:
            c.execute("DELETE FROM owner_notices WHERE phone = ?", (phone,))
            c.execute("DELETE FROM unknown_replies WHERE phone = ?", (phone,))
        c.execute("DELETE FROM users WHERE id = ?", (user_id,))
        c.commit()
