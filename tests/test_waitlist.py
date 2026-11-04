"""Waitlist abuse defenses and provider boundaries, without external requests."""

from dataclasses import replace
import json
import sqlite3
from urllib.parse import parse_qs

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from app.waitlist import WaitlistConfig, waitlist_routes, SUCCESS_MESSAGE
from app.email_eligibility import UNSUPPORTED_DOMAINS


@pytest.fixture
def config(tmp_path):
    return WaitlistConfig(list_id="test-list", enabled=True, site_key="public-site", secret_key="private-turnstile", loops_api_key="private-loops", rate_secret="stable-rate-salt", state_path=str(tmp_path / "waitlist.sqlite3"))


class Providers:
    def __init__(self, contacts=None, verification=None, write=None):
        self.calls = []
        self.contacts = [] if contacts is None else contacts
        self.verification = {"success": True, "hostname": "example.com", "action": "waitlist"} if verification is None else verification
        self.write = {"success": True, "id": "contact-id"} if write is None else write

    def __call__(self, request):
        self.calls.append(request)
        if request.url.host == "challenges.cloudflare.com":
            return httpx.Response(200, json=self.verification)
        assert request.headers["authorization"] == "Bearer private-loops"
        if request.method == "GET":
            return httpx.Response(200, json=self.contacts)
        return httpx.Response(200, json=self.write)


def client(config, providers=None, clock=lambda: 1000):
    app = FastAPI()
    app.include_router(waitlist_routes(config, transport=httpx.MockTransport(providers or Providers()), now=clock))
    return TestClient(app)


def submit(browser, email="person@gmail.com", **overrides):
    headers = {"origin": "https://example.com", **overrides.pop("headers", {})}
    data = {"email": email, "cf-turnstile-response": "one-use-token", "website": ""}
    data.update(overrides.pop("fields", {}))
    return browser.post("/api/waitlist", data=data, headers=headers, **overrides)


def test_new_contact_requires_verification_and_minimal_enrollment(config):
    providers = Providers()
    browser = client(config, providers)
    assert browser.get("/api/waitlist/config").json() == {"enabled": True, "siteKey": "public-site", "unsupportedDomains": sorted(UNSUPPORTED_DOMAINS)}
    response = submit(browser)
    assert response.status_code == 200 and response.json() == {"success": True, "message": SUCCESS_MESSAGE}
    assert [call.method for call in providers.calls] == ["POST", "GET", "POST"]
    assert parse_qs(providers.calls[0].content.decode()) == {"secret": ["private-turnstile"], "response": ["one-use-token"]}
    assert json.loads(providers.calls[-1].content) == {"email": "person@gmail.com", "source": "Pippa waitlist", "subscribed": True, "mailingLists": {config.list_id: True}, "pippaSignInEligible": False}


@pytest.mark.parametrize("change", [{"enabled": False}, {"secret_key": ""}, {"loops_api_key": ""}, {"rate_secret": ""}, {"site_key": ""}, {"origin": "https://example.com/other"}, {"trusted_proxy_cidrs": ("broken",)}])
def test_missing_configuration_fails_closed(config, change):
    providers = Providers()
    browser = client(replace(config, **change), providers)
    assert browser.get("/api/waitlist/config").json() == {"enabled": False}
    assert submit(browser).status_code == 503
    assert providers.calls == []


@pytest.mark.parametrize("verification", [{"success": False, "error-codes": ["timeout-or-duplicate"]}, {"success": True, "hostname": "evil.example", "action": "waitlist"}, {"success": True, "hostname": "example.com", "action": "login"}, {"success": "true", "hostname": "example.com", "action": "waitlist"}, []])
def test_invalid_or_replayed_turnstile_never_reaches_loops(config, verification):
    providers = Providers(verification=verification)
    assert submit(client(config, providers)).status_code == 403
    assert len(providers.calls) == 1


@pytest.mark.parametrize("fields", [{"source": "other"}, {"mailingLists": "other"}, {"pippaStage": "approved"}, {"pippaSignInEligible": "true"}, {"website": "bot.example"}, {"cf-turnstile-response": ""}, {"cf-turnstile-response": "x" * 2049}, {"email": "bad email@gmail.com"}, {"email": "a@localhost"}, {"email": "x@-bad.com"}, {"email": "é@gmail.com"}])
def test_hostile_fields_and_invalid_inputs_never_reach_providers(config, fields):
    providers = Providers()
    assert submit(client(config, providers), fields=fields).status_code == 400
    assert providers.calls == []


def test_origin_content_type_size_and_duplicate_fields(config):
    providers = Providers()
    browser = client(config, providers)
    assert submit(browser, headers={"origin": "https://evil.example"}).status_code == 403
    assert browser.post("/api/waitlist", json={"email": "a@gmail.com"}, headers={"origin": config.origin}).status_code == 400
    for body in ("email=x%40example.com&email=y%40example.com&cf-turnstile-response=x", "x" * 8193):
        assert browser.post("/api/waitlist", content=body, headers={"origin": config.origin, "content-type": "application/x-www-form-urlencoded"}).status_code == 400
    assert providers.calls == []


@pytest.mark.parametrize("contact", [{"subscribed": False, "mailingLists": {}}, {"subscribed": True, "mailingLists": {"test-list": False}}, {"subscribed": True, "mailingLists": {"test-list": True}}])
def test_duplicate_and_unsubscribe_are_preserved_without_enumeration(config, contact):
    providers = Providers(contacts=[contact])
    response = submit(client(config, providers))
    assert response.status_code == 200 and response.json()["message"] == SUCCESS_MESSAGE
    assert len(providers.calls) == 2


def test_existing_contact_update_does_not_overwrite_other_properties(config):
    providers = Providers(contacts=[{"subscribed": True, "mailingLists": {"other-list": True}, "userGroup": "Commercial", "pippaStage": "onboarded", "pippaSignInEligible": True}])
    assert submit(client(config, providers)).status_code == 200
    assert providers.calls[-1].method == "PUT"
    assert json.loads(providers.calls[-1].content) == {"email": "person@gmail.com", "mailingLists": {config.list_id: True}}


@pytest.mark.parametrize("write", [{"success": False}, {}, [], {"success": "true"}])
def test_provider_does_not_confirm_write(config, write):
    assert submit(client(config, Providers(write=write))).status_code == 503


def test_network_failure_is_generic_and_does_not_leak(config):
    def fail(request):
        raise httpx.ConnectError("private-loops secret", request=request)
    response = submit(client(config, fail))
    assert response.status_code == 503 and "private" not in response.text


def test_email_budget_persists_across_router_instances_and_expires(config):
    for _ in range(3):
        assert submit(client(config)).status_code == 200
    response = submit(client(config), email="PERSON@gmail.com")
    assert response.status_code == 429 and response.headers["retry-after"] == "600"
    assert submit(client(config, clock=lambda: 4601)).status_code == 200
    with sqlite3.connect(config.state_path) as db:
        rows = db.execute("SELECT bucket,key,at FROM waitlist_hits").fetchall()
    assert "person@gmail.com" not in str(rows) and "testclient" not in str(rows)
    assert all(len(row[1]) == 64 for row in rows)


def test_untrusted_forwarded_headers_cannot_evade_ip_budget(config):
    browser = client(config)
    for n in range(20):
        assert submit(browser, email=f"person{n}@gmail.com", headers={"x-forwarded-for": f"192.0.2.{n}", "x-real-ip": f"192.0.2.{n}"}).status_code == 200
    assert submit(browser, email="new@gmail.com", headers={"x-forwarded-for": "203.0.113.9"}).status_code == 429


def test_trusted_proxy_walks_from_right(config):
    from starlette.requests import Request
    from app.waitlist import _client_ip
    config = replace(config, trusted_proxy_cidrs=("10.0.0.0/8",))
    def request(peer, chain):
        return Request({"type": "http", "client": (peer, 123), "headers": [(b"x-forwarded-for", chain.encode())]})
    assert _client_ip(request("10.0.0.1", "1.2.3.4, 192.0.2.8, 10.0.0.2"), config) == "192.0.2.8"
    assert _client_ip(request("192.0.2.9", "1.2.3.4"), config) == "192.0.2.9"
    assert _client_ip(request("10.0.0.1", "broken"), config) == "10.0.0.1"


def test_env_requires_explicit_enable_and_uses_stable_fallback(monkeypatch):
    monkeypatch.delenv("WAITLIST_ENABLED", raising=False)
    monkeypatch.delenv("WAITLIST_RATE_SECRET", raising=False)
    monkeypatch.delenv("WAITLIST_STATE_PATH", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "existing-stable-secret")
    monkeypatch.setenv("DB_PATH", "/data/agents.db")
    config = WaitlistConfig.from_env()
    assert not config.ready and not config.enabled
    assert config.rate_secret == "existing-stable-secret"
    assert config.state_path == "/data/agents.db.waitlist.sqlite3"
    assert "existing-stable-secret" not in repr(config)


@pytest.mark.parametrize("contacts", [{"success": False}, [{"subscribed": True}], [{"subscribed": True, "mailingLists": []}], [{"mailingLists": {}}]])
def test_unknown_contact_state_fails_closed(config, contacts):
    providers = Providers(contacts=contacts)
    assert submit(client(config, providers)).status_code == 503
    assert len(providers.calls) == 2


def test_global_budget_is_shared_across_addresses(config):
    from app.waitlist import _Limiter
    limiter = _Limiter(config)
    for _ in range(200):
        assert limiter.consume((("global", "all", ((3600, 200),)),), 1000)
    assert submit(client(config)).status_code == 429
    assert submit(client(config, clock=lambda: 4601)).status_code == 200


def test_hour_ip_budget_survives_short_window_expiry(config):
    from app.waitlist import _Limiter
    limiter = _Limiter(config)
    for timestamp in (1000, 1700, 2400):
        for _ in range(20):
            assert limiter.consume((("ip", "testclient", ((600, 20), (3600, 60))),), timestamp)
    assert submit(client(config, clock=lambda: 3100)).status_code == 429


def test_streamed_body_without_content_length_is_bounded(config):
    providers = Providers()
    browser = client(config, providers)
    def chunks():
        yield b"email=person%40example.com&cf-turnstile-response="
        yield b"x" * 8192
    response = browser.post("/api/waitlist", content=chunks(), headers={"origin": config.origin, "content-type": "application/x-www-form-urlencoded"})
    assert response.status_code == 400 and providers.calls == []


def test_invalid_tokens_do_not_exhaust_someone_elses_email_budget(config):
    providers = Providers(verification={"success": False})
    browser = client(config, providers)
    for _ in range(5):
        assert submit(browser).status_code == 403
    with sqlite3.connect(config.state_path) as db:
        assert db.execute("SELECT COUNT(*) FROM waitlist_hits WHERE bucket = 'email'").fetchone()[0] == 0
    providers.verification = {"success": True, "hostname": "example.com", "action": "waitlist"}
    for _ in range(3):
        assert submit(browser).status_code == 200
    assert submit(browser).status_code == 429


@pytest.mark.parametrize("contact,status", [
    ({"subscribed": True, "mailingLists": {"test-list": True}}, 200),
    ({"subscribed": False, "mailingLists": {}}, 200),
    ({"subscribed": True, "mailingLists": {"test-list": False}}, 200),
    ({"subscribed": True, "mailingLists": {}}, 503),
    ({"mailingLists": {"test-list": True}}, 503),
])
def test_create_conflict_reads_once_without_retrying_write(config, contact, status):
    providers = Providers()
    reads = 0
    def conflict(request):
        nonlocal reads
        if request.method == "GET":
            reads += 1
            if reads > 1:
                providers.calls.append(request)
                return httpx.Response(200, json=[contact])
        if request.url.path.endswith("/create"):
            providers.calls.append(request)
            return httpx.Response(409, json={"success": False})
        return providers(request)
    response = submit(client(config, conflict))
    assert response.status_code == status
    if status == 200:
        assert response.json()["message"] == SUCCESS_MESSAGE
    assert reads == 2
    assert [request.method for request in providers.calls] == ["POST", "GET", "POST", "GET"]


def test_httpx_info_logging_does_not_expose_contact_lookup_email(config, caplog):
    import logging
    providers = Providers()
    with caplog.at_level(logging.INFO, logger="httpx"):
        response = submit(client(config, providers), email="private-person@gmail.com")
    assert response.status_code == 200
    assert any(request.method == "GET" and request.url.params["email"] == "private-person@gmail.com" for request in providers.calls)
    messages = "\n".join(record.getMessage() for record in caplog.records if record.name == "httpx")
    assert "private-person" not in messages
    assert "/contacts/find" not in messages
    assert "challenges.cloudflare.com" in messages
    assert "/contacts/create" in messages


def test_railway_ip_source_ignores_spoofed_forwarded_chain(config):
    from starlette.requests import Request
    from app.waitlist import _client_ip
    config = replace(config, client_ip_source="railway")
    assert config.ready
    request = Request({"type": "http", "client": ("10.0.0.1", 123), "headers": [
        (b"x-real-ip", b"192.0.2.8"), (b"x-forwarded-for", b"1.2.3.4, 10.0.0.2"),
    ]})
    assert _client_ip(request, config) == "192.0.2.8"


@pytest.mark.parametrize("headers", [
    [],
    [(b"x-real-ip", b"192.0.2.8"), (b"x-real-ip", b"192.0.2.9")],
    [(b"x-real-ip", b"192.0.2.8, 192.0.2.9")],
    [(b"x-real-ip", b"fe80::1%en0")],
    [(b"x-real-ip", b" 192.0.2.8")],
    [(b"x-real-ip", b"192.0.2.8:443")],
    [(b"x-real-ip", b"not-an-ip")],
    [(b"x-real-ip", b"")],
])
def test_railway_missing_or_ambiguous_address_falls_back_to_raw_peer(config, headers):
    from starlette.requests import Request
    from app.waitlist import _client_ip
    config = replace(config, client_ip_source="railway")
    request = Request({"type": "http", "client": ("10.0.0.1", 123), "headers": headers + [(b"x-forwarded-for", b"1.2.3.4")]})
    assert _client_ip(request, config) == "10.0.0.1"


def test_unknown_ip_source_fails_closed_and_env_defaults_to_socket(config, monkeypatch):
    monkeypatch.delenv("WAITLIST_CLIENT_IP_SOURCE", raising=False)
    assert WaitlistConfig.from_env().client_ip_source == "socket"
    monkeypatch.setenv("WAITLIST_CLIENT_IP_SOURCE", "railway")
    assert WaitlistConfig.from_env().client_ip_source == "railway"
    invalid = replace(config, client_ip_source="arbitrary-header")
    assert not invalid.ready
    assert submit(client(invalid)).status_code == 503


@pytest.mark.parametrize("email,eligible", [
    ("  APPROVED@GMAIL.COM  ", True),
    ("other@gmail.com", False),
    ("approved+alias@gmail.com", False),
])
def test_new_contact_eligibility_uses_exact_server_allowlist(config, email, eligible):
    providers = Providers()
    configured = replace(config, beta_allowed_emails=("approved@gmail.com",))
    response = submit(client(configured, providers), email=email)
    assert response.status_code == 200
    assert response.json() == {"success": True, "message": SUCCESS_MESSAGE}
    assert json.loads(providers.calls[-1].content)["pippaSignInEligible"] is eligible


def test_waitlist_allowlist_env_is_normalized_and_empty_by_default(monkeypatch):
    monkeypatch.delenv("BETA_ALLOWED_EMAILS", raising=False)
    assert WaitlistConfig.from_env().beta_allowed_emails == ()
    monkeypatch.setenv("BETA_ALLOWED_EMAILS", " APPROVED@GMAIL.COM, , second@gmail.com,APPROVED@gmail.com ")
    assert WaitlistConfig.from_env().beta_allowed_emails == ("approved@gmail.com", "second@gmail.com")


def test_success_message_confirms_signup_without_promising_email(config):
    response = submit(client(config))
    assert response.json()["message"] == (
        "Thanks for joining Pippa! Your signup has been saved."
    )


@pytest.mark.parametrize('email', ['a@yahoo.com', 'a@AOL.COM', 'a@icloud.com', 'a@me.com', 'a@outlook.com', 'a@mail.yahoo.com'])
def test_unsupported_provider_cannot_bypass_browser_validation(config, email):
    providers = Providers()
    response = submit(client(config, providers), email=email)
    assert response.status_code == 422
    assert response.json()['code'] == 'unsupported_email_provider'
    assert providers.calls == []


@pytest.mark.parametrize('email', ['person@.com', 'person@.yahoo.com', 'person@com'])
def test_malformed_domain_reports_email_error(config, email):
    providers = Providers()
    response = submit(client(config, providers), email=email)
    assert response.status_code == 400
    assert response.json()['code'] == 'invalid_email'
    assert providers.calls == []


@pytest.mark.parametrize('mode,expected_status,expected_code', [
    ('google', 200, None), ('other', 422, 'workspace_unverified'),
    ('outage', 503, 'email_check_unavailable'),
])
def test_custom_domain_requires_google_hosting_before_loops(config, mode, expected_status, expected_code):
    providers = Providers()
    dns_calls = []
    def handle(request):
        if request.url.host == 'dns.google':
            dns_calls.append(request)
            assert request.url.params['name'] == 'work.example'
            assert request.url.params['type'] == 'MX'
            assert 'authorization' not in request.headers
            assert 'person' not in str(request.url)
            if mode == 'outage':
                raise httpx.ReadTimeout('DNS timed out')
            target = 'smtp.google.com.' if mode == 'google' else 'mail.other.example.'
            return httpx.Response(200, json={'Status':0,'TC':False,'Question':[{'name':'work.example.','type':15}],
                'Answer':[{'name':'work.example.','type':15,'TTL':300,'data':'1 '+target}]})
        return providers(request)
    response = submit(client(config, handle), email='person@work.example')
    assert response.status_code == expected_status
    assert len(dns_calls) == 1
    if expected_code:
        assert response.json()['code'] == expected_code
        assert len(providers.calls) == 1  # only the human check, never Loops
    else:
        assert response.json()['success'] is True
        assert len(providers.calls) == 3


def survey_fields(**changes):
    return {
        'surveyVersion': '1', 'firstName': '  José  ',
        'schedulingTools': '["google_calendar","calendly","other"]',
        'schedulingToolsOther': '  Paper diary  ',
        'schedulingChallenge': '  Finding a time across time zones  ',
        'pippaWish': '  Arrange my meetings  ', **changes,
    }


def test_survey_profile_is_saved_atomically_with_new_contact(config):
    providers = Providers()
    response = submit(client(config, providers), fields=survey_fields())
    assert response.status_code == 200
    assert json.loads(providers.calls[-1].content) == {
        'email': 'person@gmail.com', 'source': 'Pippa waitlist', 'subscribed': True,
        'mailingLists': {config.list_id: True}, 'pippaSignInEligible': False,
        'firstName': 'José', 'pippaSchedulingTools': 'Google Calendar, Calendly, Other',
        'pippaSchedulingToolsOther': 'Paper diary',
        'pippaSchedulingChallenge': 'Finding a time across time zones',
        'pippaWish': 'Arrange my meetings', 'pippaSurveyVersion': '1',
    }


@pytest.mark.parametrize('name', ['李', 'Nguyễn', "O’Connor", 'Anne-Marie', 'Jean Luc', 'A' * 80])
def test_survey_accepts_unicode_names_and_omits_skipped_answers(config, name):
    providers = Providers()
    response = submit(client(config, providers), fields={'surveyVersion': '1', 'firstName': name})
    assert response.status_code == 200
    payload = json.loads(providers.calls[-1].content)
    assert payload['firstName'] == name
    assert payload['pippaSurveyVersion'] == '1'
    assert not set(payload).intersection({'pippaSchedulingTools', 'pippaSchedulingToolsOther', 'pippaSchedulingChallenge', 'pippaWish'})


@pytest.mark.parametrize('changes', [
    {'surveyVersion': '2'}, {'surveyVersion': ''}, {'firstName': ''}, {'firstName': '   '},
    {'firstName': 'a' * 81}, {'firstName': 'Son\nLe'}, {'firstName': '\tSon'},
    {'firstName': 'Son\x00'}, {'firstName': 'Son\u2028Le'},
    {'schedulingTools': 'not json'}, {'schedulingTools': '{}'}, {'schedulingTools': 'null'},
    {'schedulingTools': 'true'}, {'schedulingTools': '"google_calendar"'},
    {'schedulingTools': '[1]'}, {'schedulingTools': '[{}]'},
    {'schedulingTools': '["google_calendar","google_calendar"]'},
    {'schedulingTools': '["unsupported"]'}, {'schedulingTools': '[]', 'schedulingToolsOther': 'Paper'},
    {'schedulingToolsOther': 'x' * 121}, {'schedulingChallenge': 'x' * 601},
    {'pippaWish': 'x' * 601}, {'pippaHold': 'false'}, {'pippaSignInEligible': 'true'},
])
def test_invalid_survey_never_reaches_providers(config, changes):
    providers = Providers()
    response = submit(client(config, providers), fields=survey_fields(**changes))
    assert response.status_code == 400
    assert providers.calls == []


@pytest.mark.parametrize('fields', [
    {'firstName': 'Son'}, {'schedulingTools': '[]'}, {'pippaWish': ''},
    {'surveyVersion': '1'},
])
def test_survey_requires_version_and_name(config, fields):
    providers = Providers()
    assert submit(client(config, providers), fields=fields).status_code == 400
    assert providers.calls == []


def test_duplicate_survey_field_is_rejected(config):
    from urllib.parse import urlencode
    providers = Providers()
    body = urlencode({'email': 'person@gmail.com', 'cf-turnstile-response': 'token', **survey_fields()}) + '&firstName=other'
    response = client(config, providers).post('/api/waitlist', content=body, headers={'origin': config.origin, 'content-type': 'application/x-www-form-urlencoded'})
    assert response.status_code == 400 and providers.calls == []


def test_encoded_unicode_survey_fits_bounded_body(config):
    providers = Providers()
    fields = survey_fields(firstName='李' * 80, schedulingToolsOther='紙' * 120,
                           schedulingChallenge='会' * 600, pippaWish='約' * 600)
    assert submit(client(config, providers), fields=fields).status_code == 200
    assert json.loads(providers.calls[-1].content)['pippaWish'] == '約' * 600


def test_survey_body_above_24kib_is_rejected_before_provider(config):
    providers = Providers()
    body = 'email=person%40gmail.com&cf-turnstile-response=token&firstName=' + 'x' * (24 * 1024)
    response = client(config, providers).post('/api/waitlist', content=body, headers={'origin': config.origin, 'content-type': 'application/x-www-form-urlencoded'})
    assert response.status_code == 400 and providers.calls == []


@pytest.mark.parametrize('membership', [None, True, False])
def test_survey_does_not_overwrite_existing_profile_or_subscription(config, membership):
    contact = {'subscribed': True, 'mailingLists': {} if membership is None else {config.list_id: membership},
               'firstName': 'Existing', 'pippaWish': 'Existing wish', 'pippaHold': True}
    providers = Providers(contacts=[contact])
    assert submit(client(config, providers), fields=survey_fields()).status_code == 200
    writes = [call for call in providers.calls if call.url.host == 'app.loops.so' and call.method != 'GET']
    if membership is None:
        assert len(writes) == 1
        assert json.loads(writes[0].content) == {'email': 'person@gmail.com', 'mailingLists': {config.list_id: True}}
    else:
        assert writes == []


def test_survey_provider_failure_does_not_claim_saved(config):
    providers = Providers(write={'success': False})
    response = submit(client(config, providers), fields=survey_fields())
    assert response.status_code == 503 and response.json()['success'] is False


def test_survey_all_tool_options_have_human_readable_labels(config):
    providers = Providers()
    fields = survey_fields(schedulingTools=json.dumps([
        'google_calendar', 'outlook', 'apple_calendar', 'calendly',
        'cal_com', 'whatsapp', 'manual', 'other',
    ]))
    assert submit(client(config, providers), fields=fields).status_code == 200
    assert json.loads(providers.calls[-1].content)['pippaSchedulingTools'] == (
        'Google Calendar, Outlook, Apple Calendar, Calendly, Cal.com, WhatsApp, Manual, Other'
    )


def test_empty_optional_survey_answers_are_not_written(config):
    providers = Providers()
    fields = survey_fields(schedulingTools='[]', schedulingToolsOther=' ', schedulingChallenge=' ', pippaWish=' ')
    assert submit(client(config, providers), fields=fields).status_code == 200
    payload = json.loads(providers.calls[-1].content)
    assert not set(payload).intersection({'pippaSchedulingTools', 'pippaSchedulingToolsOther', 'pippaSchedulingChallenge', 'pippaWish'})


@pytest.mark.parametrize('suffix', ['&firstName=%FF', '&pippaSurveyVersion=1', '&schedulingTools=%5B%22google_calendar%22%5D&schedulingTools=%5B%5D'])
def test_malformed_encoding_and_forged_or_duplicate_survey_fields_are_rejected(config, suffix):
    providers = Providers()
    body = 'email=person%40gmail.com&cf-turnstile-response=token' + suffix
    response = client(config, providers).post('/api/waitlist', content=body, headers={'origin': config.origin, 'content-type': 'application/x-www-form-urlencoded'})
    assert response.status_code == 400 and providers.calls == []


def test_streamed_survey_body_without_content_length_is_bounded(config):
    providers = Providers()
    def chunks():
        yield b'email=person%40gmail.com&cf-turnstile-response=token&surveyVersion=1&firstName=Son&pippaWish='
        yield b'x' * (24 * 1024)
    response = client(config, providers).post('/api/waitlist', content=chunks(), headers={'origin': config.origin, 'content-type': 'application/x-www-form-urlencoded'})
    assert response.status_code == 400 and providers.calls == []


def test_clean_starter_requires_own_mailing_list(config):
    assert WaitlistConfig().list_id == ""
    providers = Providers()
    response = submit(client(replace(config, list_id=""), providers))
    assert response.status_code == 503
    assert providers.calls == []
