"""ElevenLabs speech I/O; audio stays in memory and credentials stay server-side."""

import re

import httpx

from app.tools.voice import EXTENSIONS

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_SPEECH_BYTES = 8 * 1024 * 1024
AUDIO_TYPES = {"audio/webm", "audio/mp4", "audio/ogg", "audio/wav", "audio/mpeg"}


class VoiceError(Exception):
    """A safe, user-visible speech error without provider/request contents."""


class ElevenLabsVoice:
    def __init__(self, api_key: str = "", voice_id: str = "", *, transport=None):
        if voice_id and not re.fullmatch(r"[A-Za-z0-9_-]+", voice_id):
            raise ValueError("Invalid ElevenLabs voice ID")
        self.api_key = api_key
        self.voice_id = voice_id
        self.transport = transport

    @property
    def stt_enabled(self) -> bool:
        return bool(self.api_key)

    @property
    def tts_enabled(self) -> bool:
        return bool(self.api_key and self.voice_id)

    async def realtime_token(self) -> str:
        """Mint a single-use frontend credential without exposing the API key."""
        if not self.stt_enabled:
            raise VoiceError("ElevenLabs speech recognition is not configured.")
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=20) as client:
                response = await client.post(
                    "https://api.elevenlabs.io/v1/single-use-token/realtime_scribe",
                    headers={"xi-api-key": self.api_key},
                    json={},
                )
            if response.status_code != 200:
                raise VoiceError(f"Live speech service failed (HTTP {response.status_code}).")
            result = response.json()
        except httpx.TimeoutException:
            raise VoiceError("Live speech authorization timed out. Please try again.") from None
        except httpx.HTTPError:
            raise VoiceError("Live speech service is unavailable. Please try again.") from None
        except ValueError:
            raise VoiceError("Live speech service returned an invalid response.") from None
        token = result.get("token") if isinstance(result, dict) else None
        if not isinstance(token, str) or not token.strip() or len(token) > 4096 or self.api_key in token:
            raise VoiceError("Live speech service returned an invalid response.")
        return token

    async def transcribe(self, audio: bytes, mime_type: str) -> str:
        if not self.stt_enabled:
            raise VoiceError("ElevenLabs speech recognition is not configured.")
        mime = mime_type.split(";")[0].strip().lower()
        if mime not in AUDIO_TYPES:
            raise VoiceError("Unsupported audio format.")
        if not audio or len(audio) > MAX_UPLOAD_BYTES:
            raise VoiceError("Audio must be nonempty and no larger than 5 MB.")
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=45) as client:
                response = await client.post(
                    "https://api.elevenlabs.io/v1/speech-to-text",
                    headers={"xi-api-key": self.api_key},
                    files={"file": (f"voice.{EXTENSIONS[mime]}", audio, mime)},
                    data={"model_id": "scribe_v2", "tag_audio_events": "false", "diarize": "false"},
                )
            if response.status_code != 200:
                raise VoiceError(f"Speech recognition service failed (HTTP {response.status_code}).")
            result = response.json()
        except httpx.TimeoutException:
            raise VoiceError("Speech recognition timed out. Please try again.") from None
        except httpx.HTTPError:
            raise VoiceError("Speech recognition service is unavailable. Please try again.") from None
        except ValueError:
            raise VoiceError("Speech recognition returned an invalid response.") from None
        text = result.get("text") if isinstance(result, dict) else None
        if not isinstance(text, str):
            raise VoiceError("Speech recognition returned an invalid response.")
        if not text.strip():
            raise VoiceError("No speech was detected. Please try again.")
        return text.strip()

    async def speak(self, text: str) -> bytes:
        if not self.tts_enabled:
            raise VoiceError("ElevenLabs speech playback is not configured.")
        if not text.strip() or len(text) > 8000:
            raise VoiceError("Speech text must be nonempty and no longer than 8000 characters.")
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=45) as client:
                async with client.stream(
                    "POST", f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}",
                    headers={"xi-api-key": self.api_key},
                    params={"output_format": "mp3_44100_128"},
                    json={"text": text, "model_id": "eleven_multilingual_v2"},
                ) as response:
                    if response.status_code == 402:
                        raise VoiceError('ElevenLabs requires a paid plan or more credits for this voice. Choose a default voice or review your ElevenLabs plan.')
                    if response.status_code != 200:
                        raise VoiceError(f"Speech playback service failed (HTTP {response.status_code}).")
                    audio = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        if len(audio) + len(chunk) > MAX_SPEECH_BYTES:
                            raise VoiceError("Generated speech is too large. Try a shorter message.")
                        audio.extend(chunk)
        except httpx.TimeoutException:
            raise VoiceError("Speech playback timed out. Please try again.") from None
        except httpx.HTTPError:
            raise VoiceError("Speech playback service is unavailable. Please try again.") from None
        if not audio:
            raise VoiceError("Speech playback returned empty audio.")
        return bytes(audio)
