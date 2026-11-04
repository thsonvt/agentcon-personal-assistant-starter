import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.store import Store
from app.timeutil import SGT
from app.router import IncomingMessage
from tests.fakes import FakeSender

NOW = datetime(2026, 10, 3, 18, tzinfo=SGT)


class Users:
    def __init__(self):
        self.people = {1: SimpleNamespace(id=1, phone='6590000001', status='active', is_admin=True),
                       2: SimpleNamespace(id=2, phone='6590000002', status='active', is_admin=False)}

    def get(self, ident):
        return self.people.get(ident)

    def by_phone(self, phone):
        return next((u for u in self.people.values() if u.phone == phone), None)

    def list(self):
        return list(self.people.values())


class Model:
    async def extract_image(self, data, mime):
        return 'Evaluation needs representative tasks.'

    async def classify(self, text, title):
        return 'review' if 'dinner' in text.lower() else 'observation'

    async def summarize(self, title, items):
        return {'takeaways': [{'text': 'Evaluate representative tasks.', 'source_ids': [i['id'] for i in items]}],
                'observations': [], 'questions': []}


class Media:
    def download_media(self, media_id):
        return (b'\x89PNG\r\n\x1a\nslide', 'image/png') if media_id == 'image' else (b'OggSvoice', 'audio/ogg')


class Transcriber:
    def transcribe(self, data, mime, **kwargs):
        return 'My observation about evaluation.'


@pytest.fixture
def capture(tmp_path):
    from app.capture_store import CaptureStore
    from app.conference_capture import ConferenceCapture
    store = Store(str(tmp_path / 'capture.db'))
    users, sender = Users(), FakeSender()
    records = CaptureStore(store, lambda: NOW)
    pages = SimpleNamespace(link=lambda session: f"https://pippa.example/captures/{session['id']}?token=private")
    service = ConferenceCapture(records, users, store, sender, Model(), Transcriber(), Media(),
                                lambda: NOW, pages)
    return SimpleNamespace(store=store, records=records, users=users, sender=sender, service=service)


def incoming(text=None, ident='m1', user=1, **kw):
    return IncomingMessage(sender=f'659000000{user}', text=text, message_id=ident, **kw)


def start(capture):
    assert capture.service.ingest(incoming("Pippa, start collecting notes for Jason's talk"))
    return capture.records.current(1)


def drain(capture):
    asyncio.run(capture.service.drain_once())


def test_full_capture_retains_sources_and_finishes_after_pending_items(capture):
    session = start(capture)
    assert session['title'] == "Jason's talk"
    assert capture.service.ingest(incoming('Useful observation', 'text'))
    assert capture.service.ingest(incoming(None, 'image-msg', image_id='image', caption='This chart is useful', kind='image'))
    assert capture.service.ingest(incoming(None, 'voice-msg', audio_id='voice', kind='audio'))
    assert capture.service.ingest(incoming('finish this session', 'stop'))
    assert capture.records.get(1, session['id'])['state'] == 'closing'
    assert len(capture.records.pending_items()) == 3
    drain(capture)
    ready = capture.records.get(1, session['id'])
    assert ready['state'] == 'ready'
    assert len(capture.records.items(1, session['id'])) == 3
    assert all(i['status'] == 'ready' for i in capture.records.items(1, session['id']))
    assert any('notes' in body.lower() and 'https://' in body for _, body in capture.sender.sent)


def test_deduplication_happens_before_any_worker(capture):
    session = start(capture)
    note = incoming('A note', 'duplicate')
    assert capture.service.ingest(note)
    assert capture.service.ingest(note)
    assert len(capture.records.items(1, session['id'])) == 1


def test_assistant_escape_account_commands_and_unknown_users_are_not_captured(capture):
    session = start(capture)
    for text in ['assistant: book a meeting tomorrow', 'delete my data', 'help', 'status', "what's my next meeting?"]:
        assert capture.service.ingest(incoming(text, text)) is False
    assert capture.service.ingest(IncomingMessage('unknown', 'start collecting notes', 'stranger')) is False
    assert capture.records.items(1, session['id']) == []


def test_unrelated_notes_preserved_for_review_and_can_be_included(capture):
    session = start(capture)
    capture.service.ingest(incoming('Dinner at seven', 'dinner'))
    drain(capture)
    item = capture.records.items(1, session['id'])[0]
    assert item['category'] == 'review' and not item['included']
    capture.service.ingest(incoming(f"include item {item['id']}", 'include'))
    assert capture.records.items(1, session['id'])[0]['included']
    capture.service.ingest(incoming('finish this session', 'finish'))
    drain(capture)
    capture.service.ingest(incoming('exclude that', 'exclude'))
    assert not capture.records.get(1, session['id'])['summary_json']
    drain(capture)
    assert capture.records.get(1, session['id'])['state'] == 'ready'


def test_failed_media_visible_and_retry_keeps_original_item(capture):
    session = start(capture)
    capture.service.ingest(incoming(None, 'image-msg', image_id='image', kind='image'))
    capture.service.ingest(incoming('finish this session', 'finish'))
    async def fail(*args):
        raise RuntimeError('secret provider payload')
    capture.service.model.extract_image = fail
    drain(capture)
    item = capture.records.items(1, session['id'])[0]
    assert item['status'] == 'failed' and item['media']
    assert 'secret' not in str(item['error'])
    capture.service.model = Model()
    capture.service.ingest(incoming('retry capture', 'retry'))
    drain(capture)
    assert len(capture.records.items(1, session['id'])) == 1
    assert capture.records.items(1, session['id'])[0]['status'] == 'ready'


def test_restart_processes_already_accepted_capture(capture):
    from app.conference_capture import ConferenceCapture
    from app.capture_store import CaptureStore
    session = start(capture)
    capture.service.ingest(incoming('Persist me before acknowledgement', 'note'))
    capture.service.ingest(incoming('finish this session', 'stop'))
    restarted = ConferenceCapture(CaptureStore(capture.store, lambda: NOW), capture.users, capture.store,
        capture.sender, Model(), Transcriber(), Media(), lambda: NOW, capture.service.pages)
    asyncio.run(restarted.drain_once())
    assert capture.records.get(1, session['id'])['state'] == 'ready'


def test_disabled_user_is_not_processed(capture):
    session = start(capture)
    capture.service.ingest(incoming('Private', 'note'))
    capture.users.people[1].status = 'disabled'
    drain(capture)
    assert capture.records.items(1, session['id'])[0]['status'] == 'pending'
    assert not capture.sender.sent


def test_confirmed_capture_deletion_is_scoped(capture):
    session = start(capture)
    capture.service.ingest(incoming('delete capture', 'delete'))
    assert capture.records.get(1, session['id'])
    capture.service.ingest(incoming('confirm delete capture', 'confirm'))
    assert capture.records.get(1, session['id']) is None


def test_cross_user_correction_does_not_change_note(capture):
    session = start(capture)
    capture.service.ingest(incoming('Secret', 'private'))
    item = capture.records.items(1, session['id'])[0]
    capture.service.ingest(incoming('start collecting notes for Other', 'other-start', user=2))
    capture.service.ingest(incoming(f"exclude item {item['id']}", 'attack', user=2))
    assert capture.records.items(1, session['id'])[0]['included']


def test_failed_notifications_are_not_retried_automatically(capture):
    start(capture)
    capture.sender.exc = RuntimeError('network failed')
    drain(capture)
    capture.sender.exc = None
    drain(capture)
    assert not capture.sender.sent


def test_capture_accepts_more_than_chat_rate_limit_but_bounds_session(capture):
    session = start(capture)
    for i in range(105):
        assert capture.service.ingest(incoming('note', f'n-{i}'))
    assert len(capture.records.items(1, session['id'])) == 100


def test_late_image_is_not_silently_added_to_closed_session(capture):
    session = start(capture)
    capture.service.ingest(incoming('finish this session', 'stop'))
    assert capture.service.ingest(incoming(None, 'late', image_id='image', kind='image'))
    assert capture.records.items(1, session['id']) == []
    drain(capture)
    assert any('closed' in body.lower() for _, body in capture.sender.sent)


def test_disabling_during_transcription_prevents_further_model_calls(capture):
    start(capture)
    capture.service.ingest(incoming(None, 'voice', audio_id='voice', kind='audio'))
    calls = []
    def transcribe(*args):
        capture.users.people[1].status = 'disabled'
        return 'Private transcription'
    async def classify(*args):
        calls.append(args)
        return 'observation'
    capture.service.transcriber.transcribe = transcribe
    capture.service.model.classify = classify
    drain(capture)
    assert calls == []


def test_correction_racing_failed_summary_still_regenerates(capture):
    session = start(capture)
    capture.service.ingest(incoming('One', 'one'))
    capture.service.ingest(incoming('Two', 'two'))
    capture.service.ingest(incoming('finish this session', 'finish'))
    async def correct_then_fail(title, items):
        capture.service.ingest(incoming(f"exclude item {items[-1]['id']}", 'correction'))
        raise RuntimeError('provider interrupted')
    capture.service.model.summarize = correct_then_fail
    drain(capture)
    assert capture.records.get(1, session['id'])['state'] == 'closing'
    capture.service.model = Model()
    drain(capture)
    assert capture.records.get(1, session['id'])['state'] == 'ready'


def test_captured_duplicate_never_falls_through_after_routing_changes(capture, monkeypatch):
    start(capture)
    message = incoming('My observation', 'note')
    capture.service.ingest(message)
    monkeypatch.setattr(capture.store, 'find_open_invite', lambda phone: ('contact', 'invite'))
    assert capture.service.ingest(message) is True


def test_session_deletion_erases_queued_summary_notifications(capture):
    session = start(capture)
    capture.service.ingest(incoming('My private note', 'private'))
    capture.service.ingest(incoming('finish this session', 'finish'))
    drain(capture)
    capture.service.ingest(incoming('delete capture', 'delete'))
    capture.service.ingest(incoming('confirm delete capture', 'confirm'))
    rows = capture.records.conn.execute('SELECT body FROM capture_notifications').fetchall()
    assert not any(session['id'] in r['body'] or 'representative' in r['body'] for r in rows)


def test_deleted_notice_snapshot_is_not_sent_after_prior_send_await(capture):
    session = start(capture)
    capture.service.ingest(incoming('capture status', 'status'))
    sent = []
    def send_then_delete(phone, body):
        sent.append(body)
        capture.records.delete(1, session['id'])
    capture.sender.send_text = send_then_delete
    drain(capture)
    assert len(sent) == 1


def test_explicit_include_before_classification_does_not_claim_excluded(capture):
    session = start(capture)
    capture.service.ingest(incoming('Dinner observation', 'note'))
    item = capture.records.items(1, session['id'])[0]
    capture.service.ingest(incoming(f"include item {item['id']}", 'include'))
    drain(capture)
    assert capture.records.item(1, item['id'])['included']
    assert not any('saved for review and excluded' in body for _, body in capture.sender.sent)
