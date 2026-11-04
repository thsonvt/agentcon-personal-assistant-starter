"""One public 30-minute meeting per connected host.

Anonymous forms are signed, expiring and bound to an HttpOnly browser cookie.
A durable request is recorded before insertion. Uncertain writes are reconciled
by their deterministic Google event ID, never retried with a new ID.
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from app.booking import BOOKING_LOCK
from app.booking_pages import render_public_booking, render_booking_settings
from app.router import RateLimiter
from app.signup import _client_ip
from app.tools.calendar import CalendarError, Event
from app.users import _serialized_write

UTC = timezone.utc
COOKIE = 'pippa_booking_browser'
AGE = timedelta(hours=1)
DURATION = timedelta(minutes=30)
HEADERS = {'Cache-Control': 'no-store', 'Referrer-Policy': 'same-origin', 'X-Content-Type-Options': 'nosniff'}
RESERVED = frozenset('about account accounts admin api app assets auth b2b billing book booking bookings calendar callback connections contact dashboard docs favicon health help legal login logout oauth privacy robots settings signin signup static support terms users webhook webhooks www .well-known events people network inbox event-admin captures'.split())
PENDING = 'Your booking is still being checked. Please keep this page and try Confirm booking again with the same details. Do not submit another booking.'


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _clean(value, maximum, label, required=True):
    value = str(value).strip()
    if len(value) > maximum or (required and not value) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Enter a valid ' + label + '.')
    return value


def _zone(value):
    try:
        # Some browser timezone aliases are absent from slim OS timezone databases.
        if value == 'Asia/Saigon':
            value = 'Asia/Ho_Chi_Minh'
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError('Choose a valid timezone.') from None


def _aware(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('Choose a time with a timezone.')
    try:
        return parsed.astimezone(UTC)
    except OverflowError:
        raise ValueError("Choose a supported date and time.") from None


class PublicBooking:
    def __init__(self, connections, users, calendar_for_user, now, base_url, secret, host_email=None):
        self.connections, self.users = connections, users
        self.calendar_for_user, self.now, self.base_url = calendar_for_user, now, base_url.rstrip('/')
        self.secret = secret.encode() if isinstance(secret, str) else secret
        self.conn = users.conn
        self.network_profile = None
        self.host_email = host_email
        self.read_limiter = RateLimiter(limit=60, window=timedelta(hours=1))
        self.host_read_limiter = RateLimiter(limit=240, window=timedelta(hours=1))
        self.write_limiter = RateLimiter(limit=10, window=timedelta(hours=1))
        self.host_limiter = RateLimiter(limit=30, window=timedelta(hours=1))
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS public_booking_profiles (
                user_id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT NOT NULL, title TEXT NOT NULL, timezone TEXT NOT NULL,
                location TEXT NOT NULL, work_start TEXT NOT NULL, work_end TEXT NOT NULL,
                weekdays TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS public_booking_requests (
                request_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                payload_hash TEXT NOT NULL, event_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS host_booking_notifications (
                request_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL,
                first_attempt_at TEXT, attempted_at TEXT, attempts INTEGER NOT NULL DEFAULT 0
            );
            CREATE TRIGGER IF NOT EXISTS host_booking_notifications_delete_user
            AFTER DELETE ON users BEGIN
                DELETE FROM host_booking_notifications WHERE user_id=OLD.id;
            END;
            CREATE TRIGGER IF NOT EXISTS public_booking_delete_user
            AFTER DELETE ON users BEGIN
                DELETE FROM public_booking_requests WHERE user_id=OLD.id;
                DELETE FROM public_booking_profiles WHERE user_id=OLD.id;
            END;
        ''')
        self.conn.commit()
        self.router = APIRouter()
        self._routes()

    def _profile(self, username=None, user_id=None):
        query, value = ('username=? COLLATE NOCASE', username) if username is not None else ('user_id=?', user_id)
        row = self.conn.execute('SELECT * FROM public_booking_profiles WHERE '+query, (value,)).fetchone()
        if row is None:
            return None
        profile = dict(row)
        profile['weekdays'] = json.loads(profile['weekdays'])
        profile['enabled'] = bool(profile['enabled'])
        profile['duration_min'] = 30
        return profile

    def _ready(self, user_id):
        user = self.users.get(user_id)
        return bool(user and not user.is_admin and user.status in ('approved', 'active')
                    and user.calendar_ready and not user.google_disconnected
                    and user.google_revocation_status != 'pending')

    def _host(self, username):
        profile = self._profile(username=username)
        return profile if profile and profile['enabled'] and self._ready(profile['user_id']) else None

    def _calendar(self, profile):
        # Never trust the profile or capability captured at request start.
        current = self._host(profile['username'])
        if not current or current['user_id'] != profile['user_id']:
            raise CalendarError('Host unavailable')
        return self.calendar_for_user(profile['user_id'])

    def _error(self, message, status=400):
        return JSONResponse({'error': message}, status_code=status, headers=HEADERS)

    def _origin_ok(self, request):
        origin = request.headers.get('origin')
        expected = urlsplit(self.base_url)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            return False
        return not origin or origin.rstrip('/') == f'{expected.scheme}://{expected.netloc}'

    def _settings(self, user, session, status=200, **extra):
        profile = self._profile(user_id=user.id) or dict(username='', display_name=user.name, title='30-minute meeting',
            timezone='Asia/Singapore', location='', work_start='09:00', work_end='18:00', weekdays=[0,1,2,3,4], enabled=False)
        problems = self.conn.execute('''SELECT COUNT(*) FROM host_booking_notifications n
            JOIN public_booking_requests b ON b.request_id=n.request_id
            WHERE n.user_id=? AND b.status='confirmed' AND n.status IN ('failed','unknown','unconfigured','sending')''',
            (user.id,)).fetchone()[0]
        data = dict(profile=profile, csrf_token=session['csrf'], base_url=self.base_url, calendar_ready=self._ready(user.id),
                    notification_problem_count=problems, **extra)
        return HTMLResponse(render_booking_settings(data), status_code=status, headers=HEADERS)

    def _fingerprint(self, profile):
        return _hash(json.dumps(profile, sort_keys=True))

    def _token(self, profile, browser):
        user = self.users.get(profile['user_id'])
        data = dict(id=secrets.token_hex(24), host=profile['user_id'], identity=_hash(user.google_subject or user.email or ''), profile=self._fingerprint(profile),
                    browser=_hash(browser), exp=int((self.now()+AGE).timestamp()))
        value = base64.urlsafe_b64encode(json.dumps(data, separators=(',', ':')).encode()).decode().rstrip('=')
        return value+'.'+hmac.new(self.secret, value.encode(), hashlib.sha256).hexdigest()

    def _verify(self, request, value, profile):
        try:
            if len(value) > 2048:
                return None
            payload, signature = value.split('.')
            if not hmac.compare_digest(signature, hmac.new(self.secret,payload.encode(),hashlib.sha256).hexdigest()):
                return None
            data = json.loads(base64.urlsafe_b64decode(payload+'='*(-len(payload)%4)))
            browser = request.cookies.get(COOKIE, '')
            user = self.users.get(profile['user_id'])
            if (not browser or data['host'] != profile['user_id']
                    or data['identity'] != _hash(user.google_subject or user.email or '')
                    or not hmac.compare_digest(data['browser'], _hash(browser))):
                return None
            if data['exp'] <= self.now().timestamp():
                # Expired unused forms cannot create, but the same browser may
                # recover a recorded outcome for seven days without another insert.
                old = self.conn.execute('SELECT 1 FROM public_booking_requests WHERE request_id=? AND user_id=?', (data['id'],profile['user_id'])).fetchone()
                if old is None or data['exp'] + timedelta(days=7).total_seconds() <= self.now().timestamp():
                    return None
            return data
        except (ValueError, TypeError, KeyError):
            return None

    def _candidates(self, profile, day, zone):
        """Generate wall-clock host slots, including both real DST folds, never gaps."""
        left = datetime.combine(day, time(), zone).astimezone(UTC)
        right = datetime.combine(day+timedelta(days=1), time(), zone).astimezone(UTC)
        host_zone = _zone(profile['timezone'])
        host_day, final_day = left.astimezone(host_zone).date(), right.astimezone(host_zone).date()
        first, last = time.fromisoformat(profile['work_start']), time.fromisoformat(profile['work_end'])
        values = set()
        while host_day <= final_day:
            if host_day.weekday() in profile['weekdays']:
                wall = datetime.combine(host_day, first)
                close = datetime.combine(host_day, last)
                while wall + DURATION <= close:
                    for fold in (0,1):
                        start = wall.replace(tzinfo=host_zone, fold=fold).astimezone(UTC)
                        end = start+DURATION
                        # The round trip excludes nonexistent wall times. End must
                        # remain within that day's host hours, including DST days.
                        local_end = end.astimezone(host_zone)
                        if (start.astimezone(host_zone).replace(tzinfo=None) == wall
                                and local_end.date() == host_day and first <= local_end.time() <= last
                                and left <= start < right and start >= self.now()+timedelta(hours=2)
                                and end <= self.now()+timedelta(days=30)):
                            values.add(start)
                    wall += DURATION
            host_day += timedelta(days=1)
        return sorted(values), left, right

    def _busy(self, profile, start, end):
        busy = self._calendar(profile).busy_periods(start,end)
        # Also defend the contract of injected adapters and malformed provider data.
        if not isinstance(busy,list):
            raise CalendarError('Incomplete availability')
        for interval in busy:
            if (not isinstance(interval,(tuple,list)) or len(interval)!=2
                    or any(not isinstance(v,datetime) or v.tzinfo is None for v in interval)
                    or interval[1] <= interval[0]):
                raise CalendarError('Incomplete availability')
        busy = list(busy)
        pending = self.conn.execute("SELECT result FROM public_booking_requests WHERE user_id=? AND status='pending'", (profile['user_id'],)).fetchall()
        for row in pending:
            result = json.loads(row['result'])
            busy.append((_aware(result['start']), _aware(result['end'])))
        if self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='group_meetings'").fetchone():
            for row in self.conn.execute("SELECT data FROM group_meetings WHERE user_id=?", (profile['user_id'],)):
                meeting = json.loads(row['data'])
                if meeting['status'] == 'pending':
                    busy.append((_aware(meeting['selected_start']), _aware(meeting['selected_end'])))
        return busy

    def _confirmed(self, request_id, event, result, expected_event_id):
        if (not isinstance(event, Event) or event.id != expected_event_id or event.start != _aware(result['start'])
                or event.end != _aware(result['end']) or event.all_day):
            raise CalendarError('Google did not confirm the requested event')
        result = {**result, 'location': event.location or result['location'] or event.meet_link or ''}
        with self.conn:
            self.conn.execute("UPDATE public_booking_requests SET status='confirmed', result=? WHERE request_id=?",
                              (json.dumps(result), request_id))
            receipt = self.conn.execute('SELECT * FROM host_booking_notifications WHERE request_id=?', (request_id,)).fetchone()
            if receipt and not receipt['attempts']:
                payload = json.loads(receipt['payload'])
                event_key = base64.urlsafe_b64encode(f"{event.id} {payload['email']}".encode()).decode().rstrip('=')
                payload['dataVariables']['calendarUrl'] = event.source_url or 'https://calendar.google.com/calendar/event?eid='+event_key
                if result['location']:
                    payload['dataVariables']['location'] = result['location']
                self.conn.execute('UPDATE host_booking_notifications SET payload=? WHERE request_id=?',
                                  (json.dumps(payload), request_id))
        self._notify_host(request_id)
        return self._booking_result(request_id, result)

    def _notification_status(self, request_id):
        receipt = self.conn.execute('SELECT status FROM host_booking_notifications WHERE request_id=?', (request_id,)).fetchone()
        return receipt['status'] if receipt else 'not_requested'

    def _booking_result(self, request_id, result):
        return JSONResponse({**result, 'host_notification': self._notification_status(request_id)}, headers=HEADERS)

    def _notify_host(self, request_id):
        receipt = self.conn.execute('''SELECT n.* FROM host_booking_notifications n
            JOIN public_booking_requests b ON b.request_id=n.request_id
            WHERE n.request_id=? AND b.status='confirmed' ''', (request_id,)).fetchone()
        if not receipt or receipt['status'] in ('accepted', 'suppressed'):
            return
        payload = json.loads(receipt['payload'])
        host = self.users.get(receipt['user_id'])
        if not self._ready(receipt['user_id']) or not host.google_subject or host.email != payload['email']:
            with self.conn:
                self.conn.execute("UPDATE host_booking_notifications SET status='suppressed' WHERE request_id=?", (request_id,))
            return
        if not self.host_email or not self.host_email.ready:
            # Preserve uncertainty when a previously attempted provider is disabled.
            if not receipt['attempts']:
                with self.conn:
                    self.conn.execute("UPDATE host_booking_notifications SET status='unconfigured' WHERE request_id=?", (request_id,))
            return
        now = self.now()
        if (receipt['attempts'] >= 3
                or (receipt['attempted_at'] and now - _aware(receipt['attempted_at']) < timedelta(minutes=1))
                or (receipt['first_attempt_at'] and now - _aware(receipt['first_attempt_at']) >= timedelta(hours=23))):
            return
        # Commit before the network call: a process exit is an uncertain attempt,
        # and retries must stay inside Loops' 24-hour idempotency window.
        if not receipt['attempts']:
            payload['transactionalId'] = self.host_email.transactional_id
        with self.conn:
            self.conn.execute('''UPDATE host_booking_notifications SET status='sending',
                first_attempt_at=COALESCE(first_attempt_at,?), attempted_at=?, attempts=attempts+1, payload=?
                WHERE request_id=?''', (now.isoformat(), now.isoformat(), json.dumps(payload), request_id))
        try:
            status = self.host_email.send(payload, request_id)
        except Exception:
            status = 'unknown'
        if status not in ('accepted', 'failed', 'unknown', 'unconfigured'):
            status = 'unknown'
        with self.conn:
            self.conn.execute('UPDATE host_booking_notifications SET status=? WHERE request_id=?', (status, request_id))

    @_serialized_write
    def retry_notifications(self):
        if not self.host_email or not self.host_email.ready:
            return
        with BOOKING_LOCK:
            now = self.now()
            receipts = self.conn.execute('''SELECT n.request_id FROM host_booking_notifications n
                JOIN public_booking_requests b ON b.request_id=n.request_id
                WHERE b.status='confirmed' AND n.status IN ('queued','sending','unknown','failed','unconfigured')
                AND n.attempts<3 AND (n.attempted_at IS NULL OR julianday(n.attempted_at)<=julianday(?))
                AND (n.first_attempt_at IS NULL OR julianday(n.first_attempt_at)>julianday(?))
                ORDER BY n.attempted_at LIMIT 20''',
                ((now-timedelta(minutes=1)).isoformat(), (now-timedelta(hours=23)).isoformat())).fetchall()
            for receipt in receipts:
                self._notify_host(receipt['request_id'])

    def _pending(self, request_id):
        return JSONResponse({'status':'pending','error':PENDING, 'host_notification': self._notification_status(request_id)}, status_code=202, headers=HEADERS)

    def _routes(self):
        @self.router.get('/connections/scheduling')
        def settings(request: Request):
            user, session = self.connections._auth(request)
            return self._settings(user, session) if user else RedirectResponse('/signin',status_code=303,headers=HEADERS)

        @self.router.post('/connections/scheduling')
        async def save_settings(request: Request):
            if not self._origin_ok(request):
                return self._error('Refresh this page before saving.',403)
            form = await request.form(max_files=0, max_fields=20, max_part_size=4096)
            return await run_in_threadpool(self._save_settings, request, form)

        @self.router.get('/booking/{username}/slots')
        def slots(request: Request, username: str, date: str = '', tz: str = ''):
            profile = self._host(username)
            if not profile:
                return self._error('This booking page is unavailable.',404)
            if (not self.read_limiter.allow('slots:'+_client_ip(request),self.now())
                    or not self.host_read_limiter.allow(str(profile['user_id']),self.now())):
                return self._error('Too many availability checks. Please try again later.',429)
            try:
                zone = _zone(tz)
                day = datetime.strptime(date,'%Y-%m-%d').date()
                if not self.now().astimezone(zone).date() <= day <= (self.now()+timedelta(days=30)).astimezone(zone).date():
                    raise ValueError('Choose a date within the next 30 days.')
                candidates, left, right = self._candidates(profile,day,zone)
                busy = self._busy(profile,left,right) if candidates else []
                result = [dict(start=start.astimezone(zone).isoformat(),end=(start+DURATION).astimezone(zone).isoformat(),
                               label=start.astimezone(zone).strftime('%-I:%M %p %Z')) for start in candidates
                          if not any(a<start+DURATION and b>start for a,b in busy)]
                return JSONResponse(dict(slots=result,date=day.isoformat(),timezone=tz),headers=HEADERS)
            except (ValueError,TypeError):
                return self._error('Choose a valid date within the next 30 days and a valid timezone.')
            except CalendarError:
                return self._error('Availability could not be checked. Please try again later.',503)

        @self.router.post('/booking/{username}/reserve')
        async def reserve(request: Request, username: str):
            if not self._origin_ok(request):
                return self._error('Refresh the booking page before continuing.',403)
            form = await request.form(max_files=0, max_fields=20, max_part_size=4096)
            return await run_in_threadpool(self._reserve, request, username, form)

        @self.router.get('/{username}/30min')
        def public_page(request: Request, username: str):
            profile = self._host(username)
            if not profile:
                return HTMLResponse(render_public_booking({'error':'This booking page is unavailable.'}),status_code=404,headers=HEADERS)
            browser = request.cookies.get(COOKIE) or secrets.token_urlsafe(32)
            zone = _zone(profile['timezone'])
            data = dict(profile={k:profile[k] for k in ('username','display_name','title','timezone','location','duration_min')},
                        booking_token=self._token(profile,browser),now=self.now().astimezone(UTC).isoformat(),
                        horizon_end=(self.now()+timedelta(days=30)).astimezone(UTC).isoformat(),
                        today=self.now().astimezone(zone).date().isoformat(),
                        last_day=(self.now()+timedelta(days=30)).astimezone(zone).date().isoformat())
            response = HTMLResponse(render_public_booking(data),headers=HEADERS)
            response.set_cookie(COOKIE,browser,max_age=int(timedelta(days=8).total_seconds()),secure=True,httponly=True,samesite='lax')
            return response

        @self.router.get('/{username}')
        def personal_page(request: Request, username: str):
            if self.network_profile is not None:
                network = self.network_profile(request, username)
                if network is not None:
                    return network
            profile = self._host(username)
            if not profile:
                return HTMLResponse(render_public_booking({'error':'This booking page is unavailable.'}),status_code=404,headers=HEADERS)
            return RedirectResponse('/'+profile['username']+'/30min',status_code=307,headers=HEADERS)

    @_serialized_write
    def _reserve(self, request, username, form):
        profile = self._host(username)
        if not profile:
            return self._error('This booking page is unavailable.',404)
        if not self._origin_ok(request):
            return self._error('Refresh the booking page before continuing.',403)
        token = self._verify(request,str(form.get('booking_token','')),profile)
        if token is None:
            return self._error('This booking form expired. If you already submitted it, contact the host before making another booking. Otherwise refresh the page.',403)
        if not self.write_limiter.allow(_client_ip(request),self.now()):
            return self._error('Too many booking attempts. Please try again later.',429)
        try:
            if form.get('website'):
                raise ValueError('Invalid form.')
            name = _clean(form.get('name',''),100,'name')
            email = _clean(form.get('email',''),254,'email')
            if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+",email):
                raise ValueError('Enter a valid email address.')
            zone = _zone(str(form.get('timezone','')))
            start = _aware(str(form.get('start','')))
        except (ValueError,TypeError):
            return self._error('Enter your name, a valid email address, and an available time with its timezone.')
        payload_hash = _hash(json.dumps([name,email,start.isoformat(),str(zone)]))
        with BOOKING_LOCK:
            current = self._host(username)
            if not current:
                return self._error('This booking page is unavailable.',404)
            old = self.conn.execute('SELECT * FROM public_booking_requests WHERE request_id=?',(token['id'],)).fetchone()
            if old:
                if old['payload_hash'] != payload_hash:
                    return self._error('This form was already submitted with different details. Keep the original details to check its result.',409)
                result = json.loads(old['result'])
                if old['status']=='confirmed':
                    self._notify_host(token['id'])
                    return self._booking_result(token['id'],result)
                try:
                    event = self._calendar(current).get_event(old['event_id'])
                    return self._confirmed(token['id'],event,result,old['event_id'])
                except CalendarError:
                    return self._pending(token['id'])
            if token['profile'] != self._fingerprint(current):
                return self._error('The host updated this meeting. Refresh the page and choose a time again.',409)
            if not self.now()+timedelta(hours=2) <= start <= self.now()+timedelta(days=30)-DURATION:
                return self._error('Choose a time at least two hours from now and within the next 30 days.')
            candidates, _, _ = self._candidates(current,start.astimezone(zone).date(),zone)
            if start not in candidates:
                return self._error('Choose an available time within working hours, at least two hours from now.')
            if not self.host_limiter.allow(str(current['user_id']),self.now()):
                return self._error('This host has received too many booking attempts. Please try again later.',429)
            try:
                busy = self._busy(current,start,start+DURATION)
                if any(a<start+DURATION and b>start for a,b in busy):
                    return self._error('That time is no longer available. Choose another time.',409)
            except CalendarError:
                return self._error('Availability could not be checked. Nothing was booked. Please try again later.',503)
            # Persist before calling Google: even a process exit cannot cause
            # a second insert. The ID uses Google's allowed base32hex alphabet.
            event_id = 'pippa'+_hash(str(current['user_id'])+':'+token['id'])
            result = dict(status='confirmed',title=current['title'],start=start.astimezone(zone).isoformat(),
                          end=(start+DURATION).astimezone(zone).isoformat(),timezone=str(zone),
                          location=current['location'],invitation='requested')
            host = self.users.get(current['user_id'])
            payload = dict(email=host.email, dataVariables=dict(
                hostName=current['display_name'], guestName=name, guestEmail=email, title=current['title'],
                when=start.astimezone(_zone(current['timezone'])).strftime('%A, %d %B %Y at %H:%M'),
                timezone=current['timezone'], duration='30 minutes',
                location=current['location'] or 'Arrange with your guest', calendarUrl=''))
            with self.conn:
                self.conn.execute('INSERT INTO public_booking_requests VALUES (?,?,?,?,?,?,?)',
                                  (token['id'],current['user_id'],payload_hash,event_id,'pending',json.dumps(result),self.now().isoformat()))
                self.conn.execute('''INSERT INTO host_booking_notifications
                    (request_id,user_id,payload,status) VALUES (?,?,?,'queued')''',
                    (token['id'],current['user_id'],json.dumps(payload)))
            try:
                event = self._calendar(current).create_event(current['title']+' — '+name,start,start+DURATION,
                             attendee_email=email,location=current['location'] or None,event_id=event_id)
                return self._confirmed(token['id'],event,result,event_id)
            except CalendarError:
                return self._pending(token['id'])

    @_serialized_write
    def _save_settings(self, request, form):
        if not self._origin_ok(request):
            return self._error('Refresh this page before saving.',403)
        user, session, error = self.connections._post_auth(request,str(form.get('csrf_token','')))
        if error is not None:
            return error
        try:
            username = _clean(form.get('username',''),32,'username').lower()
            if not re.fullmatch(r'[a-z][a-z0-9-]{2,31}',username) or username.endswith('-') or username in RESERVED:
                raise ValueError('Choose a username of 3–32 letters, numbers or hyphens; that name may be reserved.')
            name = _clean(form.get('display_name',''),80,'display name')
            title = _clean(form.get('title',''),120,'meeting title')
            location = _clean(form.get('location',''),500,'location or meeting link',False)
            tz = str(_zone(str(form.get('timezone',''))))
            start, end = (time.fromisoformat(str(form.get(field,''))) for field in ('work_start','work_end'))
            if start >= end or any(t.second or t.microsecond or t.minute not in (0,30) or t.tzinfo for t in (start,end)):
                raise ValueError('Use working hours on a 30-minute boundary, ending after they start.')
            weekdays = sorted(set(int(v) for v in str(form.get('weekdays','')).split(',')))
            if not weekdays or any(v not in range(7) for v in weekdays):
                raise ValueError('Choose at least one working day.')
            enabled = str(form.get('enabled','')).lower() in ('on','true','1')
            with BOOKING_LOCK:
                if enabled and not self._ready(user.id):
                    return self._settings(user,session,status=403,error='Connect and verify Google Calendar before publishing. Beta approval is required.')
                try:
                    self.conn.execute('''INSERT INTO public_booking_profiles
                        (user_id,username,display_name,title,timezone,location,work_start,work_end,weekdays,enabled)
                        VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                        username=excluded.username,display_name=excluded.display_name,title=excluded.title,
                        timezone=excluded.timezone,location=excluded.location,work_start=excluded.work_start,
                        work_end=excluded.work_end,weekdays=excluded.weekdays,enabled=excluded.enabled''',
                        (user.id,username,name,title,tz,location,start.strftime('%H:%M'),end.strftime('%H:%M'),json.dumps(weekdays),int(enabled)))
                    self.conn.commit()
                except sqlite3.Error:
                    self.conn.rollback()
                    raise
        except sqlite3.IntegrityError:
            return self._settings(user,session,status=409,error='That username is already in use. Choose another.')
        except (ValueError,TypeError):
            return self._settings(user,session,status=400,error='Check your username, timezone, working days and hours. Use 30-minute boundaries and valid text.')
        return RedirectResponse('/connections/scheduling',status_code=303,headers=HEADERS)
