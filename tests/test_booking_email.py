"""Transactional host notices without live provider requests."""

import json

import httpx
import pytest

from app.booking_email import LoopsBookingEmail


PAYLOAD = {"email": "host@example.com", "dataVariables": {"guestName": "Guest", "start": "2026-10-10T09:00:00Z"}}
REQUEST_ID = "a" * 48


def test_sends_only_transactional_fields_with_stable_idempotency_and_timeout():
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(200, json={"success": True})

    payload = {**PAYLOAD, "addToAudience": True, "mailingLists": {"marketing": True}}
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        sender = LoopsBookingEmail("private-key", "host-template", client=client)
        assert sender.ready is True
        assert sender.send(payload, REQUEST_ID) == "accepted"
        assert sender.send(payload, REQUEST_ID) == "accepted"
        assert not client.is_closed

    assert len(calls) == 2
    for request in calls:
        assert request.method == "POST"
        assert str(request.url) == "https://app.loops.so/api/v1/transactional"
        assert request.headers["authorization"] == "Bearer private-key"
        assert request.headers["idempotency-key"] == "pippa-host-" + REQUEST_ID
        assert request.extensions["timeout"] == {"connect": 10, "read": 10, "write": 10, "pool": 10}
        assert json.loads(request.content) == {
            **PAYLOAD, "transactionalId": "host-template", "addToAudience": False,
        }
    assert payload["addToAudience"] is True


@pytest.mark.parametrize("body", [[], None, {}, {"success": False}, {"success": "true"}, {"success": 1}])
def test_success_status_without_explicit_acceptance_is_unknown(body):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))) as client:
        assert LoopsBookingEmail("key", "template", client).send(PAYLOAD, REQUEST_ID) == "unknown"


def test_malformed_provider_json_is_unknown():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"not json"))) as client:
        assert LoopsBookingEmail("key", "template", client).send(PAYLOAD, REQUEST_ID) == "unknown"


def test_replays_template_id_frozen_in_receipt():
    calls = []
    def provider(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={'success': True})
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        sender = LoopsBookingEmail('key', 'new-template', client)
        assert sender.send({**PAYLOAD, 'transactionalId': 'original-template'}, REQUEST_ID) == 'accepted'
    assert calls[0]['transactionalId'] == 'original-template'


@pytest.mark.parametrize("status, expected", [(400, "failed"), (401, "failed"), (422, "failed"), (429, "failed"), (409, "unknown"), (500, "unknown"), (503, "unknown"), (201, "unknown")])
def test_provider_status_classification(status, expected):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, json={"success": True}))) as client:
        assert LoopsBookingEmail("key", "template", client).send(PAYLOAD, REQUEST_ID) == expected


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError])
def test_transport_failure_is_unknown_without_retry(error):
    calls = []

    def provider(request):
        calls.append(request)
        raise error("provider request interrupted", request=request)

    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        assert LoopsBookingEmail("key", "template", client).send(PAYLOAD, REQUEST_ID) == "unknown"
    assert len(calls) == 1


def test_redirect_is_not_followed_even_with_redirects_enabled_on_injected_client():
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://other.example/mail"})

    with httpx.Client(transport=httpx.MockTransport(provider), follow_redirects=True) as client:
        assert LoopsBookingEmail("key", "template", client).send(PAYLOAD, REQUEST_ID) == "unknown"
    assert len(calls) == 1


@pytest.mark.parametrize("key, template", [("", "template"), ("key", ""), ("", "")])
def test_unconfigured_sender_never_calls_provider(key, template):
    def provider(request):
        pytest.fail("Unconfigured sender must not make a provider request")

    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        sender = LoopsBookingEmail(key, template, client)
        assert sender.ready is False
        assert sender.send(PAYLOAD, REQUEST_ID) == "unconfigured"
