"""Regression coverage for SON-82: an event is not necessarily an invitation."""

import pytest

from app import assistant_tools as tools
from tests.fakes import FakeCalendarService
from tests.test_assistant_tools import make_app

START = "2026-09-29T15:00"
END = "2026-09-29T16:00"


def book(app, **kwargs):
    return tools.create_event(app, "Meeting with Quan", START, END, **kwargs)


def invite(app, **kwargs):
    return book(app, **{"booking_mode": "invitation", "attendee_name": "Quan",
                        "attendee_email": "quan@example.com", **kwargs})


def test_missing_booking_mode_does_not_silently_create_personal_meeting():
    svc = FakeCalendarService()
    app = make_app(svc)
    result = book(app)
    assert result.startswith("CLARIFY:")
    assert svc.calls == []
    assert app.completed_writes == []


def test_explicit_personal_entry_reports_no_invitation():
    svc = FakeCalendarService()
    result = book(make_app(svc), booking_mode="personal")
    assert "no invitation sent" in result
    assert [c for c in svc.calls if c[0] == "insert"][0][1]["sendUpdates"] == "none"
    assert "attendees" not in [c for c in svc.calls if c[0] == "insert"][0][1]["body"]


def test_personal_entry_rejects_attendee_email_instead_of_sending():
    svc = FakeCalendarService()
    result = book(make_app(svc), booking_mode="personal", attendee_email="quan@example.com")
    assert result.startswith("ERROR:")
    assert svc.calls == []


@pytest.mark.parametrize("email", [None, "", "not-an-email", "a@", "a b@example.com", "a@example.com,b@example.com"])
def test_invitation_without_valid_email_cannot_create_event(email):
    svc = FakeCalendarService()
    result = invite(make_app(svc), attendee_email=email)
    assert result.startswith(("CLARIFY:", "ERROR:"))
    assert svc.calls == []


def test_unknown_person_needs_name_and_email_confirmation_in_next_run():
    svc = FakeCalendarService()
    app = make_app(svc)
    app.run_no = 1
    result = invite(app, confirmed=True)
    assert result.startswith("CONFIRM:")
    assert "Quan" in result and "quan@example.com" in result
    assert svc.calls == []
    assert invite(app, confirmed=True).startswith("CONFIRM:")
    assert svc.calls == []
    app.run_no = 2
    result = invite(app, confirmed=True)
    assert "invitation requested for Quan <quan@example.com>" in result
    assert "delivery" in result
    assert [c for c in svc.calls if c[0] == "insert"][0][1]["sendUpdates"] == "all"
    assert app.completed_writes == [result]


def test_changed_recipient_cannot_reuse_confirmation():
    svc = FakeCalendarService()
    app = make_app(svc)
    app.run_no = 1
    invite(app)
    app.run_no = 2
    result = invite(app, attendee_name="Wong", attendee_email="wong@example.com", confirmed=True)
    assert result.startswith("CONFIRM:")
    assert svc.calls == []


def test_changed_time_cannot_reuse_confirmation():
    svc = FakeCalendarService()
    app = make_app(svc)
    app.run_no = 1
    invite(app)
    app.run_no = 2
    result = tools.create_event(app, "Meeting with Quan", "2026-09-29T16:00", "2026-09-29T17:00",
                                "quan@example.com", booking_mode="invitation", attendee_name="Quan", confirmed=True)
    assert result.startswith("CONFIRM:")
    assert svc.calls == []


def test_confirmation_is_consumed_after_write():
    svc = FakeCalendarService()
    app = make_app(svc)
    app.run_no = 1
    invite(app)
    app.run_no = 2
    invite(app, confirmed=True)
    assert invite(app, confirmed=True).startswith("CONFIRM:")
    assert len([c for c in svc.calls if c[0] == "insert"]) == 1


def test_known_contact_email_mismatch_is_rejected():
    svc = FakeCalendarService()
    app = make_app(svc)
    app.store.add_contact("Quan", "6581234567", "correct@example.com")
    result = invite(app)
    assert result.startswith("CLARIFY:")
    assert "correct@example.com" in result
    assert svc.calls == []


def test_known_contact_without_supplied_email_uses_saved_email_but_requires_confirmation():
    svc = FakeCalendarService()
    app = make_app(svc)
    app.store.add_contact("Quan", "6581234567", "quan@example.com")
    result = invite(app, attendee_email=None)
    assert result.startswith("CONFIRM:")
    assert "Quan <quan@example.com>" in result
    assert svc.calls == []


def test_ambiguous_saved_contact_requires_exact_identity():
    svc = FakeCalendarService()
    app = make_app(svc)
    app.store.add_contact("Quan Nguyen", "6581234567", "one@example.com")
    app.store.add_contact("Quan Tran", "6581234568", "two@example.com")
    result = invite(app)
    assert result.startswith("CLARIFY:")
    assert "Quan Nguyen" in result and "Quan Tran" in result
    assert svc.calls == []


def test_partial_unique_contact_is_not_silently_selected():
    app = make_app()
    app.store.add_contact("Quan Nguyen", "6581234567", "quan@example.com")
    assert invite(app).startswith("CLARIFY:")


def test_exact_full_name_resolves_partial_name_ambiguity():
    app = make_app()
    app.store.add_contact("Quan Nguyen", "6581234567", "one@example.com")
    app.store.add_contact("Quan Tran", "6581234568", "two@example.com")
    result = invite(app, attendee_name="Quan Nguyen", attendee_email="one@example.com")
    assert result.startswith("CONFIRM:")


def test_duplicate_full_names_can_be_disambiguated_by_exact_saved_email():
    app = make_app()
    app.store.add_contact("Quan Nguyen", "6581234567", "one@example.com")
    app.store.add_contact("Quan Nguyen", "6581234568", "two@example.com")
    result = invite(app, attendee_name="Quan Nguyen", attendee_email="two@example.com")
    assert result.startswith("CONFIRM:")
    assert "Quan Nguyen <two@example.com>" in result


def test_saved_contacts_from_other_owner_are_not_used():
    app = make_app()
    app.store.for_user(2).add_contact("Quan", "6581234567", "private@example.com")
    result = invite(app, attendee_email=None)
    assert result.startswith("CLARIFY:")
    assert "private@example.com" not in result


def test_voice_personal_entry_requires_readback_before_write():
    svc = FakeCalendarService()
    app = make_app(svc)
    app.run_no = 1
    app.current_input_is_voice = True
    result = book(app, booking_mode="personal", confirmed=True)
    assert result.startswith("CONFIRM:")
    assert "Meeting with Quan" in result and "no invitation" in result
    assert svc.calls == []
    app.run_no = 2
    app.current_input_is_voice = False
    assert book(app, booking_mode="personal").startswith("CONFIRM:")
    assert svc.calls == []


def test_voice_personal_entry_can_be_confirmed_by_next_text_turn():
    svc = FakeCalendarService()
    app = make_app(svc)
    app.run_no = 1
    app.current_input_is_voice = True
    book(app, booking_mode="personal")
    app.run_no = 2
    app.current_input_is_voice = False
    result = book(app, booking_mode="personal", confirmed=True)
    assert "no invitation sent" in result
    assert len([c for c in svc.calls if c[0] == "insert"]) == 1


def test_email_read_cannot_create_personal_event_without_next_turn_confirmation():
    svc = FakeCalendarService()
    app = make_app(svc)
    app.run_no = 1
    app.untrusted_read_this_run = True
    result = book(app, booking_mode="personal", confirmed=True)
    assert result.startswith("CONFIRM:")
    assert svc.calls == []


def test_mixed_success_and_pending_invitation_reports_only_actual_write(monkeypatch):
    import asyncio
    import json
    from types import SimpleNamespace
    from agents import SQLiteSession
    from agents.tool_context import ToolContext
    from app import agents as agent_module

    svc = FakeCalendarService()
    app = make_app(svc)
    async def fake_run(agent, text, **kwargs):
        tool = next(t for t in agent.tools if t.name == "create_event")
        for title, mode, name, email in [
            ("Gym", "personal", None, None),
            ("Meeting with Quan", "invitation", "Quan", "quan@example.com"),
        ]:
            args = json.dumps(dict(title=title, start=START, end=END, booking_mode=mode,
                                   attendee_name=name, attendee_email=email, confirmed=False))
            await tool.on_invoke_tool(
                ToolContext(app, tool_name="create_event", tool_call_id=title, tool_arguments=args), args)
        return SimpleNamespace(final_output="Booked Gym and sent an invitation to Quan!")
    monkeypatch.setattr(agent_module.Runner, "run", fake_run)
    reply = asyncio.run(agent_module.run_agent(agent_module.build_assistant("m"), app,
                        SQLiteSession("mixed-confirmation"), "Book Gym and invite Quan"))
    assert "Created: Gym" in reply
    assert "confirm" in reply.lower()
    assert "Quan" in reply and "quan@example.com" in reply
    assert "sent an invitation" not in reply
    assert len([c for c in svc.calls if c[0] == "insert"]) == 1


def test_authoritative_confirmation_reply_is_also_persisted_in_session(monkeypatch):
    import asyncio
    import json
    from types import SimpleNamespace
    from agents import SQLiteSession
    from agents.tool_context import ToolContext
    from app import agents as agent_module

    app = make_app()
    session = SQLiteSession("persisted-confirmation")
    async def fake_run(agent, text, **kwargs):
        tool = next(t for t in agent.tools if t.name == "create_event")
        args = json.dumps(dict(title="Meeting with Quan", start=START, end=END,
                               booking_mode="invitation", attendee_name="Quan",
                               attendee_email="quan@example.com", confirmed=False))
        await tool.on_invoke_tool(
            ToolContext(app, tool_name="create_event", tool_call_id="invite", tool_arguments=args), args)
        await kwargs["session"].add_items([
            {"role": "user", "content": text},
            {"role": "assistant", "content": "Booked and sent the invitation!"},
        ])
        return SimpleNamespace(final_output="Booked and sent the invitation!")
    monkeypatch.setattr(agent_module.Runner, "run", fake_run)
    async def scenario():
        reply = await agent_module.run_agent(agent_module.build_assistant("m"), app, session, "Invite Quan")
        items = await session.get_items()
        content = items[-1]["content"]
        if isinstance(content, list):
            content = "".join(item.get("text", "") for item in content)
        assert "confirm" in reply.lower()
        assert content == reply
    asyncio.run(scenario())
