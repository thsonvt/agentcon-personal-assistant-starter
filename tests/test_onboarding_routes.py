"""Full private-beta onboarding and cross-account/CSRF boundaries."""
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.commands import Commands
from app.config import ConfigError, REQUIRED, load_settings
from app.crypto import TokenBox, generate_key
from app.signup import signup_routes
from app.store import Store
from app.timeutil import SGT
from app.tools.google_oauth import GoogleAccount, CALENDAR_SCOPE, GMAIL_SCOPE
from app.users import Users
from tests.fakes import FakeSender

NOW = datetime(2026, 10, 1, 10, tzinfo=SGT)
PHONE = "6581112222"


class Inputs(HTMLParser):
    def __init__(self, text):
        super().__init__(); self.values = {}; self.feed(text)
    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "input" and d.get("name"):
            self.values[d["name"]] = d.get("value", "")


class OAuth:
    def __init__(self):
        self.account = GoogleAccount("early@example.com", "Early User", "token", True,
                                     (CALENDAR_SCOPE, GMAIL_SCOPE))
        self.requests = []
    def auth_url(self, state, include_gmail=False):
        self.requests.append(include_gmail)
        return "https://accounts.google.com/o/oauth2/v2/auth?state=" + state
    def exchange(self, code):
        return self.account


class Site:
    def __init__(self):
        self.users = Users(Store(":memory:"), max_users=4, allowed_emails={"early@example.com"}, auto_activate=True)
        self.users.ensure_admin("Son", "6591234567", NOW)
        self.oauth = OAuth(); self.box = TokenBox(generate_key()); self.sender = FakeSender(); self.changed = []
        self.commands = Commands(self.users, self.sender, lambda: NOW, self.box.decrypt, lambda t: None,
                                 base_url="https://testserver", test_number_mode=False)
        app = FastAPI()
        app.include_router(signup_routes(self.oauth, self.users, self.box, self.commands, "6588889999", "s"*32,
                                         lambda: NOW, on_user_changed=self.changed.append, test_number_mode=False))
        self.client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    def start(self, gmail=True):
        r = self.client.get("/signup/google", params={"gmail": int(gmail)})
        return parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    def finish(self, state):
        return self.client.get("/signup/callback", params={"state": state, "code": "valid"})
    def login(self):
        return self.finish(self.start())
    def number(self, phone=PHONE):
        form = self.client.get("/signup/number")
        csrf = Inputs(form.text).values["csrf_token"]
        return self.client.post("/signup/number", data={"phone": phone, "csrf_token": csrf})


def test_eligible_user_completes_google_number_and_automatic_activation():
    site = Site()
    assert site.login().status_code == 303
    assert site.number().status_code == 303
    user = site.users.by_email("early@example.com")
    assert user.status == "approved" and not user.joined
    before = site.client.get("/signup/status")
    assert "early@example.com" in before.text and user.join_code in before.text
    reply = site.commands.handle(PHONE, "join " + user.join_code)
    assert "You're in" in reply
    assert site.users.get(user.id).status == "active"
    page = site.client.get("/signup/status")
    assert page.status_code == 200 and "active" in page.text.lower()
    assert "calendar" in page.text.lower() and "gmail" in page.text.lower()
    assert "approve" not in page.text.lower()
    assert user.join_code not in page.text


def test_non_invited_email_does_not_store_tokens_or_reserve_a_seat():
    site = Site()
    site.oauth.account = GoogleAccount("outside@example.com", "Other", "secret", False, (CALENDAR_SCOPE,))
    r = site.login()
    assert r.status_code == 403
    assert site.users.by_email("outside@example.com") is None
    assert site.users.seats_left() == 3


@pytest.mark.parametrize("token", ["", "forged"])
def test_number_post_requires_form_bound_csrf_token(token):
    site = Site(); site.login()
    r = site.client.post("/signup/number", data={"phone": PHONE, "csrf_token": token})
    assert r.status_code == 403
    assert site.users.by_email("early@example.com").typed_phone is None


def test_status_is_private_and_has_no_store_cache():
    site = Site()
    assert site.client.get("/signup/status").status_code == 303
    site.login()
    page = site.client.get("/signup/status")
    assert "no-store" in page.headers["cache-control"]


def test_oauth_state_is_consumed_and_replay_cannot_update_the_token():
    site = Site(); state = site.start(); site.finish(state)
    site.oauth.account = GoogleAccount("early@example.com", "Early", "new-token", False, (CALENDAR_SCOPE,))
    assert site.finish(state).status_code == 400
    assert site.box.decrypt(site.users.by_email("early@example.com").google_token_enc) == "token"


def test_incremental_gmail_reconnect_cannot_switch_signed_in_google_account():
    site = Site(); site.login(); site.number()
    user = site.users.by_email("early@example.com")
    site.commands.handle(PHONE, "join " + user.join_code)
    state = site.start()
    site.oauth.account = GoogleAccount("wrong@example.com", "Wrong", "wrong-token", True, (CALENDAR_SCOPE,GMAIL_SCOPE))
    r = site.finish(state)
    assert r.status_code == 403
    assert "early@example.com" in r.text
    assert site.users.by_email("wrong@example.com") is None
    assert site.box.decrypt(site.users.get(user.id).google_token_enc) == "token"


def test_joined_user_stale_form_cannot_change_number():
    site = Site(); site.login()
    old_csrf = Inputs(site.client.get("/signup/number").text).values["csrf_token"]
    site.number(); user = site.users.by_email("early@example.com")
    site.commands.handle(PHONE, "join " + user.join_code)
    response = site.client.post("/signup/number", data={"phone":"6589990000","csrf_token":old_csrf})
    assert response.status_code in (303,400)
    assert site.users.get(user.id).phone == PHONE
    assert site.users.get(user.id).join_code is None


def test_logout_clears_private_setup_session():
    site = Site();site.login()
    page = site.client.get("/signup/status")
    csrf = Inputs(page.text).values["csrf_token"]
    assert site.client.post("/signup/logout", data={"csrf_token":"forged"}).status_code == 403
    assert site.client.post("/signup/logout", data={"csrf_token":csrf}).status_code == 303
    assert site.client.get("/signup/status").status_code == 303


def test_auto_activation_config_is_explicit_and_fail_closed():
    env = {k:"x" for k in REQUIRED} | {"OWNER_WHATSAPP":"6591234567"}
    with pytest.raises(ConfigError, match="BETA_ALLOWED_EMAILS"):
        load_settings(env | {"BETA_AUTO_APPROVE":"true"})
    cfg = load_settings(env | {"BETA_AUTO_APPROVE":"true", "BETA_ALLOWED_EMAILS":"Early@Example.com, next@example.com"})
    assert cfg.beta_auto_approve is True
    assert cfg.beta_allowed_emails == ("early@example.com","next@example.com")
    assert cfg.whatsapp_test_mode is True


def test_legacy_number_mismatch_has_recovery_without_reusing_old_code():
    site = Site(); site.login(); site.number()
    user = site.users.by_email("early@example.com")
    site.users.conn.execute("UPDATE users SET joined=1,phone='6589990000',status='pending' WHERE id=?", (user.id,))
    site.users.conn.commit()
    page = site.client.get("/signup/status")
    assert user.join_code not in page.text
    assert "Reconnect Google" in page.text
    assert "WhatsApp number verified" not in page.text
    assert site.login().status_code == 303
    reconnected = site.users.get(user.id)
    assert reconnected.joined is False and reconnected.phone is None
    assert site.number("6589990000").status_code == 303
    fresh = site.users.get(user.id)
    assert "You're in" in site.commands.handle("6589990000", "join " + fresh.join_code)
