"""Hourly check: tell the inviting user (once) about invites nobody has answered for 48 hours."""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from app.router import Sender
from app.store import Store
from app.timeutil import fmt_dt, parse_dt
from app.tools.whatsapp import WhatsAppError, WindowClosedError

log = logging.getLogger(__name__)
NUDGE_AFTER = timedelta(hours=48)


def run_nudges(
    store: Store,
    sender: Sender,
    owner_phone: str,
    now: datetime,
    phone_for_user: Callable[[int], str | None] | None = None,
) -> int:
    """Send one nudge per overdue invite, to whoever sent it. A task is marked 'nudged' only if the message went out."""
    sent = 0
    for task in store.tasks_waiting_since(now - NUDGE_AFTER):
        to = owner_phone if task.user_id == 1 else (phone_for_user(task.user_id) if phone_for_user else None)
        if not to:
            continue
        mine = store.for_user(task.user_id)
        contact = mine.get_contact(task.contact_id)
        name = contact.name if contact else "Your contact"
        text = (
            f"⏰ {name} hasn't replied to your invite for {task.purpose} "
            f"(window ends {fmt_dt(parse_dt(task.window_end))}). "
            "Want me to cancel it, or will you follow up yourself?"
        )
        try:
            sender.send_text(to, text)
        except WindowClosedError:
            # The user hasn't messaged the bot for 24h: keep it for their next message instead of retrying hourly.
            store.queue_notice(to, text, now)
            log.info("nudge for task #%d kept until the user next messages", task.id)
        except WhatsAppError:
            log.exception("Could not send nudge for task #%d; will retry next hour", task.id)
            continue
        mine.set_task_status(task.id, "nudged", now)
        sent += 1
    return sent


async def nudge_loop(
    store: Store,
    sender: Sender,
    owner_phone: str,
    now: Callable[[], datetime],
    interval_s: float = 3600,
    phone_for_user: Callable[[int], str | None] | None = None,
) -> None:
    while True:
        try:
            # Off the event loop: the check makes blocking WhatsApp calls.
            count = await asyncio.to_thread(run_nudges, store, sender, owner_phone, now(), phone_for_user)
            if count:
                log.info("nudge: sent %d reminder(s)", count)
        except Exception:
            log.exception("nudge check failed")
        await asyncio.sleep(interval_s)
