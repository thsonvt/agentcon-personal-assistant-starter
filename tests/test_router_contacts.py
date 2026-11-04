import asyncio
from datetime import datetime, timedelta

from app.router import CONTACT_ERROR_REPLY, NON_TEXT_REPLY, UNKNOWN_REPLY, IncomingMessage, PartialRunError, Router
from app.store import Store
from app.timeutil import SGT
from tests.fakes import FakeSender

OWNER = "6591234567"
EDMUND = "6581234567"
NOW = datetime(2026, 10, 1, 10, tzinfo=SGT)


def setup(open_task=True, exc=None, reply="How about Fri 2 Oct, 3pm?", transcript="Friday 3pm works"):
    store = Store(":memory:")
    contact = store.add_contact("Edmund", EDMUND)
    task = None
    if open_task:
        task = store.create_loop_in_task(contact.id, "a call", 30, NOW, NOW + timedelta(days=3), NOW)
    calls = []

    async def run_owner(text):
        return "owner reply"

    async def run_contact(c, t, text):
        calls.append((c.name, t.id, t.status, text))
        if exc:
            raise exc
        return reply

    router = Router(OWNER, store, FakeSender(), run_owner, lambda: NOW, run_contact=run_contact, transcribe=lambda media_id: transcript)
    return router, calls, task


def send(router, sender, text="hi", mid="m1"):
    asyncio.run(router.handle(IncomingMessage(sender=sender, text=text, message_id=mid)))


def test_contact_with_open_invite_goes_to_delegate():
    router, calls, task = setup()
    send(router, EDMUND, "yes, what times work?")
    assert calls == [("Edmund", task.id, "negotiating", "yes, what times work?")]
    assert router.sender.sent == [(EDMUND, "How about Fri 2 Oct, 3pm?")]


def test_first_reply_moves_task_to_negotiating():
    router, _, task = setup()
    send(router, EDMUND)
    assert router.store.get_task(task.id).status == "negotiating"


def test_nudged_task_still_reaches_delegate():
    router, calls, task = setup()
    router.store.set_task_status(task.id, "nudged", NOW)
    send(router, EDMUND, "sorry, late reply")
    assert calls and router.store.get_task(task.id).status == "negotiating"


def test_saved_contact_without_open_invite_is_a_stranger():
    router, calls, _ = setup(open_task=False)
    send(router, EDMUND)
    assert calls == []
    assert router.sender.sent == [(EDMUND, UNKNOWN_REPLY)]


def test_cancelled_invite_is_a_stranger():
    router, calls, task = setup()
    router.store.set_task_status(task.id, "cancelled", NOW)
    send(router, EDMUND)
    assert calls == []
    assert router.sender.sent == [(EDMUND, UNKNOWN_REPLY)]


def test_contact_photo_gets_text_only_reply():
    router, calls, _ = setup()
    send(router, EDMUND, text=None)
    assert calls == []
    assert router.sender.sent == [(EDMUND, NON_TEXT_REPLY)]


def test_contact_voice_note_is_transcribed_like_Pippas():
    # D21: invited contacts can send voice notes too; they see what was heard before anything is booked.
    router, calls, task = setup()
    asyncio.run(router.handle(IncomingMessage(sender=EDMUND, text=None, message_id="m1", audio_id="MEDIA1")))
    assert calls == [("Edmund", task.id, "negotiating", "[voice] Friday 3pm works")]
    assert router.sender.sent == [(EDMUND, "🎙️ Heard: Friday 3pm works\n\nHow about Fri 2 Oct, 3pm?")]
    assert router.sender.typing == ["m1"]


def test_contact_without_open_invite_voice_note_is_not_transcribed():
    heard = []
    router, calls, _ = setup(open_task=False)
    router.transcribe = lambda media_id: heard.append(media_id) or "x"
    asyncio.run(router.handle(IncomingMessage(sender=EDMUND, text=None, message_id="m1", audio_id="MEDIA1")))
    assert heard == [] and calls == []
    assert router.sender.sent == [(EDMUND, UNKNOWN_REPLY)]


def test_delegate_crash_apologises_to_contact_and_warns_owner():
    router, _, _ = setup(exc=RuntimeError("OpenAI down"))
    send(router, EDMUND)
    assert (EDMUND, CONTACT_ERROR_REPLY) in router.sender.sent
    assert any(to == OWNER and "error" in body for to, body in router.sender.sent)


def test_delegate_crash_after_booking_confirms_booking_to_contact():
    router, _, _ = setup(exc=PartialRunError(["Booked: A call with Edmund, Fri 2 Oct, 3:00pm–3:30pm"]))
    send(router, EDMUND)
    to, body = router.sender.sent[-1]
    assert to == EDMUND and "You're booked" in body and "3:00pm" in body
