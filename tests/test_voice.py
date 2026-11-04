"""Plan 2A: downloading a voice note from WhatsApp and turning it into text."""

import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, OpenAI

from app.config import REQUIRED, load_settings
from app.tools.voice import TranscriptionError, Transcriber
from app.tools.whatsapp import WhatsAppClient, WhatsAppError

MEDIA_URL = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=123"


def wa_with(handler):
    return WhatsAppClient("tok", "PNID", http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_download_media_asks_graph_for_the_url_then_downloads_it_with_the_token():
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers["Authorization"]))
        if request.url.host == "graph.facebook.com":
            return httpx.Response(200, json={"url": MEDIA_URL, "mime_type": "audio/ogg; codecs=opus", "file_size": 5, "id": "MEDIA1"})
        return httpx.Response(200, content=b"OggS!")

    assert wa_with(handler).download_media("MEDIA1") == (b"OggS!", "audio/ogg; codecs=opus")
    assert seen == [
        ("https://graph.facebook.com/v25.0/MEDIA1", "Bearer tok"),
        (MEDIA_URL, "Bearer tok"),
    ]


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(404, json={"error": {"message": "not found"}}),
        lambda r: httpx.Response(200, json={"url": MEDIA_URL, "mime_type": "audio/ogg"})
        if r.url.host == "graph.facebook.com" else httpx.Response(403),
    ],
    ids=["unknown-media-id", "expired-download-url"],
)
def test_download_failures_raise_whatsapp_error(handler):
    with pytest.raises(WhatsAppError):
        wa_with(handler).download_media("MEDIA1")


def test_download_network_failure_raises_whatsapp_error():
    def handler(request):
        raise httpx.ConnectError("down")

    with pytest.raises(WhatsAppError, match="download"):
        wa_with(handler).download_media("MEDIA1")


def test_oversized_media_is_refused_before_downloading():
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(200, json={"url": MEDIA_URL, "mime_type": "audio/ogg", "file_size": 50_000_000})

    with pytest.raises(WhatsAppError, match="too large"):
        wa_with(handler).download_media("MEDIA1")
    assert seen == ["graph.facebook.com"]


def test_transcriber_sends_the_audio_with_a_matching_file_name():
    # Uses the real OpenAI SDK against a fake HTTP server, so the request shape is the real one.
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "  Book gym tomorrow at 6.  "})

    client = OpenAI(api_key="k", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    text = Transcriber(client, "whisper-1").transcribe(b"OggS!", "audio/ogg; codecs=opus")
    assert text == "Book gym tomorrow at 6."
    assert seen["url"] == "https://api.openai.com/v1/audio/transcriptions"
    assert b'filename="voice.ogg"' in seen["body"]
    assert b"whisper-1" in seen["body"]
    assert b"codecs" not in seen["body"]  # the bare MIME type is sent


def test_transcriber_uses_the_configured_model():
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text="hi")

    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)))
    Transcriber(client, "gpt-4o-mini-transcribe").transcribe(b"x", "audio/mpeg")
    assert calls[0]["model"] == "gpt-4o-mini-transcribe"
    assert calls[0]["file"][0] == "voice.mp3"


def test_silence_gives_empty_text():
    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=lambda **kw: SimpleNamespace(text=None))))
    assert Transcriber(client).transcribe(b"x", "audio/ogg") == ""


def test_openai_failure_raises_transcription_error():
    def create(**kwargs):
        raise APIConnectionError(request=httpx.Request("POST", "https://api.openai.com"))

    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)))
    with pytest.raises(TranscriptionError):
        Transcriber(client).transcribe(b"x", "audio/ogg")


def test_transcribe_model_setting_is_optional():
    env = {key: "x" for key in REQUIRED} | {"OWNER_WHATSAPP": "6591234567"}
    assert load_settings(env).openai_transcribe_model == "whisper-1"
    env["OPENAI_TRANSCRIBE_MODEL"] = " gpt-4o-mini-transcribe "
    assert load_settings(env).openai_transcribe_model == "gpt-4o-mini-transcribe"


def test_transcriber_gives_up_quickly_instead_of_holding_the_queue():
    # Final review: the SDK default (600s, 2 retries) would block every later message from Pippa.
    from app.wiring import make_transcriber

    env = {key: "x" for key in REQUIRED} | {"OWNER_WHATSAPP": "6591234567"}
    transcriber = make_transcriber(load_settings(env))
    assert transcriber.client.timeout == 60
    assert transcriber.client.max_retries == 1
