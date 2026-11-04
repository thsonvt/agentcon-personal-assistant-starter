"""Real SDK request regressions for language and spelling context (SON-81).

The reported voice recording is unavailable; these verify request controls, not
recognition accuracy on that recording.
"""

from email.parser import BytesParser
from email.policy import default

import httpx
import pytest
from openai import OpenAI

from app.tools.voice import Transcriber


def recording_client(transcript="Meet Quan at Marina Bay Sands Expo tomorrow at 2pm."):
    requests = []

    def handler(request):
        message = BytesParser(policy=default).parsebytes(
            f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + request.content
        )
        requests.append({
            part.get_param("name", header="content-disposition"): part.get_payload(decode=True).decode()
            for part in message.iter_parts()
        })
        return httpx.Response(200, json={"text": transcript})

    client = OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return client, requests


def test_configured_language_and_name_context_reach_real_transcription_request():
    client, requests = recording_client()
    text = Transcriber(client, language=" EN ", prompt="Quan,  Marina Bay Sands Expo").transcribe(b"OggS!", "audio/ogg")
    assert text == "Meet Quan at Marina Bay Sands Expo tomorrow at 2pm."
    assert requests[0]["language"] == "en"
    assert requests[0]["prompt"] == "Quan, Marina Bay Sands Expo"
    assert requests[0]["model"] == "whisper-1"


def test_no_preference_preserves_automatic_multilingual_transcription():
    client, requests = recording_client("Hẹn gặp Quân ngày mai.")
    assert Transcriber(client).transcribe(b"OggS!", "audio/ogg") == "Hẹn gặp Quân ngày mai."
    assert "language" not in requests[0]
    assert "prompt" not in requests[0]


def test_per_request_preferences_do_not_leak_between_users():
    client, requests = recording_client()
    transcriber = Transcriber(client)
    transcriber.transcribe(b"OggS!", "audio/ogg", language="en", prompt="Quan")
    transcriber.transcribe(b"OggS!", "audio/ogg", language="vi", prompt="Quân")
    transcriber.transcribe(b"OggS!", "audio/ogg")
    assert requests[0]["language"] == "en"
    assert requests[1]["language"] == "vi"
    assert requests[1]["prompt"] == "Quân"
    assert "language" not in requests[2]
    assert "prompt" not in requests[2]


def test_auto_and_empty_prompt_can_clear_configured_defaults_for_one_call():
    client, requests = recording_client()
    transcriber = Transcriber(client, language="en", prompt="Owner-only name")
    transcriber.transcribe(b"OggS!", "audio/ogg", language="auto", prompt="")
    transcriber.transcribe(b"OggS!", "audio/ogg")
    assert "language" not in requests[0]
    assert "prompt" not in requests[0]
    assert requests[1]["language"] == "en"
    assert requests[1]["prompt"] == "Owner-only name"


def test_context_is_bounded_without_splitting_unicode():
    client, requests = recording_client()
    context = "Quân, 東京, Marina Bay Sands Expo. " * 40
    Transcriber(client).transcribe(b"OggS!", "audio/ogg", prompt=context)
    actual = requests[0]["prompt"]
    assert actual.startswith("Quân, 東京, Marina Bay Sands Expo.")
    assert len(actual.encode("utf-8")) <= 224
    assert "�" not in actual


@pytest.mark.parametrize("language", ["English", "en-US", "12"])
def test_invalid_language_format_is_rejected_before_request(language):
    client, requests = recording_client()
    with pytest.raises(ValueError, match="language"):
        Transcriber(client, language=language)
    assert requests == []
