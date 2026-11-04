import asyncio
import json

import httpx
import pytest

from app.tools.elevenlabs import ElevenLabsVoice, VoiceError


def voice(handler, **kwargs):
    return ElevenLabsVoice("secret-key", "voice_123", transport=httpx.MockTransport(handler), **kwargs)


def test_configuration_and_voice_id_validation():
    assert not ElevenLabsVoice().stt_enabled
    assert ElevenLabsVoice("key").stt_enabled
    assert not ElevenLabsVoice("key").tts_enabled
    assert ElevenLabsVoice("key", "voice-123").tts_enabled
    with pytest.raises(ValueError, match="voice ID"):
        ElevenLabsVoice("key", "../other?query=x")


@pytest.mark.parametrize("mime,extension", [("audio/webm;codecs=opus", "webm"), ("audio/mp4", "m4a"), ("audio/ogg", "ogg"), ("audio/wav", "wav"), ("audio/mpeg", "mp3")])
def test_transcribe_request_and_trimmed_result(mime, extension):
    def handler(request):
        assert str(request.url) == "https://api.elevenlabs.io/v1/speech-to-text"
        assert request.headers["xi-api-key"] == "secret-key"
        assert f'filename="voice.{extension}"'.encode() in request.content
        assert f"Content-Type: {mime.split(';')[0]}".encode() in request.content
        assert b"scribe_v2" in request.content
        assert b'name="tag_audio_events"\r\n\r\nfalse' in request.content
        assert b'name="diarize"\r\n\r\nfalse' in request.content
        return httpx.Response(200, json={"text": "  Hello Pippa  "})

    assert asyncio.run(voice(handler).transcribe(b"audio", mime)) == "Hello Pippa"


def test_speak_request_and_audio_result():
    def handler(request):
        assert request.url.path == "/v1/text-to-speech/voice_123"
        assert request.url.params["output_format"] == "mp3_44100_128"
        assert request.headers["xi-api-key"] == "secret-key"
        assert json.loads(request.content) == {"text": "Hello", "model_id": "eleven_multilingual_v2"}
        return httpx.Response(200, content=b"mp3-audio", headers={"content-type": "audio/mpeg"})

    assert asyncio.run(voice(handler).speak("Hello")) == b"mp3-audio"


@pytest.mark.parametrize("audio,mime", [(b"", "audio/webm"), (b"x" * (5 * 1024 * 1024 + 1), "audio/webm"), (b"x", "text/plain")])
def test_invalid_audio_is_rejected_before_network(audio, mime):
    def handler(request):
        pytest.fail("Invalid input reached provider")

    with pytest.raises(VoiceError):
        asyncio.run(voice(handler).transcribe(audio, mime))


@pytest.mark.parametrize("text", ["", "   ", "x" * 8001])
def test_invalid_text_is_rejected_before_network(text):
    with pytest.raises(VoiceError):
        asyncio.run(voice(lambda request: pytest.fail("Invalid input reached provider")).speak(text))


def test_missing_configuration_has_actionable_errors():
    with pytest.raises(VoiceError, match="configured"):
        asyncio.run(ElevenLabsVoice().transcribe(b"x", "audio/webm"))
    with pytest.raises(VoiceError, match="configured"):
        asyncio.run(ElevenLabsVoice("key").speak("Hello"))


@pytest.mark.parametrize("status", [401, 403, 429, 500])
@pytest.mark.parametrize("operation", ["transcribe", "speak"])
def test_provider_failures_do_not_expose_secrets_or_response(status, operation):
    adapter = voice(lambda request: httpx.Response(status, text="secret-key private transcript"))
    with pytest.raises(VoiceError) as error:
        asyncio.run(adapter.transcribe(b"x", "audio/webm") if operation == "transcribe" else adapter.speak("private transcript"))
    assert "secret-key" not in str(error.value)
    assert "private transcript" not in str(error.value)
    assert str(status) in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize("exception", [httpx.ReadTimeout, httpx.ConnectError])
def test_transport_errors_are_sanitized(exception):
    def handler(request):
        raise exception("secret-key private transcript", request=request)

    with pytest.raises(VoiceError) as error:
        asyncio.run(voice(handler).transcribe(b"x", "audio/webm"))
    assert "secret-key" not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize("response", [httpx.Response(200, text="not json"), httpx.Response(200, json=[]), httpx.Response(200, json={"text": 12}), httpx.Response(200, json={"text": "  "})])
def test_invalid_or_empty_transcription(response):
    with pytest.raises(VoiceError):
        asyncio.run(voice(lambda request: response).transcribe(b"x", "audio/webm"))


@pytest.mark.parametrize("content", [b"", b"x" * (8 * 1024 * 1024 + 1)])
def test_empty_or_oversized_speech_is_rejected(content):
    with pytest.raises(VoiceError):
        asyncio.run(voice(lambda request: httpx.Response(200, content=content, headers={"content-type": "audio/mpeg"})).speak("Hello"))


def test_voice_payment_error_explains_the_user_action_without_leaking_provider_body():
    with pytest.raises(VoiceError) as error:
        asyncio.run(voice(lambda request: httpx.Response(402, text='private provider body')).speak('Hello'))
    assert 'paid' in str(error.value) and 'voice' in str(error.value)
    assert 'private provider' not in str(error.value)


def test_realtime_token_request_and_result():
    def handler(request):
        assert request.method == "POST"
        assert str(request.url) == "https://api.elevenlabs.io/v1/single-use-token/realtime_scribe"
        assert request.headers["xi-api-key"] == "secret-key"
        assert json.loads(request.content) == {}
        assert request.extensions["timeout"]["read"] == 20
        return httpx.Response(200, json={"token": "sutkn_single_use"})

    assert asyncio.run(voice(handler).realtime_token()) == "sutkn_single_use"


def test_realtime_token_requires_configuration():
    with pytest.raises(VoiceError, match="configured"):
        asyncio.run(ElevenLabsVoice().realtime_token())


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_realtime_token_status_error_is_sanitized(status):
    with pytest.raises(VoiceError) as error:
        asyncio.run(voice(lambda request: httpx.Response(status, text="secret-key private-token")).realtime_token())
    assert str(status) in str(error.value)
    assert "secret-key" not in str(error.value)
    assert "private-token" not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize("exception", [httpx.ReadTimeout, httpx.ConnectError])
def test_realtime_token_transport_error_is_sanitized(exception):
    def handler(request):
        raise exception("secret-key private-token", request=request)

    with pytest.raises(VoiceError) as error:
        asyncio.run(voice(handler).realtime_token())
    assert "secret-key" not in str(error.value)
    assert "private-token" not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize("response", [
    httpx.Response(200, text="not json"),
    httpx.Response(200, json=[]),
    httpx.Response(200, json={}),
    httpx.Response(200, json={"token": 12}),
    httpx.Response(200, json={"token": "  "}),
    httpx.Response(200, json={"token": "x" * 4097}),
    httpx.Response(200, json={"token": "secret-key"}),
    httpx.Response(200, json={"token": "prefix-secret-key-suffix"}),
])
def test_realtime_token_rejects_invalid_or_reflected_credentials(response):
    with pytest.raises(VoiceError, match="invalid response"):
        asyncio.run(voice(lambda request: response).realtime_token())
