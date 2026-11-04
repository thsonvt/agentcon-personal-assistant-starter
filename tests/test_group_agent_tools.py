"""WhatsApp coordination prepares drafts; only later exact approval opens them."""
import asyncio
import copy
import importlib
import json
from types import SimpleNamespace

import pytest
from agents.tool_context import ToolContext
from app import agents
from tests.test_assistant_tools import make_app


class Groups:
    base_url = "https://example.com"
    def __init__(self):
        self.created = []
        self.activated = []
        self.meeting = dict(id="request1", title="A call", duration_min=60, timezone="Asia/Singapore",
                            status="draft", revision=1, location="", options=[dict(id="o1", start="2026-10-14T14:30:00+08:00", end="2026-10-14T15:30:00+08:00")],
                            guests=[dict(name="Tonya", email="tonya@example.com", status="awaiting", selected=[]),
                                    dict(name="Joel", email="joel@example.com", status="awaiting", selected=[])])
    def create_draft(self, user_id, **fields):
        self.created.append((user_id, fields))
        return copy.deepcopy(self.meeting)
    def host_view(self, user_id, meeting_id):
        if user_id != 2 or meeting_id != "request1":
            raise ValueError("Request not found")
        return copy.deepcopy(self.meeting)
    def activate(self, user_id, meeting_id):
        self.activated.append((user_id, meeting_id))
        self.meeting["status"] = "waiting"
        return self.host_view(user_id, meeting_id)


def module():
    try: return importlib.import_module("app.group_tools")
    except ModuleNotFoundError: pytest.fail("Group coordination tools are not implemented")


def setup():
    app = make_app()
    app.store = app.store.for_user(2)
    app.group_meetings = Groups()
    app.run_no = 1
    return app


def prepare(app):
    return module().prepare_group_meeting(app, "A call", 60, "Asia/Singapore",
        "2026-10-14T14:30:00+08:00", "2026-10-14T17:00:00+08:00",
        "Tonya", "tonya@example.com", "Joel", "joel@example.com")


def test_assistant_exposes_coordination_but_delegate_does_not():
    names = {tool.name for tool in agents.ASSISTANT_TOOLS}
    assert {"prepare_group_meeting", "activate_group_meeting", "group_meeting_status"} <= names
    assert not {"prepare_group_meeting", "activate_group_meeting", "group_meeting_status"} & {t.name for t in agents.DELEGATE_TOOLS}


def test_prepare_is_scoped_draft_and_does_not_send_or_book():
    app = setup()
    result = prepare(app)
    assert result.startswith("CONFIRM:") and "60" in result
    assert "Tonya" in result and "Joel" in result and "2026" in result
    assert app.group_meetings.created[0][0] == 2
    assert not app.group_meetings.activated
    assert "not been booked" in app.booking_reply


def test_activation_needs_next_turn_exact_proposal():
    app = setup(); prepare(app)
    assert module().activate_group_meeting(app, "request1", confirmed=True).startswith("CONFIRM:")
    assert not app.group_meetings.activated
    app.run_no += 1
    result = module().activate_group_meeting(app, "request1", confirmed=True)
    assert "ready to share" in result.lower()
    assert app.group_meetings.activated == [(2, "request1")]
    assert "not been booked" in app.booking_reply


def test_changed_participant_or_options_cannot_reuse_approval():
    app = setup(); prepare(app);app.run_no += 1
    app.group_meetings.meeting["guests"][1]["email"] = "different@example.com"
    assert module().activate_group_meeting(app, "request1", confirmed=True).startswith("CONFIRM:")
    assert not app.group_meetings.activated


def test_wrong_request_is_not_activated_and_legacy_admin_is_not_bound_to_web_user():
    app = setup();prepare(app);app.run_no += 1
    assert module().activate_group_meeting(app, "other", confirmed=True).startswith("ERROR:")
    app.store = app.store.for_user(1)
    assert "Connections" in prepare(app)
    assert len(app.group_meetings.created) == 1


def test_group_mutations_cannot_follow_untrusted_mail_read():
    app = setup();app.untrusted_read_this_run = True
    tool = next((t for t in agents.ASSISTANT_TOOLS if t.name == "activate_group_meeting"), None)
    assert tool is not None
    args = json.dumps({"meeting_id":"request1","confirmed":True})
    result = asyncio.run(tool.on_invoke_tool(ToolContext(app,tool_name=tool.name,tool_call_id="group",tool_arguments=args),args))
    assert result.startswith("CLARIFY:") and "No change made" in result
    assert not app.group_meetings.activated
