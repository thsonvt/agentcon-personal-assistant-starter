"""Authenticated event attendee routes; public links never contain credentials."""
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request, Path
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.connections import SESSION_AGE, SESSION_COOKIE
from app.event_pages import render_event_page
from app.event_qr import qr_svg
from app.event_store import EventStore, EventError
from app.router import RateLimiter

HEADERS = {'Cache-Control':'no-store', 'Referrer-Policy':'same-origin',
           'X-Content-Type-Options':'nosniff', 'X-Robots-Tag':'noindex, nofollow'}
MAX_BODY = 16384


class EventNetworking:
    def __init__(self, connections, users, public_booking, now, base_url, store=None):
        self.connections, self.users, self.public_booking = connections, users, public_booking
        self.now, self.base_url = now, base_url.rstrip('/')
        self.store = store or EventStore(users, public_booking, now, self.base_url)
        connections.event_access = self.store
        public_booking.network_profile = self.personal_page
        self.limiter = RateLimiter(limit=60, window=timedelta(hours=1))
        self.router = APIRouter()
        self._routes()

    def _page(self, view, status=200, user=None, session=None, **data):
        data.update(signed_in=bool(user), csrf_token=session['csrf'] if session else '', user_id=user.id if user else None)
        return HTMLResponse(render_event_page(view, data), status_code=status, headers=HEADERS)

    def _error(self, message='This page is unavailable.', status=404):
        return self._page('error', status=status, error=message)

    def _redirect(self, target):
        return RedirectResponse(target, status_code=303, headers=HEADERS)

    def _event(self, slug):
        try:
            return self.store.get_event(slug)
        except EventError:
            return None

    def _membership(self, slug, user):
        if user is None:
            return None
        try:
            row = self.store.membership(slug, user.id)
            return row if row and row['claimed_at'] and row['checked_in_at'] and not row['removed_at'] else None
        except EventError:
            return None

    async def _form(self, request):
        user, session = self.connections._auth(request)
        if user is None:
            slug = request.path_params.get('slug')
            target = '/events/' + quote(slug, safe='') if slug else '/network/signin'
            return None, None, None, self._redirect(target)
        origin = request.headers.get('origin')
        base = urlsplit(self.base_url)
        if request.headers.get('sec-fetch-site') == 'cross-site' or (origin and origin.rstrip('/') != f'{base.scheme}://{base.netloc}'):
            return user, session, None, self._error('Refresh this page before continuing.', 403)
        if request.headers.get('content-type', '').split(';', 1)[0].lower() != 'application/x-www-form-urlencoded':
            return user, session, None, self._error('Use this page’s form to continue.', 415)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                return user, session, None, self._error('This form is too large.', 413)
        try:
            fields = parse_qs(body.decode('utf-8'), keep_blank_values=True, max_num_fields=30)
        except (UnicodeError, ValueError):
            return user, session, None, self._error('Check this form and try again.', 400)
        csrf = fields.get('csrf_token', [''])
        if len(csrf) != 1 or not csrf[0] or not secrets.compare_digest(csrf[0].encode('utf-8'), session['csrf'].encode('utf-8')):
            return user, session, None, self._error('This form expired. Refresh the page and try again.', 403)
        if not self.limiter.allow(str(user.id), self.now()):
            return user, session, None, self._error('Too many changes. Please try again later.', 429)
        return user, session, fields, None

    @staticmethod
    def _value(form, key, default=''):
        values = form.get(key, [default])
        if len(values) != 1:
            raise EventError('Submit each field only once.')
        return values[0]

    def _editable_profile(self, user, member):
        try:
            profile = self.store.profile(user.id, viewer_id=user.id)
        except EventError:
            existing = self.public_booking._profile(user_id=user.id) or {}
            profile = dict(user_id=user.id, display_name=user.name, username=existing.get('username') or self.store.suggest_username(user.id, user.name),
                           building='', intents=[], visibility='hidden')
        profile['directory_opt_in'] = bool(member['directory_opt_in'])
        return profile

    def _profile_page(self, request, profile):
        user, session = self.connections._auth(request)
        uid = profile['user_id']
        shared_events = []
        if user and user.id != uid:
            for event in profile.get('events', []):
                target = self.store.membership(event['slug'], uid)
                if target and target['directory_opt_in']:
                    shared_events.append(event)
        return self._page('profile', user=user, session=session, profile=profile,
            is_owner=bool(user and user.id == uid), shared_events=shared_events,
            idempotency_key=secrets.token_urlsafe(24), profile_url=f'{self.base_url}/people/{uid}')

    def personal_page(self, request, username):
        user, _ = self.connections._auth(request)
        try:
            profile = self.store.profile_by_username(username, viewer_id=user.id if user else None)
        except EventError:
            profile = None
        return self._profile_page(request, profile) if profile else None

    def _ready_booking_link(self, user_id):
        profile = self.public_booking._profile(user_id=user_id)
        if profile and self.public_booking._host(profile['username']):
            return self.base_url + '/' + quote(profile['username'], safe='') + '/30min'
        return None

    def _routes(self):
        @self.router.get('/network/signin')
        def network_signin(request: Request):
            user, session = self.connections._auth(request)
            return self._page('signin', user=user, session=session, events=self.store.list_events())

        @self.router.get('/network/privacy')
        def network_privacy(request: Request):
            user, session = self.connections._auth(request)
            return self._page('privacy', user=user, session=session, event_only=bool(user and user.event_only))

        @self.router.post('/network/privacy/delete')
        async def remove_networking(request: Request):
            user, session, fields, error = await self._form(request)
            if error is not None: return error
            issued_at = datetime.fromisoformat(session['expires_at']) - SESSION_AGE
            if not timedelta(0) <= self.now() - issued_at <= timedelta(minutes=15):
                return self._page('privacy', status=403, user=user, session=session, event_only=user.event_only,
                    error='Sign in with Google again through your event within 15 minutes before removing data.', reauthenticate=True)
            try:
                if self._value(fields, 'confirmation') != 'DELETE':
                    raise EventError('Type DELETE exactly to confirm removing your event data.')
                result = self.store.delete_networking_data(user.id)
            except EventError as exc:
                return self._page('privacy', status=400, user=user, session=session, event_only=user.event_only, error=str(exc))
            removed_account = result['account_deleted']
            response = self._page('privacy', user=None if removed_account else user,
                                  session=None if removed_account else session, deleted=True, account_deleted=removed_account)
            if removed_account:
                response.delete_cookie(SESSION_COOKIE, secure=True, httponly=True, samesite='lax')
            return response

        @self.router.get('/events/{slug}')
        def landing(request: Request, slug: str):
            event = self._event(slug)
            if not event:
                return self._error()
            user, session = self.connections._auth(request)
            return self._page('landing', user=user, session=session, event=event, member=self._membership(slug, user))

        @self.router.get('/events/{slug}/claim')
        def claim_page(request: Request, slug: str):
            event = self._event(slug)
            if not event:
                return self._error()
            user, session = self.connections._auth(request)
            if self._membership(slug, user):
                return self._redirect(f'/events/{quote(slug, safe="")}/profile')
            return self._page('claim', user=user, session=session, event=event)

        @self.router.post('/events/{slug}/claim')
        async def claim(request: Request, slug: str):
            user, session, fields, error = await self._form(request)
            if error is not None: return error
            event = self._event(slug)
            if not event: return self._error()
            try:
                self.store.claim(slug, user)
            except EventError as exc:
                return self._page('claim', status=400, user=user, session=session, event=event, error=str(exc))
            return self._redirect(f'/events/{quote(slug, safe="")}/profile')

        @self.router.get('/events/{slug}/profile')
        def edit(request: Request, slug: str):
            event = self._event(slug)
            if not event: return self._error()
            user, session = self.connections._auth(request)
            member = self._membership(slug, user)
            if not member: return self._redirect(f'/events/{quote(slug, safe="")}/claim')
            profile = self._editable_profile(user, member)
            return self._page('edit', user=user, session=session, event=event, member=member, profile=profile)

        @self.router.post('/events/{slug}/profile')
        async def save_profile(request: Request, slug: str):
            user, session, fields, error = await self._form(request)
            if error is not None: return error
            event = self._event(slug)
            if not event: return self._error()
            member = self._membership(slug, user)
            if not member: return self._error('Claim your checked-in profile first.', 403)
            try:
                self.store.update_profile(slug, user.id, display_name=self._value(fields,'display_name'),
                    building=self._value(fields,'building'), intents=fields.get('intents', []),
                    visibility=self._value(fields,'visibility','hidden'),
                    directory_opt_in=self._value(fields,'directory_opt_in') == 'on', username=self._value(fields,'username'))
            except EventError as exc:
                profile = self._editable_profile(user, member)
                return self._page('edit', status=400, user=user, session=session, event=event, member=member, profile=profile, error=str(exc))
            return self._redirect(f'/events/{quote(slug, safe="")}/profile')

        @self.router.get('/events/{slug}/directory')
        def directory(request: Request, slug: str, q: str = ''):
            event = self._event(slug)
            if not event: return self._error()
            user, session = self.connections._auth(request)
            member = self._membership(slug, user)
            if not member: return self._redirect(f'/events/{quote(slug, safe="")}/claim')
            try:
                people = self.store.directory(slug, user.id, search=q)
            except EventError as exc:
                return self._error(str(exc), 400)
            return self._page('directory', user=user, session=session, event=event, member=member, people=people, search=q)

        @self.router.get('/events/{slug}/qr.svg')
        def event_qr(slug: str):
            if not self._event(slug): return self._error()
            try:
                return Response(qr_svg(f'{self.base_url}/events/{quote(slug,safe="")}'), media_type='image/svg+xml', headers=HEADERS)
            except ValueError:
                return self._error('Use the event link to share this page.', 400)

        @self.router.get('/people/{profile_id}')
        def profile_page(request: Request, profile_id: int = Path(ge=1, le=2**63-1)):
            user, _ = self.connections._auth(request)
            try:
                profile = self.store.profile(profile_id, viewer_id=user.id if user else None)
            except EventError:
                profile = None
            if profile:
                return self._profile_page(request, profile)
            return self._page('private', status=404) if user is None else self._error()

        @self.router.get('/people/{profile_id}/qr.svg')
        def profile_qr(request: Request, profile_id: int = Path(ge=1, le=2**63-1)):
            user, _ = self.connections._auth(request)
            try:
                if not self.store.profile(profile_id, viewer_id=user.id if user else None): return self._error()
                return Response(qr_svg(f'{self.base_url}/people/{profile_id}'), media_type='image/svg+xml', headers=HEADERS)
            except EventError:
                return self._error()
            except ValueError:
                return self._error('Use the profile link to share this page.', 400)

        @self.router.post('/people/{profile_id}/requests')
        async def send_request(request: Request, profile_id: int = Path(ge=1, le=2**63-1)):
            user, session, fields, error = await self._form(request)
            if error is not None: return error
            try:
                result = self.store.send_followup(self._value(fields,'event_slug'), user.id, profile_id,
                    self._value(fields,'purpose'), self._value(fields,'note'), self._value(fields,'idempotency_key'))
            except EventError as exc:
                return self._error(str(exc), 400)
            return self._redirect('/network/inbox/' + result['id'])

        @self.router.get('/network/inbox')
        def inbox(request: Request):
            user, session = self.connections._auth(request)
            if user is None: return self._redirect('/network/signin')
            return self._page('inbox', user=user, session=session, requests=self.store.inbox(user.id))

        @self.router.get('/network/inbox/{request_id}')
        def detail(request: Request, request_id: str):
            user, session = self.connections._auth(request)
            if user is None: return self._redirect('/network/signin')
            try:
                result = self.store.get_followup(request_id, user.id)
            except EventError:
                return self._error()
            if not result: return self._error()
            other_id = result['recipient_id'] if result['sender_id'] == user.id else result['sender_id']
            booking_url = self._ready_booking_link(other_id) if result['state'] in ('accepted', 'time_proposed', 'time_agreed') else None
            return self._page('detail', user=user, session=session, request=result, booking_url=booking_url)

        @self.router.post('/network/inbox/{request_id}/{action}')
        async def followup_action(request: Request, request_id: str, action: str):
            user, session, fields, error = await self._form(request)
            if error is not None: return error
            try:
                current = self.store.get_followup(request_id, user.id)
                if not current: return self._error()
                version = int(self._value(fields,'version')) if action != 'block' else None
                if action in ('accept','decline','withdraw'):
                    self.store.respond(request_id,user.id,action,version=version)
                elif action == 'propose':
                    zone_name = self._value(fields,'timezone')
                    zone = ZoneInfo(zone_name)
                    duration = int(self._value(fields,'duration','30'))
                    if duration not in (30,60): raise EventError('Choose a 30 or 60 minute meeting.')
                    slots = []
                    for index in range(1,4):
                        value = self._value(fields,f'slot_{index}')
                        if not value: continue
                        local = datetime.fromisoformat(value)
                        if local.tzinfo is not None: raise EventError('Choose a local time and its timezone.')
                        start = local.replace(tzinfo=zone)
                        # A daylight-saving gap is not a valid local wall time.
                        if start.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != local:
                            raise EventError('That local time does not exist in this timezone. Choose another time.')
                        if start.utcoffset() != local.replace(tzinfo=zone,fold=1).utcoffset():
                            raise EventError('That local time happens twice. Choose a time outside the daylight-saving change.')
                        slots.append(dict(start=start.isoformat(),end=(start.astimezone(timezone.utc)+timedelta(minutes=duration)).isoformat()))
                    self.store.propose_times(request_id,user.id,slots,zone_name,self._value(fields,'location'),version)
                elif action == 'confirm':
                    self.store.confirm_time(request_id,user.id,int(self._value(fields,'slot_index')),version)
                elif action == 'block':
                    target = current['recipient_id'] if current['sender_id'] == user.id else current['sender_id']
                    slug = current['event_slug']
                    self.store.block(slug,user.id,target)
                    return self._redirect('/network/inbox')
                else:
                    return self._error()
            except EventError as exc:
                return self._error(str(exc), 400)
            except (ValueError,TypeError,OverflowError,ZoneInfoNotFoundError,StopIteration):
                return self._error('Check the time, timezone and current request version, then try again.', 400)
            return self._redirect('/network/inbox/' + request_id)
