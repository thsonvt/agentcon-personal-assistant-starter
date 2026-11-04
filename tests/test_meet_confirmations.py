"""Meet links reach receipts and confirmations without sending real invitations."""
import asyncio
import json
from dataclasses import replace

import pytest
from agents.tool_context import ToolContext

from app import agents, assistant_tools, delegate_tools
from tests.test_assistant_tools import make_app
from tests.test_attendee_confirmation import invite
from tests.test_delegate_tools import make as make_delegate
from tests.test_group_meetings import site as group_site
from tests.test_public_booking import BookingEmail, site as public_site

MEET = 'https://meet.google.com/abc-defg-hij'


def test_invitation_proposal_discloses_automatic_meet():
    app = make_app()
    result = invite(app)
    assert 'Google Meet will be added' in result
    assert 'proceed without one' not in result


def test_agent_readback_discloses_automatic_meet():
    app = make_app()
    args = json.dumps(dict(title='Call', start='2026-09-29T15:00', end='2026-09-29T16:00',
                           booking_mode='invitation', attendee_name='Quan', attendee_email='quan@example.com'))
    asyncio.run(agents.create_event.on_invoke_tool(
        ToolContext(app, tool_name='create_event', tool_call_id='meet', tool_arguments=args), args))
    assert 'Google Meet will be added' in app.booking_reply
    assert 'proceeding without one' not in app.booking_reply


def test_personal_voice_proposal_keeps_location_question():
    app = make_app()
    app.current_input_is_voice = True
    result = assistant_tools.create_event(app, 'Study', '2026-09-29T15:00', '2026-09-29T16:00', booking_mode='personal')
    assert 'confirmation to proceed without one' in result
    assert 'Google Meet' not in result


def test_host_booking_confirmation_includes_returned_meet(monkeypatch):
    app = make_app()
    original = app.calendar.create_event
    monkeypatch.setattr(app.calendar, 'create_event', lambda *a, **kw: replace(original(*a, **kw), meet_link=MEET))
    app.run_no = 1
    invite(app)
    app.run_no = 2
    result = invite(app, confirmed=True)
    assert MEET in result
    assert app.completed_writes == [result]


def test_delegate_booking_confirmation_includes_returned_meet(monkeypatch):
    d = make_delegate()
    original = d.app.calendar.create_event
    monkeypatch.setattr(d.app.calendar, 'create_event', lambda *a, **kw: replace(original(*a, **kw), meet_link=MEET))
    result = delegate_tools.book_agreed_time(d, '2026-10-02T15:00')
    assert MEET in result
    assert MEET in d.app.whatsapp.sent[0][1]


@pytest.mark.parametrize('location, expected', [('', MEET), ('Office', 'Office')])
def test_public_booking_persists_join_details_for_guest_and_host(public_site, monkeypatch, location, expected):
    site = public_site
    site.obj.host_email = sender = BookingEmail()
    site.publish(location=location)
    original = site.cal.create_event
    monkeypatch.setattr(site.cal, 'create_event', lambda *a, **kw: replace(original(*a, **kw), meet_link=MEET))
    token = site.start()
    response = site.book(token)
    assert response.json()['location'] == expected
    assert sender.calls[0][0]['dataVariables']['location'] == expected
    assert site.book(token).json()['location'] == expected
    assert len(site.cal.writes) == len(sender.calls) == 1


def test_public_reconciliation_does_not_change_attempted_email(public_site):
    site = public_site
    site.obj.host_email = sender = BookingEmail('unknown')
    site.publish(location='')
    site.book(site.start())
    row = site.db.conn.execute('SELECT * FROM public_booking_requests').fetchone()
    receipt = site.db.conn.execute('SELECT payload FROM host_booking_notifications').fetchone()['payload']
    event = replace(site.cal.events[row['event_id']], meet_link=MEET)
    result = site.obj._confirmed(row['request_id'], event, json.loads(row['result']), row['event_id'])
    assert result.body and json.loads(result.body)['location'] == MEET
    assert site.db.conn.execute('SELECT payload FROM host_booking_notifications').fetchone()['payload'] == receipt
    assert len(sender.calls) == 1


def test_group_booking_saves_returned_meet(group_site, monkeypatch):
    site = group_site
    original = site.cal.create_event
    monkeypatch.setattr(site.cal, 'create_event', lambda *a, **kw: replace(original(*a, **kw), meet_link=MEET))
    meeting = site.obj.activate(site.user.id, site.draft(location='')['id'])
    choice = [meeting['options'][0]['id']]
    site.vote(meeting, 0, choice)
    site.vote(meeting, 1, choice)
    saved = site.obj.host_view(site.user.id, meeting['id'])
    assert saved['status'] == 'booked'
    assert saved['location'] == MEET
