"""Durable conference collection. Models see evidence and have no action tools."""
import asyncio
import logging
import re
from datetime import timedelta

from app.config import normalize_phone
from app.router import RateLimiter

log = logging.getLogger(__name__)
START = re.compile(r"^(?:pippa[,!]?\s+)?(?:please\s+)?(?:start\s+(?:collecting|capturing)(?:\s+(?:session\s+)?notes)?|capture\s+start)(?:\s+(?:for|about|on)\s+|\s*:\s*|\s+)?(.*?)\s*[.!]?$", re.I)
FINISH = re.compile(r"^(?:(?:finish|stop|end)(?:\s+collecting)?(?:\s+(?:this|the|my))?\s+(?:session|capture|notes)|capture\s+(?:finish|stop)|put everything together)[.!]?$", re.I)
CORRECTION = re.compile(r"^(include|exclude)\s+(?:(?:item|note)\s+(\d+)|(that))[.!]?$", re.I)
CALENDAR_QUESTION = re.compile(r"^(?:what(?:'s| is| are)|show|check|list)\s+my\s+(?:next\s+)?(?:meeting|calendar|schedule|availability|events)\b", re.I)
ACCOUNT_COMMAND = re.compile(r"^(?:delete my data[.!]?|help|status|users|(?:join|approve|reject|disable)\s+\S+)$", re.I)
EMPTY_SUMMARY = {'takeaways': [], 'observations': [], 'questions': []}


class ConferenceCapture:
    def __init__(self, captures, users, store, sender, model, transcriber, media, now, pages, commands=None):
        self.captures, self.users, self.store = captures, users, store
        self.sender, self.model, self.transcriber, self.media = sender, model, transcriber, media
        self.now, self.pages, self.commands = now, pages, commands
        self._lock = asyncio.Lock()
        self._deleting = {}
        self._limiter = RateLimiter(limit=180)

    def _active(self, user_id):
        user = self.users.get(user_id)
        return user if user is not None and user.status == 'active' else None

    def ingest(self, msg) -> bool:
        """Called synchronously after webhook signature verification, before HTTP 200.

        True means durably accounted for (accepted, duplicate, or visible rejection).
        No media/model/WhatsApp network operations happen in this transaction.
        """
        if self.captures.seen(msg.message_id) or self.store.has_seen_message(msg.message_id):
            return True  # replay must not switch lanes after invite/account/session changes
        phone = normalize_phone(msg.sender)
        user = self.users.by_phone(phone)
        if user is None or user.status != 'active':
            return False
        if self.store.find_open_invite(phone):
            return False  # existing invited-contact routing has precedence
        text = (msg.text or '').strip()
        if (ACCOUNT_COMMAND.fullmatch(text) or text.lower().startswith('assistant:')
                or CALENDAR_QUESTION.match(text)
                or (self.commands and phone in self.commands._confirming_delete)):
            return False
        current, latest = self.captures.current(user.id), self.captures.latest(user.id)
        start = START.fullmatch(text)
        finish = FINISH.fullmatch(text)
        correction = CORRECTION.fullmatch(text)
        control = text.lower().rstrip('.!')
        is_control = control in ('capture status', 'my session notes', 'retry capture',
                                 'capture retry', 'delete capture', 'confirm delete capture')
        if not (current or start or finish or correction or is_control):
            # Report post-finish media explicitly, rather than losing it to the generic photo response.
            if not (latest and (msg.audio_id or getattr(msg, 'image_id', None))):
                return False
        with self.captures.transaction():
            if self.captures.seen(msg.message_id):
                return True
            self.captures.mark_seen(user.id, msg.message_id)
            if not self._limiter.allow(phone, self.now()):
                if self._limiter.should_notify(phone, self.now()):
                    self.captures.notify(user.id, phone, 'Capture rate limit reached. Please wait before sending more notes; this item was not saved.')
                return True
            try:
                self._accept(user, msg, text, start, finish, correction, control, current, latest)
            except ValueError as exc:
                self.captures.notify(user.id, phone, f'Capture: {exc}')
        return True

    def _accept(self, user, msg, text, start, finish, correction, control, current, latest):
        target = (current or latest or {}).get('id')
        def reply(body):
            self.captures.notify(user.id, user.phone, body, session_id=target)
        if control != 'confirm delete capture':
            self._deleting.pop(user.id, None)
        if start:
            if current:
                reply(f"Already collecting for {current['title']}. Say finish this session before starting another.")
                return
            title = (start.group(1) or 'Conference session').strip()
            if len(title) > 160:
                raise ValueError('Use a session title of at most 160 characters.')
            session = self.captures.start(user.id, user.phone, title)
            target = session['id']
            reply(f"Started collecting for {session['title']}. Send slide photos, text or voice notes. "
                  'Say finish this session when done. Use capture status or exclude that to review. '
                  'For ordinary requests, say assistant: followed by your request. Notes expire after seven days.')
            return
        session = current or latest
        if session is None:
            reply('No session yet. Say start collecting notes for followed by the talk title.')
            return
        if control == 'delete capture':
            self._deleting[user.id] = (session['id'], self.now() + timedelta(minutes=5))
            reply('Delete this session and its original photos, voice notes and summary? Reply confirm delete capture next to erase it.')
            return
        if control == 'confirm delete capture':
            pending = self._deleting.pop(user.id, None)
            if not pending or pending[0] != session['id'] or pending[1] <= self.now():
                reply('Nothing deleted. Say delete capture first to choose the session.')
                return
            self.captures.delete(user.id, session['id'])
            target = None
            reply('Deleted this session and its notes. Its access link no longer works.')
            return
        if control in ('capture status', 'my session notes'):
            items = self.captures.items(user.id, session['id'], include_media=False)
            pending = sum(i['status'] == 'pending' for i in items)
            failed = sum(i['status'] == 'failed' for i in items)
            review = sum(not i['included'] for i in items)
            reply(f"{session['title']}: {session['state']}. {len(items)} items; {pending} pending, "
                  f"{failed} failed, {review} excluded/review.\n{self.pages.link(session)}\n"
                  'This private link gives access to this session to anyone you share it with.')
            return
        if control in ('retry capture', 'capture retry'):
            self.captures.retry(user.id, session['id'])
            reply('Retry queued. Original saved items will be reused; capture status shows progress.')
            return
        if correction:
            items = self.captures.items(user.id, session['id'], include_media=False)
            item_id = int(correction.group(2)) if correction.group(2) else (items[-1]['id'] if items else None)
            if item_id is None or not any(i['id'] == item_id for i in items):
                raise ValueError('That item is not in your current session. Use capture status to open its item list.')
            include = correction.group(1).lower() == 'include'
            self.captures.set_included(user.id, session['id'], item_id, include)
            reply(f"Item {item_id} {'included' if include else 'excluded'}. Finished notes will be updated.")
            return
        if finish:
            if current:
                self.captures.finish(user.id, current['id'])
                reply('Collection closed. I’ll finish processing saved items and put your notes together. Use capture status for progress.')
            else:
                reply(f"This session is already closed ({session['state']}). Use capture status for your notes.")
            return
        if not current:
            reply('This session is closed. This new item was not added. Start collecting notes for a new session.')
            return
        image_id = getattr(msg, 'image_id', None)
        kind = 'image' if image_id else 'audio' if msg.audio_id else 'text'
        original = (getattr(msg, 'caption', None) or '') if image_id else text
        if kind == 'text' and (not original or getattr(msg, 'kind', 'text') not in ('text', '')):
            raise ValueError('Send text, a JPEG/PNG slide photo, or a voice note. This attachment was not saved.')
        item = self.captures.add_item(user.id, current['id'], msg.message_id, kind,
            text=original, media_id=image_id or msg.audio_id,
            source_timestamp=getattr(msg, 'source_timestamp', None))
        # Receipt says queued rather than implying that remote media is already downloaded.
        reply(f"Item {item['id']} queued. Use exclude that to leave it out.")

    async def _notifications(self):
        for notice in self.captures.pending_notifications():
            if not self._active(notice['user_id']):
                continue
            if notice.get('session_id') and not self.captures.get(notice['user_id'], notice['session_id']):
                continue
            claimed = self.captures.notification_state(notice['id'], 'sending')
            if not claimed:
                continue  # deletion or another claim invalidated the queued snapshot
            try:
                await asyncio.to_thread(self.sender.send_text, notice['phone'], notice['body'])
            except Exception:
                # An uncertain send is deliberately not blindly retried on restart.
                self.captures.notification_state(notice['id'], 'failed')
                log.warning('Capture notification failed; receipt=%s', notice['id'])
            else:
                self.captures.notification_state(notice['id'], 'sent')

    def _item_exists(self, item):
        return self._active(item['user_id']) and self.captures.item(item['user_id'], item['id'])

    async def _process(self, item):
        uid, iid = item['user_id'], item['id']
        if not self._item_exists(item):
            return
        # pending_items returns bounded metadata, so at most one media body is
        # loaded here; retries reuse the durable original rather than download again.
        item = self.captures.item(uid, iid)
        session = self.captures.get(uid, item['session_id'])
        if not session:
            return
        try:
            kind = item['kind']
            if kind in ('image', 'audio'):
                data, mime = item['media'], item['mime_type']
                if not data:
                    data, mime = await asyncio.to_thread(self.media.download_media, item['media_id'])
                    mime = mime.split(';')[0].strip().lower()
                    if not self._item_exists(item):
                        return
                    if kind == 'image' and not (
                        (mime == 'image/png' and data.startswith(b'\x89PNG\r\n\x1a\n')) or
                        (mime == 'image/jpeg' and data.startswith(b'\xff\xd8\xff'))
                    ):
                        raise ValueError('Unsupported photo format')
                    if kind == 'audio' and mime not in ('audio/ogg', 'audio/mpeg', 'audio/mp4', 'audio/aac', 'audio/amr', 'audio/wav', 'audio/webm'):
                        raise ValueError('Unsupported audio format')
                    self.captures.save_media(uid, iid, data, mime)
                if kind == 'image':
                    extracted = await self.model.extract_image(data, mime)
                    category = 'slide'
                else:
                    extracted = await asyncio.to_thread(self.transcriber.transcribe, data, mime)
                    if not self._item_exists(item):
                        return
                    category = await self.model.classify(extracted, session['title'])
            else:
                extracted = item['original_text']
                category = await self.model.classify(extracted, session['title'])
            if not isinstance(extracted, str) or not extracted.strip() or len(extracted) > 20000:
                raise ValueError('Empty or overlong extraction')
            if category not in ('observation', 'question', 'review', 'slide'):
                raise ValueError('Invalid classification')
            if not self._item_exists(item):
                return
            completed = self.captures.complete_item(uid, iid, extracted, category, included=category != 'review')
            if completed and category == 'review' and not completed['included']:
                self.captures.notify(uid, session['phone'], f'Item {iid} saved for review and excluded from the summary. '
                    f'Say include item {iid} if it belongs to this session. Use assistant: for ordinary requests.', session_id=session['id'])
        except Exception:
            if self._item_exists(item):
                self.captures.fail_item(uid, iid, 'Could not process this item. Use retry capture or resend a clearer item.')
                self.captures.notify(uid, session['phone'], f'Item {iid} could not be processed. Any original downloaded media is retained. Use retry capture.', session_id=session['id'])
            log.warning('Capture item processing failed; item=%s', iid)

    @staticmethod
    def _fingerprint(items):
        return tuple((i['id'], i['status'], i['included'], i['category'], i['extracted_text']) for i in items)

    async def _summarize(self, session):
        uid, sid = session['user_id'], session['id']
        if not self._active(uid):
            return
        items = self.captures.items(uid, sid, include_media=False)
        if any(i['status'] == 'pending' for i in items):
            return
        included = [i for i in items if i['status'] == 'ready' and i['included']]
        # Bytes never go to the text summarizer. Captions and source roles remain explicit.
        sources = [{k: v for k, v in i.items() if k != 'media'} for i in included]
        try:
            summary = await self.model.summarize(session['title'], sources) if sources else dict(EMPTY_SUMMARY)
            current = self.captures.get(uid, sid)
            if not self._active(uid) or not current or current['state'] != 'closing':
                return
            if self._fingerprint(items) != self._fingerprint(self.captures.items(uid, sid, include_media=False)):
                return  # a correction raced the model; next pass uses fresh inputs
            with self.captures.transaction():
                self.captures.save_summary(uid, sid, summary)
                failed = sum(i['status'] == 'failed' for i in items)
                review = sum(not i['included'] for i in items)
                lead = 'Your session notes are ready.' if sources else 'Session closed; there are no included, readable notes to summarize.'
                points = [p['text'] for p in summary.get('takeaways', [])[:3]]
                recap = '\n'.join(f'• {p[:250]}' for p in points)
                self.captures.notify(uid, session['phone'], f"{lead}\n{recap}\n"
                    f"{failed} failed items; {review} excluded/review items.\n{self.pages.link(current)}\n"
                    'Based only on what you sent. Private link: anyone you share it with can view this session.', session_id=sid)
        except Exception:
            current = self.captures.get(uid, sid)
            if (self._active(uid) and current and current['state'] == 'closing'
                    and self._fingerprint(items) == self._fingerprint(self.captures.items(uid, sid, include_media=False))):
                self.captures.fail_summary(uid, sid, 'Summary could not be generated. Your sources are saved. Use retry capture.')
                self.captures.notify(uid, session['phone'], 'I could not finish the summary. Your sources are saved; use retry capture.', session_id=sid)
            log.warning('Capture summary failed; session=%s', sid)

    async def drain_once(self):
        async with self._lock:
            self.captures.expire()
            await self._notifications()
            active_ids = [u.id for u in self.users.list() if u.status == 'active']
            for item in self.captures.pending_items(user_ids=active_ids):
                await self._process(item)
            for session in self.captures.closing_sessions():
                await self._summarize(session)
            await self._notifications()

    async def run(self):
        while True:
            try:
                await self.drain_once()
            except Exception:
                log.warning('Capture worker pass failed; pending work remains durable')
            await asyncio.sleep(1)
