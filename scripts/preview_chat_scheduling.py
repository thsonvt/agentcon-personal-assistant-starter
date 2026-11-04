"""Synthetic chat/Calendar demo. No API credentials, model calls or invitations."""
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.chat_scheduling import ChatScheduling
from app.connections import Connections
from app.crypto import TokenBox, generate_key
from app.group_meetings import GroupMeetings
from app.pages import privacy
from app.store import Store
from app.users import Users, CALENDAR_SCOPE
from scripts.preview_group_meetings import DemoCalendar

BASE = 'http://localhost:8834'


async def demo_interpreter(messages, now):
    """Only recognizes the documented synthetic Alice/Bob script, not general NLP."""
    supplied = [m['content'] for m in messages if m['role']=='user']
    text = '\n'.join(supplied)
    result = dict(title='Test group call',duration_min=60,timezone='Asia/Singapore',location=None,clarification=None,guests=[])
    if 'night' in text.lower() and 'morning' not in text.lower():
        result['clarification']='Did Alice mean 9:30 a.m. to noon, or an evening window?'
        return result
    if 'alice@example.com' not in text:
        result['clarification']='Please paste Alice’s availability and email, then Bob’s reply.'
        return result
    source_a = next(m for m in supplied if 'alice@example.com' in m)
    alice = dict(name='Alice',email='alice@example.com',windows=[dict(start='2026-10-12T09:30:00',end='2026-10-12T12:00:00',timezone='Asia/Singapore',kind='window',source=source_a)])
    if 'bob@example.com' not in text:
        result['guests']=[alice]
        result['clarification']='What time works for Bob, and what is his email address?'
        return result
    source_b=next(m for m in reversed(supplied) if 'bob@example.com' in m)
    day='13' if '13 October' in source_b else '12'
    bob = dict(name='Bob',email='bob@example.com',windows=[dict(start=f'2026-10-{day}T10:00:00',end=None,timezone='Asia/Singapore',kind='start',source=source_b)])
    result['guests']=[alice,bob]
    return result


def create_preview():
    now=lambda:datetime.now(timezone.utc)
    users=Users(Store(':memory:'));box=TokenBox(generate_key())
    users.ensure_admin('Preview admin','6590000000',now())
    user,_=users.sign_in_identity('preview-host','Son (demo)','host@example.com',now())
    users._update(user.id,now(),status='approved',google_token_enc=box.encrypt('fake-refresh'),google_scopes=json.dumps([CALENDAR_SCOPE]),calendar_status='connected')
    connections=Connections(None,users,box,SimpleNamespace(),'',now,test_number_mode=False)
    calendar=DemoCalendar()
    groups=GroupMeetings(connections,users,lambda uid:calendar,now,BASE,'preview-only-secret'*4)
    chats=ChatScheduling(groups,demo_interpreter)
    app=FastAPI()
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=['localhost','127.0.0.1','testserver'])
    @app.middleware('http')
    async def banner(request,call_next):
        response=await call_next(request)
        if 'text/html' not in response.headers.get('content-type',''):return response
        content=b''.join([chunk async for chunk in response.body_iterator]).decode()
        notice='<aside style="padding:10px 20px;background:#fff0cc;font:13px system-ui">Local demo — simulated AI and Calendar. No real invitations.</aside>'
        headers={k:v for k,v in response.headers.items() if k.lower() not in ('content-length','set-cookie')}
        result=HTMLResponse(content.replace('<body>','<body>'+notice,1),status_code=response.status_code,headers=headers)
        result.raw_headers.extend((k,v) for k,v in response.raw_headers if k.lower()==b'set-cookie')
        return result
    @app.get('/__preview/host')
    def host():
        token=secrets.token_urlsafe(32)
        users.conn.execute('INSERT INTO connection_sessions VALUES (?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),user.id,secrets.token_urlsafe(32),(now()+timedelta(hours=4)).isoformat()));users.conn.commit()
        response=RedirectResponse('/connections/chat',303);response.set_cookie('pippa_session',token,httponly=True,samesite='lax');return response
    @app.get('/__preview/events')
    def events():return {'simulated':True,'count':len(calendar.events),'attendees':list(calendar.attendees.values())}
    @app.get('/privacy')
    def privacy_page():return HTMLResponse(privacy())
    app.include_router(connections.router);app.include_router(chats.router);app.include_router(groups.router)
    return app
