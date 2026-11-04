from app.config import load_settings
from app.config import REQUIRED
from app.wiring import build_runtime
from tests.fakes import FakeSender


def test_build_runtime_wires_everything_without_network(tmp_path):
    env = {key: "x" for key in REQUIRED}
    env["OWNER_WHATSAPP"] = "+65 9123 4567"
    env["DB_PATH"] = str(tmp_path / "agent.db")
    runtime = build_runtime(load_settings(env), sender=FakeSender())
    assert runtime.router.owner_phone == "6591234567"
    assert runtime.app_ctx.calendar.calendar_id == "primary"
    assert (tmp_path / "agent.db").exists()
    assert runtime.router.run_contact is not None
    assert runtime.app_ctx.owner_phone == "6591234567"
    assert runtime.app_ctx.whatsapp is runtime.router.sender
    assert len(runtime.jobs) == 1
    assert runtime.router.transcribe is not None  # voice notes are wired up


def test_contact_runs_get_their_own_context(tmp_path, monkeypatch):
    # Review Focus: Pippa's run and a contact's run can overlap; they must not share completed_writes.
    import asyncio
    from datetime import datetime, timedelta

    from app import wiring
    from app.timeutil import SGT

    env = {key: "x" for key in REQUIRED}
    env["OWNER_WHATSAPP"] = "6591234567"
    env["DB_PATH"] = str(tmp_path / "agent.db")
    runtime = build_runtime(load_settings(env), sender=FakeSender())
    seen = {}

    async def fake_run_agent(agent, app, session, text, keep=30, context=None):
        seen["app"], seen["context"], seen["session"] = app, context, session
        return "ok"

    monkeypatch.setattr(wiring, "run_agent", fake_run_agent)
    now = datetime(2026, 10, 1, 10, tzinfo=SGT)
    contact = runtime.store.add_contact("Edmund", "6581234567")
    task = runtime.store.create_loop_in_task(contact.id, "a call", 30, now, now + timedelta(days=2), now)
    asyncio.run(runtime.router.run_contact(contact, task, "hi"))
    assert seen["app"] is not runtime.app_ctx
    assert seen["app"].completed_writes is not runtime.app_ctx.completed_writes
    assert seen["context"].task_id == task.id
    assert seen["session"].session_id == "1:6581234567"  # Plan 2B: Delegate chats are keyed "<inviter>:<contact>"
