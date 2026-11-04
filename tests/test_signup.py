"""Plan 2B: the landing page and sign-up flow, driven through FastAPI with Google faked."""

from datetime import datetime
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.commands import Commands
from app.crypto import TokenBox, generate_key
from app.signup import signup_routes
from app.store import Store
from app.timeutil import SGT
from app.tools.google_oauth import GoogleAccount, GoogleOAuthError, CALENDAR_SCOPE
from app.users import Users
from tests.fakes import FakeSender

NOW = datetime(2026, 10, 1, 10, tzinfo=SGT)
Pippa = "6591234567"


class FakeOAuth:
    def __init__(self, account=None, error=None):
        self.account = account or GoogleAccount("edmund@example.com", "Edmund Lim", "1//refresh", granted_scopes=(CALENDAR_SCOPE,))
        self.error = error
        self.codes: list[str] = []

    def auth_url(self, state):
        return f"https://accounts.google.com/o/oauth2/v2/auth?state={state}"

    def exchange(self, code):
        self.codes.append(code)
        if self.error:
            raise self.error
        return self.account


class Site:
    def __init__(self, oauth=None, max_users=5):
        self.users = Users(Store(":memory:"), max_users=max_users)
        self.users.ensure_admin("Pippa", Pippa, NOW)
        self.box = TokenBox(generate_key())
        self.sender = FakeSender()
        self.oauth = oauth or FakeOAuth()
        commands = Commands(self.users, self.sender, lambda: NOW, decrypt=self.box.decrypt, revoke=lambda t: None)
        app = FastAPI()
        app.include_router(
            signup_routes(
                oauth=self.oauth, users=self.users, box=self.box, commands=commands,
                bot_whatsapp="15550001234", session_secret="s" * 32, now=lambda: NOW,
            )
        )
        self.client = TestClient(app, base_url="https://testserver", follow_redirects=False)

    def sign_in(self, code="auth-code"):
        start = self.client.get("/signup/google")
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        return self.client.get("/signup/callback", params={"code": code, "state": state})

    def submit_number(self, phone, calendly=""):
        from tests.test_onboarding_routes import Inputs
        token = Inputs(self.client.get("/signup/number").text).values["csrf_token"]
        return self.client.post("/signup/number", data={"phone": phone, "calendly": calendly, "csrf_token": token})


def test_landing_page_explains_and_offers_sign_in():
    page = Site().client.get("/signup")
    assert page.status_code == 200
    assert "Sign in with Google" in page.text
    assert "unverified" in page.text.lower()  # warns about Google's screen up front
    assert "delete my data" in page.text  # says how to leave


def test_landing_page_says_when_signups_are_full():
    site = Site(max_users=1)  # only Pippa
    page = site.client.get("/signup")
    assert "Sign-ups are full" in page.text
    assert "Sign in with Google" not in page.text


def test_sign_in_redirects_to_google_with_a_state_cookie():
    start = Site().client.get("/signup/google")
    assert start.status_code == 302
    assert start.headers["location"].startswith("https://accounts.google.com/")
    cookie = start.headers["set-cookie"]
    assert "oauth_state=" in cookie and "HttpOnly" in cookie and "Secure" in cookie and "samesite=lax" in cookie.lower()


def test_full_happy_path():
    site = Site()
    callback = site.sign_in()
    assert callback.status_code == 303 and callback.headers["location"] == "/signup/number"
    number_page = site.client.get("/signup/number")
    assert "Hi Edmund" in number_page.text
    done = site.submit_number("+65 8111 2222")
    assert done.status_code == 303 and done.headers["location"] == "/signup/done"
    user = site.users.by_email("edmund@example.com")
    assert site.box.decrypt(user.google_token_enc) == "1//refresh"  # stored encrypted
    final = site.client.get("/signup/done")
    assert user.join_code in final.text
    assert f"https://wa.me/15550001234?text=join%20{user.join_code}" in final.text
    # Pippa is told, with what to do next.
    assert any(to == Pippa and f"approve {user.join_code}" in body for to, body in site.sender.sent)


def test_callback_with_a_forged_state_is_refused():
    # Review Focus: a sign-in link someone else started must not create an account.
    site = Site()
    site.client.get("/signup/google")
    bad = site.client.get("/signup/callback", params={"code": "c", "state": "attacker"})
    assert bad.status_code == 400
    assert site.oauth.codes == []
    assert site.users.by_email("edmund@example.com") is None


def test_callback_without_a_state_cookie_is_refused():
    site = Site()
    bad = site.client.get("/signup/callback", params={"code": "c", "state": "x"})
    assert bad.status_code == 400


def test_person_who_cancels_at_google_gets_a_friendly_page():
    site = Site()
    start = site.client.get("/signup/google")
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    page = site.client.get("/signup/callback", params={"error": "access_denied", "state": state})
    assert page.status_code == 200 and "didn't allow" in page.text


def test_google_problem_is_shown_plainly():
    site = Site(oauth=FakeOAuth(error=GoogleOAuthError("The bot needs access to your Google Calendar.")))
    page = site.sign_in()
    assert page.status_code == 200 and "needs access to your Google Calendar" in page.text


def test_returning_user_just_gets_a_fresh_token():
    site = Site()
    site.sign_in()
    site.submit_number("6581112222")
    user = site.users.by_email("edmund@example.com")
    site.users.approve(user.join_code, NOW)
    site.users.join(user.join_code, "6581112222", NOW)
    site.oauth.account = GoogleAccount("edmund@example.com", "Edmund Lim", "1//new")
    page = site.sign_in()
    assert page.status_code == 303 and page.headers["location"] == "/signup/status"
    assert site.box.decrypt(site.users.by_email("edmund@example.com").google_token_enc) == "1//new"


def test_full_at_callback_time():
    site = Site(max_users=1)
    page = site.sign_in()
    assert "Sign-ups are full" in page.text


def test_number_page_needs_the_signup_cookie():
    site = Site()
    assert site.client.get("/signup/number").headers["location"] == "/signup"
    assert site.client.post("/signup/number", data={"phone": "6581112222"}).headers["location"] == "/signup"
    assert site.client.get("/signup/done").headers["location"] == "/signup"


def test_bad_number_is_shown_on_the_form():
    site = Site()
    site.sign_in()
    page = site.submit_number("123")
    assert page.status_code == 400 and "country code" in page.text


def test_names_are_escaped():
    # Review Focus: a Google display name is attacker-controlled text.
    site = Site(oauth=FakeOAuth(account=GoogleAccount("x@gmail.com", "<script>alert(1)</script>", "1//r")))
    site.sign_in()
    page = site.client.get("/signup/number")
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;" in page.text


def test_sign_in_is_rate_limited_per_ip():
    site = Site()
    codes = [site.client.get("/signup/google", headers={"X-Forwarded-For": "1.2.3.4"}).status_code for _ in range(11)]
    assert codes[:10] == [302] * 10 and codes[10] == 429
    assert site.client.get("/signup/google", headers={"X-Forwarded-For": "5.6.7.8"}).status_code == 302
