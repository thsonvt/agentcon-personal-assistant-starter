"""Event admission preserves the separate, bounded assistant beta entitlement."""
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.connections import Connections
from app.crypto import TokenBox, generate_key
from app.signup import signup_routes
from app.store import Store
from app.users import CALENDAR_SCOPE, SignupError, SignupsFull, Users
from tests.test_connections_routes import OAuth
from tests.test_connections_users import NOW


def database(**kwargs):
    db = Users(Store(':memory:'), **kwargs)
    db.ensure_admin('Owner', '6591234567', NOW)
    return db


def event_user(db, email='a@example.com', subject='sub-a', **kwargs):
    return db.sign_in_event_identity(subject, 'Attendee', email, NOW, **kwargs)[0]


def test_event_identity_has_separate_capacity_and_no_assistant_grants():
    db = database(max_users=1, auto_activate=True, allowed_emails=['beta@example.com'])
    user = event_user(db)
    assert user.event_only and user.status == 'pending'
    assert not user.google_token_enc and not user.phone and not user.joined
    assert not user.calendar_ready and user.google_scopes == ()
    assert db.seats_left() == 0
    again, is_new = db.sign_in_event_identity('sub-a', 'Different name', user.email, NOW)
    assert not is_new and again == user


def test_event_identities_do_not_consume_beta_seats_and_capacity_is_bounded():
    db = database(max_users=2)
    event_user(db, capacity=2)
    event_user(db, 'b@example.com', 'sub-b', capacity=2)
    assert db.seats_left() == 1
    with pytest.raises(SignupsFull):
        event_user(db, 'c@example.com', 'sub-c', capacity=2)
    beta, _ = db.sign_in_identity('beta', 'Beta', 'beta@example.com', NOW)
    assert not beta.event_only and db.seats_left() == 0


@pytest.mark.parametrize('change', ['subject', 'email', 'disabled', 'rejected', 'admin'])
def test_event_identity_cannot_rebind_or_reenable_account(change):
    db = database()
    user = event_user(db)
    subject, email = 'sub-a', user.email
    if change == 'subject': subject = 'different'
    elif change == 'email': email = 'different@example.com'
    elif change == 'admin': db._update(user.id, NOW, is_admin=1)
    else: db._update(user.id, NOW, status=change)
    before = db.get(user.id)
    with pytest.raises(SignupError):
        db.sign_in_event_identity(subject, 'Replacement', email, NOW)
    assert db.get(user.id) == before


def test_existing_beta_identity_and_grants_are_not_changed_by_event_signin():
    db = database()
    existing, _ = db.start_signup('Beta name', 'a@example.com', 'encrypted', NOW, [CALENDAR_SCOPE])
    user, is_new = db.sign_in_event_identity('sub-a', 'Roster name', existing.email, NOW)
    assert not is_new and not user.event_only
    assert user.name == existing.name and user.google_token_enc == 'encrypted'
    assert user.status == existing.status and user.google_scopes == existing.google_scopes


@pytest.mark.parametrize('route', ['signin', 'approve', 'approve_beta', 'calendar'])
@pytest.mark.parametrize('reason', ['not_invited', 'full'])
def test_event_assistant_upgrade_requires_both_invitation_and_seat(route, reason):
    db = database(max_users=1 if reason == 'full' else 5,
                  allowed_emails=['a@example.com'] if reason == 'full' else ['beta@example.com'])
    user = event_user(db)
    db._update(user.id, NOW, join_code='EVENT1')
    before = db.get(user.id)
    with pytest.raises(SignupError):
        if route == 'signin': db.sign_in_identity('sub-a', 'A', user.email, NOW)
        elif route == 'approve': db.approve('EVENT1', NOW)
        elif route == 'approve_beta': db.approve_beta(user.id, NOW)
        else:
            db.commit_connection(user.id, 0, NOW, google_token_enc='new',
                                 google_scopes='["'+CALENDAR_SCOPE+'"]', calendar_status='connected')
    assert db.get(user.id) == before


def test_invited_event_user_can_upgrade_once_and_consumes_beta_seat():
    db = database(max_users=2, auto_activate=True, allowed_emails=['a@example.com'])
    user = event_user(db)
    upgraded, is_new = db.sign_in_identity('sub-a', 'A', user.email, NOW)
    assert not is_new and upgraded.id == user.id and not upgraded.event_only
    assert upgraded.status == 'approved' and not upgraded.calendar_ready
    assert db.seats_left() == 0
    assert db.sign_in_identity('sub-a', 'A', user.email, NOW)[0] == upgraded


class EventAccess:
    def __init__(self):
        self.eligible = {'a@example.com'}
    def get_event(self, slug):
        return {'slug': slug} if slug == 'ai-tinkerers' else None
    def eligible_for_claim(self, slug, email):
        return slug == 'ai-tinkerers' and email in self.eligible


@pytest.fixture
def event_site():
    db = database(max_users=1, auto_activate=True, allowed_emails=['beta@example.com'])
    oauth = OAuth()
    access = EventAccess()
    clock = [NOW]
    commands = SimpleNamespace(announce_signup=lambda user: None)
    box = TokenBox(generate_key())
    obj = Connections(oauth, db, box, commands, '15550001234', lambda: clock[0], event_access=access)
    app = FastAPI()
    app.include_router(obj.router)
    app.include_router(signup_routes(oauth, db, box, commands, '15550001234', 's'*32,
                                    lambda: clock[0], connections_callback=obj.callback))
    client = TestClient(app, base_url='https://testserver', follow_redirects=False)
    def start(**kwargs):
        return client.get('/signin/google', params={'event': 'ai-tinkerers', **kwargs})
    def finish(started, **kwargs):
        state = parse_qs(urlparse(started.headers['location']).query)['state'][0]
        return client.get('/signup/callback', params={'state': state, 'code': 'code', **kwargs})
    return SimpleNamespace(db=db, oauth=oauth, access=access, clock=clock, obj=obj,
                           client=client, start=start, finish=finish)


def test_event_oauth_returns_to_explicit_claim_without_calendar_or_redirect_injection(event_site):
    site = event_site
    result = site.finish(site.start(next='https://evil.example'))
    assert result.status_code == 303 and result.headers['location'] == '/events/ai-tinkerers/claim'
    assert site.db.by_email('a@example.com').event_only
    assert site.client.cookies.get('pippa_session')
    assert site.oauth.calls == [('identity', None)]


@pytest.mark.parametrize('event', ['unknown', '//evil.example', '../../connections', 'https://evil.example'])
def test_event_oauth_rejects_unknown_or_noncanonical_event(event_site, event):
    result = event_site.start(event=event)
    assert result.status_code == 400
    assert not event_site.oauth.calls


def test_event_oauth_rechecks_roster_at_callback(event_site):
    started = event_site.start()
    event_site.access.eligible.clear()
    assert event_site.finish(started).status_code == 403
    assert event_site.db.by_email('a@example.com') is None


def test_event_context_cannot_be_injected_into_ordinary_oauth_callback(event_site):
    started = event_site.client.get('/signin/google')
    assert event_site.finish(started, event='ai-tinkerers').status_code == 403
    assert event_site.db.by_email('a@example.com') is None


def test_event_oauth_remains_bound_to_browser_and_single_use(event_site):
    site = event_site
    started = site.start()
    oauth_cookie = site.client.cookies.get('pippa_oauth')
    site.client.cookies.delete('pippa_oauth')
    assert site.finish(started).status_code == 400
    site.client.cookies.set('pippa_oauth', oauth_cookie)
    assert site.finish(started).status_code == 400
    assert site.db.by_email('a@example.com') is None


def test_hundred_distinct_attendees_can_start_on_one_wifi(event_site):
    for index in range(100):
        event_site.client.cookies.clear()
        event_site.clock[0] = NOW + timedelta(seconds=index*9)
        assert event_site.start().status_code == 302


def test_event_signin_limits_one_browser_without_rotating_its_cookie(event_site):
    site = event_site
    initial = site.start()
    cookie = site.client.cookies.get('pippa_event_browser')
    assert cookie and 'HttpOnly' in initial.headers['set-cookie'] and 'Secure' in initial.headers['set-cookie']
    for _ in range(9):
        assert site.start().status_code == 302
        assert site.client.cookies.get('pippa_event_browser') == cookie
    assert site.start().status_code == 429
    site.clock[0] += timedelta(hours=1, seconds=1)
    assert site.start().status_code == 302


def test_event_signin_coarse_limit_cannot_be_bypassed_by_forwarded_header(event_site):
    site = event_site
    for index in range(300):
        site.client.cookies.clear()
        response = site.client.get('/signin/google?event=ai-tinkerers', headers={'x-forwarded-for': f'192.0.2.{index}'})
        assert response.status_code == 302
    site.client.cookies.clear()
    assert site.start().status_code == 429


def test_event_user_cannot_start_calendar_grant_without_assistant_entitlement(event_site):
    site = event_site
    assert site.finish(site.start()).status_code == 303
    before = site.db.by_email('a@example.com')
    result = site.client.get('/connections/google?service=calendar')
    assert result.status_code == 403
    assert site.db.get(before.id) == before
    assert site.oauth.calls == [('identity', None)]


@pytest.mark.parametrize('reason', ['not_invited', 'full'])
def test_legacy_event_join_cannot_bypass_assistant_entitlement(reason):
    db = database(max_users=1 if reason == 'full' else 5,
                  allowed_emails=['a@example.com'] if reason == 'full' else ['beta@example.com'])
    user = event_user(db)
    # Simulate persisted legacy state: even an existing credential and join code
    # cannot bypass the seat boundary when proving the phone number.
    db.conn.execute("UPDATE users SET typed_phone=?,join_code=?,join_code_expires_at=?,"
                    "google_token_enc=?,google_scopes=?,calendar_status='connected' WHERE id=?",
                    ('6581112222', 'EVENT1', (NOW+timedelta(minutes=30)).isoformat(),
                     'encrypted', '["'+CALENDAR_SCOPE+'"]', user.id))
    db.conn.commit()
    before = db.get(user.id)
    with pytest.raises(SignupError):
        db.join('EVENT1', '6581112222', NOW)
    assert db.get(user.id) == before


def test_event_limiter_caps_retained_keys_and_reclaims_expired_entries():
    from app.connections import _BoundedEventLimiter
    limiter = _BoundedEventLimiter(10, max_keys=2)
    assert limiter.allow('one', NOW) and limiter.allow('two', NOW)
    assert not limiter.allow('third', NOW)
    assert len(limiter.hits) == 2
    assert limiter.allow('third', NOW+timedelta(hours=1, seconds=1))
    assert list(limiter.hits) == ['third']


def test_event_oauth_context_survives_connections_restart(event_site):
    site = event_site
    started = site.start()
    restarted = Connections(site.oauth, site.db, site.obj.box, site.obj.commands,
                            site.obj.bot_whatsapp, lambda: site.clock[0], event_access=site.access)
    intent = site.db.conn.execute('SELECT event_slug FROM connection_oauth').fetchone()
    assert intent['event_slug'] == 'ai-tinkerers'
    # Callback method uses the same persistent intent, independent of the old object.
    state = parse_qs(urlparse(started.headers['location']).query)['state'][0]
    from starlette.requests import Request
    request = Request({'type': 'http', 'headers': [(b'cookie',
                      ('pippa_oauth='+site.client.cookies.get('pippa_oauth')).encode())]})
    result = restarted.callback(request, state=state, code='code')
    assert result.headers['location'] == '/events/ai-tinkerers/claim'


def test_rejected_browser_retries_do_not_exhaust_venue_budget(event_site):
    site = event_site
    for _ in range(10):
        assert site.start().status_code == 302
    for _ in range(301):
        assert site.start().status_code == 429
    site.client.cookies.clear()
    assert site.start().status_code == 302


def test_hundred_attendees_complete_oauth_on_one_wifi(event_site):
    from app.tools.google_oauth import GoogleAccount
    site = event_site
    for index in range(100):
        email = f'attendee{index}@example.com'
        site.access.eligible.add(email)
        site.oauth.account = GoogleAccount(email, f'Attendee {index}', '',
                                          granted_scopes=('openid', 'email'),
                                          subject=f'event-sub-{index}', access_token='access')
        site.client.cookies.clear()
        site.clock[0] = NOW + timedelta(seconds=index * 9)
        result = site.finish(site.start())
        assert result.status_code == 303
        assert result.headers['location'] == '/events/ai-tinkerers/claim'
        assert site.obj._session(site.client.cookies.get('pippa_session'))
        user = site.db.by_email(email)
        assert user.event_only and not user.google_token_enc and user.status == 'pending'
    assert site.db.conn.execute('SELECT COUNT(*) FROM users WHERE event_only=1').fetchone()[0] == 100
    assert site.db.seats_left() == 0  # The single beta seat still belongs to the organizer.


def test_default_event_capacity_accepts_150_and_refuses_151():
    db = database(max_users=1)
    for index in range(150):
        event_user(db, email=f'attendee{index}@example.com', subject=f'sub-{index}')
    with pytest.raises(SignupsFull):
        event_user(db, email='overflow@example.com', subject='overflow')
    assert db.by_email('overflow@example.com') is None
    assert db.conn.execute('SELECT COUNT(*) FROM users WHERE event_only=1').fetchone()[0] == 150


def test_invitation_revoked_during_calendar_oauth_cannot_upgrade_event_identity(event_site):
    from app.tools.google_oauth import GoogleAccount
    site = event_site
    assert site.finish(site.start()).status_code == 303
    user = site.db.by_email('a@example.com')
    site.db.max_users = 2
    site.db.allowed_emails = frozenset({'a@example.com'})
    started = site.client.get('/connections/google?service=calendar')
    assert started.status_code == 302
    site.db.allowed_emails = frozenset({'other@example.com'})
    site.oauth.account = GoogleAccount(user.email, user.name, 'refresh-token',
                                      granted_scopes=(CALENDAR_SCOPE,), subject=user.google_subject,
                                      access_token='access')
    result = site.finish(started)
    assert result.status_code == 403
    assert site.db.get(user.id) == user
    assert site.db.seats_left() == 1


class _AdmissionRaceConnection:
    """Hold the old unprotected insertion until both independent readers finish.

    A transaction-owned connection must not rely on this shared connection.
    """
    def __init__(self, conn, barrier):
        self.conn, self.barrier = conn, barrier

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def execute(self, statement, *args):
        if statement.startswith('INSERT INTO users (name,email,status,google_subject,google_scopes,calendar_status'):
            self.barrier.wait(timeout=10)
        return self.conn.execute(statement, *args)


def _event_admission_process(path, index, barrier, outcomes):
    db = Users(Store(path), max_users=5)
    db.conn = _AdmissionRaceConnection(db.conn, barrier)
    try:
        user, created = db.sign_in_event_identity(f'race-{index}', 'Attendee', f'race{index}@example.com', NOW, capacity=1)
        outcomes.put(('admitted', user.id, created))
    except SignupsFull:
        outcomes.put(('full',))
    except Exception as exc:
        outcomes.put(('unexpected', type(exc).__name__))
    finally:
        db.conn.close()


def test_independent_processes_cannot_overfill_event_capacity(tmp_path):
    import multiprocessing
    path = str(tmp_path / 'admission.sqlite')
    owner = Users(Store(path))
    owner.ensure_admin('Organizer', '6591234567', NOW)
    context = multiprocessing.get_context('spawn')
    barrier, outcomes = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_event_admission_process, args=(path, index, barrier, outcomes))
                 for index in range(2)]
    try:
        for process in processes:
            process.start()
        results = [outcomes.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert sorted(result[0] for result in results) == ['admitted', 'full']
        assert owner.conn.execute('SELECT COUNT(*) FROM users WHERE event_only=1').fetchone()[0] == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=2)
        outcomes.close()
        owner.conn.close()


def test_failed_event_admission_rolls_back_before_unrelated_session_commit():
    import sqlite3
    db = database()
    # RAISE(FAIL) leaves this statement's earlier changes pending; the admission
    # transaction itself must roll them back, rather than trusting statement abort.
    db.conn.executescript("""
        CREATE TRIGGER fail_event_admission AFTER INSERT ON users
        WHEN NEW.event_only=1
        BEGIN SELECT RAISE(FAIL,'synthetic admission failure'); END;
    """)
    with pytest.raises(sqlite3.IntegrityError, match='synthetic admission failure'):
        event_user(db)
    db.conn.commit()  # An unrelated OAuth/session commit must not publish the user.
    assert db.by_email('a@example.com') is None
    db.conn.execute('DROP TRIGGER fail_event_admission')
    db.conn.commit()
    assert event_user(db).event_only  # The failed attempt released its write lock.
