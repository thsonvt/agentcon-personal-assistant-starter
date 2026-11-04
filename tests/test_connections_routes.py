from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs,urlparse
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.crypto import TokenBox,generate_key
from app.store import Store
from app.users import Users,CALENDAR_SCOPE
from app.tools.google_oauth import GoogleAccount,GoogleOAuthError,GMAIL_SCOPE
from app.signup import signup_routes
from tests.test_connections_users import NOW

class OAuth:
    def __init__(self):
        self.account=GoogleAccount('a@example.com','A','',granted_scopes=('openid','email'),subject='sub-a',access_token='access')
        self.calls=[]
        self.fail_revoke=False
    def connection_auth_url(self,state,verifier,service='identity',email=None):
        self.calls.append((service,email))
        return 'https://accounts.google.com/auth?state='+state
    def exchange_connection(self,code,verifier): return self.account
    def check_connection(self,access,service): return ('connected','Verified')
    def refresh_connection(self,token): return 'access'
    def revoke(self,token):
        if self.fail_revoke: raise GoogleOAuthError('offline')

@pytest.fixture
def site(monkeypatch):
    import app.connections as module
    monkeypatch.setattr(module,'render_connection_page',lambda view,data: str({'view':view,**data}))
    db=Users(Store(':memory:'))
    db.ensure_admin('Owner','6591234567',NOW)
    oauth=OAuth(); box=TokenBox(generate_key()); changed=[]
    clock=[NOW]
    commands=SimpleNamespace(announce_signup=lambda user: None)
    obj=module.Connections(oauth,db,box,commands,'15550001234',lambda:clock[0],on_user_changed=changed.append)
    app=FastAPI();app.include_router(obj.router)
    app.include_router(signup_routes(oauth,db,box,commands,'15550001234','s'*32,lambda:clock[0],connections_callback=obj.callback))
    client=TestClient(app,base_url='https://testserver',follow_redirects=False)
    def flow(path='/signin/google',**params):
        start=client.get(path,params=params)
        state=parse_qs(urlparse(start.headers['location']).query)['state'][0]
        return client.get('/signup/callback',params={'state':state,'code':'code'}),state
    def csrf():
        session=obj._session(client.cookies.get(module.SESSION_COOKIE))
        return session['csrf']
    return SimpleNamespace(db=db,oauth=oauth,box=box,client=client,flow=flow,csrf=csrf,obj=obj,clock=clock,changed=changed)

def test_identity_signin_then_returning_signin_preserves_connection(site):
    result,state=site.flow()
    assert result.status_code==303 and result.headers['location']=='/connections/calendar'
    user=site.db.by_email('a@example.com')
    assert user.google_token_enc is None
    site.db._update(user.id,NOW,google_token_enc=site.box.encrypt('refresh'),google_scopes='["'+CALENDAR_SCOPE+'"]')
    result,_=site.flow()
    assert result.headers['location']=='/connections'
    assert site.box.decrypt(site.db.get(user.id).google_token_enc)=='refresh'

def test_state_is_consumed_once_even_for_denial(site):
    start=site.client.get('/signin/google');state=parse_qs(urlparse(start.headers['location']).query)['state'][0]
    cookie=site.client.cookies.get('pippa_oauth')
    assert site.client.get('/signup/callback',params={'state':state,'error':'access_denied'}).status_code==200
    site.client.cookies.set('pippa_oauth',cookie)
    assert site.client.get('/signup/callback',params={'state':state,'code':'code'}).status_code==400
    assert site.db.by_email('a@example.com') is None

def test_oauth_is_bound_to_starting_browser(site):
    start=site.client.get('/signin/google');state=parse_qs(urlparse(start.headers['location']).query)['state'][0]
    site.client.cookies.clear()
    assert site.client.get('/signup/callback',params={'state':state,'code':'code'}).status_code==400

def test_grant_rejects_different_google_identity(site):
    site.flow(); user=site.db.by_email('a@example.com')
    site.oauth.account=GoogleAccount('b@example.com','B','other',granted_scopes=(CALENDAR_SCOPE,),subject='sub-b',access_token='a')
    result,_=site.flow('/connections/google',service='calendar')
    assert result.status_code==403 and site.db.get(user.id).google_token_enc is None

def test_grant_checks_access_then_next_step_and_missing_gmail_stays_unconnected(site):
    site.flow()
    site.oauth.account=GoogleAccount('a@example.com','A','refresh',granted_scopes=(CALENDAR_SCOPE,),subject='sub-a',access_token='access')
    result,_=site.flow('/connections/google',service='calendar')
    assert result.headers['location']=='/connections/gmail'
    user=site.db.by_email('a@example.com')
    assert user.calendar_status=='connected' and user.gmail_status=='not_connected'
    site.oauth.account=GoogleAccount('a@example.com','A','',granted_scopes=(CALENDAR_SCOPE,),subject='sub-a',access_token='access')
    result,_=site.flow('/connections/google',service='gmail')
    assert 'permission' in result.text.lower()
    assert site.db.get(user.id).gmail_status=='not_connected'
    assert site.box.decrypt(site.db.get(user.id).google_token_enc)=='refresh'

@pytest.mark.parametrize('path',['/connections/check','/connections/whatsapp','/connections/booking','/connections/disconnect','/connections/logout'])
def test_all_mutations_require_csrf(site,path):
    site.flow()
    assert site.client.post(path,data={'phone':'6581112222','booking_url':'https://calendly.com/a'}).status_code==403

def test_account_id_is_derived_from_session_and_booking_isolated(site):
    site.flow();a=site.db.by_email('a@example.com')
    b,_=site.db.sign_in_identity('sub-b','B','b@example.com',NOW)
    assert site.client.post('/connections/booking',data={'csrf_token':site.csrf(),'user_id':b.id,'booking_url':'https://calendly.com/a'}).status_code==303
    assert site.db.get(a.id).calendly_url=='https://calendly.com/a' and site.db.get(b.id).calendly_url is None

def test_disconnect_locally_disables_despite_provider_error(site):
    site.flow();user=site.db.by_email('a@example.com')
    site.db._update(user.id,NOW,google_token_enc=site.box.encrypt('refresh'),google_scopes='["'+CALENDAR_SCOPE+'"]',calendly_url='https://calendly.com/a')
    site.oauth.fail_revoke=True
    assert site.client.post('/connections/disconnect',data={'csrf_token':site.csrf()}).status_code==303
    user=site.db.get(user.id)
    assert not user.google_token_enc and user.google_disconnected and user.google_revocation_status=='failed'
    assert user.calendly_url=='https://calendly.com/a' and user.id in site.changed

def test_disabled_user_and_expired_session_are_blocked(site):
    site.flow();user=site.db.by_email('a@example.com')
    site.clock[0]+=timedelta(days=31)
    assert site.client.get('/connections').status_code==303
    site.clock[0]=NOW; site.flow();site.db._update(user.id,NOW,status='disabled')
    assert site.client.get('/connections').status_code==303

def test_logout_invalidates_replayed_session_cookie(site):
    site.flow();cookie=site.client.cookies.get('pippa_session')
    assert site.client.post('/connections/logout',data={'csrf_token':site.csrf()}).status_code==303
    site.client.cookies.set('pippa_session',cookie)
    assert site.client.get('/connections').status_code==303

def test_cookie_security_and_no_store(site):
    result,_=site.flow()
    assert 'HttpOnly' in result.headers['set-cookie'] and 'Secure' in result.headers['set-cookie']
    assert result.headers['cache-control']=='no-store'
    assert site.client.get('/connections').headers['cache-control']=='no-store'

def test_oauth_attempt_expires_and_never_creates_user(site):
    start=site.client.get('/signin/google');state=parse_qs(urlparse(start.headers['location']).query)['state'][0]
    site.clock[0]+=timedelta(minutes=11)
    assert site.client.get('/signup/callback',params={'state':state,'code':'code'}).status_code==400
    assert site.db.by_email('a@example.com') is None

def test_calendar_grant_with_failed_probe_is_not_connected(site):
    site.flow()
    site.oauth.account=GoogleAccount('a@example.com','A','refresh',granted_scopes=(CALENDAR_SCOPE,),subject='sub-a',access_token='access')
    site.oauth.check_connection=lambda access,service: ('needs_attention','API temporarily unavailable')
    result,_=site.flow('/connections/google',service='calendar')
    assert result.headers['location']=='/connections/calendar'
    user=site.db.by_email('a@example.com')
    assert user.calendar_status=='needs_attention'
    assert site.client.post('/connections/whatsapp',data={'csrf_token':site.csrf(),'phone':'6581112222'}).status_code==400

def test_check_revoked_token_preserves_identity_and_reports_attention(site):
    site.flow();user=site.db.by_email('a@example.com')
    site.db._update(user.id,NOW,google_token_enc=site.box.encrypt('refresh'),google_scopes='["'+CALENDAR_SCOPE+'"]')
    def revoked(token): raise GoogleOAuthError('Google authorization was revoked. Reconnect Google.')
    site.oauth.refresh_connection=revoked
    site.client.post('/connections/check',data={'csrf_token':site.csrf()})
    user=site.db.get(user.id)
    assert user.calendar_status=='needs_attention' and 'revoked' in user.calendar_detail
    assert user.google_subject=='sub-a' and user.google_checked_at==NOW.isoformat()

def test_reconnect_cannot_drop_existing_gmail_grant(site):
    site.flow();user=site.db.by_email('a@example.com')
    import json
    site.db._update(user.id,NOW,google_token_enc=site.box.encrypt('old'),google_scopes=json.dumps([CALENDAR_SCOPE,GMAIL_SCOPE]))
    site.oauth.account=GoogleAccount('a@example.com','A','new',granted_scopes=(CALENDAR_SCOPE,),subject='sub-a',access_token='access')
    result,_=site.flow('/connections/google',service='calendar')
    assert 'fewer permissions' in result.text
    assert site.box.decrypt(site.db.get(user.id).google_token_enc)=='old'

def test_grant_started_before_disconnect_cannot_restore_access(site):
    site.flow();user=site.db.by_email('a@example.com')
    site.oauth.account=GoogleAccount('a@example.com','A','new',granted_scopes=(CALENDAR_SCOPE,),subject='sub-a',access_token='access')
    start=site.client.get('/connections/google',params={'service':'calendar'})
    state=parse_qs(urlparse(start.headers['location']).query)['state'][0]
    site.client.post('/connections/disconnect',data={'csrf_token':site.csrf()})
    response=site.client.get('/signup/callback',params={'state':state,'code':'code'})
    assert response.status_code==400
    assert site.db.get(user.id).google_disconnected and not site.db.get(user.id).google_token_enc

def test_identity_only_signin_respects_allowlist_and_capacity(site):
    site.db.auto_activate=True;site.db.allowed_emails=frozenset({'allowed@example.com'})
    result,_=site.flow()
    assert result.status_code==403 and site.db.by_email('a@example.com') is None
    site.db.auto_activate=False;site.db.max_users=1
    result,_=site.flow()
    assert result.status_code==403 and site.db.by_email('a@example.com') is None

def test_autoactivation_requires_checked_calendar_and_exact_whatsapp_join(site):
    site.db.auto_activate=True;site.db.allowed_emails=frozenset({'a@example.com'})
    site.flow();user=site.db.by_email('a@example.com')
    assert user.status=='approved'
    site.oauth.account=GoogleAccount('a@example.com','A','refresh',granted_scopes=(CALENDAR_SCOPE,),subject='sub-a',access_token='access')
    site.flow('/connections/google',service='calendar')
    assert site.db.get(user.id).status=='approved'
    site.client.post('/connections/whatsapp',data={'csrf_token':site.csrf(),'phone':'6581112222'})
    user=site.db.get(user.id)
    from app.users import SignupError
    with pytest.raises(SignupError): site.db.join(user.join_code,'6583334444',NOW)
    assert site.db.join(user.join_code,'6581112222',NOW).status=='active'

def test_account_deletion_removes_connection_sessions_and_pending_grants(site):
    site.flow();user=site.db.by_email('a@example.com')
    site.client.get('/connections/google',params={'service':'gmail'})
    site.db.delete(user.id)
    assert site.db.conn.execute('SELECT count(*) FROM connection_sessions WHERE user_id=?',(user.id,)).fetchone()[0]==0
    assert site.db.conn.execute('SELECT count(*) FROM connection_oauth WHERE user_id=?',(user.id,)).fetchone()[0]==0

@pytest.mark.parametrize('path,target',[('/signup/google','/connections/calendar'),('/signup/google?gmail=true','/connections/gmail'),('/signup/gmail','/connections/gmail')])
def test_runtime_legacy_google_entrypoints_use_connections(site,path,target):
    response=site.client.get(path)
    assert response.status_code==303 and response.headers['location']==target

def test_outstanding_legacy_callback_cannot_replace_bound_connection(site):
    from app.crypto import sign
    import json
    site.flow();user=site.db.by_email('a@example.com')
    site.db._update(user.id,NOW,google_token_enc=site.box.encrypt('old'))
    site.oauth.exchange=lambda code: GoogleAccount('a@example.com','A','replacement',granted_scopes=(CALENDAR_SCOPE,))
    site.client.cookies.set('oauth_state',sign(json.dumps({'state':'legacy','gmail':False,'email':None}),'s'*32,NOW))
    response=site.client.get('/signup/callback',params={'state':'legacy','code':'old'})
    assert response.status_code==403
    assert site.box.decrypt(site.db.get(user.id).google_token_enc)=='old'

def test_disconnect_blocks_reconnect_until_provider_revocation_finishes(site):
    site.flow();user=site.db.by_email('a@example.com')
    site.db._update(user.id,NOW,google_token_enc=site.box.encrypt('old'))
    attempted=[]
    def revoke(token):
        attempted.append(site.client.get('/connections/google',params={'service':'calendar'}))
        # A second disconnect must not acquire a second revocation operation.
        if len(attempted)==1:
            attempted.append(site.client.post('/connections/disconnect',data={'csrf_token':site.csrf()}))
    site.oauth.revoke=revoke
    assert site.client.post('/connections/disconnect',data={'csrf_token':site.csrf()}).status_code==303
    assert attempted[0].status_code==409
    assert len(attempted)==2
    assert site.db.get(user.id).google_revocation_status=='succeeded'


@pytest.mark.parametrize('view', ['/connections', '/connections/whatsapp'])
def test_legacy_mismatch_renders_approved_setup_without_false_link_or_wait(site, monkeypatch, view):
    from app.connection_pages import render_connection_page
    monkeypatch.setattr('app.connections.render_connection_page', render_connection_page)
    site.db.auto_activate=True; site.db.allowed_emails=frozenset({'a@example.com'})
    site.flow(); user=site.db.by_email('a@example.com')
    site.db._update(user.id,NOW,status='approved',joined=1,phone='6581112222',typed_phone='81112222',google_token_enc='legacy',google_scopes=None)
    response=site.client.get(view)
    assert 'Beta access approved' in response.text
    assert 'Waiting for beta approval' not in response.text and 'Your number is linked' not in response.text
    assert 'href="/connections/calendar"' in response.text
    assert 'country code' in response.text


def test_authenticated_legacy_recovery_preserves_link_until_calendar_is_checked(site):
    site.flow(); user=site.db.by_email('a@example.com')
    original=site.db._update(user.id,NOW,status='approved',joined=1,phone='6581112222',typed_phone='81112222',google_token_enc='legacy',google_scopes=None)
    payload={'csrf_token':site.csrf(),'phone':'+65 8111 2222'}
    assert site.client.post('/connections/whatsapp',data=payload).status_code==400
    assert site.db.get(user.id)==original
    site.db._update(user.id,NOW,google_scopes='["'+CALENDAR_SCOPE+'"]',calendar_status='connected')
    assert site.client.post('/connections/whatsapp',data=payload).status_code==303
    recovered=site.db.get(user.id)
    assert not recovered.joined and recovered.phone is None and recovered.join_code
    assert recovered.typed_phone=='6581112222'
