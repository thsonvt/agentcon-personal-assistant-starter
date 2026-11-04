"""Local owner's web transport, using the same runtime and real provider clients.

Used only by scripts/run_web_local.py, which binds to loopback. Public main:app
does not load this local owner session or expose administrator web access.
"""
import secrets
from datetime import timedelta
from html import escape

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.connections import SESSION_COOKIE
from app.crypto import sign, unsign
from app.wiring import sgt_now


class LocalOwner:
    local_owner_id = 1

    def __init__(self, users):
        self.users, self.now = users, sgt_now
        self.secret = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)

    def _auth(self, request):
        token = request.cookies.get(SESSION_COOKIE, '')
        if unsign(token, self.secret, self.now(), timedelta(hours=4)) != 'local-owner':
            return None, None
        user = self.users.get(self.local_owner_id)
        if user is None or not user.is_admin or user.status != 'active':
            return None, None
        return user, {'csrf': self.csrf}

    def _post_auth(self, request, csrf):
        user, session = self._auth(request)
        if not user:
            return None, None, RedirectResponse('/signin', status_code=303)
        if not csrf.isascii() or not secrets.compare_digest(csrf, self.csrf):
            return user, session, HTMLResponse('Refresh the page before continuing.', status_code=403)
        return user, session, None


def build_local_web(runtime):
    service = runtime.web_assistant
    if service is None:
        raise ValueError('The existing backend needs the complete sign-up configuration for web chat.')
    owner = LocalOwner(runtime.users)
    service.connections = owner
    service.origin = 'http://127.0.0.1:8016'
    app = FastAPI()
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1'])
    @app.middleware('http')
    async def local_headers(request, call_next):
        response = await call_next(request)
        # Native form POSTs get Origin:null under no-referrer in Chromium.
        response.headers['Referrer-Policy'] = 'same-origin'
        return response
    app.include_router(service.router)
    groups = runtime.group_meetings
    if groups is not None:
        groups.connections = owner
        groups.base_url = service.origin
        previous_calendar = groups.calendar_for_user

        def calendar_for_user(user_id):
            if user_id == owner.local_owner_id:
                groups._ready(user_id)
                return runtime.app_ctx.calendar
            return previous_calendar(user_id)

        groups.calendar_for_user = calendar_for_user
        app.include_router(groups.router)
        if runtime.chat_scheduling is not None:
            runtime.chat_scheduling.connections = owner
            app.include_router(runtime.chat_scheduling.router)

    @app.get('/signin')
    def signin(request: Request):
        nonce = secrets.token_urlsafe(32)
        response = HTMLResponse('<!doctype html><html><head><meta name="viewport" '
            'content="width=device-width,initial-scale=1"><title>Open Pippa</title></head>'
            '<body style="background:#f8f5ef;color:#332d27;font:18px system-ui;max-width:560px;margin:12vh auto;padding:24px">'
            '<h1>Pippa</h1><p>This local app uses your existing assistant, Calendar and ElevenLabs credentials.</p>'
            '<form method="post" action="/signin"><input type="hidden" name="nonce" value="' + escape(nonce) + '">'
            '<button style="padding:14px 24px;font:inherit" type="submit">Open Pippa</button></form></body></html>',
            headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
        response.set_cookie('pippa_local_login', sign(nonce, owner.secret, owner.now()),
                            max_age=300, httponly=True, secure=False, samesite='strict')
        return response

    @app.post('/signin')
    def sign_in_submit(request: Request, nonce: str = Form('')):
        expected = unsign(request.cookies.get('pippa_local_login', ''), owner.secret,
                          owner.now(), timedelta(minutes=5))
        if (request.headers.get('sec-fetch-site') == 'cross-site'
                or request.headers.get('origin') != service.origin
                or not expected or not nonce.isascii() or not secrets.compare_digest(nonce, expected)):
            return HTMLResponse('Refresh this page before signing in.', status_code=403)
        response = RedirectResponse('/app', status_code=303)
        response.set_cookie(SESSION_COOKIE, sign('local-owner', owner.secret, owner.now()),
                            max_age=14400, httponly=True, secure=False, samesite='strict')
        response.delete_cookie('pippa_local_login')
        return response

    @app.get('/health')
    def health():
        return {'ok': True, 'mode': 'local-owner', 'voice': {
            'stt': service.voice.stt_enabled, 'tts': service.voice.tts_enabled}}

    @app.get('/connections')
    def connections(request: Request):
        user, session = owner._auth(request)
        if not user:
            return RedirectResponse('/signin', status_code=303)
        return HTMLResponse('<h1>Pippa local connections</h1><p>This app uses your existing backend’s '
                            'Google, OpenAI and ElevenLabs settings. Calendar changes are real.</p>'
                            '<p><a href="/connections">Manage Google connections</a></p>'
                            '<p><a href="/app">Return to Pippa</a></p>', headers={'Cache-Control': 'no-store'})

    @app.get('/privacy')
    def privacy():
        from app.pages import privacy as page
        return HTMLResponse(page())

    return app
