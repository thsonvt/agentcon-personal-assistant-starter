"""Personal follow-up reminders with durable confirmation and delivery states."""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging

from app.config import normalize_phone
from app.timeutil import SGT
from app.tools.whatsapp import WindowClosedError, clip

SCHEMA = """
CREATE TABLE IF NOT EXISTS followups (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
 action TEXT NOT NULL, context TEXT NOT NULL, timing_text TEXT NOT NULL,
 due_at TEXT, source TEXT NOT NULL, proposal_turn TEXT NOT NULL,
 fingerprint TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'draft',
 delivery TEXT NOT NULL DEFAULT 'waiting', message_id TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(user_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS followups_due ON followups(state, delivery, due_at);
"""


class FollowUps:
    def __init__(self, store, users, sender, now, template_name=''):
        self.store, self.users, self.sender, self.now = store, users, sender, now
        self.template_name = template_name
        with self._connection() as conn:
            conn.executescript(SCHEMA)
            # A crashed provider call may have succeeded; never resend it blindly.
            conn.execute("UPDATE followups SET delivery='uncertain' WHERE delivery='sending'")

    @contextmanager
    def _connection(self):
        conn = self.store.new_connection()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _stamp(self):
        return self.now().astimezone(timezone.utc).isoformat()

    def _future(self, value):
        try:
            dt = datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ValueError('Use an ISO date and time with timezone.') from exc
        if dt.tzinfo is None or dt.utcoffset() is None:
            raise ValueError('Include a timezone in the reminder date.')
        if dt <= self.now():
            raise ValueError('Choose a future reminder date and time.')
        return dt.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _eligible(conn, user_id, delivery=False):
        delivery_filter = " AND status='active' AND phone IS NOT NULL AND phone<>''" if delivery else ''
        return conn.execute(
            "SELECT * FROM users WHERE id=? AND event_only=0 "
            "AND ((is_admin=0 AND status IN ('active','approved')) OR (is_admin=1 AND status='active'))"
            + delivery_filter, (user_id,),
        ).fetchone()

    @staticmethod
    def _owned(conn, user_id, followup_id):
        row = conn.execute('SELECT * FROM followups WHERE user_id=? AND id=?', (user_id, followup_id)).fetchone()
        if row is None:
            raise ValueError('Follow-up not found.')
        return row

    def _describe(self, row):
        due = datetime.fromisoformat(row['due_at']).astimezone(SGT).strftime('%d %b %Y at %H:%M SGT') if row['due_at'] else 'unscheduled'
        state = 'scheduled' if row['state'] == 'pending' else row['state']
        with self._connection() as conn:
            user = self._eligible(conn, row['user_id'])
        connection_note = '\nTo receive reminders, connect WhatsApp and finish activation.' if user and (not user['phone'] or user['status'] != 'active') else ''
        return (f"Follow-up #{row['id']}: {state}; {due}; delivery: {row['delivery']}.\n"
                f"{row['action']}\nContext: {row['context']}\n"
                f"Original timing: {row['timing_text']}\nSource: {row['source']}{connection_note}")

    def prepare(self, user_id: int, action: str, context: str, timing_text: str,
                due_at: str | None, source: str, needs_confirmation: bool, run_no: str) -> str:
        action, context, timing_text, source = (v.strip() for v in (action, context, timing_text, source))
        if not action or len(action) > 1000 or any(len(v) > 2000 for v in (context, timing_text, source)):
            raise ValueError('Use a nonempty action up to 1000 characters and context, timing and source up to 2000 each.')
        due = self._future(due_at) if due_at else None
        fingerprint = hashlib.sha256(json.dumps([action, context, timing_text, due, source, needs_confirmation]).encode()).hexdigest()
        state = 'draft' if needs_confirmation or not due else 'pending'
        with self._connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not self._eligible(conn, user_id):
                raise ValueError('This account is not eligible for personal reminders.')
            conn.execute(
                'INSERT OR IGNORE INTO followups (user_id, action, context, timing_text, due_at, source, proposal_turn, fingerprint, state, created_at, updated_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (user_id, action, context, timing_text, due, source, str(run_no), fingerprint, state, self._stamp(), self._stamp()),
            )
            row = conn.execute('SELECT * FROM followups WHERE user_id=? AND fingerprint=?', (user_id, fingerprint)).fetchone()
        message = self._describe(row)
        if row['state'] == 'draft' and row['due_at']:
            message += '\nConfirm this proposed date in your next message to schedule it.'
        prefix = ('CONFIRM: ' if row['due_at'] else 'CLARIFY: ') if row['state'] == 'draft' else ('Saved: ' if row['state'] == 'pending' else 'Existing: ')
        return prefix + message

    create = prepare

    def confirm(self, user_id: int, followup_id: int, run_no: str) -> str:
        with self._connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = self._owned(conn, user_id, followup_id)
            if not self._eligible(conn, user_id):
                raise ValueError('This account is not eligible for personal reminders.')
            if row['state'] != 'draft':
                prefix = 'Saved: ' if row['state'] == 'pending' else 'Existing: '
                return prefix + self._describe(row)
            if str(run_no) == row['proposal_turn']:
                raise ValueError('Confirm this proposal in a later message.')
            if not row['due_at']:
                return 'CLARIFY: ' + self._describe(row)
            self._future(row['due_at'])
            conn.execute("UPDATE followups SET state='pending',updated_at=? WHERE id=?", (self._stamp(), followup_id))
            return 'Saved: ' + self._describe(self._owned(conn, user_id, followup_id))

    def list(self, user_id: int) -> str:
        with self._connection() as conn:
            if not self._eligible(conn, user_id):
                raise ValueError('This account is not eligible for personal reminders.')
            rows = conn.execute(
                "SELECT * FROM followups WHERE user_id=? ORDER BY "
                "CASE WHEN state IN ('pending','draft') THEN 0 ELSE 1 END, "
                "CASE WHEN state IN ('pending','draft') THEN due_at IS NULL END, "
                "CASE WHEN state IN ('pending','draft') THEN due_at END, id DESC LIMIT 50",
                (user_id,),
            ).fetchall()
            counts = conn.execute(
                "SELECT COALESCE(SUM(state IN ('pending','draft')),0) AS open_count, "
                "COALESCE(SUM(state IN ('done','cancelled')),0) AS closed_count "
                "FROM followups WHERE user_id=?", (user_id,),
            ).fetchone()
        if not rows:
            return 'No personal follow-ups saved.'
        descriptions = []
        for row in rows:
            bounded = dict(row)
            for key, limit in (('action', 300), ('context', 300), ('timing_text', 150), ('source', 150)):
                bounded[key] = clip(bounded[key], limit)
            descriptions.append(self._describe(bounded))
        header = (f"First 50 follow-ups; open: {counts['open_count']}; closed: {counts['closed_count']}. "
                  'Open reminders come first. Use the follow-up ID to make changes.')
        return header + '\n\n' + '\n\n'.join(descriptions)

    def update(self, user_id: int, followup_id: int, operation: str, due_at: str | None = None) -> str:
        if operation not in ('done', 'cancel', 'snooze'):
            raise ValueError('Choose done, cancel or snooze.')
        due = self._future(due_at) if operation == 'snooze' else None
        with self._connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = self._owned(conn, user_id, followup_id)
            if not self._eligible(conn, user_id):
                raise ValueError('This account is not eligible for personal reminders.')
            if row['delivery'] == 'sending':
                raise ValueError('This reminder is being sent. Try again shortly.')
            if operation == 'snooze':
                if row['state'] in ('done', 'cancelled'):
                    raise ValueError('This follow-up is closed. Create a new reminder instead.')
                if row['state'] == 'draft' and row['due_at']:
                    raise ValueError('Confirm the proposed reminder first, or cancel and create a new proposal.')
                conn.execute("UPDATE followups SET due_at=?,state='pending',delivery='waiting',message_id=NULL,updated_at=? WHERE id=?", (due, self._stamp(), followup_id))
            else:
                conn.execute('UPDATE followups SET state=?,updated_at=? WHERE id=?', ('done' if operation == 'done' else 'cancelled', self._stamp(), followup_id))
            return 'Updated: ' + self._describe(self._owned(conn, user_id, followup_id))

    def _send(self, followup_id, queued=False, phone=None):
        expected = 'queued' if queued else 'waiting'
        with self._connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT * FROM followups WHERE id=? AND state='pending' AND delivery=? AND due_at<=?", (followup_id, expected, self._stamp())).fetchone()
            if row is None:
                return 0
            user = self._eligible(conn, row['user_id'], delivery=True)
            if user is None or (phone is not None and user['phone'] != phone):
                return 0
            conn.execute("UPDATE followups SET delivery='sending',updated_at=? WHERE id=?", (self._stamp(), followup_id))
            target = user['phone']
        # Recheck eligibility and the current phone immediately before external I/O.
        with self._connection() as conn:
            current = self._eligible(conn, row['user_id'], delivery=True)
            if current is None or current['phone'] != target:
                conn.execute('UPDATE followups SET delivery=? WHERE id=?', (expected, followup_id))
                return 0
        body = (f"Reminder #{row['id']}: {clip(row['action'], 1000)}\n"
                f"Context: {clip(row['context'], 2000)}\n"
                f"Reply 'done #{row['id']}' or 'snooze #{row['id']}' with a new date and time.")
        delivery, message_id = 'uncertain', None
        try:
            try:
                message_id = self.sender.send_text(target, body)
            except WindowClosedError:
                if queued or not self.template_name:
                    delivery = 'queued'
                else:
                    template_body = (f"Reminder #{row['id']}: {clip(row['action'], 300)}\n"
                                     f"Context: {clip(row['context'], 450)}\n"
                                     f"Reply 'done #{row['id']}' or 'snooze #{row['id']}' with a new date and time.")
                    message_id = self.sender.send_template(target, self.template_name, [template_body])
            if message_id:
                delivery = 'accepted'
        except WindowClosedError:
            delivery = 'queued'
        except Exception:
            # Unknown provider outcomes need reconciliation, not automatic retries.
            delivery = 'uncertain'
        with self._connection() as conn:
            conn.execute("UPDATE followups SET delivery=?,message_id=?,updated_at=? WHERE id=? AND delivery='sending'", (delivery, message_id, self._stamp(), followup_id))
        return int(delivery == 'accepted')

    def tick(self) -> int:
        with self._connection() as conn:
            ids = [row['id'] for row in conn.execute("SELECT id FROM followups WHERE state='pending' AND delivery='waiting' AND due_at<=? ORDER BY due_at,id LIMIT 50", (self._stamp(),))]
        return sum(self._send(ident) for ident in ids)

    def deliver_queued(self, phone: str) -> int:
        phone = normalize_phone(phone)
        with self._connection() as conn:
            ids = [row['id'] for row in conn.execute("SELECT f.id FROM followups f JOIN users u ON u.id=f.user_id WHERE u.phone=? AND f.state='pending' AND f.delivery='queued' ORDER BY f.due_at,f.id LIMIT 50", (phone,))]
        return sum(self._send(ident, queued=True, phone=phone) for ident in ids)

    async def run(self):
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except Exception:
                logging.getLogger(__name__).error('Follow-up scheduler tick failed; check database/service health.')
            await asyncio.sleep(60)
