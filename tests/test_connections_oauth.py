from urllib.parse import parse_qs,urlparse
import httpx
import pytest
from app.tools.google_oauth import GoogleOAuth,GoogleOAuthError,CALENDAR_SCOPE,GMAIL_SCOPE

def oauth(status=200,scopes='openid email',subject='sub',verified=True):
    seen=[]
    def handler(request):
        seen.append(request)
        if request.url.path == '/token':
            return httpx.Response(200,json={'access_token':'access','refresh_token':'refresh','scope':scopes})
        if request.url.path == '/v1/userinfo':
            return httpx.Response(200,json={'sub':subject,'email':'a@example.com','email_verified':verified})
        return httpx.Response(status,json={'error':{'status':'PERMISSION_DENIED','errors':[{'reason':'accessNotConfigured'}]}})
    return GoogleOAuth('id','secret','https://pippa.test/signup/callback',httpx.Client(transport=httpx.MockTransport(handler))),seen

def test_identity_url_requests_no_integration_scope_and_uses_pkce():
    g,_=oauth()
    q=parse_qs(urlparse(g.connection_auth_url('state','verifier')).query)
    assert set(q['scope'][0].split()) == {'openid','email','profile'}
    assert q['code_challenge_method']==['S256'] and 'prompt' not in q

def test_incremental_consent_requests_only_chosen_service():
    g,_=oauth()
    q=parse_qs(urlparse(g.connection_auth_url('state','verifier','gmail','a@example.com')).query)
    assert GMAIL_SCOPE in q['scope'][0] and CALENDAR_SCOPE not in q['scope'][0]
    assert q['include_granted_scopes']==['true'] and q['login_hint']==['a@example.com']

def test_identity_exchange_keeps_verified_subject_and_pkce():
    g,seen=oauth()
    account=g.exchange_connection('code','verifier')
    assert account.subject=='sub' and account.email=='a@example.com'
    assert parse_qs(seen[0].content.decode())['code_verifier']==['verifier']

@pytest.mark.parametrize('subject,verified',[('',True),('sub',False),('sub','false')])
def test_unverified_or_missing_subject_is_rejected(subject,verified):
    g,_=oauth(subject=subject,verified=verified)
    with pytest.raises(GoogleOAuthError): g.exchange_connection('code','verifier')

@pytest.mark.parametrize('status,expected',[(200,'connected'),(401,'needs_attention'),(403,'needs_attention'),(503,'needs_attention')])
def test_read_only_capability_check_classifies_failures(status,expected):
    g,seen=oauth(status=status)
    state,detail=g.check_connection('access','calendar')
    assert state==expected
    assert seen[-1].url.path=='/calendar/v3/users/me/calendarList' and seen[-1].url.params['maxResults']=='1'
    if status==403: assert 'enabled' in detail

def test_gmail_check_only_reads_profile():
    g,seen=oauth()
    assert g.check_connection('access','gmail')[0]=='connected'
    assert seen[-1].url.path=='/gmail/v1/users/me/profile'
