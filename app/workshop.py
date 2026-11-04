"""Single-owner, loopback-only Calendar workshop; public runtime is separate."""
import hashlib
import json
import os
import secrets
import sqlite3
import threading
from contextlib import closing
from datetime import date, datetime, time, timedelta
from html import escape
from pathlib import Path

from agents import SQLiteSession, set_default_openai_key
from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.agents import assistant_instructions, build_assistant, run_agent
from app.assistant_tools import AppContext
from app.connections import SESSION_COOKIE
from app.crypto import TokenBox, generate_key, sign, unsign
from app.local_web import LocalOwner
from app.store import Store
from app.timeutil import SGT
from app.tools.calendar import CalendarAuthError, GoogleCalendar, build_service
from app.tools.calendly import BookingLink
from app.tools.elevenlabs import ElevenLabsVoice
from app.tools.google_oauth import CALENDAR_SCOPE, GoogleOAuth, GoogleOAuthError
from app.users import Users
from app.web_assistant import WebAssistant
from app.wiring import sgt_now
from app.workshop_pages import form, page

ORIGIN = 'http://127.0.0.1:8016'
CALLBACK = ORIGIN + '/signup/callback'
OAUTH_COOKIE = 'pippa_workshop_oauth'
CALENDAR_TOOLS = {'list_events', 'find_conflicts', 'get_free_slots', 'create_event', 'update_event',
                  'delete_event', 'get_event_details', 'save_contact', 'list_contacts', 'remember',
                  'recall', 'forget', 'set_working_hours'}


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _token_box(env, database, saved):
    supplied = env.get('TOKEN_KEY', '').strip()
    if database == ':memory:':
        return TokenBox(supplied or generate_key())
    path = Path(database + '.key')
    if path.is_symlink():
        raise ValueError('The workshop encryption key must not be a symbolic link.')
    if path.exists():
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd) as source:
            key = source.read().strip()
            if hasattr(os, 'fchmod'):
                os.fchmod(source.fileno(), 0o600)
            else:
                os.chmod(path, 0o600)
        if supplied and not secrets.compare_digest(key, supplied):
            raise ValueError('TOKEN_KEY differs from the saved workshop key. Restore the original key.')
    elif supplied:
        key = supplied
    elif saved:
        raise ValueError('The workshop encryption key is missing. Restore the original .key file.')
    else:
        key = generate_key()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'w') as destination:
            destination.write(key)
    return TokenBox(key)


class Workshop:
    def __init__(self, env):
        self.env = {key: value.strip() for key, value in env.items() if isinstance(value, str)}
        self.database = self.env.get('DB_PATH') or './data/workshop.db'
        if self.database != ':memory:':
            path = Path(self.database)
            if path.is_symlink():
                raise ValueError('Use a regular file for the workshop database.')
            if path.exists():
                if not path.is_file():
                    raise ValueError('Use a regular file for the workshop database.')
                # Inspect ownership before Store can run any migrations.
                with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as existing_db:
                    tables = {row[0] for row in existing_db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")}
                    if ('users' in tables and 'workshop_settings' not in tables
                            and existing_db.execute('SELECT 1 FROM users LIMIT 1').fetchone()):
                        raise ValueError('This database belongs to the full application. Choose a fresh workshop DB_PATH.')
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store = Store(self.database)
        conn = self.store.conn
        conn.execute('CREATE TABLE IF NOT EXISTS workshop_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)')
        conn.commit()
        self.users = Users(self.store)
        existing = self.users.get(1)
        self.users.ensure_admin(existing.name if existing else self.env.get('OWNER_NAME') or 'You', '', sgt_now())
        self.box = _token_box(self.env, self.database, bool(existing and existing.google_token_enc))
        if existing and existing.google_token_enc:
            self.box.decrypt(existing.google_token_enc)
        if self.database != ':memory:':
            os.chmod(self.database, 0o600)
        self.owner = LocalOwner(self.users)
        self.owner.workshop_calendar = True
        self.client_id = self.env.get('GOOGLE_CLIENT_ID', '')
        self.client_secret = self.env.get('GOOGLE_CLIENT_SECRET', '')
        self.oauth = GoogleOAuth(self.client_id, self.client_secret, CALLBACK) if self.google_configured else None
        fingerprint = conn.execute("SELECT value FROM workshop_settings WHERE name='oauth_client'").fetchone()
        if fingerprint and self.google_configured and fingerprint['value'] != self.client_fingerprint:
            raise ValueError('Google OAuth credentials changed. Restore the original credentials or use a fresh DB_PATH.')
        self.pending = {}
        self.lock = threading.RLock()
        self.context = None
        self.context_token = None
        self.session = None
        self.agent = None
        if self.ai_configured:
            set_default_openai_key(self.env['OPENAI_API_KEY'])
            def instructions(ctx, agent):
                return assistant_instructions(ctx, agent).replace('on WhatsApp', 'in this local web app') + (
                    '\nLocal workshop capabilities override references to other services above: '
                    'only the provided Calendar, contacts and memory tools are available. '
                    'Gmail, WhatsApp, Calendly, group links and reminder delivery are unavailable. '
                    'Never claim to access those services. Use /calendar for the direct agenda and '
                    '/connections to reconnect Google. Preserve all booking readback and confirmation rules.')
            base = build_assistant(self.env['OPENAI_MODEL'])
            self.agent = base.clone(name='Pippa workshop', instructions=instructions,
                                    tools=[tool for tool in base.tools if tool.name in CALENDAR_TOOLS])
        self.web = WebAssistant(self.owner, self.run,
            ElevenLabsVoice(self.env.get('ELEVENLABS_API_KEY', ''), self.env.get('ELEVENLABS_VOICE_ID', '')), ORIGIN)

    @property
    def google_configured(self):
        return bool(self.client_id and self.client_secret)

    @property
    def ai_configured(self):
        return bool(self.env.get('OPENAI_API_KEY') and self.env.get('OPENAI_MODEL'))

    @property
    def client_fingerprint(self):
        return _hash(self.client_id + '\0' + self.client_secret)

    def calendar(self):
        user = self.users.get(1)
        if not self.google_configured or not user.google_token_enc:
            raise CalendarAuthError('Connect Google in Connections first.')
        token = user.google_token_enc
        def guard():
            if self.users.get(1).google_token_enc != token:
                raise CalendarAuthError('Google connection changed. Reconnect Google.')
        return GoogleCalendar(build_service(self.client_id, self.client_secret, self.box.decrypt(token)),
            calendar_id=self.env.get('GOOGLE_CALENDAR_ID') or 'primary', before_request=guard)

    async def run(self, user, text):
        if not self.users.get(1).google_token_enc:
            return 'Connect Google Calendar in Connections first, then ask again.'
        if not self.ai_configured:
            return ('Your calendar is connected. Open /calendar to see your events without an AI key. '
                    'For chat and scheduling, set OPENAI_API_KEY and OPENAI_MODEL in .env, then restart the launcher.')
        current = self.users.get(1)
        if self.context is None or self.context_token != current.google_token_enc:
            self.context = AppContext(calendar=self.calendar(), calendly=BookingLink(None), store=self.store,
                now=sgt_now, owner_name=current.first_name,
                reauth_message='ERROR: Google Calendar access expired. Reconnect Google in /connections.')
            self.context_token = current.google_token_enc
        if self.session is None:
            self.session = SQLiteSession('1:workshop', self.database)
        return await run_agent(self.agent, self.context, self.session, text)


def build_workshop_app(env=None):
    workshop = Workshop(os.environ if env is None else env)
    app = FastAPI()
    app.state.workshop = workshop
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1'])
    app.include_router(workshop.web.router)

    @app.middleware('http')
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    def redirect(target):
        return RedirectResponse(target, status_code=303)

    def authenticate(request, csrf=None):
        user, session = workshop.owner._auth(request)
        if not user:
            return None, redirect('/signin')
        if csrf is not None and (request.headers.get('origin') != ORIGIN
                or request.headers.get('sec-fetch-site') == 'cross-site'
                or not csrf.isascii() or not secrets.compare_digest(csrf, session['csrf'])):
            return None, page('Refresh this page', '<p>The request could not be verified. Refresh and try again.</p>', 403)
        return session, None

    @app.get('/')
    def home():
        return redirect('/connections')

    @app.get('/signin')
    def signin():
        nonce = secrets.token_urlsafe(32)
        response = page('Your calendar, on your laptop',
            '<p class="workshop-lead">A little less on your mind. Connect Google to bring your real calendar into one familiar place.</p>'
            '<div class="workshop-steps"><article><span>01</span><h3>Connect your calendar</h3><p>Sign in with Google and choose what to share.</p></article>'
            '<article><span>02</span><h3>See your day</h3><p>Read your actual events, directly from Google.</p></article>'
            '<article><span>03</span><h3>Make room for a meeting</h3><p>Add your AI key to plan and schedule in chat.</p></article></div>'
            '<form method="post" action="/signin"><input type="hidden" name="nonce" value="' + nonce + '">'
            '<button type="submit">Open local assistant</button></form>')
        response.set_cookie('pippa_local_login', sign(nonce, workshop.owner.secret, sgt_now()),
                            max_age=300, httponly=True, samesite='strict')
        return response

    @app.post('/signin')
    def login(request: Request, nonce: str = Form('')):
        expected = unsign(request.cookies.get('pippa_local_login', ''), workshop.owner.secret,
                          sgt_now(), timedelta(minutes=5))
        if (request.headers.get('origin') != ORIGIN or request.headers.get('sec-fetch-site') == 'cross-site'
                or not expected or not nonce.isascii() or not secrets.compare_digest(nonce, expected)):
            return page('Refresh this page', '<p>Open the local sign-in page and try again.</p>', 403)
        response = redirect('/connections')
        response.set_cookie(SESSION_COOKIE, sign('local-owner', workshop.owner.secret, sgt_now()),
                            max_age=14400, httponly=True, samesite='lax')
        response.delete_cookie('pippa_local_login')
        return response

    @app.get('/connections')
    def connections(request: Request):
        session, error = authenticate(request)
        if error is not None:
            return error
        user = workshop.users.get(1)
        missing = [key for key in ('GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET') if not workshop.env.get(key)]
        if missing:
            content = ('<section><h2>One-time Google setup</h2><p>Add these to your private <code>.env</code>: '
                + ', '.join(f'<code>{key}</code>' for key in missing)
                + '.</p><p>Enable Google Calendar API and create a Web application OAuth client. Register this exact redirect:</p>'
                + f'<p><code>{CALLBACK}</code></p><p>Follow the README Google setup steps, then restart this launcher.</p></section>')
        else:
            content = '<section><h2>Google Calendar</h2>'
            if user.google_token_enc:
                content += f'<p>Connected account: <strong>{escape(user.email or "")}</strong>.</p><p><a class="button" href="/calendar">View my calendar</a></p>'
            else:
                content += '<p>Connect your Google account to see your actual calendar events.</p>'
            content += form('/connections/google', 'Reconnect Google' if user.google_token_enc else 'Connect Google', session['csrf']) + '</section>'
        if missing:
            content += form('/connections/google', 'Connect Google', session['csrf'], disabled=True)
        content += ('<section><h2>Chat and scheduling</h2><p>' +
            ('Ready. <a href="/app">Open chat</a> to ask about your day or schedule a meeting.' if workshop.ai_configured else
             'Optional: add <code>OPENAI_API_KEY</code> and <code>OPENAI_MODEL</code> to <code>.env</code> and restart. Viewing your calendar needs no AI key.') + '</p></section>')
        return page('Connections', content, user=user)

    @app.post('/connections/google')
    def google_start(request: Request, csrf: str = Form('')):
        session, error = authenticate(request, csrf)
        if error is not None:
            return error
        if not workshop.google_configured:
            return page('Google setup needed', '<p>Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env, then restart.</p>', 503)
        now = sgt_now()
        state, browser, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        with workshop.lock:
            workshop.pending = {key: value for key, value in workshop.pending.items() if value['expires'] > now}
            if len(workshop.pending) >= 10:
                return page('Try again shortly', '<p>Too many sign-in attempts. Wait ten minutes and try again.</p>', 429)
            workshop.pending[_hash(state)] = dict(browser=_hash(browser), verifier=verifier,
                session=_hash(request.cookies.get(SESSION_COOKIE, '')), expires=now + timedelta(minutes=10))
        response = RedirectResponse(workshop.oauth.connection_auth_url(state, verifier, 'calendar', workshop.users.get(1).email), status_code=302)
        response.set_cookie(OAUTH_COOKIE, browser, max_age=600, httponly=True, samesite='lax')
        return response

    @app.get('/signup/callback')
    def callback(request: Request, state: str = '', code: str = '', error: str = ''):
        with workshop.lock:
            intent = workshop.pending.pop(_hash(state), None)
        session, auth_error = authenticate(request)
        def failed(message):
            response = page('Google connection not changed', f'<p>{escape(message)}</p><p><a href="/connections">Return to Connections</a></p>', 400)
            response.delete_cookie(OAUTH_COOKIE)
            return response
        if (not intent or auth_error is not None or intent['expires'] <= sgt_now()
                or not secrets.compare_digest(intent['browser'], _hash(request.cookies.get(OAUTH_COOKIE, '')))
                or not secrets.compare_digest(intent['session'], _hash(request.cookies.get(SESSION_COOKIE, '')))):
            return failed('That sign-in expired or belongs to another browser. Please try again.')
        if error or not code:
            return failed('You did not allow access. No connection was changed.')
        try:
            account = workshop.oauth.exchange_connection(code, intent['verifier'])
            if (not isinstance(account.subject, str) or not account.subject
                    or not isinstance(account.refresh_token, str) or not account.refresh_token
                    or CALENDAR_SCOPE not in (account.granted_scopes or ())):
                return failed('Google Calendar permission and long-term access are required. Reconnect and allow Calendar access.')
            with workshop.lock:
                current = workshop.users.get(1)
                if current.google_subject and current.google_subject != account.subject:
                    return failed('This is a different Google account. Use the original account or a fresh DB_PATH for another person.')
                # Keep the token and the client that issued it in one transaction.
                with workshop.store.conn:
                    workshop.store.conn.execute(
                        "UPDATE users SET name=?, email=?, google_subject=?, google_token_enc=?, "
                        "google_scopes=?, google_disconnected=0, calendar_status='connected', updated_at=? WHERE id=1",
                        (account.name, account.email, account.subject, workshop.box.encrypt(account.refresh_token),
                         json.dumps(account.granted_scopes), sgt_now().isoformat()))
                    workshop.store.conn.execute("INSERT OR REPLACE INTO workshop_settings VALUES ('oauth_client', ?)", (workshop.client_fingerprint,))
        except GoogleOAuthError:
            return failed('Google could not complete sign-in. Check your OAuth client, test-user email and Calendar permission, then try again.')
        response = redirect('/calendar')
        response.delete_cookie(OAUTH_COOKIE)
        return response

    @app.get('/calendar')
    def calendar(request: Request, day: str = ''):
        session, error = authenticate(request)
        if error is not None:
            return error
        if not workshop.users.get(1).google_token_enc or not workshop.google_configured:
            return redirect('/connections')
        try:
            selected = date.fromisoformat(day) if day else sgt_now().date()
            start = datetime.combine(selected, time.min, SGT)
            end = start + timedelta(days=1)
        except (ValueError, OverflowError):
            return page('Choose a valid date', '<p>Use a date in YYYY-MM-DD format.</p>', 400)
        try:
            events = workshop.calendar().list_events(start, end, limit=100)
        except CalendarAuthError:
            return page('Reconnect Google', '<p>Calendar access expired or was revoked. <a href="/connections">Reconnect Google</a> and try again.</p>', 502)
        except Exception:
            return page('Calendar unavailable', '<p>Google Calendar could not be read. Check that Calendar API is enabled and the selected calendar is accessible, then try again. No cached events are shown.</p><p><a href="/connections">Check connection</a></p>', 502)
        content = (f'<p>{escape(workshop.users.get(1).email or "")} · <strong>Asia/Singapore</strong></p>'
            f'<p class="muted">Calendar: {escape(workshop.env.get("GOOGLE_CALENDAR_ID") or "primary")} · Read from Google on this request.</p>'
            '<form method="get" action="/calendar"><label for="day">Date</label>'
            f'<input id="day" name="day" type="date" value="{selected.isoformat()}"> <button>Show events</button></form><section>')
        for event in events:
            timing = 'All day' if event.all_day else f'{event.start:%H:%M}–{event.end:%H:%M}'
            content += f'<article class="event"><h2>{escape(event.title)}</h2><p>{timing}</p>'
            if event.location:
                content += f'<p>{escape(event.location)}</p>'
            content += '</article>'
        content += ('<p>No events on this date in the selected calendar.</p>' if not events else '') + '</section>'
        if len(events) == 100:
            content += '<p>Showing the first 100 events. Open Google Calendar to inspect additional events.</p>'
        content += '<p><a class="button" href="/app">Chat &amp; schedule a meeting</a></p>'
        return page(f'Your calendar · {selected:%d %b %Y}', content, user=workshop.users.get(1), current='calendar')

    @app.get('/connections/chat')
    def chat():
        return redirect('/app')

    @app.get('/health')
    def health():
        return {'ok': True, 'mode': 'workshop', 'google_configured': workshop.google_configured, 'ai_configured': workshop.ai_configured}

    @app.get('/privacy')
    def privacy():
        return page('Local workshop privacy', '<p>Your Google token is encrypted in the local database. Calendar events are read from Google. When you use AI chat, its conversation and tool results are sent to your configured AI provider. Calendar changes and invitations are real. Keep this app on your laptop and never expose it publicly.</p>')

    return app
