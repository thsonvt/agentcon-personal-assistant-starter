import json

import httpx
import pytest

from app.tools.whatsapp import MAX_TEXT, WhatsAppClient, WhatsAppError, WindowClosedError


def client_with(handler):
    return WhatsAppClient("tok", "PNID", http=httpx.Client(transport=httpx.MockTransport(handler)))


def ok(request):
    return httpx.Response(200, json={"messages": [{"id": "wamid.sent"}]})


def test_send_text_posts_expected_payload():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return ok(request)

    assert client_with(handler).send_text("6591234567", "Hello") == "wamid.sent"
    assert seen["url"] == "https://graph.facebook.com/v25.0/PNID/messages"
    assert seen["auth"] == "Bearer tok"
    assert seen["body"] == {
        "messaging_product": "whatsapp",
        "to": "6591234567",
        "type": "text",
        "text": {"body": "Hello"},
    }


def test_long_text_is_clipped_to_whatsapp_limit():
    # Review Focus: WhatsApp rejects text over 4096 characters.
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)["text"]["body"]
        return ok(request)

    client_with(handler).send_text("65", "x" * 5000)
    assert len(seen["body"]) == MAX_TEXT
    assert seen["body"].endswith("…")


def test_send_template_payload():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return ok(request)

    client_with(handler).send_template("6581234567", "loop_in_intro", ["Edmund", "a 30-minute call"])
    template = seen["body"]["template"]
    assert template["name"] == "loop_in_intro"
    assert template["language"] == {"code": "en"}
    assert template["components"][0]["parameters"] == [
        {"type": "text", "text": "Edmund"},
        {"type": "text", "text": "a 30-minute call"},
    ]


def test_window_closed_error_is_recognised():
    client = client_with(
        lambda r: httpx.Response(400, json={"error": {"code": 131047, "message": "Re-engagement message"}})
    )
    with pytest.raises(WindowClosedError):
        client.send_text("65", "hi")


def test_other_api_errors_raise_whatsapp_error():
    client = client_with(lambda r: httpx.Response(401, json={"error": {"code": 190, "message": "Invalid token"}}))
    with pytest.raises(WhatsAppError, match="error \\(401\\)") as err:
        client.send_text("65", "hi")
    assert not isinstance(err.value, WindowClosedError)
    assert err.value.code == 190


def test_network_failure_raises_whatsapp_error():
    def handler(request):
        raise httpx.ConnectError("down")

    with pytest.raises(WhatsAppError, match="reach"):
        client_with(handler).send_text("65", "hi")


@pytest.mark.parametrize("payload", [
    {}, [], None, True, {"messages": None}, {"messages": {}}, {"messages": []},
    {"messages": [None]}, {"messages": [{}]}, {"messages": [{"id": None}]},
    {"messages": [{"id": 123}]}, {"messages": [{"id": ""}]},
    {"messages": [{"id": " "}]}, {"messages": [{"id": "x" * 513}]},
])
@pytest.mark.parametrize("method", ["text", "template"])
def test_send_requires_a_valid_message_id(payload, method):
    client = client_with(lambda r: httpx.Response(200, content=json.dumps(payload)))
    with pytest.raises(WhatsAppError, match="response"):
        if method == "text":
            client.send_text("65", "hello")
        else:
            client.send_template("65", "welcome", [])


@pytest.mark.parametrize("status", [200, 400])
def test_non_json_response_has_safe_error(status):
    client = client_with(lambda r: httpx.Response(status, content=b"private-provider-body tok"))
    with pytest.raises(WhatsAppError) as err:
        client.send_text("65", "hello")
    assert "private-provider-body" not in str(err.value)
    assert "tok" not in str(err.value)


@pytest.mark.parametrize("payload", [None, [], True, {"error": None}, {"error": []}, {"error": "private"},
                                      {"error": {"code": [], "message": "private-provider-body tok"}},
                                      {"error": {"code": True, "message": {"private": "tok"}}}])
def test_malformed_error_shape_has_safe_error(payload):
    client = client_with(lambda r: httpx.Response(400, content=json.dumps(payload)))
    with pytest.raises(WhatsAppError) as err:
        client.send_text("65", "hello")
    assert err.value.code is None
    assert "private" not in str(err.value) and "tok" not in str(err.value)


def test_provider_error_never_echoes_untrusted_body():
    client = client_with(lambda r: httpx.Response(400, json={"error": {"code": 131030, "message": "private token tok"}}))
    with pytest.raises(WhatsAppError) as err:
        client.send_text("65", "hello")
    assert err.value.code == 131030
    assert "private" not in str(err.value) and "tok" not in str(err.value)


def test_typing_success_does_not_require_message_id():
    client_with(lambda r: httpx.Response(200, json={"success": True})).mark_read_typing("wamid.1")
