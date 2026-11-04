"""Speech to text for WhatsApp voice notes, using OpenAI's transcription API."""

from openai import OpenAI, OpenAIError

DEFAULT_TRANSCRIBE_MODEL = "whisper-1"
# Whisper accepts up to 224 tokens of spelling context. A UTF-8 byte ceiling is
# deliberately conservative across languages and avoids adding a tokenizer.
MAX_TRANSCRIPTION_PROMPT_BYTES = 224

# OpenAI works out the audio format from the file name, so give it the right extension.
EXTENSIONS = {
    "audio/ogg": "ogg",  # WhatsApp voice notes: "audio/ogg; codecs=opus"
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/aac": "m4a",
    "audio/amr": "amr",
    "audio/wav": "wav",
    "audio/webm": "webm",
}


class TranscriptionError(Exception):
    """The audio couldn't be turned into text."""


def _language_hint(language: str | None) -> str | None:
    value = (language or "").strip().lower()
    if value in {"", "auto"}:
        return None
    if len(value) != 2 or not value.isascii() or not value.isalpha():
        raise ValueError("Transcription language must be an ISO 639-1 code or 'auto'")
    return value


def _spelling_context(prompt: str | None) -> str:
    text = " ".join((prompt or "").split())
    return text.encode("utf-8")[:MAX_TRANSCRIPTION_PROMPT_BYTES].decode("utf-8", errors="ignore").strip()


class Transcriber:
    def __init__(
        self, client: OpenAI, model: str = DEFAULT_TRANSCRIBE_MODEL, *,
        language: str | None = None, prompt: str | None = None,
    ):
        self.client = client
        self.model = model
        self.language = _language_hint(language)
        self.prompt = _spelling_context(prompt)

    def transcribe(
        self, audio: bytes, mime_type: str, *,
        language: str | None = None, prompt: str | None = None,
    ) -> str:
        """Return spoken words without translation; may be "" for silence.

        None inherits the instance setting. Use language="auto" or prompt=""
        to clear defaults for one request. Hints never mutate shared settings.
        Supply only this user's known names/context, not another user's history.
        Spelling hints improve recognition but do not verify attendee identity.
        """
        base = mime_type.split(";")[0].strip().lower()
        filename = f"voice.{EXTENSIONS.get(base, 'ogg')}"
        language_hint = self.language if language is None else _language_hint(language)
        context = self.prompt if prompt is None else _spelling_context(prompt)
        options = {}
        if language_hint:
            options["language"] = language_hint
        if context:
            options["prompt"] = context
        try:
            result = self.client.audio.transcriptions.create(
                model=self.model, file=(filename, audio, base), **options,
            )
        except OpenAIError as exc:
            raise TranscriptionError(f"Transcription failed: {type(exc).__name__}") from exc
        return (result.text or "").strip()
