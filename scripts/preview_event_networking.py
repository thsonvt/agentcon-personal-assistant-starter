"""Isolated local attendee preview. No credentials, provider requests or real messages.

Run: uvicorn scripts.preview_event_networking:create_preview --factory --host 127.0.0.1 --port 8833
Open http://localhost:8833/__preview in separate browser profiles to try both participants.
This factory is not mounted by production wiring. All data disappears on exit.
"""
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.connections import Connections, SESSION_AGE
from app.event_networking import EventNetworking
from app.pages import privacy
from app.public_booking import PublicBooking
from app.store import Store
from app.users import Users

BASE_URL = 'http://localhost:8833'


def create_preview():
    base_url = os.environ.get('EVENT_PREVIEW_BASE_URL', BASE_URL).rstrip('/')
    origin = urlsplit(base_url)
    local = origin.hostname in ('localhost', '127.0.0.1', 'testserver')
    if (not origin.hostname or origin.username or origin.password or origin.path
            or origin.query or origin.fragment or origin.scheme not in ('http', 'https')
            or (not local and origin.scheme != 'https')):
        raise ValueError('Preview URL must be an HTTPS origin (HTTP is allowed only locally).')
    secure = origin.scheme == 'https'
    now = lambda: datetime.now(timezone.utc)
    users = Users(Store(':memory:'), max_users=20)
    users.ensure_admin('Preview organizer','6590000000',now())
    connections = Connections(None, users, None, SimpleNamespace(), '', now)
    def no_provider(user_id):
        raise AssertionError('This preview never calls Calendar providers.')
    booking = PublicBooking(connections, users, no_provider, now, base_url, secrets.token_urlsafe(32))
    events = EventNetworking(connections, users, booking, now, base_url)
    events.store.create_event('builders-night','Builders Night, Singapore', now() + timedelta(hours=4))
    people = {}
    seed = [('alice', 'Alice Tan', 'Making knowledge easier to find. Currently exploring search, small models and great questions.', ['coffee','technical']),
            ('bob', 'Bob Le', 'A WhatsApp assistant that helps busy people make time for the conversations that matter.', ['technical','collaboration']),
            ('maya', 'Maya Lim', 'Designing useful AI products for people who do not spend their days thinking about AI.', ['coffee','work']),
            ('ravi', 'Ravi Kumar', 'Reliable agents, evaluations and developer tools. Always up for learning by building.', ['technical','collaboration'])]
    for handle, name, building, interests in seed:
        person, _ = users.sign_in_event_identity('demo-'+handle, name, handle+'@example.com', now())
        people[handle] = person
        events.store.import_roster('builders-night', [dict(source_id=handle,email=person.email,name=name)], actor_id=1)
        events.store.check_in('builders-night',handle,'demo-checkin-'+handle,actor_id=1)
        events.store.claim('builders-night',person)
        events.store.update_profile('builders-night',person.id,name,building,interests,'event',True,username=handle)
    newcomer, _ = users.sign_in_event_identity('demo-new', 'New attendee', 'new@example.com', now())
    people['new'] = newcomer
    events.store.import_roster('builders-night',[dict(source_id='new',email=newcomer.email,name=newcomer.name)],actor_id=1)
    events.store.check_in('builders-night','new','demo-checkin-new',actor_id=1)
    events.store.send_followup('builders-night',people['maya'].id,people['alice'].id,'coffee',
                              'Your approach to knowledge search sounds useful. I’d love to compare notes over coffee.', 'demo-maya-alice')
    app = FastAPI()
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[
        origin.hostname, 'localhost', '127.0.0.1', 'testserver', 'healthcheck.railway.app',
    ])

    @app.get('/health')
    def health(): return {'ok': True, 'mode': 'demo'}

    @app.middleware('http')
    async def banner(request, call_next):
        response = await call_next(request)
        if 'text/html' not in response.headers.get('content-type',''):
            return response
        body = b''.join([chunk async for chunk in response.body_iterator]).decode()
        label = 'Local demo' if local else 'Shared demo'
        notice = ('<aside style="padding:12px 20px;background:#fff0cc;color:#493600;font:13px system-ui">'
                  f'<strong>{label} — fictional attendees.</strong> Use sample information only. '
                  'No real Google or Calendar calls. Data resets when the demo restarts. '
                  '<a href="/__preview">Switch demo person</a></aside>')
        headers = {key:value for key,value in response.headers.items() if key.lower() not in ('content-length','set-cookie')}
        result = HTMLResponse(body.replace('<body>','<body>'+notice,1),status_code=response.status_code,headers=headers)
        for key,value in response.raw_headers:
            if key.lower() == b'set-cookie': result.raw_headers.append((key,value))
        return result

    @app.get('/')
    def home(): return RedirectResponse('/__preview',status_code=303)

    @app.get('/__preview')
    def select():
        options = ''.join('<li><a href="/__preview/as/'+handle+'">'+person.name+'</a></li>' for handle,person in people.items())
        return HTMLResponse('<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width,initial-scale=1">'
                            '<title>Event preview</title></head><body><main style="max-width:600px;margin:40px auto;padding:20px;font:16px/1.7 system-ui">'
                            '<h1>Try a conversation from both sides</h1><p>Use a different browser profile for each person. Your choice applies only to this browser.</p>'
                            '<ul>'+options+'</ul><p><a href="/__preview/anonymous">View as a visitor</a></p><p>New attendee starts at the claim screen. Others have editable event-only profiles.</p></main></body></html>')

    @app.get('/__preview/as/{handle}')
    def identity(handle: str):
        if handle not in people: return HTMLResponse('Unknown demo person.',status_code=404)
        token = secrets.token_urlsafe(32)
        users.conn.execute('INSERT INTO connection_sessions VALUES (?,?,?,?)',
            (hashlib.sha256(token.encode()).hexdigest(),people[handle].id,secrets.token_urlsafe(32),(now()+SESSION_AGE).isoformat()))
        users.conn.commit()
        target = '/events/builders-night/claim' if handle == 'new' else '/events/builders-night/directory'
        response = RedirectResponse(target,status_code=303)
        response.set_cookie('pippa_session',token,httponly=True,samesite='lax',secure=secure)
        return response

    @app.get('/__preview/anonymous')
    def anonymous():
        response = RedirectResponse('/events/builders-night',status_code=303)
        response.delete_cookie('pippa_session',secure=secure,httponly=True,samesite='lax')
        return response

    @app.get('/signin/google')
    @app.get('/signin')
    def signin(): return RedirectResponse('/__preview',status_code=303)

    @app.get('/privacy')
    def privacy_page(): return HTMLResponse(privacy())

    app.include_router(events.router)
    app.include_router(booking.router)
    return app
