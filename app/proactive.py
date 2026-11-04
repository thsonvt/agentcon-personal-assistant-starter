"""One opt-in ongoing responsibility, with durable generation and delivery receipts."""
import asyncio
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from app.timeutil import SGT, fmt_dt, fmt_time
from app.meeting_context import _calendar_url
from app.tools.whatsapp import WindowClosedError

log = logging.getLogger(__name__)


class ProactiveBriefs:
    def __init__(self, store, users, calendar_for_user, generate, sender, now, evidence_for_event=None):
        self.store, self.users = store, users
        self.calendar_for_user, self.generate = calendar_for_user, generate
        self.sender, self.now = sender, now
        self.evidence_for_event = evidence_for_event
        self._lock = asyncio.Lock()
        with self._db() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS proactive_goals (
                    user_id INTEGER PRIMARY KEY, phone TEXT NOT NULL,
                    enabled INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                    last_check TEXT);
                CREATE TABLE IF NOT EXISTS proactive_briefs (
                    user_id INTEGER NOT NULL, event_id TEXT NOT NULL, event_start TEXT NOT NULL,
                    body TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '',
                    detail_fingerprint TEXT NOT NULL DEFAULT '', state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id,event_id,event_start));
                CREATE TABLE IF NOT EXISTS proactive_attempts (
                    user_id INTEGER NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL,
                    PRIMARY KEY(user_id,day));
            ''')
            columns = {r['name'] for r in conn.execute('PRAGMA table_info(proactive_briefs)')}
            for name in ('detail', 'detail_fingerprint'):
                if name not in columns:
                    conn.execute(f"ALTER TABLE proactive_briefs ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        from app.daily_agenda import DailyAgenda
        self.agenda = DailyAgenda(self)

    @contextmanager
    def _db(self):
        conn = self.store.new_connection()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _eligible(self, user):
        return bool(user and user.phone and user.status == 'active' and not user.event_only
                    and not user.google_disconnected
                    and user.calendar_authorized is not False and user.google_revocation_status != 'pending'
                    and (user.is_admin or user.calendar_ready))

    def _current(self, user_id, revision=None, connection_version=None):
        user = self.users.get(user_id)
        if not self._eligible(user):
            return None
        with self._db() as conn:
            goal = conn.execute('SELECT * FROM proactive_goals WHERE user_id=?', (user_id,)).fetchone()
        if (not goal or not goal['enabled'] or goal['phone'] != user.phone
                or (revision is not None and goal['revision'] != revision)
                or (connection_version is not None and user.google_connection_version != connection_version)):
            return None
        return user

    def command(self, phone, text):
        text = (text or '').strip().lower()
        if text not in {'meeting briefs', 'meeting briefs on', 'meeting briefs off', 'meeting briefs status'}:
            return None
        user = self.users.by_phone(phone)
        if user is None:
            return 'Meeting briefs require an active assistant account and connected Calendar.'
        if text.endswith(' off'):
            self.pause(user.id)
            return 'Meeting briefs paused. Pending briefs have been removed.'
        if not self._eligible(user):
            self.pause(user.id)
            return 'Meeting briefs require an active assistant account and connected Calendar.'
        if text.endswith(' on'):
            with self._db() as conn:
                conn.execute('''INSERT INTO proactive_goals(user_id,phone,enabled) VALUES(?,?,1)
                    ON CONFLICT(user_id) DO UPDATE SET enabled=1,phone=excluded.phone,
                    revision=revision+1''', (user.id, phone))
            return ('Meeting briefs enabled: your day plan at 8:00am and afternoon update at 1:00pm, Singapore time. '
                    'Upcoming reminders stay short. Send agenda to refresh, meeting 1 for details, or meeting briefs off to pause. '
                    'Closed WhatsApp windows queue updates until your next message.')
        with self._db() as conn:
            goal = conn.execute('SELECT * FROM proactive_goals WHERE user_id=?', (user.id,)).fetchone()
            rows = conn.execute('SELECT state,count(*) n FROM proactive_briefs WHERE user_id=? GROUP BY state',
                                (user.id,)).fetchall()
        state = 'enabled' if goal and goal['enabled'] else 'paused'
        checked = goal['last_check'] if goal and goal['last_check'] else 'not yet checked'
        counts = ', '.join(f"{r['state']}: {r['n']}" for r in rows) or 'no briefs yet'
        return (f'Meeting briefs {state}. Agenda: 8:00am and 1:00pm Singapore time. '
                f'Last check: {checked}. {counts}. Uncertain delivery needs manual review.')

    def _clear_pending(self, conn, user_id):
        conn.execute("UPDATE proactive_briefs SET state='cancelled',body='',detail='' WHERE user_id=? AND state NOT IN ('sent','sending','uncertain')",
                     (user_id,))
        conn.execute('DELETE FROM proactive_agendas WHERE user_id=?', (user_id,))

    def pause(self, user_id):
        with self._db() as conn:
            conn.execute('UPDATE proactive_goals SET enabled=0,revision=revision+1 WHERE user_id=?', (user_id,))
            self._clear_pending(conn, user_id)

    def remove(self, user_id):
        with self._db() as conn:
            self._clear_pending(conn, user_id)
            for table in ('proactive_briefs', 'proactive_goals', 'proactive_attempts', 'proactive_agendas'):
                conn.execute(f'DELETE FROM {table} WHERE user_id=?', (user_id,))

    def _cleanup_queue(self, user_id, events, now):
        live = {(e.id, e.start.isoformat()) for e in events if not e.all_day and e.start > now}
        fingerprints = {(e.id, e.start.isoformat()): self.agenda._fingerprint(e) for e in events}
        with self._db() as conn:
            for row in conn.execute("SELECT * FROM proactive_briefs WHERE user_id=? AND state='queued'", (user_id,)).fetchall():
                if row['event_id'].startswith('@agenda:'):
                    continue
                key = (row['event_id'], row['event_start'])
                if key in live and row['detail_fingerprint'] != fingerprints[key]:
                    conn.execute("UPDATE proactive_briefs SET state='cancelled',body='',detail='' WHERE user_id=? AND event_id=? AND event_start=?",
                                 (user_id, *key))
                    continue
                if (row['event_id'], row['event_start']) not in live:
                    conn.execute("UPDATE proactive_briefs SET state='expired',body='',detail='' WHERE user_id=? AND event_id=? AND event_start=?",
                                 (user_id, row['event_id'], row['event_start']))

    def _event_current(self, calendar, event_id, event_start, fingerprint=None):
        start = datetime.fromisoformat(event_start)
        if start <= self.now():
            return False
        # list_events filters declined invitations too; get_event only filters cancellation.
        events = calendar.list_events(self.now(), start + timedelta(seconds=1), 25)
        return any(e.id == event_id and not e.all_day and e.start == start
                   and (fingerprint is None or self.agenda._fingerprint(e) == fingerprint) for e in events)

    def deliver_queued(self, phone):
        """User-initiated delivery with a dedicated queue; generic notices cannot bypass these checks."""
        user = self.users.by_phone(phone)
        if user is None:
            return
        current = self._current(user.id)
        if not current:
            self.pause(user.id)
            return
        with self._db() as conn:
            goal = conn.execute('SELECT revision FROM proactive_goals WHERE user_id=?', (user.id,)).fetchone()
            rows = conn.execute("SELECT * FROM proactive_briefs WHERE user_id=? AND state='queued'", (user.id,)).fetchall()
        for row in rows:
            key = (user.id, row['event_id'], row['event_start'])
            if datetime.fromisoformat(row['event_start']) <= self.now():
                self._save(key, 'expired')
                continue
            if row['event_id'].startswith('@agenda:'):
                try:
                    self.agenda.deliver_queued(user, goal['revision'], row)
                except Exception:
                    log.warning('Queued agenda validation unavailable for user #%d', user.id)
                continue
            try:
                valid = self._event_current(self.calendar_for_user(user.id), row['event_id'], row['event_start'], row['detail_fingerprint'])
                if valid and self.evidence_for_event is not None:
                    events = self.agenda._read(user)
                    event = next((e for e in events if e.id == row['event_id'] and e.start.isoformat() == row['event_start']), None)
                    if event is None:
                        valid = False
                    else:
                        packets = self.agenda.refresh_evidence(user, [event])
                        valid = bool(self._cached_preparation(user, event, packets[(event.id, event.start.isoformat())]))
                        valid = valid and self._event_current(self.calendar_for_user(user.id), event.id,
                                                              event.start.isoformat(), row['detail_fingerprint'])
            except Exception:
                log.warning('Queued meeting validation unavailable for user #%d', user.id)
                continue
            if not valid or not self._current(user.id, goal['revision'], user.google_connection_version):
                self._save(key, 'cancelled')
                continue
            with self._db() as conn:
                claimed = conn.execute("UPDATE proactive_briefs SET state='sending' WHERE user_id=? AND event_id=? AND event_start=? AND state='queued'", key).rowcount
            if not claimed:
                continue
            try:
                self.sender.send_text(phone, row['body'])
            except WindowClosedError:
                if self._current(user.id, goal['revision'], user.google_connection_version):
                    self._save(key, 'queued', row['body'])
                else:
                    self._save(key, 'cancelled')
            except Exception:
                self._save(key, 'uncertain', row['body'])
            else:
                self._save(key, 'sent', row['body'])

    def _claim(self, key, now, charge=True, detail_only=False):
        day = now.astimezone(timezone.utc).date().isoformat()
        with self._db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM proactive_briefs WHERE user_id=? AND event_id=? AND event_start=?', key).fetchone()
            if detail_only and (not row or row['attempts'] >= 3 or row['state'] in {'generating', 'sending'}):
                return None
            if row and not detail_only:
                if row['state'] == 'ready':
                    return row['body']
                if row['state'] not in {'failed', 'generating', 'cancelled'} or row['attempts'] >= 3:
                    return None
                wait = timedelta(minutes=5 if row['state'] == 'generating' else (0 if row['state'] == 'cancelled' else 15))
                if now < datetime.fromisoformat(row['updated_at']) + wait:
                    return None
            budget = conn.execute('SELECT count FROM proactive_attempts WHERE user_id=? AND day=?', (key[0], day)).fetchone()
            if charge and budget and budget['count'] >= 5:
                return None
            if charge:
                conn.execute('''INSERT INTO proactive_attempts VALUES(?,?,1)
                    ON CONFLICT(user_id,day) DO UPDATE SET count=count+1''', (key[0], day))
            if detail_only:
                conn.execute('UPDATE proactive_briefs SET attempts=attempts+1,updated_at=? WHERE user_id=? AND event_id=? AND event_start=?',
                             (now.isoformat(), *key))
                return ''
            conn.execute('''INSERT INTO proactive_briefs(user_id,event_id,event_start,state,attempts,updated_at)
                VALUES(?,?,?,'generating',1,?) ON CONFLICT(user_id,event_id,event_start)
                DO UPDATE SET state='generating',attempts=attempts+1,updated_at=excluded.updated_at''', (*key, now.isoformat()))
        return ''

    def _save(self, key, state, body='', detail=None, fingerprint=None):
        with self._db() as conn:
            conn.execute('''UPDATE proactive_briefs SET state=?,body=?,updated_at=?,
                detail=CASE WHEN ? IN ('failed','cancelled','expired') THEN '' ELSE COALESCE(?,detail) END
                ,detail_fingerprint=COALESCE(?,detail_fingerprint)
                WHERE user_id=? AND event_id=? AND event_start=?''',
                (state, body, self.now().isoformat(), state, detail, fingerprint, *key))

    async def tick(self):
        async with self._lock:
            now = self.now()
            with self._db() as conn:
                goals = conn.execute('SELECT * FROM proactive_goals WHERE enabled=1').fetchall()
                for queued in conn.execute("SELECT user_id,event_id,event_start FROM proactive_briefs WHERE state='queued'").fetchall():
                    if datetime.fromisoformat(queued['event_start']) <= now:
                        conn.execute("UPDATE proactive_briefs SET state='expired',body='',detail='' WHERE user_id=? AND event_id=? AND event_start=?",
                                     tuple(queued))
                # Interrupted sends have an unknown outcome; never resend automatically.
                conn.execute("UPDATE proactive_briefs SET state='uncertain' WHERE state='sending'")
                conn.execute('DELETE FROM proactive_attempts WHERE day<?',
                             ((now.astimezone(timezone.utc) - timedelta(days=7)).date().isoformat(),))
                conn.execute("DELETE FROM proactive_briefs WHERE event_start<? AND state NOT IN ('queued','sending')",
                             ((now - timedelta(days=7)).isoformat(),))
                conn.execute('DELETE FROM proactive_agendas WHERE day<?',
                             ((now.astimezone(SGT) - timedelta(days=7)).date().isoformat(),))
            for goal in goals:
                user = self._current(goal['user_id'], goal['revision'])
                if not user:
                    if not self._eligible(self.users.get(goal['user_id'])):
                        self.pause(goal['user_id'])
                    continue
                try:
                    await self.agenda.scheduled(user, goal['revision'])
                    if not self._current(user.id, goal['revision'], user.google_connection_version):
                        continue
                    calendar = self.calendar_for_user(user.id)
                    events = await asyncio.to_thread(calendar.list_events, now, now + timedelta(hours=1), 25)
                    self._cleanup_queue(user.id, events, now)
                    with self._db() as conn:
                        conn.execute('UPDATE proactive_goals SET last_check=? WHERE user_id=?', (now.isoformat(), user.id))
                    for event in events:
                        if event.all_day or not now < event.start <= now + timedelta(hours=1):
                            continue
                        await self._brief(user, goal['revision'], event)
                except Exception:
                    # Do not log private calendar content or provider response bodies.
                    log.warning('Meeting brief check failed for user #%d', user.id)

    async def _prepare(self, user, revision, event):
        key = (user.id, event.id, event.start.isoformat())
        if not self._current(user.id, revision, user.google_connection_version):
            return
        with self._db() as conn:
            previous = conn.execute('SELECT * FROM proactive_briefs WHERE user_id=? AND event_id=? AND event_start=?', key).fetchone()
        if previous and previous['state'] not in {'ready', 'failed', 'generating', 'cancelled'}:
            return
        packet = await self.evidence(user, event)
        cached = self._cached_preparation(user, event, packet)
        if previous and previous['state'] == 'ready' and previous['detail_fingerprint'] != self.agenda._fingerprint(event):
            self._save(key, 'cancelled')
        elif previous and previous['state'] == 'ready' and self.evidence_for_event is not None and not cached:
            self._save(key, 'cancelled')
        body = self._claim(key, self.now(), charge=not bool(cached))
        if body is None:
            return
        if not body:
            try:
                preparation = cached or await self._generate(user, event, packet)
                if not isinstance(preparation, str) or not preparation.strip() or len(preparation) > 3500:
                    raise ValueError('Invalid brief')
                detail = self._detail_text(event, preparation)
                number = await asyncio.to_thread(self.agenda.number, user, event)
                item = next(i for i in self.agenda._snapshot(user, [event])
                            if i['id'] == event.id and i['start'] == event.start.isoformat())
                body = self._compact_text(event, preparation, number, hint=item.get('hint'))
            except Exception:
                self._save(key, 'failed')
                return
            if not self._current(user.id, revision, user.google_connection_version):
                self._save(key, 'cancelled')
                return
            self._save(key, 'ready', body, detail=detail, fingerprint=self.agenda._fingerprint(event))
        return body

    async def answer(self, phone, text):
        return await self.agenda.answer(phone, text)

    async def evidence(self, user, event):
        if self.evidence_for_event is None:
            return None
        if not self._current(user.id, connection_version=user.google_connection_version):
            raise ValueError('Meeting preparation paused or connection changed')
        return await asyncio.wait_for(asyncio.to_thread(self.evidence_for_event, user.id, event), timeout=15)

    def _cached_preparation(self, user, event, packet):
        key = (event.id, event.start.isoformat())
        items = self.agenda._snapshot(user, [event], evidence={key: packet} if packet is not None else None)
        item = next(i for i in items if (i['id'], i['start']) == key)
        return item.get('preparation')

    async def _generate(self, user, event, packet):
        result = await asyncio.wait_for(self.generate(event, evidence=packet) if packet is not None
                                        else self.generate(event), timeout=45)
        if not isinstance(result, str) or not result.strip() or len(result) > 3500:
            raise ValueError('Invalid meeting preparation')
        if not self._current(user.id, connection_version=user.google_connection_version):
            raise ValueError('Meeting preparation paused or connection changed')
        key = (event.id, event.start.isoformat())
        self.agenda._snapshot(user, [event], hints={key: {'hint': self.agenda._one_line(result, 600),
                                                       'preparation': result}},
                              evidence={key: packet} if packet is not None else None)
        return result

    async def conversation_context(self, user):
        """Only current briefs already shown to this account may inform a follow-up."""
        if not self._current(user.id, connection_version=user.google_connection_version):
            return ''
        async with self._lock:
            events = await asyncio.to_thread(self.agenda._read, user)
            items = self.agenda._snapshot(user, events)
            with self._db() as conn:
                sent = conn.execute("SELECT event_id FROM proactive_briefs WHERE user_id=? AND state='sent'", (user.id,)).fetchall()
            sent_ids = {r['event_id'] for r in sent}
            agenda_sent = any(value.startswith('@agenda:') and value.endswith(self.now().astimezone(SGT).date().isoformat()) for value in sent_ids)
            selected = [i for i in items if i.get('selected')]
            candidates = (selected or items)[:3]
            briefs = []
            for item in candidates:
                event = next((e for e in events if e.id == item['id'] and e.start.isoformat() == item['start']
                              and e.end > self.now() and not e.all_day), None)
                if event is None or not (item.get('selected') or agenda_sent or event.id in sent_ids):
                    continue
                packet = await self.evidence(user, event)
                preparation = self._cached_preparation(user, event, packet)
                if preparation:
                    brief = {'event_id': event.id, 'meeting_number': item['number'], 'title': event.title[:500], 'start': event.start.isoformat(),
                             'preparation': preparation, 'evidence': packet}
                    if len(json.dumps(briefs + [brief], ensure_ascii=False)) <= 48000:
                        briefs.append(brief)
            if not self._current(user.id, connection_version=user.google_connection_version):
                return ''
            latest = await asyncio.to_thread(self.agenda._read, user)
            valid = {(e.id, e.start.isoformat()): self.agenda._fingerprint(e) for e in latest}
            original = {(e.id, e.start.isoformat()): self.agenda._fingerprint(e) for e in events}
            briefs = [brief for brief in briefs if valid.get((brief['event_id'], brief['start']))
                      == original[(brief['event_id'], brief['start'])]]
            self.agenda._snapshot(user, latest)
            return json.dumps(briefs, ensure_ascii=False) if briefs else ''

    async def detail(self, user, revision, event):
        # ponytail: serialize with the single background worker; split locks only if throughput demands it.
        async with self._lock:
            return await self._detail(user, revision, event)

    def _detail_text(self, event, preparation):
        text = f'{event.title[:200]}\n{fmt_dt(event.start)}–{fmt_time(event.end)}\n\n{preparation.strip()}'
        source = _calendar_url(event.source_url)
        if source:
            reference = '\nCalendar source: ' + source
            if len(text + reference) <= 4000:
                return text + reference
        return text + '\nCalendar event ID: ' + event.id[:200]

    def _compact_text(self, event, preparation, number, hint=None):
        lines = [line.strip(' •-*') for line in preparation.splitlines() if line.strip()]
        hint = hint or next((line for line in lines if not line.lower().startswith(('meeting:', 'agenda:', 'preparation', 'suggestions:'))), 'Review the agenda and note your questions.')
        return (f'⏰ Next: {self.agenda._one_line(event.title, 90)}\n'
                f'{fmt_time(event.start)}–{fmt_time(event.end)} · Singapore time\n'
                f'Prep suggestion: {self.agenda._one_line(hint, 140)}\n'
                + (f'Reply meeting {number} for details.' if number else 'Reply agenda for the latest view.'))

    async def _detail(self, user, revision, event):
        key = (user.id, event.id, event.start.isoformat())
        fingerprint = self.agenda._fingerprint(event)
        packet = await self.evidence(user, event)
        cached = self._cached_preparation(user, event, packet)
        if cached:
            latest = await asyncio.to_thread(self.agenda._read, user)
            if (any(e.id == event.id and e.start == event.start and self.agenda._fingerprint(e) == fingerprint for e in latest)
                    and self._current(user.id, revision, user.google_connection_version)):
                return self._detail_text(event, cached)
            self.agenda._snapshot(user, latest)
            return None
        with self._db() as conn:
            row = conn.execute('SELECT * FROM proactive_briefs WHERE user_id=? AND event_id=? AND event_start=?', key).fetchone()
        if row and row['state'] not in {'cancelled', 'expired', 'failed', 'generating'}:
            if self.evidence_for_event is None and row['detail'] and row['detail_fingerprint'] == fingerprint:
                return row['detail']
            if not self._current(user.id, revision, user.google_connection_version) or self._claim(key, self.now(), detail_only=True) is None:
                return None
            try:
                preparation = await self._generate(user, event, packet)
                if not isinstance(preparation, str) or not preparation.strip() or len(preparation) > 3500:
                    return None
                latest = await asyncio.to_thread(self.agenda._read, user)
                if (not any(e.id == event.id and e.start == event.start and self.agenda._fingerprint(e) == fingerprint for e in latest)
                        or not self._current(user.id, revision, user.google_connection_version)):
                    return None
                detail = self._detail_text(event, preparation)
                number = next((i['number'] for i in self.agenda._snapshot(user, latest)
                               if i['id'] == event.id and i['start'] == event.start.isoformat()), None)
                body = self._compact_text(event, preparation, number) if row['state'] in {'queued', 'ready'} else row['body']
                self._save(key, row['state'], body, detail=detail, fingerprint=fingerprint)
                return detail
            except Exception:
                return None
        if event.end <= self.now():
            return None
        await self._prepare(user, revision, event)
        events = await asyncio.to_thread(self.agenda._read, user)
        if not any(e.id == event.id and e.start == event.start and self.agenda._fingerprint(e) == fingerprint for e in events):
            self._save(key, 'cancelled')
            return None
        with self._db() as conn:
            row = conn.execute('SELECT detail FROM proactive_briefs WHERE user_id=? AND event_id=? AND event_start=?', key).fetchone()
        return row['detail'] if row else None

    async def _brief(self, user, revision, event):
        key = (user.id, event.id, event.start.isoformat())
        body = await self._prepare(user, revision, event)
        if not body:
            return
        if not self._current(user.id, revision, user.google_connection_version) or event.start <= self.now():
            self._save(key, 'cancelled')
            return
        try:
            valid = await asyncio.to_thread(self._event_current, self.calendar_for_user(user.id), event.id, event.start.isoformat(), self.agenda._fingerprint(event))
        except Exception:
            return  # keep the prepared body, retry validation next check without regeneration
        if not valid or not self._current(user.id, revision, user.google_connection_version):
            self._save(key, 'cancelled')
            return
        self._save(key, 'sending', body)
        try:
            await asyncio.to_thread(self.sender.send_text, user.phone, body)
        except WindowClosedError:
            if not self._current(user.id, revision, user.google_connection_version):
                self._save(key, 'cancelled')
                return
            self._save(key, 'queued', body)
        except Exception:
            self._save(key, 'uncertain', body)
        else:
            self._save(key, 'sent', body)

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception:
                log.warning('Meeting brief background check failed')
            await asyncio.sleep(300)
