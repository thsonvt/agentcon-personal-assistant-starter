"""Plan 2A: voice notes from Pippa are transcribed, echoed in one line, and answered."""

import asyncio
import time
from datetime import datetime
from types import SimpleNamespace

from app.agents import assistant_instructions, build_assistant
from app.router import (
    ERROR_REPLY,
    HEARD_LIMIT,
    NON_TEXT_REPLY,
    UNKNOWN_REPLY,
    VOICE_EMPTY_REPLY,
    VOICE_ERROR_REPLY,
    IncomingMessage,
    Router,
)
from app.store import Store
from app.timeutil import SGT
from app.tools.voice import TranscriptionError
from app.tools.whatsapp import WhatsAppError
from app.webhook import extract_messages
from tests.fakes import FakeSender

OWNER = "6591234567"
STRANGER = "6580000000"
NOW = datetime(2026, 9, 28, 14, tzinfo=SGT)


def make_router(transcript="Book gym tomorrow at 6", transcribe_exc=None, reply="Created: Gym, Tue 29 Sep, 6:00pm–7:00pm", run_exc=None):
    runs, transcribed = [], []

    def transcribe(media_id):
        transcribed.append(media_id)
        if transcribe_exc:
            raise transcribe_exc
        return transcript

    async def run_owner(text):
        runs.append(text)
        if run_exc:
            raise run_exc
        return reply

    router = Router(OWNER, Store(":memory:"), FakeSender(), run_owner, lambda: NOW, transcribe=transcribe)
    return router, runs, transcribed


def voice(router, sender=OWNER, audio_id="MEDIA1", message_id="wamid.v1"):
    asyncio.run(router.handle(IncomingMessage(sender=sender, text=None, message_id=message_id, audio_id=audio_id)))


def test_voice_note_is_transcribed_marked_and_answered_with_a_heard_line():
    router, runs, transcribed = make_router()
    voice(router)
    assert transcribed == ["MEDIA1"]
    assert runs == ["[voice] Book gym tomorrow at 6"]
    assert router.sender.sent == [(OWNER, "🎙️ Heard: Book gym tomorrow at 6\n\nCreated: Gym, Tue 29 Sep, 6:00pm–7:00pm")]
    assert router.sender.typing == ["wamid.v1"]


def test_long_voice_note_echo_stays_one_short_line():
    # Review Focus: a 2-minute ramble must not turn the reply into a wall of text.
    router, runs, _ = make_router(transcript="first line\nsecond line " + "word " * 100)
    voice(router)
    heard = router.sender.sent[0][1].split("\n\n")[0]
    assert "\n" not in heard
    assert len(heard.removeprefix("🎙️ Heard: ")) == HEARD_LIMIT
    assert heard.endswith("…")
    assert runs[0].endswith("word")  # the agent still gets every word


def test_stranger_voice_notes_are_never_downloaded_or_transcribed():
    # D21: transcription costs money and would process a stranger's audio.
    router, runs, transcribed = make_router()
    voice(router, sender=STRANGER)
    assert transcribed == [] and runs == []
    assert router.sender.sent == [(STRANGER, UNKNOWN_REPLY)]


def test_transcription_failure_asks_Pippa_to_type_it():
    router, runs, _ = make_router(transcribe_exc=TranscriptionError("Transcription failed: APIConnectionError"))
    voice(router)
    assert runs == []
    assert router.sender.sent == [(OWNER, VOICE_ERROR_REPLY)]


def test_download_failure_asks_Pippa_to_type_it():
    # Review Focus: the media URL expires after 5 minutes; a slow retry can hit that.
    router, runs, _ = make_router(transcribe_exc=WhatsAppError("WhatsApp media download failed (404)."))
    voice(router)
    assert runs == []
    assert router.sender.sent == [(OWNER, VOICE_ERROR_REPLY)]


def test_silent_voice_note_gets_a_retry_prompt_not_an_agent_run():
    # Review Focus: a pocket-dial or a note of background noise transcribes to "".
    router, runs, _ = make_router(transcript="   ")
    voice(router)
    assert runs == []
    assert router.sender.sent == [(OWNER, VOICE_EMPTY_REPLY)]


def test_agent_failure_after_a_voice_note_still_shows_what_was_heard():
    router, _, _ = make_router(run_exc=RuntimeError("OpenAI down"))
    voice(router)
    assert router.sender.sent == [(OWNER, f"🎙️ Heard: Book gym tomorrow at 6\n\n{ERROR_REPLY}")]


def test_without_a_transcriber_voice_gets_the_non_text_reply():
    runs = []

    async def run_owner(text):
        runs.append(text)
        return "ok"

    router = Router(OWNER, Store(":memory:"), FakeSender(), run_owner, lambda: NOW)
    voice(router)
    assert runs == []
    assert router.sender.sent == [(OWNER, NON_TEXT_REPLY)]


def test_images_still_get_the_non_text_reply():
    router, runs, transcribed = make_router()
    asyncio.run(router.handle(IncomingMessage(sender=OWNER, text=None, message_id="wamid.img")))
    assert transcribed == [] and runs == []
    assert router.sender.sent == [(OWNER, NON_TEXT_REPLY)]


def test_voice_then_text_are_answered_in_the_order_sent():
    # The transcription happens inside the per-sender lock, so a quick text can't overtake it.
    order = []

    def transcribe(media_id):
        time.sleep(0.05)
        return "first"

    async def run_owner(text):
        order.append(text)
        return "ok"

    router = Router(OWNER, Store(":memory:"), FakeSender(), run_owner, lambda: NOW, transcribe=transcribe)

    async def both():
        first = asyncio.create_task(router.handle(IncomingMessage(OWNER, None, "m1", audio_id="MEDIA1")))
        await asyncio.sleep(0.01)
        await router.handle(IncomingMessage(OWNER, "second", "m2"))
        await first

    asyncio.run(both())
    assert order == ["[voice] first", "second"]


def test_webhook_extracts_the_audio_media_id():
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": OWNER, "id": "wamid.v1", "type": "audio",
         "audio": {"id": "MEDIA1", "mime_type": "audio/ogg; codecs=opus", "voice": True}},
    ]}}]}]}
    msg = extract_messages(payload)[0]
    assert (msg.text, msg.audio_id) == (None, "MEDIA1")


def test_text_messages_have_no_audio_id():
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": OWNER, "id": "wamid.t1", "type": "text", "text": {"body": "hi"}},
    ]}}]}]}
    assert extract_messages(payload)[0].audio_id is None


def test_instructions_explain_voice_messages():
    ctx = SimpleNamespace(context=SimpleNamespace(now=lambda: NOW, store=Store(":memory:")))
    assert "[voice]" in assistant_instructions(ctx, build_assistant("m"))
