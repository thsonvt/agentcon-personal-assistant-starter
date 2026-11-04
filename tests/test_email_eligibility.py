"""Google eligibility evidence, with all DNS responses mocked."""

import asyncio

import httpx
import pytest

from app.email_eligibility import GOOGLE_MX, check_google_hosting, known_unsupported


def dns(*targets, status=0, name="example.com."):
    return {
        "Status": status, "TC": False,
        "Question": [{"name": name, "type": 15}],
        "Answer": [{"name": name, "type": 15, "TTL": 300, "data": target} for target in targets],
    }


def check(domain, payload=None, *, status=200, handler=None):
    requests = []

    def respond(request):
        requests.append(request)
        if handler:
            return handler(request)
        return httpx.Response(status, json=payload)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
            return await check_google_hosting(client, domain)

    return asyncio.run(run()), requests


@pytest.mark.parametrize("domain", ["gmail.com", "googlemail.com", "GMAIL.COM."])
def test_personal_google_needs_no_dns(domain):
    assert check(domain) == ("supported", [])


@pytest.mark.parametrize("domain", ["yahoo.com", "YAHOO.COM.SG.", "aol.com", "icloud.com", "me.com", "mac.com", "hotmail.com", "outlook.com", "live.com", "proton.me", "mail.yahoo.co.uk"])
def test_known_providers_are_blocked_without_dns(domain):
    assert known_unsupported(domain)
    assert check(domain) == ("unsupported", [])


@pytest.mark.parametrize("domain", ["notyahoo.com", "yahoo.com.example.com", "icloud.com.evil.example"])
def test_provider_matching_respects_domain_labels(domain):
    assert not known_unsupported(domain)


@pytest.mark.parametrize("target", sorted(GOOGLE_MX))
def test_official_modern_and_legacy_google_routes(target):
    result, requests = check("ExAmPlE.CoM.", dns("10 " + target.upper() + "."))
    assert result == "supported"
    assert len(requests) == 1
    request = requests[0]
    assert request.url.host == "dns.google" and request.url.path == "/resolve"
    assert dict(request.url.params) == {"name": "example.com", "type": "MX", "edns_client_subnet": "0.0.0.0/0"}
    assert request.extensions["timeout"]["read"] == 3.0
    assert "@" not in str(request.url)


@pytest.mark.parametrize("targets", [
    ["1 smtp.google.com", "10 backup.example.net"],
    ["1 aspmx.l.google.com.", "1 alt1.aspmx.l.google.com.", "5 alt2.aspmx.l.google.com."],
])
def test_all_primary_routes_google_is_sufficient(targets):
    assert check("example.com", dns(*targets))[0] == "supported"


@pytest.mark.parametrize("targets", [
    ["10 smtp.google.com.evil.example"], ["10 evilsmtp.google.com"],
    ["10 mail.google.com"], ["10 alt9.aspmx.l.google.com"],
    ["1 gateway.mimecast.com", "10 smtp.google.com"],
    ["1 smtp.google.com", "1 primary.example.net"],
    ["0 ."], [], ["1 mx.proofpoint.com"],
])
def test_unproven_custom_domains_are_unverified(targets):
    assert check("example.com", dns(*targets))[0] == "unverified"


def test_nxdomain_and_missing_answer_are_unverified():
    assert check("example.com", dns(status=3))[0] == "unverified"
    payload = dns()
    del payload["Answer"]
    assert check("example.com", payload)[0] == "unverified"


def test_cname_requires_manual_review():
    payload = dns("10 smtp.google.com")
    payload["Answer"].append({"name": "example.com.", "type": 5, "data": "other.example.com."})
    assert check("example.com", payload)[0] == "unverified"


def test_unrelated_answer_is_not_evidence():
    payload = dns("10 smtp.google.com")
    payload["Answer"][0]["name"] = "other.example.com."
    assert check("example.com", payload)[0] == "unverified"


@pytest.mark.parametrize("payload", [
    [], None, {}, {"Status": 0},
    {**dns(), "Status": False}, {**dns(), "Status": "0"},
    {**dns(), "Status": 2}, {**dns(), "Status": 5},
    {**dns(), "TC": True}, {**dns(), "TC": "false"},
    {**dns(), "Question": []}, {**dns(), "Question": [None]},
    {**dns(), "Question": [{"name": "other.com", "type": 15}]},
    {**dns(), "Question": [{"name": "example.com", "type": 1}]},
    {**dns(), "Answer": None}, {**dns(), "Answer": [None]},
    {**dns(), "Answer": [{"name": "example.com", "type": "15", "data": "1 smtp.google.com"}]},
    {**dns("1 smtp.google.com"), "Answer": [{"name": "example.com", "type": 15, "data": "1 smtp.google.com", "TTL": -1}]},
])
def test_malformed_schema_and_dns_errors_are_unavailable(payload):
    assert check("example.com", payload)[0] == "unavailable"


@pytest.mark.parametrize("value", ["smtp.google.com", "1 smtp.google.com extra", "-1 smtp.google.com", "65536 smtp.google.com", "1 https://smtp.google.com", "1 smtp.google.com..", "1 .", "1 smtp..google.com", "1 127.0.0.1"])
def test_malformed_mx_is_unavailable(value):
    assert check("example.com", dns(value))[0] == "unavailable"


@pytest.mark.parametrize("domain", ["", "com", ".com", ".yahoo.com", "foo..com", "a@gmail.com", "https://example.com", "example.com/path", " example.com", "example.com ", "-example.com", "example-.com", "a" * 64 + ".com", "127.0.0.1", "[::1]", "exämple.com"])
def test_invalid_domain_never_reaches_resolver(domain):
    assert check(domain) == ("unverified", [])


@pytest.mark.parametrize("status", [302, 400, 429, 500])
def test_http_failure_never_allows(status):
    assert check("example.com", dns("1 smtp.google.com"), status=status)[0] == "unavailable"


def test_redirects_never_follow_even_with_redirecting_client():
    result, requests = check("example.com", handler=lambda _: httpx.Response(302, headers={"location": "https://evil.example/"}))
    assert result == "unavailable" and len(requests) == 1


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout])
def test_network_failure_is_unavailable(error):
    def fail(request):
        raise error("resolver failed", request=request)
    assert check("example.com", handler=fail)[0] == "unavailable"


def test_non_json_and_oversized_responses_are_unavailable():
    assert check("example.com", handler=lambda _: httpx.Response(200, text="not json"))[0] == "unavailable"
    assert check("example.com", handler=lambda _: httpx.Response(200, text=" " * 65537))[0] == "unavailable"
