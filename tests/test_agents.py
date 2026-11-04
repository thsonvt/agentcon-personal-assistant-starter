import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from agents import SQLiteSession

from app.agents import assistant_instructions, build_assistant, run_agent
from app.router import PartialRunError
from app.store import Store
from app.timeutil import SGT


def test_assistant_has_exactly_the_owner_tools():
    agent = build_assistant("test-model")
    assert agent.model == "test-model"
    assert {tool.name for tool in agent.tools} == {
        "list_events",
        "find_conflicts",
        "get_free_slots",
        "create_event",
        "update_event",
        "delete_event",
        "get_calendly_link",
        "save_contact",
        "list_contacts",
        "start_loop_in",
        "cancel_loop_in",
        "remember",
        "recall",
        "forget",
        "set_working_hours",
        "search_email",
        "read_email",
        "get_event_details",
        "prepare_group_meeting",
        "activate_group_meeting",
        "group_meeting_status",
        "save_followup",
        "confirm_followup",
        "list_followups",
        "update_followup",
    }


def test_instructions_include_current_singapore_date_and_safety_rules():
    ctx = SimpleNamespace(context=SimpleNamespace(now=lambda: datetime(2026, 9, 28, 14, 5, tzinfo=SGT), store=Store(":memory:")))
    text = assistant_instructions(ctx, build_assistant("m"))
    assert "Monday 28 September 2026, 14:05" in text
    assert "+08:00" in text
    assert "find_conflicts" in text
    assert 'starts with "Created:"' in text


def test_tool_schemas_are_strict_and_documented():
    for tool in build_assistant("m").tools:
        assert tool.description
        assert tool.params_json_schema.get("additionalProperties") is False


def test_instructions_say_check_calendar_before_retrying_a_failed_write():
    ctx = SimpleNamespace(context=SimpleNamespace(now=lambda: datetime(2026, 9, 28, 14, 5, tzinfo=SGT), store=Store(":memory:")))
    assert "before trying again" in assistant_instructions(ctx, build_assistant("m"))


def test_schedule_answers_require_a_fresh_read_in_the_current_turn():
    ctx = SimpleNamespace(context=SimpleNamespace(now=lambda: datetime(2026, 9, 28, 14, 5, tzinfo=SGT), store=Store(":memory:")))
    text = assistant_instructions(ctx, build_assistant("m"))
    assert "call list_events in the current turn" in text
    assert "Never reuse an earlier calendar answer" in text


def test_run_failure_after_a_write_raises_partial_run_error(monkeypatch):
    app = SimpleNamespace(completed_writes=[], run_no=0)

    async def fake_run(*args, **kwargs):
        app.completed_writes.append("Created: Gym [id: e1]")
        raise RuntimeError("OpenAI 500")

    monkeypatch.setattr("app.agents.Runner.run", fake_run)
    with pytest.raises(PartialRunError) as err:
        asyncio.run(run_agent(build_assistant("m"), app, SQLiteSession("t1"), "book gym"))
    assert err.value.writes == ["Created: Gym [id: e1]"]


def test_run_failure_without_writes_is_reraised(monkeypatch):
    app = SimpleNamespace(completed_writes=["Created: old run [id: x]"], run_no=0)

    async def fake_run(*args, **kwargs):
        raise RuntimeError("OpenAI 500")

    monkeypatch.setattr("app.agents.Runner.run", fake_run)
    with pytest.raises(RuntimeError):
        asyncio.run(run_agent(build_assistant("m"), app, SQLiteSession("t2"), "hi"))


def test_assistant_instructions_include_saved_facts():
    store = Store(":memory:")
    store.add_fact("Prefers Zoom", datetime(2026, 9, 28, tzinfo=SGT))
    ctx = SimpleNamespace(context=SimpleNamespace(now=lambda: datetime(2026, 9, 28, 14, 5, tzinfo=SGT), store=store))
    assert "#1: Prefers Zoom" in assistant_instructions(ctx, build_assistant("m"))


def test_delegate_has_only_restricted_tools():
    from app.agents import build_delegate

    names = {tool.name for tool in build_delegate("m").tools}
    assert names == {"free_slots", "calendly_link", "book_agreed_time", "mark_booked_via_link", "report_to_owner"}
    # Nothing that can read event titles or touch other tasks.
    assert not names & {"list_events", "find_conflicts", "create_event", "update_event", "delete_event", "recall", "list_contacts"}


def test_delegate_instructions_name_the_task():
    from app.agents import build_delegate, delegate_instructions
    from app.delegate_tools import DelegateContext

    store = Store(":memory:")
    c = store.add_contact("Edmund", "6581234567")
    t0 = datetime(2026, 10, 2, 9, tzinfo=SGT)
    task = store.create_loop_in_task(c.id, "a 30-minute call", 30, t0, t0.replace(day=3), t0)
    app = SimpleNamespace(store=store, now=lambda: t0)
    ctx = SimpleNamespace(context=DelegateContext(app=app, task_id=task.id, contact=c))
    text = delegate_instructions(ctx, build_delegate("m"))
    assert "Edmund" in text and "a 30-minute call" in text and "30 minutes" in text
    assert "Never guess or reveal" in text


def test_instructions_describe_two_step_delete():
    ctx = SimpleNamespace(context=SimpleNamespace(now=lambda: datetime(2026, 9, 28, 14, 5, tzinfo=SGT), store=Store(":memory:")))
    text = assistant_instructions(ctx, build_assistant("m"))
    assert "confirmed=false" in text
    assert '"Deleted:"' in text


def test_every_run_gets_a_new_run_number(monkeypatch):
    # delete_event relies on this to tell "asked in an earlier message" from "asked just now".
    app = SimpleNamespace(completed_writes=[], run_no=0)

    async def fake_run(*args, **kwargs):
        return SimpleNamespace(final_output="ok")

    monkeypatch.setattr("app.agents.Runner.run", fake_run)
    session = SQLiteSession("t3")
    asyncio.run(run_agent(build_assistant("m"), app, session, "one"))
    asyncio.run(run_agent(build_assistant("m"), app, session, "two"))
    assert app.run_no == 2
