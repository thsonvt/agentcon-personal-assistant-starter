"""Plan 2B: Google sign-in for friends (web OAuth client), with Google's servers faked."""

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.tools.google_oauth import CALENDAR_SCOPE, GoogleOAuth, GoogleOAuthError


def google(token_json=None, userinfo=None, token_status=200, revoke_status=200, seen=None):
    seen = seen if seen is not None else []

    def handler(request):
        seen.append(request)
        if request.url.path == "/token":
            return httpx.Response(token_status, json=token_json if token_json is not None else {
                "access_token": "ya29.x", "refresh_token": "1//refresh", "scope": f"openid {CALENDAR_SCOPE} email",
            })
        if request.url.path == "/v1/userinfo":
            return httpx.Response(200, json=userinfo or {"email": "Edmund@Example.com", "name": "Edmund Lim", "email_verified": True})
        if request.url.path == "/revoke":
            return httpx.Response(revoke_status)
        return httpx.Response(404)

    return GoogleOAuth("web-id", "web-secret", "https://bot.example.com/signup/callback",
                       http=httpx.Client(transport=httpx.MockTransport(handler))), seen


def test_auth_url_asks_for_offline_calendar_access():
    oauth, _ = google()
    url = urlparse(oauth.auth_url("state123"))
    q = parse_qs(url.query)
    assert url.netloc == "accounts.google.com"
    assert q["client_id"] == ["web-id"]
    assert q["redirect_uri"] == ["https://bot.example.com/signup/callback"]
    assert q["state"] == ["state123"]
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert CALENDAR_SCOPE in q["scope"][0] and "email" in q["scope"][0]


def test_exchange_returns_the_account_and_refresh_token():
    oauth, seen = google()
    account = oauth.exchange("auth-code")
    assert (account.email, account.name, account.refresh_token) == ("edmund@example.com", "Edmund Lim", "1//refresh")
    body = parse_qs(seen[0].content.decode())
    assert body["code"] == ["auth-code"] and body["grant_type"] == ["authorization_code"]
    assert seen[1].headers["Authorization"] == "Bearer ya29.x"


def test_unticked_calendar_box_is_refused():
    # Review Focus: Google's consent screen lets people untick the calendar permission.
    oauth, _ = google(token_json={"access_token": "a", "refresh_token": "r", "scope": "openid email"})
    with pytest.raises(GoogleOAuthError, match="calendar"):
        oauth.exchange("code")


def test_missing_refresh_token_is_refused():
    oauth, _ = google(token_json={"access_token": "a", "scope": CALENDAR_SCOPE})
    with pytest.raises(GoogleOAuthError, match="try again"):
        oauth.exchange("code")


def test_unverified_email_is_refused():
    oauth, _ = google(userinfo={"email": "x@example.com", "name": "X", "email_verified": False})
    with pytest.raises(GoogleOAuthError, match="verified"):
        oauth.exchange("code")


def test_bad_code_is_refused():
    oauth, _ = google(token_json={"error": "invalid_grant"}, token_status=400)
    with pytest.raises(GoogleOAuthError):
        oauth.exchange("old-code")


def test_revoke_posts_the_token():
    oauth, seen = google()
    oauth.revoke("1//refresh")
    assert seen[-1].url.path == "/revoke"
    assert parse_qs(seen[-1].content.decode())["token"] == ["1//refresh"]


def test_revoke_failure_raises():
    oauth, _ = google(revoke_status=500)
    with pytest.raises(GoogleOAuthError):
        oauth.revoke("1//refresh")
