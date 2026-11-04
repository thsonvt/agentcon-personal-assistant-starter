"""Public bookings use fake calendars only; these tests never send invitations."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.store import Store
from app.users import Users, CALENDAR_SCOPE
from app.tools.calendar import CalendarError, Event, EventNotFoundError, GoogleCalendar

NOW = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


class Calendar:
    def __init__(self):
        self.busy = []
        self.events = {}
        self.writes = []
        self.reads = []
        self.fail_read = False
        self.fail_write = False
        self.write_then_fail = False
        self.before_write = None

    def busy_periods(self, start, end):
        self.reads.append((start, end))
        if self.fail_read:
            raise CalendarError('private provider detail')
        return self.busy

    def create_event(self, title, start, end, attendee_email=None, location=None, event_id=None):
        if self.before_write:
            self.before_write()
        self.writes.append(event_id)
        if self.fail_write:
            raise CalendarError('timeout')
        event = Event(event_id, title, start, end, False, location=location)
        self.events[event_id] = event
        if self.write_then_fail:
            raise CalendarError('timeout after insert')
        return event

    def get_event(self, event_id):
        if event_id not in self.events:
            raise EventNotFoundError('missing')
        return self.events[event_id]


@pytest.fixture
def site(monkeypatch):
    import app.public_booking as module
    monkeypatch.setattr(module, 'render_public_booking', lambda data: json.dumps(data))
    monkeypatch.setattr(module, 'render_booking_settings', lambda data: json.dumps(data))
    from app.connections import Connections
    db = Users(Store(':memory:'))
    user, _ = db.sign_in_identity('sub-host', 'Host', 'host@example.com', NOW)
    db._update(user.id, NOW, status='approved', google_token_enc='encrypted',
               google_scopes=json.dumps([CALENDAR_SCOPE]), calendar_status='connected')
    connections = Connections(None, db, None, SimpleNamespace(), '', lambda: clock[0])
    clock = [NOW]
    cal = Calendar()
    db.conn.execute('INSERT INTO connection_sessions VALUES (?,?,?,?)',
                    (hashlib.sha256(b'host-session').hexdigest(), user.id, 'csrf', (NOW + timedelta(days=1)).isoformat()))
    db.conn.commit()
    obj = module.PublicBooking(connections, db, lambda uid: cal, lambda: clock[0], 'https://testserver', 's'*32)
    app = FastAPI(); app.include_router(obj.router)
    client = TestClient(app, base_url='https://testserver', follow_redirects=False)
    client.cookies.set('pippa_session', 'host-session')
    settings = dict(csrf_token='csrf', username='son', display_name='Son', title='30-minute meeting',
                    timezone='Asia/Singapore', work_start='09:00', work_end='18:00', weekdays='0,1,2,3,4',
                    location='https://meet.google.com/example', enabled='on')
    def publish(**changes):
        return client.post('/connections/scheduling', data={**settings, **changes})
    def start():
        response = client.get('/son/30min')
        return response.json()['booking_token']
    def book(token, **changes):
        return client.post('/booking/son/reserve', data={
            'booking_token': token, 'name':'Visitor', 'email':'guest@example.com',
            'start':'2026-10-01T10:00:00+08:00', 'timezone':'Asia/Singapore', 'website':'', **changes})
    return SimpleNamespace(obj=obj, db=db, user=user, cal=cal, client=client, clock=clock,
                           publish=publish, start=start, book=book, settings=settings, app=app)


def test_host_without_whatsapp_can_publish_and_redirect(site):
    assert not site.db.get(site.user.id).joined
    assert site.publish().status_code == 303
    response = site.client.get('/SON')
    assert response.status_code == 307 and response.headers['location'] == '/son/30min'
    page = site.client.get('/son/30min')
    assert page.status_code == 200 and page.headers['cache-control'] == 'no-store'
    assert 'host@example.com' not in page.text and 'encrypted' not in page.text


@pytest.mark.parametrize('slug', ['signup','CONNECTIONS','booking','api','health','signin','static','privacy','admin','b2b','B2B','robots.txt','a/b','ab','with space'])
def test_reserved_and_invalid_names(site, slug):
    assert site.publish(username=slug).status_code == 400


def test_settings_csrf_origin_and_auth(site):
    assert site.publish(csrf_token='wrong').status_code == 403
    assert site.client.post('/connections/scheduling', data=site.settings, headers={'Origin':'https://evil.test'}).status_code == 403
    site.client.cookies.clear()
    assert site.publish().status_code == 303
    assert site.client.get('/connections/scheduling').headers['location'] == '/signin'


def test_username_unique_case_insensitive_and_owned_by_session(site):
    assert site.publish(username='SoN').status_code == 303
    other, _ = site.db.sign_in_identity('other','Other','other@example.com',NOW)
    site.db._update(other.id,NOW,status='approved',google_token_enc='enc',google_scopes=json.dumps([CALENDAR_SCOPE]),calendar_status='connected')
    site.db.conn.execute('UPDATE connection_sessions SET user_id=?', (other.id,)); site.db.conn.commit()
    assert site.publish(username='SON', user_id=site.user.id).status_code == 409


@pytest.mark.parametrize('change', [dict(status='disabled'),dict(status='rejected'),dict(status='pending'),dict(google_disconnected=1),dict(calendar_status='needs_reconnect'),dict(google_revocation_status='pending')])
def test_ineligible_host_cannot_publish_or_serve(site, change):
    site.publish(); token=site.start()
    site.db._update(site.user.id,NOW,**change)
    assert site.publish().status_code in (303,403)
    assert site.client.get('/son/30min').status_code == 404
    assert site.client.get('/booking/son/slots?date=2026-10-01&tz=Asia/Singapore').status_code == 404
    assert site.book(token).status_code == 404
    assert not site.cal.writes


def test_slots_notice_busy_weekdays_timezone_and_bounded_reads(site):
    site.publish()
    left=datetime.fromisoformat('2026-10-01T10:00:00+08:00')
    site.cal.busy=[(left,left+timedelta(minutes=30))]
    response=site.client.get('/booking/son/slots?date=2026-10-01&tz=Asia/Singapore')
    slots=response.json()['slots']
    assert slots[0]['start']=='2026-10-01T10:30:00+08:00'
    assert all(datetime.fromisoformat(s['end'])-datetime.fromisoformat(s['start'])==timedelta(minutes=30) for s in slots)
    assert site.client.get('/booking/son/slots?date=2026-10-03&tz=Asia/Singapore').json()['slots']==[]
    response=site.client.get('/booking/son/slots?date=2026-10-01&tz=UTC')
    assert response.json()['timezone']=='UTC'
    assert all(datetime.fromisoformat(s['start']).utcoffset()==timedelta(0) for s in response.json()['slots'])
    assert all(end-start<=timedelta(days=1,hours=1) for start,end in site.cal.reads)


def test_saigon_browser_timezone_works_without_legacy_zone_file(site, monkeypatch):
    import app.public_booking as module
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    def zone_without_legacy(name):
        if name == 'Asia/Saigon':
            raise ZoneInfoNotFoundError(name)
        return ZoneInfo(name)

    monkeypatch.setattr(module, 'ZoneInfo', zone_without_legacy)
    site.publish()
    response = site.client.get('/booking/son/slots?date=2026-10-01&tz=Asia/Saigon')
    assert response.status_code == 200
    assert response.json()['timezone'] == 'Asia/Saigon'
    slot = response.json()['slots'][0]
    assert datetime.fromisoformat(slot['start']).utcoffset() == timedelta(hours=7)
    booking = site.book(site.start(), start=slot['start'], timezone='Asia/Saigon')
    assert booking.status_code == 200
    assert len(site.cal.writes) == 1


@pytest.mark.parametrize('query',['date=2027-01-01&tz=UTC','date=2026-09-30&tz=UTC','date=invalid&tz=UTC','date=2026-10-01&tz=invalid'])
def test_invalid_slot_query_never_calls_provider(site, query):
    site.publish()
    assert site.client.get('/booking/son/slots?'+query).status_code==400
    assert not site.cal.reads


def test_calendar_error_never_exposes_private_data_or_open_slots(site):
    site.publish();site.cal.fail_read=True
    response=site.client.get('/booking/son/slots?date=2026-10-01&tz=UTC')
    assert response.status_code==503 and 'private provider detail' not in response.text
    assert site.book(site.start()).status_code==503 and not site.cal.writes


def test_booking_final_conflict_and_confirmed_idempotency(site):
    site.publish(); token=site.start()
    left=datetime.fromisoformat('2026-10-01T10:00:00+08:00')
    site.cal.busy=[(left,left+timedelta(minutes=30))]
    assert site.book(token).status_code==409 and not site.cal.writes
    site.cal.busy=[]
    result=site.book(token)
    assert result.status_code==200 and result.json()['status']=='confirmed'
    assert result.json()['invitation']=='requested'
    assert site.book(token).json()==result.json() and len(site.cal.writes)==1
    assert site.book(token,email='other@example.com').status_code==409


def test_browser_token_tamper_expiry_origin_honeypot(site):
    site.publish(); token=site.start()
    assert site.book(token+'x').status_code==403
    assert site.book(token,website='spam').status_code==400
    assert site.client.post('/booking/son/reserve',data={'booking_token':token},headers={'Origin':'https://evil.test'}).status_code==403
    browser=TestClient(site.app,base_url='https://testserver')
    assert browser.post('/booking/son/reserve',data={'booking_token':token}).status_code==403
    site.clock[0]+=timedelta(hours=2)
    assert site.book(token).status_code==403 and not site.cal.writes


@pytest.mark.parametrize('change',[{'email':'broken'},{'email':'a\r\n@example.com'},{'name':''},{'start':'2026-10-01T10:00:00'}, {'start':'2026-10-01T10:15:00+08:00'}, {'start':'2026-10-01T09:00:00+08:00'}, {'timezone':'No/Zone'}, {'start':'2026-10-03T10:00:00+08:00'}])
def test_invalid_booking_never_writes(site,change):
    site.publish()
    assert site.book(site.start(),**change).status_code==400
    assert not site.cal.writes


def test_unknown_write_reconciles_without_duplicate_after_restart(site):
    site.publish(); token=site.start(); site.cal.write_then_fail=True
    assert site.book(token).status_code==202
    from app.public_booking import PublicBooking
    restart=PublicBooking(site.obj.connections,site.db,lambda uid:site.cal,lambda:site.clock[0],'https://testserver','s'*32)
    app=FastAPI();app.include_router(restart.router)
    client=TestClient(app,base_url='https://testserver');client.cookies.update(site.client.cookies)
    result=client.post('/booking/son/reserve',data={'booking_token':token,'name':'Visitor','email':'guest@example.com','start':'2026-10-01T10:00:00+08:00','timezone':'Asia/Singapore'})
    assert result.json()['status']=='confirmed' and len(site.cal.writes)==1


def test_missing_unknown_write_never_inserts_again(site):
    site.publish();token=site.start();site.cal.fail_write=True
    assert site.book(token).status_code==202
    site.cal.fail_write=False
    assert site.book(token).status_code==202 and len(site.cal.writes)==1


class BookingEmail:
    ready = True
    transactional_id = 'booking-template'

    def __init__(self, status='accepted'):
        self.status, self.calls = status, []

    def send(self, payload, request_id):
        self.calls.append((payload, request_id))
        return self.status


def test_confirmed_booking_notifies_only_host_once(site):
    site.obj.host_email = sender = BookingEmail()
    site.publish(); token = site.start()
    response = site.book(token)
    assert response.json()['host_notification'] == 'accepted'
    payload, request_id = sender.calls[0]
    assert payload['email'] == 'host@example.com'
    assert payload['dataVariables']['guestName'] == 'Visitor'
    assert payload['dataVariables']['guestEmail'] == 'guest@example.com'
    assert payload['dataVariables']['timezone'] == 'Asia/Singapore'
    assert payload['dataVariables']['duration'] == '30 minutes'
    assert payload['dataVariables']['location'] == 'https://meet.google.com/example'
    assert payload['dataVariables']['calendarUrl'].startswith('https://calendar.google.com/')
    assert 'host@example.com' not in response.text
    assert site.book(token).json() == response.json()
    assert len(sender.calls) == len(site.cal.writes) == 1


def test_uncertain_calendar_write_waits_for_confirmation_before_host_email(site):
    site.obj.host_email = sender = BookingEmail()
    site.publish(); token = site.start(); site.cal.write_then_fail = True
    assert site.book(token).status_code == 202
    assert sender.calls == []
    assert site.book(token).json()['host_notification'] == 'accepted'
    assert len(sender.calls) == len(site.cal.writes) == 1


def test_email_failure_keeps_booking_and_retries_same_payload(site):
    site.obj.host_email = sender = BookingEmail('unknown')
    site.publish(); token = site.start()
    assert site.book(token).json()['host_notification'] == 'unknown'
    site.clock[0] += timedelta(minutes=2)
    sender.status = 'accepted'
    assert site.book(token).json()['host_notification'] == 'accepted'
    assert sender.calls[0] == sender.calls[1]
    assert len(site.cal.writes) == 1


def test_notification_retry_keeps_original_template_when_config_changes(site):
    site.obj.host_email = sender = BookingEmail('unknown')
    site.publish(); token = site.start(); site.book(token)
    original = sender.calls[0][0]
    assert original['transactionalId'] == 'booking-template'
    site.clock[0] += timedelta(minutes=2)
    replacement = BookingEmail()
    replacement.transactional_id = 'replacement-template'
    site.obj.host_email = replacement
    site.book(token)
    assert replacement.calls[0][0] == original


def test_uncertain_email_never_retries_after_provider_dedup_window(site):
    site.obj.host_email = sender = BookingEmail('unknown')
    site.publish(); token = site.start(); site.book(token)
    site.clock[0] += timedelta(hours=24)
    assert site.book(token).json()['host_notification'] == 'unknown'
    assert len(sender.calls) == 1


def test_host_settings_show_confirmation_emails_needing_attention(site):
    site.obj.host_email = BookingEmail('failed')
    site.publish(); site.book(site.start())
    page = site.client.get('/connections/scheduling')
    assert page.json()['notification_problem_count'] == 1


def test_unconfigured_email_is_explicit_and_guest_data_stays_private(site):
    site.publish()
    response = site.book(site.start())
    assert response.json()['host_notification'] == 'unconfigured'
    assert 'guest@example.com' not in response.text
    assert 'host@example.com' not in response.text


def test_notification_receipt_survives_restart_and_freezes_host_details(site):
    site.obj.host_email = sender = BookingEmail('failed')
    site.publish(location=''); token = site.start()
    site.book(token)
    site.publish(title='New title', display_name='New name', location='New location')
    from app.public_booking import PublicBooking
    restart = PublicBooking(site.obj.connections, site.db, lambda uid: site.cal,
                            lambda: site.clock[0], 'https://testserver', 's'*32, host_email=sender)
    site.clock[0] += timedelta(minutes=1)
    sender.status = 'accepted'
    restart.retry_notifications()
    assert sender.calls[0] == sender.calls[1]
    variables = sender.calls[1][0]['dataVariables']
    assert variables['hostName'] == 'Son'
    assert variables['title'] == '30-minute meeting'
    assert variables['location'] == 'Arrange with your guest'
    assert '2026' in variables['when'] and '10:00' in variables['when']
    assert len(site.cal.writes) == 1


@pytest.mark.parametrize('status', ['failed', 'unknown'])
def test_notification_retries_have_cooldown_and_three_attempt_cap(site, status):
    site.obj.host_email = sender = BookingEmail(status)
    site.publish(); token = site.start(); site.book(token)
    site.obj.retry_notifications()
    site.clock[0] += timedelta(seconds=59)
    site.obj.retry_notifications()
    assert len(sender.calls) == 1
    for _ in range(4):
        site.clock[0] += timedelta(minutes=1)
        site.obj.retry_notifications()
    assert len(sender.calls) == 3
    assert len(site.cal.writes) == 1


def test_crashed_attempt_retries_same_calendar_url_and_payload(site):
    site.obj.host_email = sender = BookingEmail('unknown')
    site.publish(); token = site.start(); site.cal.write_then_fail = True
    site.book(token)
    event_id = site.cal.writes[0]
    from dataclasses import replace
    site.cal.events[event_id] = replace(site.cal.events[event_id], source_url='https://calendar.google.com/event?eid=example')
    site.book(token)
    assert sender.calls[0][0]['dataVariables']['calendarUrl'] == 'https://calendar.google.com/event?eid=example'
    site.db.conn.execute("UPDATE host_booking_notifications SET status='sending'")
    site.db.conn.commit()
    site.clock[0] += timedelta(minutes=1)
    site.obj.retry_notifications()
    assert sender.calls[0] == sender.calls[1]
    site.clock[0] += timedelta(hours=23)
    site.obj.retry_notifications()
    assert len(sender.calls) == 2


@pytest.mark.parametrize('change', [dict(email='changed@example.com'), dict(google_disconnected=1), dict(status='disabled')])
def test_notification_retry_suppresses_changed_or_disconnected_owner(site, change):
    site.obj.host_email = sender = BookingEmail('unknown')
    site.publish(); site.book(site.start())
    site.db._update(site.user.id, NOW, **change)
    site.clock[0] += timedelta(minutes=1)
    site.obj.retry_notifications()
    assert len(sender.calls) == 1
    assert site.db.conn.execute('SELECT status FROM host_booking_notifications').fetchone()[0] == 'suppressed'


def test_notification_exception_preserves_confirmed_booking_and_unknown_receipt(site):
    sender = BookingEmail()
    def fail(payload, request_id):
        raise RuntimeError('private provider error with guest@example.com')
    sender.send = fail
    site.obj.host_email = sender
    site.publish(); response = site.book(site.start())
    assert response.json()['status'] == 'confirmed'
    assert response.json()['host_notification'] == 'unknown'
    assert 'guest@example.com' not in response.text


def test_legacy_booking_without_receipt_never_sends_retroactive_email(site):
    site.publish(); token = site.start(); site.book(token)
    site.db.conn.execute('DELETE FROM host_booking_notifications'); site.db.conn.commit()
    site.obj.host_email = sender = BookingEmail()
    assert site.book(token).json()['host_notification'] == 'not_requested'
    site.obj.retry_notifications()
    assert sender.calls == []


def test_receipt_is_committed_before_send_and_pending_booking_never_notifies(site):
    site.obj.host_email = sender = BookingEmail()
    site.publish(); token = site.start(); site.cal.write_then_fail = True
    assert site.book(token).json()['host_notification'] == 'queued'
    site.obj.retry_notifications()
    assert sender.calls == []
    def send(payload, request_id):
        receipt = site.db.conn.execute('SELECT * FROM host_booking_notifications WHERE request_id=?', (request_id,)).fetchone()
        assert receipt['status'] == 'sending' and receipt['attempts'] == 1
        assert receipt['first_attempt_at'] == site.clock[0].isoformat()
        assert json.loads(receipt['payload']) == payload
        return 'accepted'
    sender.send = send
    assert site.book(token).json()['host_notification'] == 'accepted'


def test_receipt_insert_failure_rolls_back_booking_before_calendar_write(site):
    import sqlite3
    site.publish(); token = site.start()
    site.db.conn.execute('''CREATE TRIGGER fail_receipt BEFORE INSERT ON host_booking_notifications
        BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END''')
    with pytest.raises(sqlite3.IntegrityError):
        site.book(token)
    assert not site.cal.writes
    assert site.db.conn.execute('SELECT COUNT(*) FROM public_booking_requests').fetchone()[0] == 0


def test_confirmation_database_failure_reconciles_without_second_calendar_write(site):
    import sqlite3
    site.obj.host_email = sender = BookingEmail()
    site.publish(); token = site.start()
    site.db.conn.execute('''CREATE TRIGGER fail_confirmation BEFORE UPDATE ON public_booking_requests
        WHEN NEW.status='confirmed' BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END''')
    with pytest.raises(sqlite3.IntegrityError):
        site.book(token)
    assert len(site.cal.writes) == 1 and sender.calls == []
    site.db.conn.execute('DROP TRIGGER fail_confirmation'); site.db.conn.commit()
    assert site.book(token).json()['host_notification'] == 'accepted'
    assert len(site.cal.writes) == 1 and len(sender.calls) == 1


def test_database_failure_after_mail_acceptance_keeps_uncertain_attempt_for_safe_retry(site):
    import sqlite3
    site.obj.host_email = sender = BookingEmail()
    site.publish(); token = site.start()
    site.db.conn.execute('''CREATE TRIGGER fail_acceptance BEFORE UPDATE ON host_booking_notifications
        WHEN NEW.status='accepted' BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END''')
    with pytest.raises(sqlite3.IntegrityError):
        site.book(token)
    receipt = site.db.conn.execute('SELECT * FROM host_booking_notifications').fetchone()
    assert receipt['status'] == 'sending' and receipt['attempts'] == 1
    site.db.conn.execute('DROP TRIGGER fail_acceptance'); site.db.conn.commit()
    site.clock[0] += timedelta(minutes=1)
    assert site.book(token).json()['host_notification'] == 'accepted'
    assert sender.calls[0] == sender.calls[1] and len(site.cal.writes) == 1


def test_unconfigured_receipt_can_send_after_provider_is_configured(site):
    site.publish(); token = site.start()
    assert site.book(token).json()['host_notification'] == 'unconfigured'
    site.obj.host_email = sender = BookingEmail()
    site.obj.retry_notifications()
    assert len(sender.calls) == 1
    assert site.book(token).json()['host_notification'] == 'accepted'


def test_email_when_uses_host_timezone_even_for_guest_booking_in_utc(site):
    site.obj.host_email = sender = BookingEmail()
    site.publish(); site.book(site.start(), start='2026-10-01T02:00:00+00:00', timezone='UTC')
    assert sender.calls[0][0]['dataVariables']['when'].endswith('10:00')
    assert sender.calls[0][0]['dataVariables']['timezone'] == 'Asia/Singapore'


def test_notification_retry_compares_timestamp_offsets_as_instants(site):
    from zoneinfo import ZoneInfo
    site.clock[0] = NOW.astimezone(ZoneInfo('Asia/Singapore'))
    site.obj.host_email = sender = BookingEmail('unknown')
    site.publish(); site.book(site.start())
    site.clock[0] = NOW + timedelta(minutes=1)
    sender.status = 'accepted'
    site.obj.retry_notifications()
    assert len(sender.calls) == 2


def test_owner_changed_during_calendar_write_is_suppressed_before_email(site):
    site.obj.host_email = sender = BookingEmail()
    site.publish()
    site.cal.before_write = lambda: site.db._update(site.user.id, NOW, email='changed@example.com')
    response = site.book(site.start())
    assert response.json()['status'] == 'confirmed'
    assert response.json()['host_notification'] == 'suppressed'
    assert sender.calls == []


def test_profile_change_invalidates_unused_form(site):
    site.publish();token=site.start();site.publish(title='Changed meeting')
    assert site.book(token).status_code==409 and not site.cal.writes


def test_read_rate_limit_prevents_provider_flood(site):
    site.publish()
    responses=[site.client.get('/booking/son/slots?date=2026-10-01&tz=UTC').status_code for _ in range(80)]
    assert 429 in responses and len(site.cal.reads)<80


def test_calendar_adapter_accepts_deterministic_event_id():
    from tests.fakes import FakeCalendarService
    svc=FakeCalendarService()
    cal=GoogleCalendar(svc)
    cal.create_event('Meeting',NOW,NOW+timedelta(minutes=30),event_id='abc123')
    _,kwargs=svc.calls[0]
    assert kwargs['body']['id']=='abc123'


def test_deleting_user_removes_profiles_and_requests(site):
    site.publish(); site.book(site.start())
    assert site.db.conn.execute('SELECT COUNT(*) FROM public_booking_requests').fetchone()[0]==1
    site.db.delete(site.user.id)
    assert site.db.conn.execute('SELECT COUNT(*) FROM public_booking_profiles').fetchone()[0]==0
    assert site.db.conn.execute('SELECT COUNT(*) FROM public_booking_requests').fetchone()[0]==0
    assert site.db.conn.execute('SELECT COUNT(*) FROM host_booking_notifications').fetchone()[0]==0
    assert site.client.get('/son/30min').status_code==404


def test_host_disconnect_between_final_read_and_insert_blocks_provider_write(site):
    site.publish(); token=site.start()
    def disconnect(start,end):
        site.db._update(site.user.id,NOW,google_disconnected=1)
        return []
    site.cal.busy_periods=disconnect
    assert site.book(token).status_code==202
    assert not site.cal.writes


@pytest.mark.parametrize('busy',[None,[(NOW,NOW)],[(NOW.replace(tzinfo=None),NOW+timedelta(minutes=30))],['bad']])
def test_malformed_busy_data_fails_closed(site,busy):
    site.publish();site.cal.busy=busy
    assert site.book(site.start()).status_code==503
    assert not site.cal.writes


def test_malformed_write_receipt_is_pending_not_success(site):
    site.publish()
    site.cal.create_event=lambda *args,**kwargs: {'id':'incomplete'}
    response=site.book(site.start())
    assert response.status_code==202


def test_unresolved_reservation_blocks_overlapping_new_form(site):
    site.publish();site.cal.fail_write=True
    assert site.book(site.start()).status_code==202
    site.cal.fail_write=False
    assert site.book(site.start(),email='other@example.com').status_code==409
    assert len(site.cal.writes)==1


def test_host_day_availability_across_dst_gap_and_fold(site):
    site.publish(timezone='America/New_York',weekdays='6',work_start='00:00',work_end='04:00')
    site.clock[0]=datetime(2026,10,31,0,tzinfo=timezone.utc)
    response=site.client.get('/booking/son/slots?date=2026-11-01&tz=America/New_York')
    slots=response.json()['slots']
    assert len(slots)==10
    assert len({s['start'] for s in slots})==10
    site.clock[0]=datetime(2027,3,13,0,tzinfo=timezone.utc)
    response=site.client.get('/booking/son/slots?date=2027-03-14&tz=America/New_York')
    slots=response.json()['slots']
    assert len(slots)==6 and not any('T02:' in s['start'] for s in slots)


def test_pending_result_can_be_reconciled_after_form_expiry(site):
    site.publish();token=site.start();site.cal.write_then_fail=True
    assert site.book(token).status_code==202
    site.clock[0]+=timedelta(hours=2)
    assert site.book(token).json()['status']=='confirmed'
    assert len(site.cal.writes)==1


def test_per_host_read_limit_across_different_ips(site):
    site.publish()
    site.obj.host_read_limiter.limit=2
    responses=[site.client.get('/booking/son/slots?date=2026-10-01&tz=UTC',headers={'x-forwarded-for':f'10.0.0.{i}'}).status_code for i in range(3)]
    assert responses==[200,200,429]


def test_extreme_datetime_is_validation_error_before_provider(site):
    site.publish()
    assert site.book(site.start(),start='9999-12-31T10:00:00+08:00').status_code==400
    assert not site.cal.reads and not site.cal.writes


def test_form_pages_keep_same_origin_metadata_and_still_reject_opaque_origin(site):
    # Native browser POSTs can send Origin:null under no-referrer. Keeping
    # same-origin metadata avoids that without accepting opaque origins.
    site.publish()
    for path in ('/connections/scheduling','/son/30min'):
        response=site.client.get(path)
        assert response.headers['referrer-policy']=='same-origin'
    assert site.client.post('/connections/scheduling',data=site.settings,headers={'Origin':'null','Sec-Fetch-Site':'same-origin'}).status_code==403
    assert site.client.post('/connections/scheduling',data=site.settings,headers={'Origin':'https://testserver','Sec-Fetch-Site':'same-origin'}).status_code==303
    assert site.client.post('/connections/scheduling',data=site.settings,headers={'Origin':'https://testserver','Sec-Fetch-Site':'cross-site'}).status_code==403


def test_public_page_exposes_absolute_bounds_for_visitor_timezone(site):
    site.publish()
    page=site.client.get('/son/30min').json()
    assert datetime.fromisoformat(page['now'])==NOW
    assert datetime.fromisoformat(page['horizon_end'])==NOW+timedelta(days=30)
    # The visitor is still on September 30 in Los Angeles, with valid slots.
    response=site.client.get('/booking/son/slots?date=2026-09-30&tz=America/Los_Angeles')
    assert response.status_code==200 and response.json()['slots']


@pytest.mark.parametrize('start', ['0001-01-01T00:00:00+01:00','9999-12-31T23:59:59-01:00'])
def test_overflowing_utc_conversion_rejected_before_provider(site,start):
    site.publish()
    assert site.book(site.start(),start=start).status_code==400
    assert not site.cal.reads and not site.cal.writes


def test_settings_waiting_for_booking_lock_does_not_freeze_event_loop(site):
    import asyncio
    import threading
    import httpx
    from app.booking import BOOKING_LOCK
    held, release, released = threading.Event(), threading.Event(), threading.Event()
    def hold_booking_lock():
        with BOOKING_LOCK:
            held.set()
            release.wait(0.6)
        released.set()
    thread=threading.Thread(target=hold_booking_lock)
    thread.start(); assert held.wait(1)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=site.app),base_url='https://testserver',cookies={'pippa_session':'host-session'}) as client:
            pending=asyncio.create_task(client.post('/connections/scheduling',data=site.settings))
            try:
                await asyncio.sleep(0.03)
                assert not released.is_set(), 'Settings blocked the event loop while waiting for the booking lock'
            finally:
                release.set()
                response=await pending
            assert response.status_code==303
    try:
        asyncio.run(scenario())
    finally:
        release.set();thread.join(1)
