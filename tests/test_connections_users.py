from datetime import datetime
import pytest
from app.store import Store
from app.users import Users, SignupError, CALENDAR_SCOPE
from app.timeutil import SGT

NOW = datetime(2026, 10, 1, tzinfo=SGT)

def users(**kwargs):
    result = Users(Store(':memory:'), **kwargs)
    result.ensure_admin('Owner', '6591234567', NOW)
    return result

def test_identity_only_signup_preapproves_without_granting_access():
    db = users(auto_activate=True, allowed_emails=['a@example.com'])
    user, new = db.sign_in_identity('sub-a', 'A', 'a@example.com', NOW)
    assert new and user.status == 'approved' and not user.google_token_enc
    assert user.google_subject == 'sub-a' and user.calendar_authorized is False
    with pytest.raises(SignupError, match='Calendar'):
        db.set_details(user.id, '6581112222', None, NOW)

def test_identity_signin_preserves_existing_grants_and_rejects_subject_collision():
    db = users()
    old, _ = db.start_signup('A', 'a@example.com', 'encrypted', NOW, [CALENDAR_SCOPE])
    user, new = db.sign_in_identity('sub-a', 'A', old.email, NOW)
    assert not new and user.google_token_enc == 'encrypted' and user.calendar_authorized
    with pytest.raises(SignupError):
        db.sign_in_identity('wrong-sub', 'A', old.email, NOW)
    assert db.get(user.id).google_subject == 'sub-a'

def test_owner_and_disabled_account_cannot_be_bound():
    db = users()
    db._update(1, NOW, email='owner@example.com')
    with pytest.raises(SignupError):
        db.sign_in_identity('owner-sub', 'Owner', 'owner@example.com', NOW)
    user, _ = db.sign_in_identity('sub-a', 'A', 'a@example.com', NOW)
    db._update(user.id, NOW, status='disabled')
    with pytest.raises(SignupError):
        db.sign_in_identity('sub-a', 'A', 'a@example.com', NOW)

def test_disconnect_preserves_profile_and_booking_but_disables_google():
    db = users()
    user, _ = db.sign_in_identity('sub-a', 'A', 'a@example.com', NOW)
    db._update(user.id, NOW, google_token_enc='encrypted', google_scopes='["'+CALENDAR_SCOPE+'"]',
               calendly_url='https://calendly.com/a', joined=1, phone='6581112222')
    db.disconnect_google(user.id, NOW)
    after = db.get(user.id)
    assert after.google_disconnected and after.google_token_enc is None
    assert after.calendar_status == after.gmail_status == 'not_connected'
    assert after.calendly_url == 'https://calendly.com/a' and after.joined and after.google_subject == 'sub-a'

@pytest.mark.parametrize('url', ['http://calendly.com/a','https://evil.com/a','https://calendly.com','https://x@calendly.com/a','https://calendly.com:443/a','https://calendly.com.evil/a'])
def test_booking_rejects_invalid_url(url):
    db = users()
    user, _ = db.sign_in_identity('sub-a','A','a@example.com',NOW)
    with pytest.raises(SignupError):
        db.set_booking(user.id,url,NOW)

def test_booking_add_edit_remove():
    db = users()
    user, _ = db.sign_in_identity('sub-a','A','a@example.com',NOW)
    assert db.set_booking(user.id,'https://calendly.com/a/intro',NOW).calendly_url == 'https://calendly.com/a/intro'
    assert db.set_booking(user.id,'',NOW).calendly_url is None

def test_connection_commit_cannot_overwrite_concurrent_disconnect():
    db=users();user,_=db.sign_in_identity('sub-a','A','a@example.com',NOW)
    version=user.google_connection_version
    db.disconnect_google(user.id,NOW)
    with pytest.raises(SignupError,match='changed'):
        db.commit_connection(user.id,version,NOW,google_token_enc='new')
    assert db.get(user.id).google_token_enc is None

def test_legacy_signup_cannot_overwrite_subject_bound_connection():
    db=users();user,_=db.sign_in_identity('sub-a','A','a@example.com',NOW)
    with pytest.raises(SignupError,match='Connections'):
        db.start_signup('A','a@example.com','legacy-token',NOW,[CALENDAR_SCOPE])
    assert db.get(user.id).google_token_enc is None

def test_pending_provider_revocation_blocks_new_connection_commit():
    db=users();user,_=db.sign_in_identity('sub-a','A','a@example.com',NOW)
    disconnected=db.disconnect_google(user.id,NOW)
    with pytest.raises(SignupError,match='revocation'):
        db.commit_connection(user.id,disconnected.google_connection_version,NOW,google_token_enc='new')

def test_stale_revocation_completion_does_not_change_new_connection():
    db=users();user,_=db.sign_in_identity('sub-a','A','a@example.com',NOW)
    old=db.disconnect_google(user.id,NOW)
    db._update(user.id,NOW,google_connection_version=old.google_connection_version+1,google_token_enc='new',google_revocation_status=None)
    db.finish_google_disconnect(user.id,old.google_connection_version,'succeeded',NOW)
    assert db.get(user.id).google_revocation_status is None


def beta_user(**fields):
    db = users(auto_activate=True, allowed_emails=['a@example.com'])
    user, _ = db.sign_in_identity('sub-a', 'A', 'a@example.com', NOW)
    return db, db._update(user.id, NOW, **({'status': 'pending'} | fields))


@pytest.mark.parametrize('fields', [
    {}, {'joined': 1, 'phone': '6581112222', 'typed_phone': '81112222', 'google_token_enc': 'legacy'},
    {'joined': 1, 'phone': '6581112222', 'typed_phone': '6581112222'},
    {'joined': 1, 'phone': '6581112222', 'typed_phone': '6581112222', 'google_token_enc': 'legacy', 'calendar_status': 'connected'},
])
def test_beta_approval_records_invitation_without_fabricating_setup(fields):
    db, user = beta_user(**fields)
    approved = db.approve_beta(user.id, NOW)
    assert approved.status == 'approved'
    assert approved.google_scopes == user.google_scopes
    assert approved.phone == user.phone and approved.typed_phone == user.typed_phone
    assert approved.google_token_enc == user.google_token_enc and approved.joined == user.joined
    assert db.approve_beta(user.id, NOW) == approved


@pytest.mark.parametrize('override,expected', [({}, 'active'), ({'google_disconnected': 1}, 'approved'),
    ({'calendar_status': 'unknown'}, 'approved'), ({'google_token_enc': None}, 'approved')])
def test_beta_approval_activation_requires_full_proof(override, expected):
    fields = dict(joined=1, phone='6581112222', typed_phone='6581112222', google_token_enc='token',
                  google_scopes='["'+CALENDAR_SCOPE+'"]', calendar_status='connected', join_code='ABCDEF')
    db, user = beta_user(**(fields | override))
    approved = db.approve_beta(user.id, NOW)
    assert approved.status == expected
    if expected == 'active': assert approved.join_code is None


@pytest.mark.parametrize('fields', [{'status': 'disabled'}, {'status': 'rejected'}, {'email': 'other@example.com'}, {'is_admin': 1}])
def test_beta_approval_rejects_excluded_accounts_without_mutating(fields):
    db, user = beta_user(**fields)
    with pytest.raises(SignupError): db.approve_beta(user.id, NOW)
    assert db.get(user.id) == user


def test_beta_approval_preserves_active_account_and_rejects_missing_user():
    db, user = beta_user(status='active', google_token_enc='preserve')
    assert db.approve_beta(user.id, NOW) == user
    with pytest.raises(SignupError): db.approve_beta(999, NOW)


def test_fresh_identity_signin_preapproves_existing_invitee_without_changing_grants():
    db, user = beta_user(google_token_enc='legacy', google_scopes=None, joined=1,
                         phone='6581112222', typed_phone='81112222')
    signed_in, new = db.sign_in_identity('sub-a', 'A', user.email, NOW)
    assert not new and signed_in.status == 'approved'
    assert signed_in.google_token_enc == 'legacy' and signed_in.google_scopes is None
    assert signed_in.phone == user.phone and signed_in.joined


def test_manual_identity_signup_still_requires_organizer_approval():
    db = users()
    user, _ = db.sign_in_identity('sub-a', 'A', 'a@example.com', NOW)
    assert user.status == 'pending'
    assert db.sign_in_identity('sub-a', 'A', user.email, NOW)[0].status == 'pending'


@pytest.mark.parametrize('status', ['pending', 'approved'])
def test_legacy_phone_recovery_issues_new_exact_number_proof(status):
    db, user = beta_user(status=status, joined=1, phone='6581112222', typed_phone='81112222',
                        google_token_enc='token', google_scopes='["'+CALENDAR_SCOPE+'"]', calendar_status='connected')
    recovered = db.set_details(user.id, '+65 8111 2222', None, NOW)
    assert recovered.phone is None and not recovered.joined and recovered.join_code
    assert recovered.typed_phone == '6581112222' and recovered.status == status
    with pytest.raises(SignupError, match='exact'): db.join(recovered.join_code, '81112222', NOW)
    assert db.join(recovered.join_code, '6581112222', NOW).joined


@pytest.mark.parametrize('fields,phone,link', [
    ({'calendar_status': 'unknown'}, '6581112222', None),
    ({'google_token_enc': None}, '6581112222', None),
    ({'google_disconnected': 1}, '6581112222', None),
    ({}, 'x', None), ({}, '6591234567', None), ({}, '6581112222', 'http://bad'),
    ({'status': 'active'}, '6581112222', None), ({'status': 'disabled'}, '6581112222', None),
    ({'status': 'rejected'}, '6581112222', None), ({'google_subject': None}, '6581112222', None),
    ({'typed_phone': '6581112222'}, '6581112222', None),
])
def test_legacy_recovery_validates_before_clearing_old_link(fields, phone, link):
    defaults = dict(joined=1, phone='6581112222', typed_phone='81112222', google_token_enc='token',
                    google_scopes='["'+CALENDAR_SCOPE+'"]', calendar_status='connected')
    db, user = beta_user(**(defaults | fields))
    with pytest.raises(SignupError): db.set_details(user.id, phone, link, NOW)
    assert db.get(user.id) == user


@pytest.mark.parametrize('fields', [{'calendar_status': 'unknown'}, {'google_token_enc': None},
                                   {'google_disconnected': 1}, {'google_scopes': '[]'}])
def test_calendar_loss_after_code_issue_blocks_join_without_partial_phone_link(fields):
    db, user = beta_user(status='approved', google_token_enc='token', google_scopes='["'+CALENDAR_SCOPE+'"]', calendar_status='connected')
    issued = db.set_details(user.id, '6581112222', None, NOW)
    before = db._update(user.id, NOW, **fields)
    with pytest.raises(SignupError, match='Calendar'):
        db.join(issued.join_code, '6581112222', NOW)
    assert db.get(user.id) == before


@pytest.mark.parametrize('status', ['active', 'disabled', 'rejected'])
def test_identity_signin_cannot_approve_or_reset_protected_statuses(status):
    db, before = beta_user(status=status, google_token_enc='preserve')
    if status == 'active':
        assert db.sign_in_identity('sub-a', 'A', before.email, NOW)[0] == before
    else:
        with pytest.raises(SignupError): db.sign_in_identity('sub-a', 'A', before.email, NOW)
        assert db.get(before.id) == before
