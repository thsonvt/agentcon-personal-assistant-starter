"""Authenticated web transport for Pippa; provider keys and tools stay on the server."""

import asyncio
import hashlib
import json
import logging
import re
import secrets
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from app.booking import BOOKING_LOCK
from app.router import PartialRunError, RateLimiter, partial_run_reply
from app.tools.elevenlabs import AUDIO_TYPES, MAX_UPLOAD_BYTES, VoiceError
from app.web_pages import render_web_page

HEADERS = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
           'Referrer-Policy': 'no-referrer', 'Permissions-Policy': 'microphone=(self)'}
ASSETS = Path(__file__).parent / 'static' / 'web'
log = logging.getLogger(__name__)
INTERRUPTED = ('The previous request was interrupted and its outcome is uncertain. '
               'Check your calendar before repeating a change. I have not retried it.')


class WebAssistant:
    def __init__(self, connections, run, voice, base_url):
        self.connections, self.users = connections, connections.users
        self.conn, self.run, self.voice = self.users.conn, run, voice
        parsed = urlsplit(base_url)
        self.origin = f'{parsed.scheme}://{parsed.netloc}'
        self.locks = {}
        self.message_limiter = RateLimiter(30, timedelta(hours=1))
        self.voice_limiter = RateLimiter(40, timedelta(hours=1))
        self.realtime_limiter = RateLimiter(10, timedelta(hours=1))
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS web_conversations (
                user_id INTEGER PRIMARY KEY, data TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS web_conversations_delete_user
            AFTER DELETE ON users BEGIN
                DELETE FROM web_conversations WHERE user_id=OLD.id;
            END;
        ''')
        # ponytail: one application worker, as for existing booking serialization.
        # On restart, preserve uncertain outcomes instead of replaying side effects.
        for row in self.conn.execute('SELECT user_id, data FROM web_conversations').fetchall():
            state = json.loads(row['data'])
            if state.get('pending'):
                self._append(state, 'assistant', INTERRUPTED)
                state['pending'] = False
                self._save(row['user_id'], state)
        self.conn.commit()
        self.router = APIRouter()
        self._routes()

    def _error(self, message, status=400):
        return JSONResponse({'error': message}, status_code=status, headers=HEADERS)

    def _ready(self, user):
        if user and user.is_admin and getattr(self.connections, 'local_owner_id', None) == user.id:
            return user.status == 'active'
        return bool(user and not user.is_admin and not user.event_only
                    and user.status in ('approved', 'active') and user.calendar_ready
                    and not user.google_disconnected and user.google_revocation_status != 'pending')

    def _auth(self, request, mutation=False):
        user, session = self.connections._auth(request)
        if not user:
            return None, self._error('Sign in to continue.', 401)
        if mutation:
            csrf = request.headers.get('X-CSRF-Token', '')
            origin = request.headers.get('origin')
            if (request.headers.get('sec-fetch-site') == 'cross-site'
                    or (origin is not None and origin != self.origin)
                    or not csrf or not csrf.isascii() or len(csrf) > 256
                    or not secrets.compare_digest(session['csrf'], csrf)):
                return None, self._error('Refresh the page before continuing.', 403)
        if not self._ready(user):
            return None, self._error('Connect Google Calendar and complete beta approval in Connections.', 403)
        return user, None

    async def _body(self, request, limit):
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > limit:
                raise HTTPException(413, 'This request is too large.', headers=HEADERS)
            body.extend(chunk)
        return bytes(body)

    async def _json(self, request):
        if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
            raise ValueError('Send a JSON request.')
        try:
            value = json.loads(await self._body(request, 16384))
        except (ValueError, UnicodeDecodeError):
            raise ValueError('Invalid JSON request.') from None
        if not isinstance(value, dict):
            raise ValueError('Invalid JSON request.')
        return value

    def _load(self, user_id):
        row = self.conn.execute('SELECT data FROM web_conversations WHERE user_id=?', (user_id,)).fetchone()
        return json.loads(row['data']) if row else dict(messages=[], revision=0, receipts=[], pending=False)

    def _save(self, user_id, state):
        self.conn.execute('''INSERT INTO web_conversations(user_id,data)
            SELECT ?,? WHERE EXISTS (SELECT 1 FROM users WHERE id=?)
            ON CONFLICT(user_id) DO UPDATE SET data=excluded.data''',
            (user_id, json.dumps(state), user_id))
        self.conn.commit()

    def _append(self, state, role, text, voice=False):
        state['messages'].append(dict(id=secrets.token_hex(16), role=role, content=text, voice=voice))
        state['messages'] = state['messages'][-40:]

    def _state(self, user_id):
        state = self._load(user_id)
        return dict(messages=state['messages'], revision=state['revision'], ready=True,
                    voice=dict(stt=self.voice.stt_enabled, tts=self.voice.tts_enabled),
                    pending=state.get('pending', False))

    def _response(self, user_id):
        return JSONResponse(self._state(user_id), headers=HEADERS)

    async def _message(self, request, user):
        try:
            data = await self._json(request)
        except ValueError as exc:
            return self._error(str(exc))
        text, voice = data.get('message'), data.get('voice', False)
        revision, request_id = data.get('revision'), data.get('request_id')
        if (not isinstance(text, str) or not text.strip() or len(text) > 4000
                or any(ord(c) < 32 and c not in '\n\r\t' for c in text)
                or type(voice) is not bool or type(revision) is not int or revision < 0
                or not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', request_id)):
            return self._error('Enter a message of at most 4,000 characters with a valid request identifier.')
        fingerprint = hashlib.sha256(json.dumps([text, voice, revision]).encode()).hexdigest()
        lock = self.locks.setdefault(user.id, asyncio.Lock())
        if lock.locked():
            return self._error('Pippa is still working on your previous message. Refresh shortly.', 409)
        async with lock:
            # Never hold the database/booking lock across an await.
            with BOOKING_LOCK:
                user, error = self._auth(request, mutation=True)
                if error is not None:
                    return error
                state = self._load(user.id)
                receipt = next((r for r in state['receipts'] if r['id'] == request_id), None)
                if receipt:
                    if receipt['fingerprint'] != fingerprint:
                        return self._error('This request identifier was already used for another message.', 409)
                    return self._response(user.id)
                if revision != state['revision'] or state.get('pending'):
                    return self._error('This conversation changed. Refresh before sending your draft.', 409)
                if not self.message_limiter.allow(str(user.id), self.connections.now()):
                    return self._error('Too many messages. Please try again later.', 429)
                self._append(state, 'user', text.strip(), voice)
                state['messages'][-1]['request_id'] = request_id
                state['revision'] += 1
                state['pending'] = True
                state['receipts'] = (state['receipts'] + [dict(id=request_id, fingerprint=fingerprint)])[-40:]
                self._save(user.id, state)
            try:
                reply = await self.run(user, ('[voice] ' if voice else '') + text.strip())
                if not isinstance(reply, str) or not reply.strip():
                    reply = 'I could not produce a reply. Check your calendar before repeating a change.'
            except PartialRunError as exc:
                reply = partial_run_reply(exc.writes)
            except Exception:
                log.warning('Web assistant request failed for user #%d', user.id)
                reply = 'I could not finish that request. Check your calendar before repeating a change.'
            with BOOKING_LOCK:
                # Finish the original user's receipt even if their browser logged out
                # or signed into another account while the agent was running.
                if self.users.get(user.id) is not None:
                    state = self._load(user.id)
                    self._append(state, 'assistant', reply[:8000])
                    state['pending'] = False
                    self._save(user.id, state)
                current, error = self._auth(request, mutation=True)
                if error is not None:
                    return error
                if current.id != user.id:
                    return self._error('Your signed-in account changed. Refresh to continue.', 401)
                return self._response(user.id)

    def _routes(self):
        @self.router.get('/app')
        def page(request: Request):
            user, session = self.connections._auth(request)
            if not user:
                return RedirectResponse('/signin', status_code=303, headers=HEADERS)
            data = dict(name=user.first_name, email=user.email, csrf_token=session['csrf'],
                        ready=self._ready(user), voice=dict(stt=self.voice.stt_enabled, tts=self.voice.tts_enabled))
            return HTMLResponse(render_web_page(data), headers=HEADERS)

        @self.router.get('/pippa-web-assets/{filename}')
        def asset(filename: str):
            if filename not in ('app.js', 'app.css', 'handsfree.js', 'pcm-worklet.js'):
                raise HTTPException(404)
            return FileResponse(ASSETS / filename, headers={'Cache-Control': 'no-cache',
                                                          'X-Content-Type-Options': 'nosniff'})

        @self.router.get('/api/web/state')
        def state(request: Request):
            user, error = self._auth(request)
            return error if error is not None else self._response(user.id)

        @self.router.post('/api/web/message')
        async def message(request: Request):
            user, error = self._auth(request, mutation=True)
            return error if error is not None else await self._message(request, user)

        @self.router.post('/api/web/realtime-token')
        async def realtime_token(request: Request):
            user, error = self._auth(request, mutation=True)
            if error is not None:
                return error
            try:
                await self._json(request)
            except ValueError as exc:
                return self._error(str(exc))
            current, error = self._auth(request, mutation=True)
            if error is not None:
                return error
            if current.id != user.id:
                return self._error('Your signed-in account changed. Refresh to continue.', 401)
            if not self.voice.stt_enabled:
                return self._error('Voice input is not configured.', 503)
            if not self.realtime_limiter.allow(str(user.id), self.connections.now()):
                return self._error('Too many conversation starts. Please try again later.', 429)
            try:
                token = await self.voice.realtime_token()
            except VoiceError as exc:
                return self._error(str(exc), 503)
            current, error = self._auth(request, mutation=True)
            if error is not None:
                return error
            if current.id != user.id:
                return self._error('Your signed-in account changed. Refresh to continue.', 401)
            return JSONResponse({'token': token}, headers=HEADERS)

        @self.router.post('/api/web/transcribe')
        async def transcribe(request: Request):
            user, error = self._auth(request, mutation=True)
            if error is not None:
                return error
            if not self.voice.stt_enabled:
                return self._error('Voice input is not configured. You can still type a message.', 503)
            mime = request.headers.get('content-type', '').split(';')[0].strip().lower()
            if mime not in AUDIO_TYPES:
                return self._error('Unsupported recording format.', 415)
            audio = await self._body(request, MAX_UPLOAD_BYTES)
            current, error = self._auth(request, mutation=True)
            if error is not None:
                return error
            if current.id != user.id:
                return self._error('Your signed-in account changed. Refresh to continue.', 401)
            if not audio:
                return self._error('The recording is empty.')
            if not self.voice_limiter.allow(str(user.id), self.connections.now()):
                return self._error('Too many voice requests. Please try again later.', 429)
            try:
                text = await self.voice.transcribe(audio, mime)
            except VoiceError as exc:
                return self._error(str(exc), 503)
            current, error = self._auth(request, mutation=True)
            if error is not None:
                return error
            if current.id != user.id:
                return self._error('Your signed-in account changed. Refresh to continue.', 401)
            if not text.strip() or len(text) > 4000:
                return self._error('Please make a shorter recording or type your message.')
            return JSONResponse({'text': text}, headers=HEADERS)

        @self.router.post('/api/web/speech')
        async def speech(request: Request):
            user, error = self._auth(request, mutation=True)
            if error is not None:
                return error
            if not self.voice.tts_enabled:
                return self._error('Spoken replies are not configured. The text reply is still available.', 503)
            try:
                data = await self._json(request)
            except ValueError as exc:
                return self._error(str(exc))
            current, error = self._auth(request, mutation=True)
            if error is not None:
                return error
            if current.id != user.id:
                return self._error('Your signed-in account changed. Refresh to continue.', 401)
            reply = next((m for m in self._load(user.id)['messages']
                          if m['id'] == data.get('message_id') and m['role'] == 'assistant'), None)
            if reply is None:
                return self._error('This reply is unavailable.', 404)
            if not self.voice_limiter.allow(str(user.id), self.connections.now()):
                return self._error('Too many voice requests. Please try again later.', 429)
            try:
                audio = await self.voice.speak(reply['content'])
            except VoiceError as exc:
                return self._error(str(exc), 503)
            current, error = self._auth(request, mutation=True)
            if error is not None:
                return error
            if current.id != user.id:
                return self._error('Your signed-in account changed. Refresh to continue.', 401)
            return Response(audio, media_type='audio/mpeg', headers=HEADERS)
