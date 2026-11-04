"""Context-aware briefs reuse evidence and never return stale private context."""
import asyncio
from dataclasses import replace
from datetime import timedelta

from tests.test_daily_agenda import answer, enabled, messages


class PreparedModel:
    def __init__(self):
        self.calls = []

    async def agenda(self, events, evidence=None):
        self.calls.append(('agenda', evidence))
        return [{'hint': 'Interview preparation: practise two verified career examples and questions about the role.',
                 'preparation': 'Prepare NTUC delivery impact and Samsung discovery examples. Sources: fact:1.'}
                for event in events]

    async def __call__(self, event, evidence=None):
        self.calls.append(('detail', evidence))
        return 'Updated preparation from current evidence.'


def enriched():
    env = enabled()
    env.events = env.events[:1]
    model = PreparedModel()
    env.briefs.generate = model
    packet = {'sources': [{'id': 'fact:1', 'kind': 'saved_fact', 'text': 'NTUC delivery; Samsung discovery.'}],
              'limitations': []}
    env.briefs.evidence_for_event = lambda user_id, event: packet
    return env, model, packet


def test_morning_and_detail_reuse_one_artifact_and_evidence():
    env, model, packet = enriched()
    env.tick()
    assert 'Interview preparation' in messages(env)
    assert model.calls == [('agenda', [packet])]
    detail = answer(env, 'meeting 1')
    assert 'NTUC delivery impact' in detail
    assert len(model.calls) == 1
    context = asyncio.run(env.briefs.conversation_context(env.users.get(1)))
    assert 'NTUC delivery impact' in context and 'fact:1' in context


def test_changed_calendar_cannot_reuse_or_expose_old_preparation():
    env, model, packet = enriched()
    env.tick()
    env.events = [replace(env.events[0], description='Now an informal catch-up')]
    context = asyncio.run(env.briefs.conversation_context(env.users.get(1)))
    assert 'NTUC delivery impact' not in context
    assert 'Updated preparation' in answer(env, 'meeting 1')
    assert model.calls[-1] == ('detail', packet)


def test_changed_facts_invalidate_cached_preparation():
    env, model, packet = enriched()
    env.tick()
    packet['sources'][0]['text'] = 'Career examples corrected by user.'
    detail = answer(env, 'meeting 1')
    assert 'NTUC delivery impact' not in detail
    assert 'Updated preparation' in detail


def test_paused_cancelled_and_cross_user_context_is_empty():
    env, model, packet = enriched()
    env.tick()
    env.events = []
    assert asyncio.run(env.briefs.conversation_context(env.users.get(1))) == ''
    env.briefs.pause(1)
    assert asyncio.run(env.briefs.conversation_context(env.users.get(1))) == ''
    assert asyncio.run(env.briefs.conversation_context(replace(env.users.get(1), id=999))) == ''


def test_cached_morning_preparation_reused_for_upcoming_reminder():
    env, model, packet = enriched()
    env.tick()
    env.time = env.events[0].start - timedelta(minutes=30)
    env.tick()
    assert len(model.calls) == 1
    assert 'Interview preparation' in env.sender.sent[-1][1]


def test_missing_evidence_loader_does_not_break_source_only_workflow():
    env = enabled()
    env.tick()
    assert 'Project review' in messages(env)
    assert 'Prepare a launch update' in answer(env, 'meeting 1')


def test_requested_detail_before_digest_is_available_for_follow_up():
    env, model, packet = enriched()
    detail = answer(env, 'meeting 1')
    assert 'Updated preparation' in detail
    context = asyncio.run(env.briefs.conversation_context(env.users.get(1)))
    assert 'Updated preparation' in context


def test_queued_digest_does_not_claim_its_preparation_was_seen():
    from tests.fakes import FakeSender
    from app.tools.whatsapp import WindowClosedError
    env, model, packet = enriched()
    env.briefs.sender = FakeSender(exc=WindowClosedError('closed'))
    env.tick()
    assert asyncio.run(env.briefs.conversation_context(env.users.get(1))) == ''


def test_queue_revalidates_changed_private_evidence_before_delivery():
    from tests.fakes import FakeSender
    from app.tools.whatsapp import WindowClosedError
    from tests.test_proactive import PHONE
    env, model, packet = enriched()
    env.briefs.sender = FakeSender(exc=WindowClosedError('closed'))
    env.tick()
    packet['sources'][0]['text'] = 'Career claim withdrawn.'
    env.briefs.sender = FakeSender()
    env.briefs.deliver_queued(PHONE)
    assert 'Interview preparation' not in '\n'.join(body for _, body in env.briefs.sender.sent)


def test_calendar_change_during_evidence_read_cannot_return_cached_details():
    env, model, packet = enriched()
    env.tick()

    def changed(user_id, event):
        env.events = []
        return packet

    env.briefs.evidence_for_event = changed
    reply = answer(env, 'meeting 1')
    assert 'NTUC delivery impact' not in reply
    assert 'Interview preparation' not in reply
    assert 'Project review' not in reply


def test_rich_digest_and_detail_stay_within_whatsapp_text_limit():
    env, model, packet = enriched()
    from tests.test_daily_agenda import meeting
    env.events = [meeting(str(i), 'Meeting ' + 'x' * 200, 10 + i) for i in range(8)]

    async def agenda(events, evidence=None):
        return [{'hint': 'h' * 600, 'preparation': 'p' * 3500} for event in events]

    model.agenda = agenda
    env.tick()
    assert len(messages(env)) <= 4096
    assert 'Showing' in messages(env)
    env.events[0] = replace(env.events[0], source_url='https://calendar.google.com/calendar/event?eid=' + 'a' * 1500)
    env.briefs.agenda._snapshot(env.users.get(1), env.events,
        hints={(env.events[0].id, env.events[0].start.isoformat()): {'hint': 'h', 'preparation': 'p' * 3500}})
    assert len(answer(env, 'meeting 1')) <= 4096


def test_explicit_agenda_refresh_invalidates_changed_private_evidence():
    env, model, packet = enriched()
    env.tick()
    packet['sources'][0]['text'] = 'Previously supplied career claim withdrawn.'
    assert 'Interview preparation' not in answer(env, 'agenda')


def test_calendar_change_during_evidence_read_cannot_inject_followup_context():
    env, model, packet = enriched()
    env.tick()

    def changed(user_id, event):
        env.events = []
        return packet

    env.briefs.evidence_for_event = changed
    assert asyncio.run(env.briefs.conversation_context(env.users.get(1))) == ''


def test_queued_reminder_is_cancelled_when_its_private_evidence_changes():
    from tests.fakes import FakeSender
    from app.tools.whatsapp import WindowClosedError
    from tests.test_proactive import PHONE
    env, model, packet = enriched()
    env.tick()
    env.briefs.sender = FakeSender(exc=WindowClosedError('closed'))
    env.time = env.events[0].start - timedelta(minutes=30)
    env.tick()
    packet['sources'][0]['text'] = 'Source no longer supports this career example.'
    env.briefs.sender = FakeSender()
    env.briefs.deliver_queued(PHONE)
    assert env.briefs.sender.sent == []


def test_detail_never_appends_an_unsafe_calendar_source():
    env, model, packet = enriched()
    event = replace(env.events[0], source_url='https://calendar.google.com/calendar/event?eid=evt\nhttps://evil.example')
    detail = env.briefs._detail_text(event, 'Prepare two verified examples.')
    assert 'evil.example' not in detail
    assert 'Calendar event ID:' in detail
