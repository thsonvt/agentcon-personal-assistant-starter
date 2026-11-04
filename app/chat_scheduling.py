"""Host-scoped scheduling chat. Model output is data, never booking authority."""
import asyncio
import json
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from app.booking import BOOKING_LOCK
from app.chat_pages import render_chat_page
from app.group_meetings import EMAIL, HEADERS, UTC, _clean, _when, _zone
from app.router import RateLimiter
from app.tools.calendar import CalendarError

MAX_MESSAGE = 4000
MAX_TRANSCRIPT = 24000
MAX_MESSAGES = 30
PARSE_TIMEOUT = 30


class StaleChat(ValueError):
    pass


def _local_time(value, zone):
    if not isinstance(value, str) or len(value)>40 or 'T' not in value:
        raise ValueError('Please provide a complete date and time, including the year.')
    result=_when(value,zone)
    raw=datetime.fromisoformat(value.replace('Z','+00:00'))
    if raw.tzinfo is not None and raw.utcoffset()!=result.astimezone(zone).utcoffset():
        raise ValueError('The time offset does not match its timezone. Please clarify the time.')
    return result


def _extract(data, messages, user, now):
    """Validate independent of the model; return guest windows in UTC."""
    if not isinstance(data,dict):raise ValueError('Please clarify the two guests and their availability.')
    if data.get('clarification'):
        question=data['clarification']
        if not isinstance(question,str) or len(question)>500:raise ValueError('Please clarify the dates, times and guest details.')
        # This text is a question only. It never supplies booking status.
        raise ValueError('I need a little more detail: '+question)
    if not data.get('title'):raise ValueError('What is the meeting title?')
    title=_clean(data['title'],120,'meeting title')
    if data.get('duration_min') is None:raise ValueError('Should this meeting last 30 or 60 minutes?')
    duration=data['duration_min']
    if type(duration) is not int or duration not in (30,60):raise ValueError('Choose a meeting duration of 30 or 60 minutes.')
    if not data.get('timezone'):raise ValueError('Which timezone should I use for the meeting?')
    zone=_zone(data['timezone'])
    location=_clean(data.get('location') or '',500,'location',False)
    raw_guests=data.get('guests')
    if not isinstance(raw_guests,list) or len(raw_guests)!=2:raise ValueError('Please provide exactly two guests, with names, email addresses and availability.')
    transcript='\n'.join(m['content'] for m in messages if m['role']=='user')
    supplied_emails={m.group().lower() for m in EMAIL.finditer(transcript)}
    guests=[];sources=[]
    for guest in raw_guests:
        if not isinstance(guest,dict):raise ValueError('Please provide each guest’s name, email and availability.')
        if not guest.get('name'):raise ValueError('What is each guest’s name?')
        name=_clean(guest['name'],80,'guest name')
        if name.casefold() not in transcript.casefold():raise ValueError('Please include each guest’s name in your message.')
        if not guest.get('email'):raise ValueError('What is the email address for '+name+'?')
        email=_clean(guest['email'],254,'email').lower()
        if not EMAIL.fullmatch(email) or email not in supplied_emails:raise ValueError('Please paste the exact email address for '+name+'.')
        raw_windows=guest.get('windows')
        if not isinstance(raw_windows,list) or not raw_windows:raise ValueError('What dates and times work for '+name+'? Please include the timezone.')
        if len(raw_windows)>14:raise ValueError('Please provide at most 14 availability windows for each guest.')
        windows=[]
        for w in raw_windows:
            if not isinstance(w,dict):raise ValueError('Please clarify the availability dates and times.')
            source=w.get('source')
            if not isinstance(source,str) or not source.strip() or len(source)>MAX_MESSAGE or source not in transcript:
                raise ValueError('Please paste the original availability for '+name+' so I can verify the proposed time.')
            if not w.get('timezone'):raise ValueError('What timezone applies to '+name+'’s availability?')
            guest_zone=_zone(w['timezone']);start=_local_time(w.get('start'),guest_zone)
            if w.get('kind')=='start':
                if w.get('end') is not None:raise ValueError('Please clarify whether that is an exact start or a time window.')
                end=start+timedelta(minutes=duration)
            elif w.get('kind')=='window':end=_local_time(w.get('end'),guest_zone)
            else:raise ValueError('Please clarify whether that is an exact start or a time window.')
            if start<now+timedelta(hours=2) or end>now+timedelta(days=30) or end<=start:
                raise ValueError('Use future availability at least two hours ahead and within the next 30 days.')
            if end-start>timedelta(hours=24):raise ValueError('Please give separate daily windows rather than one continuous multi-day interval.')
            windows.append((start,end))
            evidence=name+' ('+str(guest_zone)+'): '+source
            if evidence not in sources:sources.append(evidence)
        guests.append(dict(name=name,email=email,windows=windows))
    if len({user.email.lower(),*(g['email'] for g in guests)})!=3:
        raise ValueError('Use two different guest emails, both different from your own email.')
    return dict(title=title,duration_min=duration,timezone=str(zone),location=location,guests=guests,sources=sources)


class ChatScheduling:
    def __init__(self,groups,interpreter):
        self.groups,self.interpreter=groups,interpreter
        self.conn,self.connections=groups.conn,groups.connections
        self.parse_limiter=RateLimiter(limit=20,window=timedelta(hours=1))
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS scheduling_chats (
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,revision INTEGER NOT NULL,data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS scheduling_chats_user ON scheduling_chats(user_id);
            CREATE TRIGGER IF NOT EXISTS scheduling_chats_delete_user AFTER DELETE ON users BEGIN
                DELETE FROM scheduling_chats WHERE user_id=OLD.id;
            END;
        ''')
        self.conn.commit()
        self.router=APIRouter();self._routes()

    def _load(self,user_id,chat_id):
        user=self.groups._ready(user_id)
        row=self.conn.execute('SELECT data FROM scheduling_chats WHERE id=? AND user_id=?',(chat_id,user_id)).fetchone()
        if not row:raise ValueError('Chat unavailable. Start a new chat.')
        c=json.loads(row['data'])
        if c['identity']!=(user.google_subject or user.email):
            raise ValueError('Your connected Google account changed. Start a new chat.')
        if c.get('meeting_id'):
            m=self.groups.host_view(user_id,c['meeting_id'])
            if m['status'] in ('pending','booked'):c['status']=m['status']
        return c

    def _save(self,user_id,c,expected):
        # UPDATE only: a late provider/model completion cannot recreate a deleted chat.
        result=self.conn.execute('''UPDATE scheduling_chats SET revision=?,data=?
            WHERE id=? AND user_id=? AND revision=? AND EXISTS (SELECT 1 FROM users WHERE id=?)''',
            (c['revision'],json.dumps(c),c['id'],user_id,expected,user_id))
        self.conn.commit()
        if result.rowcount!=1:raise StaleChat('The chat changed. Refresh before continuing.')

    def create(self,user_id):
        with BOOKING_LOCK:
            user=self.groups._ready(user_id)
            count=self.conn.execute('SELECT COUNT(*) FROM scheduling_chats WHERE user_id=?',(user_id,)).fetchone()[0]
            if count>=100:raise ValueError('The chat limit has been reached.')
            c=dict(id=secrets.token_hex(16),revision=0,messages=[],proposal=None,status='collecting',meeting_id=None,
                   identity=user.google_subject or user.email,created_at=self.groups.now().isoformat())
            self.conn.execute('INSERT INTO scheduling_chats VALUES (?,?,?,?)',(c['id'],user_id,0,json.dumps(c)))
            self.conn.commit()
            return self._view(user_id,c)

    def _view(self,user_id,c):
        return dict(id=c['id'],revision=c['revision'],messages=c['messages'],proposal=c['proposal'],status=c['status'],
                    meeting=self.groups.host_view(user_id,c['meeting_id']) if c.get('meeting_id') else None)

    def view(self,user_id,chat_id):
        with BOOKING_LOCK:return self._view(user_id,self._load(user_id,chat_id))

    @staticmethod
    def _revision(c,revision):
        if str(c['revision'])!=str(revision):raise StaleChat('The chat changed. Refresh and review the latest proposal before continuing.')

    def _begin_message(self,user_id,chat_id,revision,message):
        with BOOKING_LOCK:
            c=self._load(user_id,chat_id);self._revision(c,revision)
            if c['status'] in ('pending','booked'):raise ValueError('Check this booking before starting a new chat for another meeting.')
            if not isinstance(message,str) or not message.strip() or len(message)>MAX_MESSAGE or any(ord(ch)<32 and ch not in '\n\r\t' for ch in message):
                raise ValueError('Enter a message of at most 4,000 characters.')
            if len(c['messages'])>=MAX_MESSAGES or sum(len(m['content']) for m in c['messages'])+len(message)>MAX_TRANSCRIPT:
                raise ValueError('This conversation is full. Start a new chat with the current details.')
            if not self.parse_limiter.allow(str(user_id),self.groups.now()):raise ValueError('Too many chat messages. Please try again later.')
            previous=c['revision']
            if c.get('meeting_id'):self.groups.cancel(user_id,c['meeting_id'])
            c.update(revision=previous+1,proposal=None,meeting_id=None,status='collecting')
            c['messages'].append(dict(role='user',content=message.strip()))
            self._save(user_id,c,previous)
            return c

    async def message(self,user_id,chat_id,revision,message):
        c=await run_in_threadpool(self._begin_message,user_id,chat_id,revision,message)
        # No database lock or blocking calendar operation across this await.
        try:
            result=await asyncio.wait_for(self.interpreter(c['messages'],self.groups.now()),timeout=PARSE_TIMEOUT)
            error=None
        except Exception:
            result=None;error='I could not interpret that message right now. Please try again with the dates, timezones and guest details.'
        return await run_in_threadpool(self._finish_message,user_id,chat_id,c['revision'],result,error)

    def _finish_message(self,user_id,chat_id,revision,result,error):
        with BOOKING_LOCK:
            c=self._load(user_id,chat_id)
            if c['revision']!=revision:return self._view(user_id,c)
            try:
                if error:raise ValueError(error)
                parsed=_extract(result,c['messages'],self.groups._ready(user_id),self.groups.now())
                duration=timedelta(minutes=parsed['duration_min'])
                intersections=sorted((max(a,x),min(b,y)) for a,b in parsed['guests'][0]['windows']
                    for x,y in parsed['guests'][1]['windows'] if min(b,y)-max(a,x)>=duration)
                if not intersections:raise ValueError('There is no common time in those windows. What alternative dates or times work for both guests?')
                busy=self.groups._busy(user_id,intersections[0][0],max(end for _,end in intersections))
                selected=None
                for start,end in intersections:
                    cursor=start
                    for left,right in sorted(busy):
                        if right<=cursor:continue
                        if left>=cursor+duration:break
                        cursor=max(cursor,right)
                    if cursor+duration<=end:
                        selected=(cursor,cursor+duration);break
                if selected is None:raise ValueError('There is no common time free on your calendar. What alternative dates or times work for both guests?')
                proposal={**parsed,'guests':[dict(name=g['name'],email=g['email']) for g in parsed['guests']],
                          'start':selected[0].isoformat(),'end':selected[1].isoformat()}
                # Identity may change while Calendar availability is in flight.
                self._load(user_id,chat_id)
                m=self.groups.create_host_proposal(user_id,proposal)
                c.update(proposal=proposal,meeting_id=m['id'],status='ready')
                response='I found one time that fits the availability you provided and your calendar. Review the guest emails, timezones and source text, then confirm to send invitations.'
            except ValueError as exc:response=str(exc)
            except CalendarError:response='I could not check your calendar. Please check your Calendar connection and try again.'
            c['messages'].append(dict(role='assistant',content=response));c['revision']+=1
            self._save(user_id,c,revision)
            return self._view(user_id,c)

    def confirm(self,user_id,chat_id,revision):
        with BOOKING_LOCK:
            c=self._load(user_id,chat_id);self._revision(c,revision)
            if c['status'] in ('pending','booked'):return self._view(user_id,c)
            if c['status']!='ready' or not c.get('meeting_id'):raise ValueError('There is no current proposal to confirm.')
            m=self.groups.confirm_host_proposal(user_id,c['meeting_id'])
            c['status']=m['status'];self._save(user_id,c,c['revision'])
            return self._view(user_id,c)

    def check(self,user_id,chat_id,revision):
        with BOOKING_LOCK:
            c=self._load(user_id,chat_id);self._revision(c,revision)
            if c.get('meeting_id') and c['status'] in ('pending','booked'):
                m=self.groups.check(user_id,c['meeting_id']);c['status']=m['status']
                self._save(user_id,c,c['revision'])
            return self._view(user_id,c)

    def _page(self,session=None,chat=None,error='',status=200):
        return HTMLResponse(render_chat_page(dict(csrf_token=session['csrf'] if session else '',chat=chat,error=error,notice='')),
                            status_code=status,headers=HEADERS)

    def _get(self,request,chat_id=None):
        user,session=self.connections._auth(request)
        if not user:return RedirectResponse('/signin',status_code=303,headers=HEADERS)
        try:
            self.groups._ready(user.id)
            return self._page(session,self.view(user.id,chat_id) if chat_id else None)
        except ValueError as exc:return self._page(session,error=str(exc),status=404 if chat_id else 400)

    async def _post(self,request,chat_id=None,action='message'):
        form,error=await self.groups._form(request)
        if error is not None:return error
        # _post_auth expects ASCII CSRF tokens; reject arbitrary Unicode safely.
        csrf=str(form.get('csrf_token',''))
        if not csrf.isascii():return self._page(error='Refresh the page before continuing.',status=403)
        user,session,error=await run_in_threadpool(self.connections._post_auth,request,csrf)
        if error is not None:return error
        try:
            revision=str(form.get('revision',''))
            if not revision.isdecimal() or len(revision)>8:raise StaleChat('Refresh the chat before continuing.')
            if chat_id is None:
                if revision!='0':raise StaleChat('Refresh the chat before continuing.')
                c=await run_in_threadpool(self.create,user.id);chat_id=c['id']
            if action=='message':c=await self.message(user.id,chat_id,revision,str(form.get('message','')))
            elif action=='confirm':c=await run_in_threadpool(self.confirm,user.id,chat_id,revision)
            elif action=='check':c=await run_in_threadpool(self.check,user.id,chat_id,revision)
            else:return self._page(session,error='Unknown action.',status=404)
            return RedirectResponse('/connections/chat/'+c['id'],status_code=303,headers=HEADERS)
        except (ValueError,CalendarError) as exc:
            c=None
            if chat_id:
                try:c=await run_in_threadpool(self.view,user.id,chat_id)
                except ValueError:pass
            error='Calendar availability could not be checked. Please try again.' if isinstance(exc,CalendarError) else str(exc)
            return self._page(session,c,error,status=409 if isinstance(exc,StaleChat) else 503 if isinstance(exc,CalendarError) else 400)

    def _routes(self):
        @self.router.get('/connections/chat')
        def index(request:Request):return self._get(request)
        @self.router.get('/connections/chat/{chat_id}')
        def chat(request:Request,chat_id:str):return self._get(request,chat_id)
        @self.router.post('/connections/chat')
        async def create(request:Request):return await self._post(request)
        @self.router.post('/connections/chat/{chat_id}/{action}')
        async def action(request:Request,chat_id:str,action:str):return await self._post(request,chat_id,action)
