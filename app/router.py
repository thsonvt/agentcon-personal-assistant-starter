"""Decide who sent a message and what happens next. No HTTP, no model code."""

import asyncio
import logging
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from app.config import normalize_phone
from app.store import Contact, LoopInTask, Store
from app.tools.whatsapp import WhatsAppError, WindowClosedError

log = logging.getLogger(__name__)

UNKNOWN_REPLY = "This is a private assistant."
RATE_LIMIT_NOTICE = "You're sending messages too quickly. Please wait a bit and try again."
ERROR_REPLY = "Sorry, something went wrong on my side. Please try again in a minute."
NON_TEXT_REPLY = "I can read text and voice messages, but not photos, files or stickers yet."
EMPTY_REPLY = "Sorry, I didn't manage to answer that. Could you rephrase?"
CONTACT_ERROR_REPLY = "Sorry, something went wrong on my side. I'll pass that on."
PENDING_REPLY = "Thanks for signing up! {admin} still needs to approve you. I'll message you here when you're in."
JOIN_HINT = "Welcome! To finish signing up, send the six-character code from your setup page, like: join 7Q4K8M"
CLOSED_TASK_REPLY = "Thanks! I've passed your message on to {owner}."
CLOSED_TASK_GRACE = timedelta(days=7)  # a contact writing within a week of their booking isn't a stranger
VOICE_ERROR_REPLY = "Sorry, I couldn't process that voice note. Could you type it instead?"
VOICE_EMPTY_REPLY = "I couldn't make out any words in that voice note. Could you try again or type it?"
HEARD_LIMIT = 200  # the "🎙️ Heard:" echo stays one short line


def heard_line(transcript: str) -> str:
    """The transcript on one line, clipped, for the echo above the reply."""
    one_line = " ".join(transcript.split())
    return one_line if len(one_line) <= HEARD_LIMIT else one_line[: HEARD_LIMIT - 1] + "…"


def tail(phone: str) -> str:
    """Last 4 digits only, so logs never hold full phone numbers."""
    return f"…{phone[-4:]}"


class PartialRunError(Exception):
    """The run failed after changing the calendar; `writes` lists what already happened."""

    def __init__(self, writes: list[str]):
        super().__init__("run failed after writing to the calendar")
        self.writes = writes


def partial_run_reply(writes: list[str]) -> str:
    done = "\n".join(f"- {w}" for w in writes)
    return (
        f"I did this:\n{done}\n\nBut something went wrong after that, so I couldn't finish. "
        "Please check your calendar before asking me again."
    )


@dataclass(frozen=True)
class IncomingMessage:
    sender: str
    text: str | None  # None for voice notes, images, stickers, etc.
    message_id: str
    audio_id: str | None = None  # WhatsApp media id of a voice note or audio file
    image_id: str | None = None
    caption: str | None = None
    source_timestamp: str | None = None
    kind: str = 'text'


class Sender(Protocol):
    def send_text(self, to: str, body: str) -> str: ...

    def send_template(self, to: str, name: str, params: list[str], language: str = "en") -> str: ...
    def mark_read_typing(self, message_id: str) -> None: ...


class RateLimiter:
    def __init__(self, limit: int = 30, window: timedelta = timedelta(hours=1)):
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[datetime]] = defaultdict(deque)
        self._notified: dict[str, datetime] = {}

    def allow(self, sender: str, now: datetime) -> bool:
        hits = self._hits[sender]
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True

    def should_notify(self, sender: str, now: datetime) -> bool:
        last = self._notified.get(sender)
        if last is not None and now - last < self.window:
            return False
        self._notified[sender] = now
        return True


class Router:
    def __init__(
        self,
        owner_phone: str,
        store: Store,
        sender: Sender,
        run_owner: Callable[[str], Awaitable[str]],
        now: Callable[[], datetime],
        limiter: RateLimiter | None = None,
        run_contact: Callable[[Contact, LoopInTask, str], Awaitable[str]] | None = None,
        transcribe: Callable[[str], str] | None = None,
        commands=None,  # app.commands.Commands: join/approve/... handled before any AI (Plan 2B)
        users=None,  # app.users.Users
        run_user: Callable[[object, str], Awaitable[str]] | None = None,  # (User, text) for signed-up friends
        transcribe_for_sender: Callable[[str, str], str] | None = None,
    ):
        self.owner_phone = normalize_phone(owner_phone)
        self.store = store
        self.sender = sender
        self.run_owner = run_owner
        self.run_contact = run_contact
        self.now = now
        self.limiter = limiter or RateLimiter()
        self.transcribe = transcribe  # media id -> text; blocking, so run in a thread
        self.transcribe_for_sender = transcribe_for_sender  # media id, authorized sender -> scoped transcription
        self.commands = commands
        self.before_notices = None
        self.users = users
        self.run_user = run_user
        self._locks: dict[str, asyncio.Lock] = {}

    async def handle(self, msg: IncomingMessage) -> None:
        phone = normalize_phone(msg.sender)
        now = self.now()
        if not self.limiter.allow(phone, now):
            log.info("message from %s: rate limited", tail(phone))
            if self.limiter.should_notify(phone, now):
                self._send(phone, RATE_LIMIT_NOTICE)
            return
        if self.commands is not None:
            # Under the sender's lock, so e.g. "yes" (delete my data) waits for their running reply to finish.
            async with self._locks.setdefault(phone, asyncio.Lock()):
                reply = self.commands.handle(phone, msg.text)
                if reply is not None:
                    log.info("message from %s: command", tail(phone))
                    take_attempt = getattr(self.commands, "take_join_attempt", None)
                    attempt_id = take_attempt(phone) if take_attempt else None
                    self._send(phone, reply, join_attempt=attempt_id)
                    if self.before_notices is not None:
                        # Process pause/deletion first, then drain only this feature's guarded queue.
                        self.before_notices(phone)
                    return
        invite = self.store.find_open_invite(phone) if self.run_contact else None
        if invite:
            # D25: an open invite outranks the sender's own assistant, until it's done or cancelled.
            contact, task = invite
            await self._handle_contact(phone, contact, task, msg, now)
            return
        if phone == self.owner_phone:
            log.info("message from %s: owner", tail(phone))
            self._deliver_notices(phone)
            await self._converse(phone, msg, self._run_owner)
            return
        user = self.users.by_phone(phone) if self.users is not None else None
        if user is None and self.users is not None:
            typed = self.users.by_typed_phone(phone)
            if typed is not None and typed.status in ("pending", "approved"):
                log.info("message from %s: user #%d hasn't sent their join code", tail(phone), typed.id)
                self._send(phone, JOIN_HINT)
                return
        if user is not None and user.status == "active" and self.run_user is not None:
            log.info("message from %s: user #%d", tail(phone), user.id)
            self._deliver_notices(phone)
            await self._converse(phone, msg, lambda text: self._run_signed_up(user, text))
            return
        if user is not None and user.status in ("pending", "approved"):
            log.info("message from %s: user #%d waiting for approval", tail(phone), user.id)
            admin = self.users.get(1)
            self._send(phone, PENDING_REPLY.format(admin=admin.first_name if admin else "The admin"))
            return
        booking = self.store.find_recent_booking(phone, now - CLOSED_TASK_GRACE) if self.run_contact else None
        if booking:
            # "Thanks!" after the booking: a friendly fixed reply, and the inviter hears what they said. No AI.
            log.info("message from %s: contact whose invite was recently booked", tail(phone))
            self._closed_task_reply(phone, booking[0], msg.text, now)
            return
        if self.store.should_send_unknown_reply(phone, now):
            log.info("message from %s: not the owner (owner is %s); sending private-assistant reply", tail(phone), tail(self.owner_phone))
            self._send(phone, UNKNOWN_REPLY)
        else:
            log.info(
                "message from %s: not the owner (owner is %s); private-assistant reply already sent in the last 24h, ignoring",
                tail(phone), tail(self.owner_phone),
            )

    async def _handle_contact(self, phone: str, contact: Contact, task: LoopInTask, msg: IncomingMessage, now: datetime) -> None:
        log.info("message from %s: contact with open invite (task #%d, %s)", tail(phone), task.id, task.status)

        async def run(text: str) -> str:
            # Re-read inside the lock: an earlier message may have booked or cancelled it meanwhile.
            # The task belongs to whoever sent the invite, not necessarily Pippa.
            mine = self.store.for_user(contact.user_id)
            current = mine.get_open_task(contact.id)
            if current is None or current.id != task.id:
                self._forward_to_owner(contact, text, now)
                return CLOSED_TASK_REPLY.format(owner=self._inviter(contact)[1])
            current = mine.mark_negotiating(current.id, now)
            return await self._run_contact(contact, current, text)

        await self._converse(phone, msg, run)

    async def _converse(self, phone: str, msg: IncomingMessage, run: Callable[[str], Awaitable[str]]) -> None:
        """Shared by Pippa and invited contacts: typing, voice notes, one run at a time, the "Heard:" line."""
        text = (msg.text or "").strip()
        is_voice = not text and msg.audio_id is not None and (self.transcribe is not None or self.transcribe_for_sender is not None)
        if not text and not is_voice:
            self._send(phone, NON_TEXT_REPLY)
            return
        self._typing(msg.message_id)
        heard = None
        # One run at a time per sender, so a follow-up message sees the previous run's result.
        # Voice notes are transcribed inside the lock too, so replies keep the order they were sent in.
        async with self._locks.setdefault(phone, asyncio.Lock()):
            if is_voice:
                try:
                    if self.transcribe_for_sender is not None:
                        heard = (await asyncio.to_thread(self.transcribe_for_sender, msg.audio_id, phone)).strip()
                    else:
                        heard = (await asyncio.to_thread(self.transcribe, msg.audio_id)).strip()
                except Exception:
                    log.exception("Could not transcribe voice note from %s", tail(phone))
                    self._send(phone, VOICE_ERROR_REPLY)
                    return
                if not heard:
                    self._send(phone, VOICE_EMPTY_REPLY)
                    return
                log.info("voice note from %s: %d characters transcribed", tail(phone), len(heard))
                text = f"[voice] {heard}"
            reply = await run(text)
        body = reply if reply.strip() else EMPTY_REPLY
        if heard:
            body = f"🎙️ Heard: {heard_line(heard)}\n\n{body}"
        self._send(phone, body)

    async def _run_owner(self, text: str) -> str:
        try:
            return await self.run_owner(text)
        except PartialRunError as exc:
            log.exception("Assistant run failed after writing to the calendar")
            return partial_run_reply(exc.writes)
        except Exception:
            log.exception("Assistant run failed")
            return ERROR_REPLY

    async def _run_signed_up(self, user, text: str) -> str:
        try:
            return await self.run_user(user, text)
        except PartialRunError as exc:
            log.exception("Assistant run for user #%d failed after writing to the calendar", user.id)
            return partial_run_reply(exc.writes)
        except Exception:
            log.exception("Assistant run for user #%d failed", user.id)
            return ERROR_REPLY

    async def _run_contact(self, contact: Contact, task: LoopInTask, text: str) -> str:
        try:
            return await self.run_contact(contact, task, text)
        except PartialRunError as exc:
            log.exception("Delegate run failed after booking")
            return "You're booked: " + "; ".join(exc.writes) + ". (Something went wrong after that, but the booking stands.)"
        except Exception:
            log.exception("Delegate run failed")
            # The inviter hears about it (not Pippa, unless it's his invite): it's their contact and their meeting.
            self._tell_inviter(
                contact, f"⚠️ I hit an error while arranging {task.purpose} with {contact.name}. You may want to message them yourself.", self.now()
            )
            return CONTACT_ERROR_REPLY

    def _typing(self, message_id: str) -> None:
        """Nice to have: a failure here must never stop the reply."""
        try:
            self.sender.mark_read_typing(message_id)
        except Exception as exc:  # noqa: BLE001 - best-effort (D20), whatever went wrong
            log.warning("Could not show typing indicator: %s", exc)

    def _inviter(self, contact: Contact) -> tuple[str, str]:
        """(phone, first name) of the user who saved this contact."""
        if contact.user_id == 1 or self.users is None:
            admin = self.users.get(1) if self.users is not None else None
            return self.owner_phone, admin.first_name if admin else "Pippa"
        user = self.users.get(contact.user_id)
        return (user.phone or "", user.first_name) if user else ("", "them")

    def _closed_task_reply(self, phone: str, contact: Contact, text: str | None, now: datetime) -> None:
        self._forward_to_owner(contact, text, now)
        self._send(phone, CLOSED_TASK_REPLY.format(owner=self._inviter(contact)[1]))

    def _forward_to_owner(self, contact: Contact, text: str | None, now: datetime) -> None:
        said = (text or "").strip() or "(a voice note or attachment)"
        self._tell_inviter(contact, f"💬 {contact.name}: {said}", now)

    def _tell_inviter(self, contact: Contact, body: str, now: datetime) -> None:
        """Message whoever saved this contact; keep it for later if their 24h window is closed."""
        to, _ = self._inviter(contact)
        if not to:
            return
        try:
            self.sender.send_text(to, body)
        except WindowClosedError:
            self.store.queue_notice(to, body, now)
        except WhatsAppError:
            log.exception("Could not forward a message from %s", tail(contact.phone))

    def _deliver_notices(self, phone: str) -> None:
        """Notices kept while this user's 24h window was closed go out first, as one message."""
        if self.before_notices is not None:
            self.before_notices(phone)
        notices = self.store.take_notices(phone)
        if not notices:
            return
        body = "While you were away:\n" + "\n".join(f"• {n}" for n in notices)
        try:
            self.sender.send_text(phone, body)
        except WhatsAppError:
            log.exception("Could not deliver %d kept notice(s) to %s", len(notices), tail(phone))
            for n in notices:
                self.store.queue_notice(phone, n, self.now())

    def _send(self, to: str, body: str, join_attempt: int | None = None) -> None:
        try:
            message_id = self.sender.send_text(to, body)
        except WhatsAppError as exc:
            if join_attempt is not None:
                self.commands.receipts.record_reply(join_attempt, to, error_code=exc.code, failed=True)
            log.exception("Could not send WhatsApp message to %s", tail(to))
        else:
            if join_attempt is not None:
                self.commands.receipts.record_reply(join_attempt, to, message_id=message_id)
