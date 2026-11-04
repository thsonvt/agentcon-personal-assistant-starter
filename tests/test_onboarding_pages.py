"""Onboarding truthfulness, next actions, and output-escaping contracts."""

from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import quote

from app import pages


def user(**changes):
    defaults = dict(first_name="Son", email="son@example.com", typed_phone="6581234567",
                    phone=None, joined=False, status="pending", join_code="ABC234",
                    calendar_authorized=True, gmail_authorized=False)
    return SimpleNamespace(**(defaults | changes))


def test_landing_explains_optional_access_and_real_test_number_gate():
    html = pages.landing(2, auto_activate=True, test_number_mode=True)
    assert "Calendar" in html and "Gmail is optional" in html
    assert "six-character code" in html
    assert "Meta" in html and "test number" in html
    assert "Advanced" not in html and "Continue anyway" not in html
    assert "eligible" in html.lower()
    assert "verification is not complete" in html
    assert "limited to invited test users" not in html


def test_number_form_posts_csrf_and_explains_exact_number_verification():
    html = pages.number_form("Son", csrf_token='token"<>', auto_activate=True, test_number_mode=False)
    assert 'name="csrf_token"' in html and 'value="token&quot;&lt;&gt;"' in html
    assert "same WhatsApp number" in html
    assert "Meta test number" not in html
    assert 'action="/signup/number"' in html


def test_pending_status_separates_permissions_phone_and_activation():
    html = pages.onboarding_status(user(), "6599999999", csrf_token="token")
    assert "son@example.com" in html
    assert "Calendar access authorized" in html
    assert "Gmail not connected" in html
    assert "Waiting for WhatsApp verification" in html
    assert "Beta access pending" in html
    assert "ABC234" in html
    assert 'href="https://wa.me/6599999999?text=join%20ABC234"' in html
    assert 'href="/signup/number"' in html
    assert 'href="/signup/status"' in html
    assert 'method="post" action="/signup/logout"' in html
    assert 'name="csrf_token" value="token"' in html


def test_unknown_scopes_are_not_presented_as_connected():
    html = pages.onboarding_status(user(calendar_authorized=None, gmail_authorized=None), "6599999999")
    assert "Calendar access needs checking" in html
    assert "Gmail access needs checking" in html
    assert "Calendar access authorized" not in html
    assert 'href="/signup/google"' in html
    assert 'href="/signup/gmail"' in html


def test_verified_pending_status_does_not_claim_active_or_repeat_join_code():
    html = pages.onboarding_status(user(joined=True, phone="6581234567"), "6599999999")
    assert "WhatsApp number verified" in html
    assert "Beta access pending" in html
    assert "ABC234" not in html
    assert "Pippa is ready" not in html


def test_active_status_offers_guided_commands_and_optional_gmail():
    html = pages.onboarding_status(user(joined=True, phone="6581234567", status="active", join_code=None), "6599999999")
    assert "Pippa is ready" in html
    assert "help" in html and "tomorrow" in html
    assert 'href="https://wa.me/6599999999?text=help"' in html
    assert 'href="/signup/gmail"' in html
    assert "Waiting for WhatsApp verification" not in html


def test_disabled_status_does_not_offer_activation_or_join():
    html = pages.onboarding_status(user(status="disabled"), "6599999999")
    assert "Beta access disabled" in html
    assert "ABC234" not in html and "?text=join" not in html
    assert "Pippa is ready" not in html


def test_user_values_and_deep_link_parameters_are_escaped():
    unsafe = '<img src=x onerror="alert(1)">'
    html = pages.onboarding_status(user(first_name=unsafe, email=unsafe, typed_phone=unsafe, join_code=unsafe), '1/2?x="')
    assert unsafe not in html
    assert "&lt;img" in html
    assert quote('1/2?x="', safe="") in html
    assert quote("join " + unsafe, safe="") in html


def test_gmail_page_explains_same_account_optional_readonly_processing():
    html = pages.gmail_connection(expected_email='<script>alert(1)</script>')
    assert "Gmail is optional" in html and "read-only" in html
    assert "cannot send or delete" in html and "OpenAI" in html
    assert "same Google account" in html
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_done_is_backward_compatible_and_does_not_claim_approval():
    html = pages.done("Son", "ABC234", "6599999999")
    assert "ABC234" in html
    assert "same WhatsApp number" in html
    assert "test number" in html and "Meta" in html
    assert "will approve you shortly" not in html
    assert "expires in 30 minutes" in html
    assert "7 days" not in html


def test_expired_join_code_is_not_offered_for_verification():
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    html = pages.onboarding_status(
        user(join_code_expires_at="2026-10-01T00:00:00+00:00"), "6599999999", now=now,
    )
    assert "code has expired" in html
    assert "ABC234" not in html and "?text=join" not in html
    assert 'href="/signup/number"' in html
def test_full_beta_keeps_existing_account_reconnection_available():
    from app import pages
    html = pages.landing(0, auto_activate=True, test_number_mode=False)
    assert 'href="/signup/google"' in html
    assert 'resume' in html.lower()
