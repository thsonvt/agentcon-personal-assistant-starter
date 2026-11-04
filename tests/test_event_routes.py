"""Attendee routes use local storage and session cookies; never provider APIs."""
import hashlib
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.store import Store
from app.users import Users
from app.connections import Connections
from app.public_booking import PublicBooking

NOW = datetime(2026, 10, 3, 2, 0, tzinfo=timezone.utc)


@pytest.fixture
def site():
    from app.event_networking import EventNetworking
    users = Users(Store(':memory:'))
    users.ensure_admin('Organizer', '6599990000', NOW)
    connections = Connections(None, users, None, SimpleNamespace(), '', lambda: NOW)
    booking = PublicBooking(connections, users, lambda uid: pytest.fail('No Calendar access'), lambda: NOW,
                            'https://testserver', 's' * 32)
    events = EventNetworking(connections, users, booking, lambda: NOW, 'https://testserver')
    events.store.create_event('builders', 'Builders Night', NOW + timedelta(days=1))
    alice, _ = users.sign_in_identity('alice-sub', 'Alice', 'alice@example.com', NOW)
    bob, _ = users.sign_in_identity('bob-sub', 'Bob', 'bob@example.com', NOW)
    events.store.import_roster('builders', [dict(source_id='a', email=alice.email, name='Alice'),
                                          dict(source_id='b', email=bob.email, name='Bob')], actor_id=1)
    for source in ('a', 'b'):
        events.store.check_in('builders', source, 'receipt-' + source, actor_id=1)
    app = FastAPI()
    app.include_router(events.router)
    app.include_router(booking.router)
    client = TestClient(app, base_url='https://testserver', follow_redirects=False)
    def login(user):
        token = 'session-' + str(user.id)
        users.conn.execute('INSERT OR REPLACE INTO connection_sessions VALUES (?,?,?,?)',
            (hashlib.sha256(token.encode()).hexdigest(), user.id, 'csrf', (NOW + timedelta(days=1)).isoformat()))
        users.conn.commit()
        client.cookies.set('pippa_session', token)
    def claim(user):
        login(user)
        return client.post('/events/builders/claim', data={'csrf_token': 'csrf'})
    def publish(user, **extra):
        login(user)
        return client.post('/events/builders/profile', data={
            'csrf_token': 'csrf', 'username': user.name.lower(), 'display_name': user.name,
            'building': 'Tools for people', 'intents': 'coffee', 'visibility': 'event',
            'directory_opt_in': 'on', **extra})
    return SimpleNamespace(users=users, connections=connections, booking=booking, events=events,
                           alice=alice, bob=bob, client=client, app=app, login=login, claim=claim, publish=publish)


def test_event_landing_and_claim_are_identity_only_and_private_by_default(site):
    response = site.client.get('/events/builders')
    assert response.status_code == 200
    assert '/signin/google?event=builders' in response.text
    assert 'Calendar permission' in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-robots-tag'] == 'noindex, nofollow'
    assert site.claim(site.alice).status_code == 303
    response = site.client.get('/events/builders/profile')
    assert response.status_code == 200 and 'Save profile' in response.text
    site.client.cookies.clear()
    assert site.client.get(f'/people/{site.alice.id}').status_code == 404
    assert site.client.get('/events/builders/directory').status_code == 303


def test_unchecked_in_attendee_cannot_claim_and_missing_event_is_404(site):
    site.events.store.import_roster('builders', [dict(source_id='c', email='c@example.com', name='C')], actor_id=1)
    outsider, _ = site.users.sign_in_identity('c-sub', 'C', 'c@example.com', NOW)
    site.login(outsider)
    assert site.client.post('/events/builders/claim', data={'csrf_token':'csrf'}).status_code == 400
    assert site.client.get('/events/missing').status_code == 404


def test_forms_enforce_csrf_origin_and_bounded_bodies(site):
    site.claim(site.alice)
    for data, headers, status in [({'csrf_token':'bad'}, {}, 403),
            ({'csrf_token':'csrf'}, {'Origin':'https://evil.example'}, 403),
            ({'csrf_token':'csrf','building':'x'*20000}, {}, 413)]:
        assert site.client.post('/events/builders/profile', data=data, headers=headers).status_code == status
    assert site.client.post('/events/builders/profile', content=b'x', headers={'Content-Type':'application/json'}).status_code == 415


def test_directory_visibility_search_escape_and_qr(site):
    site.claim(site.alice); site.claim(site.bob)
    assert site.publish(site.alice, display_name='<script>Alice</script>').status_code == 303
    site.login(site.bob)
    response = site.client.get('/events/builders/directory?q=Tools')
    assert response.status_code == 200 and '&lt;script&gt;Alice&lt;/script&gt;' in response.text
    assert '<script>Alice</script>' not in response.text
    assert 'alice@example.com' not in response.text
    assert 'session-' not in response.text
    assert site.client.get(f'/people/{site.alice.id}').status_code == 200
    assert site.client.get(f'/people/{site.alice.id}/qr.svg').status_code == 200
    site.client.cookies.clear()
    assert site.client.get(f'/people/{site.alice.id}').status_code == 404
    assert site.client.get(f'/people/{site.alice.id}/qr.svg').status_code == 404
    assert site.publish(site.alice, visibility='public', directory_opt_in='').status_code == 303
    site.client.cookies.clear()
    assert site.client.get('/alice').status_code == 200
    assert site.client.get(f'/people/{site.alice.id}').status_code == 200
    site.login(site.bob)
    assert '<script>Alice</script>' not in site.client.get('/events/builders/directory').text


def test_followup_accept_propose_confirm_and_no_calendar_claim(site):
    site.claim(site.alice); site.claim(site.bob)
    site.publish(site.alice); site.publish(site.bob)
    site.login(site.alice)
    response = site.client.post(f'/people/{site.bob.id}/requests', data={
        'csrf_token':'csrf','event_slug':'builders','purpose':'technical','note':'Compare agent evals',
        'idempotency_key':'request-one'})
    assert response.status_code == 303
    detail = response.headers['location']
    assert detail.startswith('/network/inbox/')
    site.login(site.bob)
    page = site.client.get(detail)
    assert 'Compare agent evals' in page.text and 'Accept request' in page.text
    version = re.search(r'name="version" value="(\d+)"', page.text)[1]
    assert site.client.post(detail+'/accept', data={'csrf_token':'csrf','version':version}).status_code == 303
    page = site.client.get(detail)
    version = re.search(r'name="version" value="(\d+)"', page.text)[1]
    assert site.client.post(detail+'/propose', data={'csrf_token':'csrf','version':version,
        'timezone':'Asia/Singapore','location':'Cafe','slot_1':'2026-10-05T10:00','duration':'30'}).status_code == 303
    page = site.client.get(detail)
    assert 'Waiting for the other person' in page.text
    site.login(site.alice)
    page = site.client.get(detail)
    version = re.search(r'name="version" value="(\d+)"', page.text)[1]
    assert site.client.post(detail+'/confirm', data={'csrf_token':'csrf','version':version,'slot_index':'0'}).status_code == 303
    page = site.client.get(detail)
    assert 'Time agreed' in page.text and 'No calendar event has been created' in page.text


def test_request_detail_and_mutation_require_participant(site):
    site.claim(site.alice); site.claim(site.bob); site.publish(site.bob)
    site.login(site.alice)
    response = site.client.post(f'/people/{site.bob.id}/requests', data={
        'csrf_token':'csrf','event_slug':'builders','purpose':'coffee','note':'Hello', 'idempotency_key':'private'})
    detail = response.headers['location']
    outsider, _ = site.users.sign_in_identity('out', 'Outsider', 'out@example.com', NOW)
    site.login(outsider)
    assert site.client.get(detail).status_code == 404
    assert site.client.post(detail+'/accept', data={'csrf_token':'csrf','version':'1'}).status_code in (400,404)


@pytest.mark.parametrize('name', ['events','people','network','inbox'])
def test_event_names_reserved_for_booking(name):
    from app.public_booking import RESERVED
    assert name in RESERVED


def test_removed_member_cannot_open_profile_editor(site):
    site.claim(site.alice); site.publish(site.alice)
    site.events.store.remove_member('builders', 'a', actor_id=1)
    response = site.client.get('/events/builders/profile')
    assert response.status_code == 303 and response.headers['location'].endswith('/claim')


def test_unlisted_public_profile_is_not_in_directory(site):
    site.claim(site.alice); site.claim(site.bob)
    site.publish(site.alice, visibility='public', directory_opt_in='')
    site.login(site.bob)
    response = site.client.get('/events/builders/directory')
    assert f'href="/people/{site.alice.id}"' not in response.text
    response = site.client.get(f'/people/{site.alice.id}')
    assert response.status_code == 200
    assert 'Send a request' not in response.text


def test_block_hides_profiles_and_prevents_new_followup(site):
    site.claim(site.alice); site.claim(site.bob); site.publish(site.alice); site.publish(site.bob)
    site.login(site.alice)
    fields = dict(csrf_token='csrf', event_slug='builders', purpose='coffee', note='Hello', idempotency_key='blocked')
    detail = site.client.post(f'/people/{site.bob.id}/requests',data=fields).headers['location']
    site.login(site.bob)
    assert site.client.post(detail+'/block',data={'csrf_token':'csrf'}).status_code == 303
    site.login(site.alice)
    assert site.client.get(f'/people/{site.bob.id}').status_code == 404
    assert site.client.post(f'/people/{site.bob.id}/requests',data={**fields,'idempotency_key':'new'}).status_code == 400


def test_local_preview_provides_separate_browser_identity_selection():
    from scripts.preview_event_networking import create_preview
    app = create_preview()
    alice = TestClient(app, base_url='http://localhost:8833', follow_redirects=False)
    bob = TestClient(app, base_url='http://localhost:8833', follow_redirects=False)
    assert 'Local demo' in alice.get('/__preview').text
    assert alice.get('/__preview/as/alice').status_code == 303
    assert bob.get('/__preview/as/bob').status_code == 303
    assert alice.cookies.get('pippa_session') != bob.cookies.get('pippa_session')
    assert alice.get('/events/builders-night/directory').status_code == 200
    assert bob.get('/network/inbox').status_code == 200


def test_profile_validation_preserves_hidden_draft_and_never_exposes_tokens(site):
    site.claim(site.alice)
    for extra in [dict(username='people'), dict(visibility='everyone'), dict(intents='invented'),
                  dict(building='x'*281), dict(display_name=''), dict(username='x/../../private')]:
        response = site.publish(site.alice, **extra)
        assert response.status_code == 400
        assert 'Traceback' not in response.text
        assert 'alice@example.com' not in response.text
        assert 'session-' not in response.text
    site.client.cookies.clear()
    assert site.client.get(f'/people/{site.alice.id}').status_code == 404


def test_duplicate_fields_and_rate_limit_have_safe_errors(site):
    site.claim(site.alice)
    response = site.client.post('/events/builders/profile',content='csrf_token=csrf&csrf_token=csrf',
                               headers={'Content-Type':'application/x-www-form-urlencoded'})
    assert response.status_code == 403
    response = site.client.post('/events/builders/profile',content='csrf_token=csrf&username=alice&username=other',
                               headers={'Content-Type':'application/x-www-form-urlencoded'})
    assert response.status_code == 400
    site.events.limiter.limit = 1
    assert site.client.post('/events/builders/profile',data={'csrf_token':'csrf'}).status_code == 429


def test_request_validation_and_idempotent_resubmit(site):
    site.claim(site.alice); site.claim(site.bob); site.publish(site.bob)
    site.login(site.alice)
    fields = dict(csrf_token='csrf',event_slug='builders',purpose='coffee',note='Hello',idempotency_key='same')
    path = f'/people/{site.bob.id}/requests'
    for extra in [dict(purpose='invalid'), dict(note='x'*501), dict(event_slug='missing'), dict(idempotency_key='')]:
        response = site.client.post(path,data={**fields,**extra})
        assert response.status_code == 400
        assert 'Traceback' not in response.text
    first = site.client.post(path,data=fields)
    again = site.client.post(path,data=fields)
    assert first.status_code == again.status_code == 303
    assert first.headers['location'] == again.headers['location']
    assert site.client.post(path,data={**fields,'note':'Changed'}).status_code == 400


def test_proposal_rejects_past_missing_and_dst_invalid_times(site):
    site.claim(site.alice); site.claim(site.bob); site.publish(site.bob)
    site.login(site.alice)
    detail = site.client.post(f'/people/{site.bob.id}/requests',data=dict(
        csrf_token='csrf',event_slug='builders',purpose='coffee',note='Hello',idempotency_key='times')).headers['location']
    site.login(site.bob)
    assert site.client.post(detail+'/accept', data={'csrf_token':'csrf','version':'0'}).status_code == 303
    fields = dict(csrf_token='csrf',version='1',timezone='Asia/Singapore',location='Cafe',duration='30',slot_1='2026-10-05T10:00')
    for extra in [dict(slot_1='2026-10-01T10:00'),dict(slot_1=''),dict(slot_1='2026-10-05T10:00+08:00'),
                  dict(timezone='bad'),dict(duration='900'),dict(slot_1='2027-03-14T02:30',timezone='America/New_York'),
                  dict(slot_1='2026-11-01T01:30',timezone='America/New_York')]:
        assert site.client.post(detail+'/propose',data={**fields,**extra}).status_code == 400
    assert site.client.post(detail+'/propose',data=fields).status_code == 303
    assert site.client.post(detail+'/confirm',data={'csrf_token':'csrf','version':'2','slot_index':'0'}).status_code == 400
    site.login(site.alice)
    assert site.client.post(detail+'/confirm',data={'csrf_token':'csrf','version':'1','slot_index':'0'}).status_code == 400
    assert site.client.post(detail+'/confirm',data={'csrf_token':'csrf','version':'2','slot_index':'-1'}).status_code == 400


def test_own_unpublished_profile_does_not_offer_broken_preview(site):
    site.claim(site.alice)
    response = site.client.get('/events/builders/profile')
    assert response.status_code == 200
    assert 'value="alice"' in response.text
    assert 'View my profile' not in response.text


def test_native_form_pages_preserve_same_origin_metadata(site):
    # Native browser form POSTs can send Origin:null under no-referrer.
    # Same-origin preserves native form metadata without external referrer disclosure.
    site.claim(site.alice); site.claim(site.bob); site.publish(site.bob)
    site.login(site.alice)
    for path in ('/events/builders','/events/builders/profile','/events/builders/directory',
                 f'/people/{site.bob.id}','/network/inbox'):
        assert site.client.get(path).headers['referrer-policy'] == 'same-origin'


def test_native_post_origin_validation_still_rejects_opaque_and_cross_site(site):
    site.claim(site.alice); site.claim(site.bob); site.publish(site.bob)
    site.login(site.alice)
    data = dict(csrf_token='csrf',event_slug='builders',purpose='coffee',note='Hello',idempotency_key='browser-origin')
    path = f'/people/{site.bob.id}/requests'
    assert site.client.post(path,data=data,headers={'Origin':'null','Sec-Fetch-Site':'same-origin'}).status_code == 403
    assert site.client.post(path,data=data,headers={'Origin':'https://testserver','Sec-Fetch-Site':'cross-site'}).status_code == 403
    assert site.client.post(path,data=data,headers={'Origin':'https://testserver','Sec-Fetch-Site':'same-origin'}).status_code == 303


def test_unicode_csrf_is_a_safe_rejection(site):
    site.claim(site.alice)
    assert site.client.post('/events/builders/profile',data={'csrf_token':'☃'}).status_code == 403


@pytest.mark.parametrize('profile_id', ['0','-1','9223372036854775808','9'*100])
def test_profile_id_is_bounded_before_sqlite(site, profile_id):
    assert site.client.get('/people/'+profile_id).status_code == 422
    assert site.client.get('/people/'+profile_id+'/qr.svg').status_code == 422
    assert site.client.post('/people/'+profile_id+'/requests',data={}).status_code == 422


def test_overflowing_end_time_is_a_safe_validation_error(site):
    site.claim(site.alice); site.claim(site.bob); site.publish(site.bob)
    site.login(site.alice)
    detail = site.client.post(f'/people/{site.bob.id}/requests',data=dict(
        csrf_token='csrf',event_slug='builders',purpose='coffee',note='Hello',idempotency_key='overflow')).headers['location']
    site.login(site.bob)
    site.client.post(detail+'/accept',data={'csrf_token':'csrf','version':'0'})
    response = site.client.post(detail+'/propose',data=dict(csrf_token='csrf',version='1',timezone='UTC',
        location='Cafe',duration='60',slot_1='9999-12-31T23:59'))
    assert response.status_code == 400


def test_event_login_entry_preserves_identity_only_path_and_private_profile_secrecy(site):
    site.claim(site.alice); site.publish(site.alice)
    site.client.cookies.clear()
    assert site.client.get('/network/inbox').headers['location'] == '/network/signin'
    assert site.client.get('/network/inbox/unknown').headers['location'] == '/network/signin'
    response = site.client.get('/network/signin')
    assert response.status_code == 200 and '/signin/google?event=builders' in response.text
    assert 'alice@example.com' not in response.text and 'Alice' not in response.text
    private = site.client.get(f'/people/{site.alice.id}')
    unknown = site.client.get('/people/987654321')
    assert private.status_code == unknown.status_code == 404
    assert private.text == unknown.text
    assert '/network/signin' in private.text
    assert site.client.post('/events/builders/profile',data={'csrf_token':'csrf'}).headers['location'] == '/events/builders'


def test_networking_privacy_and_deletion_require_fresh_login_and_exact_confirmation(site):
    site.claim(site.alice); site.publish(site.alice)
    response = site.client.get('/network/privacy')
    assert response.status_code == 200 and '14 days' in response.text and '30 days' in response.text
    assert 'DELETE' in response.text and 'Calendar' in response.text
    assert '/network/privacy' in site.client.get('/events/builders').text
    fields = dict(csrf_token='csrf', confirmation='DELETE')
    assert site.client.post('/network/privacy/delete',data=fields).status_code == 403
    from app.connections import SESSION_AGE
    site.users.conn.execute('UPDATE connection_sessions SET expires_at=?',( (NOW+SESSION_AGE).isoformat(),))
    site.users.conn.commit()
    assert site.client.post('/network/privacy/delete',data={**fields,'confirmation':'delete'}).status_code == 400
    assert site.client.post('/network/privacy/delete',data={**fields,'csrf_token':'bad'}).status_code == 403
    response = site.client.post('/network/privacy/delete',data=fields)
    assert response.status_code == 200 and 'Event data removed' in response.text
    assert site.users.get(site.alice.id) is not None
    assert site.client.get(f'/people/{site.alice.id}').status_code == 404


def test_accepted_request_shows_ready_booking_and_optional_upgrade_after_time_agreed(site):
    import json
    from app.users import CALENDAR_SCOPE
    site.claim(site.alice); site.claim(site.bob); site.publish(site.bob)
    site.users._update(site.bob.id,NOW,status='approved',google_token_enc='sealed',
                       google_scopes=json.dumps([CALENDAR_SCOPE]),calendar_status='connected')
    site.users.conn.execute('UPDATE public_booking_profiles SET enabled=1 WHERE user_id=?',(site.bob.id,))
    site.users.conn.commit()
    site.login(site.alice)
    detail = site.client.post(f'/people/{site.bob.id}/requests',data=dict(csrf_token='csrf',event_slug='builders',
        purpose='coffee',note='Hello',idempotency_key='booking-upgrade')).headers['location']
    assert '/bob/30min' not in site.client.get(detail).text
    site.login(site.bob)
    site.client.post(detail+'/accept',data={'csrf_token':'csrf','version':'0'})
    site.login(site.alice)
    assert 'href="https://testserver/bob/30min"' in site.client.get(detail).text
    site.client.post(detail+'/propose',data=dict(csrf_token='csrf',version='1',timezone='UTC',location='Cafe',
        duration='30',slot_1='2026-10-05T10:00'))
    site.login(site.bob)
    site.client.post(detail+'/confirm',data={'csrf_token':'csrf','version':'2','slot_index':'0'})
    response = site.client.get(detail)
    assert 'Time agreed' in response.text and '/connections/calendar' in response.text
    assert '/connections/whatsapp' in response.text and 'beta eligibility' in response.text


def test_failed_reserved_handle_booking_save_rolls_back_before_event_write(tmp_path):
    from app.event_networking import EventNetworking
    users = Users(Store(str(tmp_path/'events.db')))
    users.ensure_admin('Organizer','6599990000',NOW)
    connections = Connections(None,users,None,SimpleNamespace(),'',lambda:NOW)
    booking = PublicBooking(connections,users,lambda uid:pytest.fail('No provider'),lambda:NOW,'https://testserver','s'*32)
    events = EventNetworking(connections,users,booking,lambda:NOW,'https://testserver')
    events.store.conn.execute('PRAGMA busy_timeout=50')
    events.store.create_event('builders','Builders',NOW+timedelta(days=1))
    alice,_ = users.sign_in_identity('alice','Alice','alice@example.com',NOW)
    bob,_ = users.sign_in_identity('bob','Bob','bob@example.com',NOW)
    events.store.import_roster('builders',[dict(source_id='a',name='Alice',email=alice.email)],actor_id=1)
    events.store.check_in('builders','a','receipt',actor_id=1)
    events.store.claim('builders',alice)
    events.store.update_profile('builders',alice.id,'Alice',username='alice')
    events.store.update_profile('builders',alice.id,'Alice',username='alice-new')
    users.conn.execute('INSERT INTO connection_sessions VALUES(?,?,?,?)',
                       (hashlib.sha256(b'bob').hexdigest(),bob.id,'csrf',(NOW+timedelta(days=1)).isoformat()))
    users.conn.commit()
    app=FastAPI();app.include_router(booking.router)
    client=TestClient(app,base_url='https://testserver',follow_redirects=False)
    client.cookies.set('pippa_session','bob')
    response=client.post('/connections/scheduling',data=dict(csrf_token='csrf',username='alice',display_name='Bob',
        title='Meeting',timezone='Asia/Singapore',work_start='09:00',work_end='18:00',weekdays='0,1,2,3,4',location='Cafe'))
    assert response.status_code==409
    assert not users.conn.in_transaction
    assert events.store.create_event('next-event','Next Event',NOW+timedelta(days=2))['slug']=='next-event'


def test_event_only_networking_delete_removes_identity_and_clears_session(site):
    from app.connections import SESSION_AGE
    person,_ = site.users.sign_in_event_identity('new-only','New Person','new-person@example.com',NOW)
    site.events.store.import_roster('builders',[dict(source_id='new',name='New Person',email=person.email)],actor_id=1)
    site.events.store.check_in('builders','new','new-checkin',actor_id=1)
    site.claim(person)
    site.users.conn.execute('UPDATE connection_sessions SET expires_at=? WHERE user_id=?',((NOW+SESSION_AGE).isoformat(),person.id))
    site.users.conn.commit()
    response=site.client.post('/network/privacy/delete',data={'csrf_token':'csrf','confirmation':'DELETE'})
    assert response.status_code==200 and 'Event data removed' in response.text
    assert site.users.get(person.id) is None
    assert 'pippa_session=""' in response.headers['set-cookie']
    assert site.client.get('/network/inbox').headers['location']=='/network/signin'


def test_fresh_preview_signin_can_remove_synthetic_event_account():
    from scripts.preview_event_networking import create_preview
    client=TestClient(create_preview(),base_url='http://localhost:8833',follow_redirects=False)
    client.get('/__preview/as/alice')
    page=client.get('/network/privacy')
    csrf=re.search(r'name="csrf_token" value="([^"]+)"',page.text)[1]
    response=client.post('/network/privacy/delete',data={'csrf_token':csrf,'confirmation':'DELETE'},
                         headers={'Origin':'http://localhost:8833','Sec-Fetch-Site':'same-origin'})
    assert response.status_code==200 and 'Event data removed' in response.text
    assert 'event-only identity and sign-in sessions were also removed' in response.text
    assert client.get('/network/inbox').headers['location']=='/network/signin'
