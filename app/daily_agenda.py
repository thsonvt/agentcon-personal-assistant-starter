"""Two daily checkpoints and a compact, private WhatsApp meeting timeline."""
import asyncio
import hashlib
import json
import re
from datetime import timedelta, timezone

from app.timeutil import SGT, fmt_dt, fmt_time
from app.tools.whatsapp import WindowClosedError


class DailyAgenda:
    def __init__(self, briefs):
        self.briefs = briefs
        with briefs._db() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS proactive_agendas (
                user_id INTEGER NOT NULL, day TEXT NOT NULL, events_json TEXT NOT NULL,
                PRIMARY KEY(user_id,day))''')

    @staticmethod
    def _fingerprint(event):
        return hashlib.sha256(json.dumps([event.title, event.description, event.location,
                                         event.end.isoformat(), event.meet_link]).encode()).hexdigest()

    def _read(self, user):
        now = self.briefs.now().astimezone(SGT)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        events = self.briefs.calendar_for_user(user.id).list_events(start, start + timedelta(days=1), 100)
        return sorted((e for e in events if e.end > start and e.start < start + timedelta(days=1)),
                      key=lambda e: (e.start, e.id))

    def _snapshot(self, user, events, hints=None, evidence=None):
        day = self.briefs.now().astimezone(SGT).date().isoformat()
        with self.briefs._db() as conn:
            goal = conn.execute('SELECT enabled FROM proactive_goals WHERE user_id=?', (user.id,)).fetchone()
            if not goal or not goal['enabled']:
                raise ValueError('Agenda paused')
            row = conn.execute('SELECT events_json FROM proactive_agendas WHERE user_id=? AND day=?',
                               (user.id, day)).fetchone()
            items = json.loads(row['events_json']) if row else []
            for event in events:
                if event.all_day:
                    continue
                key = (event.id, event.start.isoformat())
                item = next((i for i in items if (i['id'], i['start']) == key), None)
                if item is None:
                    item = {'id': key[0], 'start': key[1], 'number': len(items) + 1}
                    items.append(item)
                fingerprint = self._fingerprint(event)
                if (item.get('fingerprint') != fingerprint
                        or (evidence and key in evidence and item.get('evidence') != evidence[key])):
                    for field in ('hint', 'preparation', 'evidence'):
                        item.pop(field, None)
                item['fingerprint'] = fingerprint
                if evidence and key in evidence:
                    item['evidence'] = evidence[key]
                if hints and key in hints:
                    value = hints[key]
                    item['hint'] = value['hint'] if isinstance(value, dict) else value
                    if isinstance(value, dict):
                        item['preparation'] = value['preparation']
            conn.execute('''INSERT INTO proactive_agendas VALUES(?,?,?)
                ON CONFLICT(user_id,day) DO UPDATE SET events_json=excluded.events_json''',
                (user.id, day, json.dumps(items)))
        return items

    @staticmethod
    def _one_line(value, limit):
        value = re.sub(r'https?://\S+', '', value or '')
        value = re.sub(r'<[^>]*>', ' ', value)
        value = ' '.join(value.split())
        return value if len(value) <= limit else value[:limit - 1].rstrip() + '…'

    def _hint(self, event, item):
        if item.get('hint'):
            return 'Prep suggestion: ' + self._one_line(item['hint'], 600)
        if event.description and self._one_line(event.description, 100):
            return 'Calendar agenda: ' + self._one_line(event.description, 100)
        return 'Prep suggestion: No agenda supplied — note your questions.'

    def render(self, user, events, kind='today'):
        now = self.briefs.now().astimezone(SGT)
        items = self._snapshot(user, events)
        timed = [e for e in events if not e.all_day]
        shown = [e for e in timed if e.end > now] if kind == 'afternoon' else timed
        heading = '🌅 Your day' if kind == 'morning' else ('☀️ Still ahead' if kind == 'afternoon' else '📅 Your day')
        lines = [f'{heading} · {now:%a} {now.day} {now:%b} · Singapore time',
                 f"{len(timed)} {'meeting' if len(timed) == 1 else 'meetings'} today · {sum(e.end > now for e in timed)} remaining"]
        if not shown:
            lines.append('No meetings still ahead.' if kind == 'afternoon' else 'No timed meetings today.')
        displayed = 0
        for event in shown[:8]:
            item = next(i for i in items if i['id'] == event.id and i['start'] == event.start.isoformat())
            marker = '✓ earlier' if event.end <= now else ('● in progress' if event.start <= now else '○')
            entry = [f"\n{item['number']}. {fmt_time(event.start)}–{fmt_time(event.end)} {marker}",
                     '   ' + self._one_line(event.title, 90), '   ' + self._hint(event, item)]
            if len('\n'.join(lines + entry)) > 3500:
                break
            lines += entry
            displayed += 1
        all_day = [e for e in events if e.all_day]
        if all_day:
            lines.append('\nAll-day: ' + '; '.join(self._one_line(e.title, 45) for e in all_day[:3]))
            if len(all_day) > 3:
                lines.append(f'Plus {len(all_day) - 3} other all-day entries.')
        if len(shown) > displayed:
            lines.append(f'\nShowing {displayed} of {len(shown)}. Reply meeting N for any listed number.')
        if len(events) == 100:
            lines.append('Calendar view is capped at 100 entries; counts may be incomplete.')
        if shown:
            first = next(i['number'] for i in items if i['id'] == shown[0].id and i['start'] == shown[0].start.isoformat())
            lines.append(f'\nReply meeting {first} for details, or agenda to refresh.')
        else:
            lines.append('\nReply agenda to refresh.')
        return '\n'.join(lines)

    def number(self, user, event):
        events = self._read(user)
        items = self._snapshot(user, events)
        item = next((i for i in items if i['id'] == event.id and i['start'] == event.start.isoformat()), None)
        return item['number'] if item else None

    async def answer(self, phone, text):
        text = re.sub(r'^\[voice\]\s*', '', text or '', flags=re.I).strip().lower().rstrip('.?!')
        match = re.fullmatch(r'(?:meeting|details|meeting details)\s+(\d{1,3})', text)
        upcoming = {'upcoming meetings', 'afternoon agenda', 'what meetings are coming up', 'any more meetings after this'}
        kind = 'afternoon' if text in upcoming else 'today'
        if not match and text not in upcoming | {'agenda', 'today agenda', "today's agenda", 'my agenda',
                                                'what meetings do i have today', "what's my agenda today"}:
            return None
        user = self.briefs.users.by_phone(phone)
        if not user or not self.briefs._current(user.id):
            return 'Agenda requires an active Calendar-connected account. Send meeting briefs on to enable it.'
        try:
            events = await asyncio.to_thread(self._read, user)
            if not self.briefs._current(user.id, connection_version=user.google_connection_version):
                return 'Your Calendar connection changed. Reconnect, then send meeting briefs on.'
            if not match:
                shown = [e for e in events if not e.all_day and (kind != 'afternoon' or e.end > self.briefs.now())][:8]
                await asyncio.wait_for(asyncio.to_thread(self.refresh_evidence, user, shown), timeout=20)
                events = await asyncio.to_thread(self._read, user)
                if not self.briefs._current(user.id, connection_version=user.google_connection_version):
                    return 'Meeting briefs are paused or your Calendar connection changed.'
                return self.render(user, events, kind)
            items = self._snapshot(user, events)
            item = next((i for i in items if i['number'] == int(match.group(1))), None)
            event = next((e for e in events if item and e.id == item['id'] and e.start.isoformat() == item['start']), None)
            if event is None:
                return 'That meeting is no longer on today’s calendar. Send agenda for the latest view.'
            with self.briefs._db() as conn:
                goal = conn.execute('SELECT revision FROM proactive_goals WHERE user_id=?', (user.id,)).fetchone()
            detail = await self.briefs.detail(user, goal['revision'], event)
            if not self.briefs._current(user.id, goal['revision'], user.google_connection_version):
                return 'Meeting briefs are paused or your Calendar connection changed.'
            if detail:
                self.select(user, event)
                return detail
            latest = await asyncio.to_thread(self._read, user)
            current = next((e for e in latest if e.id == event.id and e.start == event.start), None)
            if current is None:
                return 'That meeting is no longer on today’s calendar. Send agenda for the latest view.'
            if self._fingerprint(current) != self._fingerprint(event):
                return 'That meeting changed while I prepared it. Send agenda for the latest view.'
            items = self._snapshot(user, latest)
            item = next(i for i in items if i['id'] == current.id and i['start'] == current.start.isoformat())
            if not self.briefs._current(user.id, goal['revision'], user.google_connection_version):
                return 'Meeting briefs are paused or your Calendar connection changed.'
            return (f'{current.title}\n{fmt_dt(current.start)}–{fmt_time(current.end)}\n'
                    + self._hint(current, item) + '\nDetailed preparation is unavailable right now.')
        except Exception:
            return 'I couldn’t read today’s calendar. Try again, or check your Calendar connection.'

    async def scheduled(self, user, revision):
        now = self.briefs.now().astimezone(SGT)
        kind = 'morning' if now.hour == 8 else ('afternoon' if now.hour == 13 else None)
        if kind is None:
            return
        day = now.date().isoformat()
        expiry = now.replace(hour=13, minute=0, second=0, microsecond=0) if kind == 'morning' else now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        key = (user.id, f'@agenda:{kind}:{day}', expiry.isoformat())
        with self.briefs._db() as conn:
            existing = conn.execute('SELECT state FROM proactive_briefs WHERE user_id=? AND event_id=? AND event_start=?', key).fetchone()
        if existing and existing['state'] in {'sent', 'queued', 'sending', 'uncertain', 'expired'}:
            return
        events = await asyncio.to_thread(self._read, user)
        selected = [e for e in events if not e.all_day and (kind != 'afternoon' or e.end > now)][:8]
        generator = getattr(self.briefs.generate, 'agenda', None)
        with self.briefs._db() as conn:
            budget = conn.execute('SELECT count FROM proactive_attempts WHERE user_id=? AND day=?',
                                  (user.id, now.astimezone(timezone.utc).date().isoformat())).fetchone()
        use_model = bool(generator and selected and (not budget or budget['count'] < 5))
        body = self.briefs._claim(key, now, charge=use_model)
        if body is None:
            return
        hints = None
        evidence = None
        if not body and use_model:
            try:
                async def prepare():
                    nonlocal evidence
                    if self.briefs.evidence_for_event is not None:
                        packets = [await self.briefs.evidence(user, event) for event in selected]
                        evidence = {(e.id, e.start.isoformat()): p for e, p in zip(selected, packets)}
                        return await generator(selected, evidence=packets)
                    return await generator(selected)

                values = await asyncio.wait_for(prepare(), timeout=45)
                if (len(values) != len(selected) or any(not self._valid_preparation(v) for v in values)):
                    raise ValueError('Invalid agenda hints')
                hints = {(e.id, e.start.isoformat()): value for e, value in zip(selected, values)}
            except Exception:
                pass  # Calendar-source hints are the honest fallback; do not lose the day plan.
        if not self.briefs._current(user.id, revision, user.google_connection_version):
            self.briefs._save(key, 'cancelled')
            return
        # Refresh again after generation: time/attendance changes must be reflected before delivery.
        events = await asyncio.to_thread(self._read, user)
        if hints:
            original = {(e.id, e.start.isoformat()): self._fingerprint(e) for e in selected}
            hints = {key: value for key, value in hints.items()
                     if any((e.id, e.start.isoformat()) == key and self._fingerprint(e) == original[key] for e in events)}
        self._snapshot(user, events, hints, evidence)
        body = self.render(user, events, kind)
        if not self.briefs._current(user.id, revision, user.google_connection_version):
            self.briefs._save(key, 'cancelled')
            return
        self.briefs._save(key, 'sending', body)
        try:
            await asyncio.to_thread(self.briefs.sender.send_text, user.phone, body)
        except WindowClosedError:
            if self.briefs._current(user.id, revision, user.google_connection_version):
                self.briefs._save(key, 'queued', body)
            else:
                self.briefs._save(key, 'cancelled')
        except Exception:
            self.briefs._save(key, 'uncertain', body)
        else:
            self.briefs._save(key, 'sent', body)

    @staticmethod
    def _valid_preparation(value):
        if isinstance(value, str):
            return bool(value.strip()) and len(value) <= 600
        return (isinstance(value, dict) and isinstance(value.get('hint'), str)
                and bool(value['hint'].strip()) and len(value['hint']) <= 600
                and isinstance(value.get('preparation'), str)
                and bool(value['preparation'].strip()) and len(value['preparation']) <= 3500)

    def select(self, user, event):
        items = self._snapshot(user, [event])
        for item in items:
            item['selected'] = item['id'] == event.id and item['start'] == event.start.isoformat()
        with self.briefs._db() as conn:
            conn.execute('UPDATE proactive_agendas SET events_json=? WHERE user_id=? AND day=?',
                         (json.dumps(items), user.id, self.briefs.now().astimezone(SGT).date().isoformat()))

    def refresh_evidence(self, user, events):
        if self.briefs.evidence_for_event is not None:
            evidence = {(e.id, e.start.isoformat()): self.briefs.evidence_for_event(user.id, e)
                        for e in events[:8] if not e.all_day}
            if not self.briefs._current(user.id, connection_version=user.google_connection_version):
                raise ValueError('Meeting preparation paused or connection changed')
            self._snapshot(user, events, evidence=evidence)
            return evidence
        return {}

    def deliver_queued(self, user, revision, row):
        key = (user.id, row['event_id'], row['event_start'])
        kind = row['event_id'].split(':')[1]
        events = self._read(user)
        shown = [e for e in events if not e.all_day and (kind != 'afternoon' or e.end > self.briefs.now())][:8]
        self.refresh_evidence(user, shown)
        events = self._read(user)
        if not self.briefs._current(user.id, revision, user.google_connection_version):
            self.briefs._save(key, 'cancelled')
            return
        body = self.render(user, events, kind)
        with self.briefs._db() as conn:
            claimed = conn.execute("UPDATE proactive_briefs SET state='sending' WHERE user_id=? AND event_id=? AND event_start=? AND state='queued'", key).rowcount
        if not claimed:
            return
        try:
            self.briefs.sender.send_text(user.phone, body)
        except WindowClosedError:
            if self.briefs._current(user.id, revision, user.google_connection_version):
                self.briefs._save(key, 'queued', body)
            else:
                self.briefs._save(key, 'cancelled')
        except Exception:
            self.briefs._save(key, 'uncertain', body)
        else:
            self.briefs._save(key, 'sent', body)
