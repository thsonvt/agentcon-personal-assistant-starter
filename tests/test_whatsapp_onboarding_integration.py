"""Browser status and number input integrate with durable join/delivery receipts."""
from app.onboarding import OnboardingReceipts
from app.tools.google_oauth import GoogleAccount, CALENDAR_SCOPE
from tests.test_connections_routes import site, NOW


def ready_calendar(site):
    site.flow()
    site.oauth.account = GoogleAccount('a@example.com', 'A', 'refresh', granted_scopes=(CALENDAR_SCOPE,),
                                      subject='sub-a', access_token='access')
    site.flow('/connections/google', service='calendar')
    return site.db.by_email('a@example.com')


def test_status_requires_authentication_and_never_exposes_secrets(site):
    assert site.client.get('/connections/whatsapp/status').status_code == 401
    ready_calendar(site)
    result = site.client.get('/connections/whatsapp/status')
    assert result.status_code == 200
    assert 'no-store' in result.headers['cache-control']
    assert result.json()['revision']
    for secret in ('refresh', 'sub-a', site.csrf()):
        assert secret not in result.text


def test_explicit_country_code_is_forwarded_and_unprefixed_local_number_is_rejected(site):
    user = ready_calendar(site)
    bad = site.client.post('/connections/whatsapp', data={'csrf_token': site.csrf(), 'phone': '90000002'})
    assert bad.status_code == 400
    assert not site.db.get(user.id).join_code
    result = site.client.post('/connections/whatsapp', data={'csrf_token': site.csrf(), 'country_code': '+65', 'phone': '90000002'})
    assert result.status_code == 303
    assert site.db.get(user.id).typed_phone == '6590000002'


def test_browser_status_exposes_received_mismatch_and_meta_reply_failure(site):
    user = ready_calendar(site)
    user = site.db.set_details(user.id, '+6590000002', None, NOW)
    receipts = OnboardingReceipts(site.db.conn, now=lambda: NOW)
    site.obj.receipts = receipts
    before = site.client.get('/connections/whatsapp/status').json()['revision']
    attempt = receipts.begin(user, '6599999999', NOW)
    receipts.finish(attempt, 'number_mismatch')
    receipts.record_reply(attempt, '6599999999', error_code=131030)
    payload = site.client.get('/connections/whatsapp/status').json()
    assert payload['revision'] != before
    assert payload['onboarding']['attempt_status'] == 'number_mismatch'
    assert payload['onboarding']['reply_status'] == 'failed'
    assert payload['onboarding']['reply_error_code'] == 131030
    assert not payload['joined'] and not payload['active']
    assert user.join_code not in str(payload)


def test_status_does_not_accept_a_different_account_id(site):
    own = ready_calendar(site)
    other, _ = site.db.sign_in_identity('other-sub', 'Other', 'other@example.com', NOW)
    site.db._update(other.id, NOW, status='active', joined=1, phone='6581234567', typed_phone='6581234567')
    payload = site.client.get('/connections/whatsapp/status', params={'user_id': other.id}).json()
    assert not payload['active'] and not payload['joined']


def test_runtime_wires_provider_receipts_into_browser_state(tmp_path, monkeypatch):
    import hashlib
    import hmac
    import json
    from fastapi.testclient import TestClient
    from app import wiring
    from app.crypto import TokenBox
    from app.webhook import create_app
    from tests.fakes import FakeSender
    from tests.test_multiuser_wiring import settings
    monkeypatch.setattr(wiring, 'build_service', lambda *args: object())
    runtime = wiring.build_runtime(settings(tmp_path), sender=FakeSender())
    user, _ = runtime.users.start_signup('A', 'a@example.com', runtime.box.encrypt('refresh'), NOW, (CALENDAR_SCOPE,))
    user = runtime.users.set_details(user.id, '+6581234567', None, NOW)
    receipts = runtime.router.commands.receipts
    attempt = receipts.begin(user, '6581234567', NOW)
    receipts.finish(attempt, 'verified')
    receipts.record_reply(attempt, '6581234567', message_id='outbound-test')
    app = create_app('verify', 'secret', runtime.store, runtime.router.handle, lambda: NOW,
                     routes=runtime.routes, status_handler=runtime.status_handler)
    payload = {'entry': [{'changes': [{'value': {'statuses': [{'id': 'outbound-test',
        'recipient_id': '6581234567', 'status': 'failed', 'errors': [{'code': 131030}]}]}}]}]}
    body = json.dumps(payload).encode()
    signature = 'sha256=' + hmac.new(b'secret', body, hashlib.sha256).hexdigest()
    result = TestClient(app).post('/webhook', content=body, headers={'X-Hub-Signature-256': signature})
    assert result.status_code == 200
    assert receipts.view(user)['reply_error_code'] == 131030
