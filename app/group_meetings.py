"""Private host-plus-two-guest coordination with durable, single-write booking.

Guest links are bearer capabilities, not proof of email ownership. Links and
votes are revision-scoped. Unknown Google writes are looked up, never repeated.
All provider I/O runs in FastAPI's thread pool under the shared booking lock.
"""
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from app.booking import BOOKING_LOCK
from app.group_pages import render_group_page
from app.router import RateLimiter
from app.signup import _client_ip
from app.tools.calendar import CalendarError, Event

UTC = dt_timezone.utc
COOKIE = 'pippa_group_browser'
HEADERS = {'Cache-Control':'no-store', 'Referrer-Policy':'same-origin', 'X-Content-Type-Options':'nosniff', 'X-Robots-Tag':'noindex, nofollow'}
EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+")


def _clean(value, limit, label, required=True):
    value = str(value).strip()
    if len(value)>limit or (required and not value) or any(ord(c)<32 or ord(c)==127 for c in value):
        raise ValueError('Enter a valid '+label+'.')
    return value


def _zone(value):
    try:
        return ZoneInfo(value)
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise ValueError('Choose a valid timezone.') from None


def _when(value, zone):
    try:
        parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.tzinfo is None:
            first, second = parsed.replace(tzinfo=zone,fold=0), parsed.replace(tzinfo=zone,fold=1)
            if first.utcoffset()!=second.utcoffset() or first.astimezone(UTC).astimezone(zone).replace(tzinfo=None)!=parsed:
                raise ValueError()
            parsed = first
        return parsed.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        raise ValueError('Use an unambiguous date and time in the selected timezone.') from None


class GroupMeetings:
    def __init__(self, connections, users, calendar_for_user, now, base_url, secret):
        self.connections, self.users, self.conn = connections, users, users.conn
        self.calendar_for_user, self.now, self.base_url = calendar_for_user, now, base_url.rstrip('/')
        self.secret = secret.encode() if isinstance(secret,str) else secret
        self.read_limiter = RateLimiter(limit=120,window=timedelta(hours=1))
        self.write_limiter = RateLimiter(limit=40,window=timedelta(hours=1))
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS group_meetings (
                id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS group_meetings_user ON group_meetings(user_id);
            CREATE TRIGGER IF NOT EXISTS group_meetings_delete_user AFTER DELETE ON users BEGIN
                DELETE FROM group_meetings WHERE user_id=OLD.id;
            END;
        ''')
        self.conn.commit()
        self.router=APIRouter()
        self._routes()

    def _ready(self,user_id):
        user=self.users.get(user_id)
        if user and user.is_admin and getattr(self.connections, 'local_owner_id', None) == user.id:
            if user.status == 'active':
                return user
        if not (user and not user.is_admin and user.status in ('approved','active') and user.calendar_ready
                and not user.google_disconnected and user.google_revocation_status!='pending'):
            raise ValueError('Connect Google Calendar and complete beta approval before coordinating a meeting.')
        return user

    def _load(self,user_id,meeting_id):
        row=self.conn.execute('SELECT data FROM group_meetings WHERE id=? AND user_id=?',(meeting_id,user_id)).fetchone()
        if row is None: raise ValueError('Meeting request unavailable.')
        return json.loads(row['data'])

    def _save(self,user_id,m):
        # A provider response can arrive after account deletion. Check ownership
        # in this same SQL statement so its cleanup trigger cannot be undone.
        self.conn.execute('''INSERT INTO group_meetings (id,user_id,data)
            SELECT ?,id,? FROM users WHERE id=?
            ON CONFLICT(id) DO UPDATE SET data=excluded.data''',
            (m['id'],json.dumps(m),user_id))
        self.conn.commit()

    def _link_expired(self,m):
        return self.now()>=datetime.fromisoformat(m['expires_at'])

    def _status(self,m):
        if m['status'] not in ('booked','pending','cancelled') and self._link_expired(m):
            return 'expired'
        return m['status']

    def _token(self,m,index):
        payload=f"{m['id']}.{m['revision']}.{index}"
        return payload+'.'+hmac.new(self.secret,payload.encode(),hashlib.sha256).hexdigest()

    def _guest(self,token):
        try:
            if len(token)>200:raise ValueError()
            mid,rev,index,signature=token.split('.')
            row=self.conn.execute('SELECT user_id,data FROM group_meetings WHERE id=?',(mid,)).fetchone()
            if not row:raise ValueError()
            m=json.loads(row['data']); index=int(index)
            if m.get('mode')=='host_provided':raise ValueError()
            if index not in (0,1) or str(m['revision'])!=rev or not hmac.compare_digest(token,self._token(m,index)):
                raise ValueError()
            self._check_identity(row['user_id'],m)
            if m['status'] in ('draft','cancelled'):raise ValueError()
            return row['user_id'],m,index
        except (ValueError,TypeError):
            raise ValueError('This private meeting link is unavailable.') from None

    def _busy(self,user_id,start,end,exclude=None):
        self._ready(user_id)
        busy=self.calendar_for_user(user_id).busy_periods(start,end)
        if not isinstance(busy,list) or any(not isinstance(v,(list,tuple)) or len(v)!=2 or
            any(not isinstance(t,datetime) or t.tzinfo is None for t in v) or v[1]<=v[0] for v in busy):
            raise CalendarError('Availability could not be checked.')
        busy=list(busy)
        for row in self.conn.execute('SELECT data FROM group_meetings WHERE user_id=?',(user_id,)):
            m=json.loads(row['data'])
            if m['status']=='pending' and m['id']!=exclude:
                busy.append((datetime.fromisoformat(m['selected_start']),datetime.fromisoformat(m['selected_end'])))
        if self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='public_booking_requests' AND type='table'").fetchone():
            for row in self.conn.execute("SELECT result FROM public_booking_requests WHERE user_id=? AND status='pending'",(user_id,)):
                result=json.loads(row['result']);busy.append((datetime.fromisoformat(result['start']),datetime.fromisoformat(result['end'])))
        return busy

    def _window(self,user_id,duration_min,timezone,window_start,window_end):
        zone=_zone(timezone);start,end=_when(window_start,zone),_when(window_end,zone)
        duration=timedelta(minutes=duration_min)
        if start<self.now()+timedelta(hours=2) or end>self.now()+timedelta(days=30) or end-start<duration:
            raise ValueError('Choose a window at least two hours ahead, within 30 days, long enough for this meeting.')
        busy=self._busy(user_id,start,end)
        cursor=start
        # Calculate the local grid offset, then add on the UTC timeline.
        # Wall-clock arithmetic resets fold=1 during a repeated DST hour.
        wall=cursor.astimezone(zone)
        remainder=(30-wall.minute%30)%30
        if wall.second or wall.microsecond:
            remainder=30-wall.minute%30
        cursor=start.replace(second=0,microsecond=0)+timedelta(minutes=remainder)
        options=[]
        while cursor+duration<=end and len(options)<6:
            if start<=cursor and not any(a<cursor+duration and b>cursor for a,b in busy):
                options.append(dict(id=str(len(options)+1),start=cursor.isoformat(),end=(cursor+duration).isoformat()))
            cursor+=timedelta(minutes=30)
        if not options:raise ValueError('No free times in this window. Choose another window.')
        return dict(timezone=str(zone),window_start=start.astimezone(zone).isoformat(),window_end=end.astimezone(zone).isoformat(),
                    options=options,expires_at=min(self.now()+timedelta(days=7),end).isoformat())

    def create_draft(self,user_id,title,duration_min,timezone,window_start,window_end,guest1_name,guest1_email,guest2_name,guest2_email,location=''):
        with BOOKING_LOCK:
            user=self._ready(user_id)
            try: duration_min=int(duration_min)
            except (ValueError,TypeError):raise ValueError('Choose 30 or 60 minutes.') from None
            if duration_min not in (30,60):raise ValueError('Choose 30 or 60 minutes.')
            guests=[]
            for name,email in ((guest1_name,guest1_email),(guest2_name,guest2_email)):
                name=_clean(name,80,'guest name');email=_clean(email,254,'email').lower()
                if not EMAIL.fullmatch(email):raise ValueError('Enter each guest’s exact email address.')
                guests.append(dict(name=name,email=email,status='awaiting',selected=[]))
            if len({user.email.lower(),guests[0]['email'],guests[1]['email']})!=3:
                raise ValueError('Use two distinct guest email addresses, different from your own.')
            m=dict(id=secrets.token_hex(16),title=_clean(title,120,'title'),duration_min=duration_min,
                   location=_clean(location,500,'location',False),host_name=user.name,status='draft',revision=1,
                   guests=guests,created_at=self.now().isoformat(),identity=user.google_subject or user.email,
                   **self._window(user_id,duration_min,timezone,window_start,window_end))
            self._save(user_id,m)
            return self.host_view(user_id,m['id'])

    def host_view(self,user_id,meeting_id):
        with BOOKING_LOCK:
            m=self._load(user_id,meeting_id);m['status']=self._status(m)
            m.pop('identity',None)
            if m.get('mode')=='host_provided':return m
            m['needs_reminder']=m['status']=='waiting' and self.now()>=datetime.fromisoformat(m['created_at'])+timedelta(hours=48)
            for i,g in enumerate(m['guests']):
                g['link']='' if self._link_expired(m) or m['status'] in ('draft','expired','cancelled') else self.base_url+'/m/'+self._token(m,i)
                g['message']=(f"Hi {g['name']}, {m['host_name']} is arranging {m['title']} ({m['duration_min']} minutes). "
                              f"Please choose every time that works for you: {g['link']}\nPippa will book one time accepted by both guests. No account needed.") if g['link'] else ''
            return m

    def list_for_host(self,user_id):
        with BOOKING_LOCK:
            ids=[r['id'] for r in self.conn.execute('SELECT id,data FROM group_meetings WHERE user_id=? ORDER BY rowid DESC',(user_id,)) if json.loads(r['data']).get('mode')!='host_provided'][:100]
            return [self.host_view(user_id,mid) for mid in ids]

    def create_host_proposal(self,user_id,proposal):
        """Store the host's exact readback, without manufacturing guest replies."""
        with BOOKING_LOCK:
            user=self._ready(user_id)
            duration=proposal.get('duration_min')
            if type(duration) is not int or duration not in (30,60):raise ValueError('Choose 30 or 60 minutes.')
            zone=_zone(proposal.get('timezone'))
            start,end=_when(proposal.get('start'),zone),_when(proposal.get('end'),zone)
            if end-start!=timedelta(minutes=duration) or start<self.now()+timedelta(hours=2) or end>self.now()+timedelta(days=30):
                raise ValueError('Choose an exact time at least two hours ahead and within 30 days.')
            if not isinstance(proposal.get('guests'),list) or len(proposal['guests'])!=2:
                raise ValueError('Provide exactly two guests.')
            guests=[]
            for guest in proposal['guests']:
                name=_clean(guest.get('name',''),80,'guest name');email=_clean(guest.get('email',''),254,'email').lower()
                if not EMAIL.fullmatch(email):raise ValueError('Enter each guest’s exact email address.')
                guests.append(dict(name=name,email=email))
            if len({user.email.lower(),*(g['email'] for g in guests)})!=3:
                raise ValueError('Use two distinct guest email addresses, different from your own.')
            if any(a<end and b>start for a,b in self._busy(user_id,start,end)):
                raise ValueError('That exact time is no longer available. Ask for another proposal.')
            m=dict(id=secrets.token_hex(16),mode='host_provided',status='draft',revision=1,
                   title=_clean(proposal.get('title',''),120,'title'),duration_min=duration,
                   location=_clean(proposal.get('location') or '',500,'location',False),timezone=str(zone),
                   selected_start=start.isoformat(),selected_end=end.isoformat(),guests=guests,
                   host_name=user.name,identity=user.google_subject or user.email,created_at=self.now().isoformat(),
                   expires_at=min(start-timedelta(hours=2),self.now()+timedelta(days=7)).isoformat())
            self._save(user_id,m)
            return self.host_view(user_id,m['id'])

    def confirm_host_proposal(self,user_id,meeting_id):
        """One exact slot, one durable write claim; uncertainty is lookup-only."""
        with BOOKING_LOCK:
            m=self._load(user_id,meeting_id);self._check_identity(user_id,m)
            if m.get('mode')!='host_provided':raise ValueError('Chat proposal unavailable.')
            if m['status'] in ('pending','booked'):return self.host_view(user_id,meeting_id)
            if self._status(m)!='draft':raise ValueError('This proposal expired. Ask for a new time.')
            start,end=datetime.fromisoformat(m['selected_start']),datetime.fromisoformat(m['selected_end'])
            if start<self.now()+timedelta(hours=2) or any(a<end and b>start for a,b in self._busy(user_id,start,end,exclude=m['id'])):
                raise ValueError('That exact time is no longer available. Ask for another proposal and confirm it.')
            # Recheck identity after provider availability I/O, before claiming.
            self._check_identity(user_id,m)
            m.update(status='pending',event_id='pippag'+hashlib.sha256(f"{user_id}:{m['id']}:{m['revision']}".encode()).hexdigest())
            self._save(user_id,m)
            try:
                event=self.calendar_for_user(user_id).create_event(m['title'],start,end,
                    attendee_emails=[g['email'] for g in m['guests']],location=m['location'] or None,event_id=m['event_id'])
                self._confirm(user_id,m,event)
            except CalendarError:pass
            return self.host_view(user_id,meeting_id)

    def activate(self,user_id,meeting_id):
        with BOOKING_LOCK:
            self._ready(user_id);m=self._load(user_id,meeting_id)
            if self._status(m)!='draft':raise ValueError('Only a current draft can be activated.')
            self._check_identity(user_id,m)
            m['status']='waiting';self._save(user_id,m)
            return self.host_view(user_id,meeting_id)

    def _check_identity(self,user_id,m):
        user=self._ready(user_id)
        if m['identity']!=(user.google_subject or user.email):
            raise ValueError('Your connected Google account changed. Create a new meeting request.')

    def revise(self,user_id,meeting_id,timezone,window_start,window_end):
        with BOOKING_LOCK:
            m=self._load(user_id,meeting_id);self._check_identity(user_id,m)
            if m['status'] in ('pending','booked','cancelled'):
                raise ValueError('This request cannot be revised. Check a pending booking before making changes.')
            m.update(self._window(user_id,m['duration_min'],timezone,window_start,window_end))
            m.update(status='draft',revision=m['revision']+1,created_at=self.now().isoformat())
            for g in m['guests']:g.update(status='awaiting',selected=[])
            self._save(user_id,m)
            return self.host_view(user_id,meeting_id)

    def cancel(self,user_id,meeting_id):
        with BOOKING_LOCK:
            m=self._load(user_id,meeting_id)
            if m['status'] in ('pending','booked'):
                raise ValueError('Check the pending or booked calendar event before making changes.')
            m['status']='cancelled';self._save(user_id,m)
            return self.host_view(user_id,meeting_id)

    def _confirm(self,user_id,m,event):
        if not isinstance(event,Event) or event.id!=m['event_id'] or event.all_day or event.start!=datetime.fromisoformat(m['selected_start']) or event.end!=datetime.fromisoformat(m['selected_end']):
            raise CalendarError('The calendar write result is not confirmed.')
        m['status']='booked';m['event_url']=event.source_url or ''
        m['location']=event.location or m['location'] or event.meet_link or '';self._save(user_id,m)

    def check(self,user_id,meeting_id):
        with BOOKING_LOCK:
            m=self._load(user_id,meeting_id);self._check_identity(user_id,m)
            if m['status']=='pending':
                try:self._confirm(user_id,m,self.calendar_for_user(user_id).get_event(m['event_id']))
                except CalendarError:pass
            elif m.get('mode')!='host_provided' and self._status(m) not in ('draft','booked','cancelled','expired'):
                self._book_if_ready(user_id,m)
            return self.host_view(user_id,meeting_id)

    def _book_if_ready(self,user_id,m):
        if any(g['status']=='awaiting' for g in m['guests']):return
        common=set(m['guests'][0]['selected'])&set(m['guests'][1]['selected'])
        if not common:
            m['status']='no_common';self._save(user_id,m);return
        self._check_identity(user_id,m)
        options=[o for o in m['options'] if o['id'] in common and datetime.fromisoformat(o['start'])>=self.now()+timedelta(hours=2)]
        try:
            for o in options:
                start,end=datetime.fromisoformat(o['start']),datetime.fromisoformat(o['end'])
                if any(a<end and b>start for a,b in self._busy(user_id,start,end,exclude=m['id'])):continue
                # Durable claim precedes insertion; its stable ID prevents retry duplication.
                m.update(status='pending',selected_start=o['start'],selected_end=o['end'],event_id='pippag'+hashlib.sha256(f"{user_id}:{m['id']}:{m['revision']}".encode()).hexdigest())
                self._save(user_id,m)
                try:
                    event=self.calendar_for_user(user_id).create_event(m['title'],start,end,
                        attendee_emails=[g['email'] for g in m['guests']],location=m['location'] or None,event_id=m['event_id'])
                    self._confirm(user_id,m,event)
                except CalendarError:pass
                return
        except CalendarError:
            pass
        m['status']='needs_help';self._save(user_id,m)

    def _origin_ok(self,request):
        expected=urlsplit(self.base_url)
        origin=request.headers.get('origin')
        return request.headers.get('sec-fetch-site')!='cross-site' and (not origin or origin.rstrip('/')==f'{expected.scheme}://{expected.netloc}')

    def _csrf(self,browser,token,revision):
        return hmac.new(self.secret,f'{browser}:{token}:{revision}'.encode(),hashlib.sha256).hexdigest()

    def _page(self,view,data,status=200):
        return HTMLResponse(render_group_page(view,data),status_code=status,headers=HEADERS)

    def _error(self,message,status=400):
        return self._page('error',{'error':message,'base_url':self.base_url},status)

    def _guest_page(self,request,token,notice=''):
        with BOOKING_LOCK:
            try:user_id,m,index=self._guest(token)
            except ValueError:return self._error('This private meeting link is unavailable.',404)
            if self._link_expired(m):return self._error('This meeting link expired. Ask your host for new options.',410)
            browser=request.cookies.get(COOKIE) or secrets.token_urlsafe(32)
            guest=m['guests'][index]
            zone=str(_zone(request.query_params.get('timezone') or guest.get('timezone') or m['timezone']))
            # Never include other guests, identity bindings or provider IDs in public HTML.
            public={k:m[k] for k in ('id','title','duration_min','timezone','window_start','window_end','location','status','revision','expires_at','options','host_name')}
            if m.get('selected_start'):public['selected_start']=m['selected_start']
            result=self._page('guest',dict(meeting=public,guest_name=guest['name'],selected=guest['selected'],token=token,
                timezone=zone,csrf_token=self._csrf(browser,token,m['revision']),base_url=self.base_url,notice=notice))
            result.set_cookie(COOKIE,browser,max_age=7*24*3600,secure=urlsplit(self.base_url).scheme=='https',httponly=True,samesite='lax')
            return result

    def _guest_post(self,request,token,form):
        with BOOKING_LOCK:
            try:user_id,m,index=self._guest(token)
            except ValueError:return self._error('This private meeting link is unavailable.',404)
            browser=request.cookies.get(COOKIE,'')
            if not browser or not hmac.compare_digest(str(form.get('csrf_token','')).encode(),self._csrf(browser,token,m['revision']).encode()):
                return self._error('Refresh the meeting page before replying.',403)
            if self._link_expired(m):return self._error('This meeting link expired. Ask your host for new options.',410)
            if str(form.get('revision',''))!=str(m['revision']) or form.get('response')!='submit':
                return self._error('The options changed. Refresh before replying.',409)
            if m['status'] in ('pending','booked'):
                return self._guest_page(request,token)
            selected=[] if form.get('none')=='1' else list(dict.fromkeys(form.getlist('selected')))
            if len(selected)>6 or not set(selected)<=set(o['id'] for o in m['options']):return self._error('Choose only the displayed times.')
            try:_zone(str(form.get('timezone','')))
            except ValueError:return self._error('Choose a valid timezone.')
            m['guests'][index].update(selected=selected,status='replied' if selected else 'none',timezone=str(form.get('timezone')))
            m['status']='waiting';self._save(user_id,m)
            try:self._book_if_ready(user_id,m)
            except (CalendarError,ValueError):
                return self._error('Your reply is saved. The host needs to check their calendar connection.',503)
            return self._guest_page(request,token,notice='Your availability is saved.')

    def _host_page(self,request,meeting_id=None):
        user,session=self.connections._auth(request)
        if not user:return RedirectResponse('/signin',status_code=303,headers=HEADERS)
        try:
            data=dict(csrf_token=session['csrf'],base_url=self.base_url,user_name=user.name)
            if meeting_id:
                data['meeting']=self.host_view(user.id,meeting_id)
                if data['meeting'].get('mode')=='host_provided':return self._error('Meeting request unavailable.',404)
            else:data.update(meetings=self.list_for_host(user.id),timezone='Asia/Singapore')
            return self._page('host' if meeting_id else 'list',data)
        except ValueError:return self._error('Meeting request unavailable.',404)

    def _host_post(self,request,form,meeting_id=None,action=None):
        user,session,error=self.connections._post_auth(request,str(form.get('csrf_token','')))
        if error is not None:return error
        try:
            if meeting_id:
                if self._load(user.id,meeting_id).get('mode')=='host_provided':return self._error('Meeting request unavailable.',404)
                if action=='activate':m=self.activate(user.id,meeting_id)
                elif action=='cancel':m=self.cancel(user.id,meeting_id)
                elif action=='check':m=self.check(user.id,meeting_id)
                elif action=='revise':m=self.revise(user.id,meeting_id,**{k:str(form.get(k,'')) for k in ('timezone','window_start','window_end')})
                else:return self._error('Unknown action.',404)
            else:
                keys=('title','duration_min','timezone','window_start','window_end','guest1_name','guest1_email','guest2_name','guest2_email','location')
                m=self.create_draft(user.id,**{k:str(form.get(k,'')) for k in keys})
            return RedirectResponse('/connections/meetings/'+m['id'],status_code=303,headers=HEADERS)
        except (ValueError,CalendarError) as exc:
            message = str(exc) if isinstance(exc,ValueError) else 'Calendar availability could not be checked. Please try again.'
            status = 400 if isinstance(exc,ValueError) else 503
            data=dict(csrf_token=session['csrf'],base_url=self.base_url,user_name=user.name,error=message,values=dict(form))
            if meeting_id:
                try:data['meeting']=self.host_view(user.id,meeting_id)
                except ValueError:return self._error('Meeting request unavailable.',404)
            else:data.update(meetings=self.list_for_host(user.id),timezone='Asia/Singapore')
            return self._page('host' if meeting_id else 'list',data,status)

    async def _form(self,request):
        if not self._origin_ok(request):return None,self._error('Refresh the page before continuing.',403)
        if not self.write_limiter.allow(_client_ip(request),self.now()):return None,self._error('Too many requests. Try again later.',429)
        body=b''
        async for chunk in request.stream():
            body+=chunk
            if len(body)>16384:return None,self._error('This form is too large.',413)
        request._body=body
        form=await request.form(max_files=0,max_fields=24,max_part_size=4096)
        return form,None

    def _routes(self):
        @self.router.get('/connections/meetings')
        def index(request:Request):return self._host_page(request)
        @self.router.post('/connections/meetings')
        async def create(request:Request):
            form,error=await self._form(request)
            return error if error is not None else await run_in_threadpool(self._host_post,request,form)
        @self.router.get('/connections/meetings/{meeting_id}')
        def host(request:Request,meeting_id:str):return self._host_page(request,meeting_id)
        @self.router.post('/connections/meetings/{meeting_id}/{action}')
        async def action(request:Request,meeting_id:str,action:str):
            form,error=await self._form(request)
            return error if error is not None else await run_in_threadpool(self._host_post,request,form,meeting_id,action)
        @self.router.get('/m/{token}')
        def guest(request:Request,token:str):
            if not self.read_limiter.allow(_client_ip(request),self.now()):return self._error('Too many requests. Try again later.',429)
            try:return self._guest_page(request,token)
            except ValueError:return self._error('Choose a valid timezone.')
        @self.router.post('/m/{token}')
        async def reply(request:Request,token:str):
            form,error=await self._form(request)
            return error if error is not None else await run_in_threadpool(self._guest_post,request,token,form)
