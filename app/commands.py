"""Commands handled by plain code before any AI runs (D24).

- `join CODE` from any number: links that number to a sign-up.
- `approve CODE`, `reject CODE`, `users`, `disable 1234`: the admin (Pippa) only.
- `delete my data`: an active user, confirmed by a plain "yes" as the very next message.

`Commands.handle` returns the reply to send, or None when the message isn't a command.
"""

import logging
import re
from collections.abc import Callable
from datetime import datetime

from app.config import normalize_phone
from app.onboarding import OnboardingReceipts
from app.router import tail
from app.tools.whatsapp import WhatsAppError
from app.users import SignupError, User, Users

log = logging.getLogger(__name__)

WELCOME = (
    "You're in! 🎉 Ask about your calendar or free time, or ask me to schedule a meeting. "
    "You can send voice notes too. Try: \"what's my next meeting?\" Send help or status any time."
)
WAITING = "Got it! {admin} still needs to approve you. I'll message you here when you're in."
DELETE_QUESTION = (
    "This deletes your account, saved facts, contacts, invites and chat history, and removes this bot's "
    "access to your Google Calendar. Reply yes to delete, or anything else to keep everything."
)
DELETED = "Done. Everything is deleted and Google access is revoked. Thanks for testing! 👋"
DELETED_NO_REVOKE = (
    "Done. Everything is deleted. I couldn't reach Google to revoke access, so please remove "
    "\"Pippa\" at https://myaccount.google.com/permissions."
)
KEPT = "OK, nothing was deleted."

JOIN = re.compile(r"^join\s+([A-Za-z0-9]{4}|[A-Za-z0-9]{6})$", re.IGNORECASE)
APPROVE = re.compile(r"^(approve|reject)\s+([A-Za-z0-9]{4}|[A-Za-z0-9]{6})$", re.IGNORECASE)
DISABLE = re.compile(r"^disable\s+(\d{4})$", re.IGNORECASE)
DELETE = re.compile(r"^delete my data[.!]?$", re.IGNORECASE)


def _who(user: User) -> str:
    return f"{user.name} (…{user.phone[-4:]})" if user.phone else user.name


class Commands:
    def __init__(
        self,
        users: Users,
        sender,
        now: Callable[[], datetime],
        decrypt: Callable[[str], str],
        revoke: Callable[[str], None],
        on_user_removed: Callable[[int], None] = lambda user_id: None,
        base_url: str = "",
        test_number_mode: bool = True,
        receipts: OnboardingReceipts | None = None,
        proactive_command: Callable[[str, str], str | None] | None = None,
    ):
        self.users = users
        self.sender = sender
        self.now = now
        self.decrypt = decrypt
        self.revoke = revoke
        self.on_user_removed = on_user_removed
        self.base_url = base_url.rstrip("/")
        self.test_number_mode = test_number_mode
        self.receipts = receipts or OnboardingReceipts(users.conn, now)
        self.proactive_command = proactive_command
        self._join_attempts: dict[str, int] = {}
        self._confirming_delete: dict[str, int] = {}  # phone asked "Reply yes to delete" -> user id

    @property
    def admin(self) -> User:
        admin = self.users.get(1)
        assert admin is not None
        return admin

    def handle(self, phone: str, text: str | None) -> str | None:
        phone = normalize_phone(phone)
        text = (text or "").strip()
        if phone in self._confirming_delete:
            user_id = self._confirming_delete.pop(phone)
            return self._delete(user_id) if text.lower() in ("yes", "yes.", "yes!") else KEPT
        if not text:
            return None
        if text.lower() in ('meeting briefs', 'meeting briefs on', 'meeting briefs off', 'meeting briefs status'):
            if self.proactive_command is None:
                return 'Meeting briefs are not enabled in this deployment.'
            return self.proactive_command(phone, text)
        if m := JOIN.match(text):
            return self._join(phone, m.group(1))
        if phone == self.admin.phone:
            if m := APPROVE.match(text):
                return self._approve(m.group(2)) if m.group(1).lower() == "approve" else self._reject(m.group(2))
            if text.lower() == "users":
                return self._list()
            if m := DISABLE.match(text):
                return self._disable(m.group(1))
        if DELETE.match(text):
            return self._ask_delete(phone)
        if text.lower() in ("help", "status"):
            user = self.users.by_phone(phone) or self.users.by_typed_phone(phone)
            if user is None:
                return None
            if text.lower() == "status" and user.verified_join and user.phone == phone:
                # User-initiated delivery retry after Meta recipient setup; retain existing proof.
                attempt_id = self.receipts.begin(user, phone, self.now())
                self.receipts.finish(attempt_id, "verified")
                self._join_attempts[phone] = attempt_id
            return self._help(user) if text.lower() == "help" else self._status(user)
        return None

    # --- helpers --------------------------------------------------------------
    def tell(self, phone: str, body: str) -> None:
        try:
            self.sender.send_text(phone, body)
        except WhatsAppError:
            log.exception("Could not message %s", tail(phone))

    def tell_admin(self, body: str) -> None:
        self.tell(self.admin.phone, body)

    def announce_signup(self, user: User) -> None:
        """Called by the sign-up page when someone finishes it."""
        if not self.receipts.claim_announcement(user):
            return
        setup = " Meta test-number recipient setup is still required: add the number in Meta → WhatsApp → API Setup." if self.test_number_mode else ""
        if self.users.auto_activate:
            next_step = "Eligible for automatic activation after number verification."
        else:
            next_step = f"Reply: approve {user.join_code}"
        self.tell_admin(
            f"🆕 Setup submitted: {user.name} ({user.email}, …{(user.typed_phone or '????')[-4:]}), "
            "awaiting WhatsApp number verification. "
            f"{next_step}{setup}"
        )

    def _gmail_status(self, user: User) -> str:
        if user.gmail_authorized is True and user.google_token_enc:
            return "Gmail: connected for read-only email lookup (permission recorded at sign-in)."
        state = "optional, not connected" if user.gmail_authorized is False else "permission not yet verified"
        if not user.google_token_enc and not user.is_admin:
            state = "not connected"
        link = f"Connect optional read-only Gmail: {self.base_url}/signup/gmail" if self.base_url else "Ask the organizer for the optional Gmail connection link."
        return f"Gmail: {state}. {link}"

    def _status(self, user: User) -> str:
        if user.calendar_authorized is True and user.google_token_enc:
            calendar = "authorized at sign-in"
        elif user.calendar_authorized is False:
            calendar = "not authorized"
        elif user.is_admin:
            calendar = "managed by the organizer's configuration; live access not checked"
        else:
            calendar = "permission not yet verified" if user.google_token_enc else "not connected"
        phone = "verified" if user.joined else "awaiting join from your entered number"
        return f"Account: {user.status}. WhatsApp number: {phone}. Google Calendar: {calendar}. {self._gmail_status(user)}"

    def _help(self, user: User) -> str:
        if user.status in ("disabled", "rejected"):
            return f"Your account is {user.status}. Contact the organizer before using the assistant again. Send status to check your account."
        if user.status != "active":
            return self._status(user) + " Finish the signup and WhatsApp join steps before using the assistant."
        return (
            "Ask about your calendar, find free time, create or change an event, or arrange a meeting with a saved contact. "
            "Voice notes work too. Invitations require recipient confirmation. "
            + ('Send agenda for today’s timeline, meeting 1 for details, or meeting briefs on/off/status. ' if self.proactive_command else '')
            + self._status(user)
            + " Send status to check your account, or delete my data to remove your account."
        )

    def _welcome(self, user: User) -> str:
        setup = " This uses Meta's test number; recipient setup must remain enabled." if self.test_number_mode else ""
        return f"{WELCOME} {self._status(user)}{setup}"

    # --- commands -------------------------------------------------------------
    def take_join_attempt(self, phone: str) -> int | None:
        """Router consumes the receipt while it still holds this sender's command lock."""
        return self._join_attempts.pop(normalize_phone(phone), None)

    def _join(self, phone: str, code: str) -> str:
        self._join_attempts.pop(phone, None)
        if replay := self.receipts.replay(code, phone, self.users):
            user, attempt_id = replay
            self._join_attempts[phone] = attempt_id
            return self._status(user)
        before = self.users.by_code(code)
        # Never associate an unproved stranger's later attempt with a verified account.
        track = before is not None and not before.verified_join and before.status in ("pending", "approved")
        now = self.now()
        attempt_id = self.receipts.begin(before, phone, now) if track else None
        if attempt_id is not None:
            self._join_attempts[phone] = attempt_id
        try:
            user = self.users.join(code, phone, now)
        except SignupError as exc:
            if attempt_id is not None:
                expired = before.join_code_expires_at and now >= datetime.fromisoformat(before.join_code_expires_at)
                reason = "code_expired" if expired else ("number_mismatch" if before.typed_phone != phone else "waiting")
                self.receipts.finish(attempt_id, reason)
            return str(exc)
        if attempt_id is not None:
            self.receipts.finish(attempt_id, "verified")
        note = f" (they typed …{user.typed_phone[-4:]})" if user.typed_phone and user.typed_phone != phone else ""
        if user.status == "active":
            self.tell_admin(f"✅ {_who(user)} joined and is now active.{note}")
            return self._welcome(user)
        self.tell_admin(f"📨 {_who(user)} sent their join code{note}. Reply: approve {user.join_code}")
        return WAITING.format(admin=self.admin.first_name)

    def _approve(self, code: str) -> str:
        try:
            user = self.users.approve(code, self.now())
        except SignupError as exc:
            return str(exc)
        if user.status == "active":
            self.tell(user.phone, self._welcome(user))
            return f"Approved {_who(user)}. They're in, and I've welcomed them."
        return f"Approved {user.name}. They'll be in once they send \"join {user.join_code}\" from WhatsApp."

    def _reject(self, code: str) -> str:
        try:
            user = self.users.reject(code, self.now())
        except SignupError as exc:
            return str(exc)
        self._revoke(user)  # a rejected sign-up keeps no calendar access
        self.users.clear_token(user.id, self.now())
        self.on_user_removed(user.id)
        return f"Rejected {user.name}."

    def _revoke(self, user: User) -> bool:
        if not user.google_token_enc:
            return True
        try:
            self.revoke(self.decrypt(user.google_token_enc))
            return True
        except Exception:  # noqa: BLE001 - leaving must not depend on Google being reachable
            log.exception("Could not revoke Google access for user #%d", user.id)
            return False

    def _list(self) -> str:
        lines = []
        for u in self.users.list():
            number = f"…{u.phone[-4:]}" if u.phone else f"typed …{(u.typed_phone or '????')[-4:]}"
            extra = f", code {u.join_code}" if u.join_code and u.status in ("pending", "approved") else ""
            lines.append(f"#{u.id} {u.name} ({number}): {u.status}{', admin' if u.is_admin else ''}{extra}")
        return "Users:\n" + "\n".join(lines) + f"\nSeats left: {self.users.seats_left()}"

    def _disable(self, last4: str) -> str:
        try:
            user = self.users.disable(last4, self.now())
        except SignupError as exc:
            return str(exc)
        self.on_user_removed(user.id)
        return f"Disabled {_who(user)}."

    def _ask_delete(self, phone: str) -> str | None:
        # Anyone the bot knows can leave, whatever their status (a pending sign-up is known by the number they typed).
        user = self.users.by_phone(phone) or self.users.by_typed_phone(phone)
        if user is None:
            return None
        if user.is_admin:
            return "The admin account can't be deleted this way."
        self._confirming_delete[phone] = user.id
        return DELETE_QUESTION

    def _delete(self, user_id: int) -> str:
        user = self.users.get(user_id)
        if user is None:
            return KEPT
        revoked = self._revoke(user)
        self.users.delete(user.id)
        self.on_user_removed(user.id)
        self.tell_admin(f"👋 {_who(user)} deleted their data and left.")
        return DELETED if revoked else DELETED_NO_REVOKE
