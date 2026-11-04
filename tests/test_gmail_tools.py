import asyncio
import json
from types import SimpleNamespace

import pytest
from agents.tool_context import ToolContext

from app import gmail_tools
from app.agents import build_assistant
from app.tools.gmail import EmailMessage, GmailPermissionError, GmailAuthError, GmailTemporaryError
from tests.test_backlog_integration import app_context


def test_email_html_maps_evidence_is_returned_with_source_and_taint():
    app = app_context()
    message = EmailMessage("abc", "Dinner with Chris", "Chris", "29 Sep", "Dinner at six",
                           ("https://maps.app.goo.gl/example",), "https://mail.google.com/mail/u/0/#all/abc")
    app.gmail = SimpleNamespace(search_messages=lambda q, n: [message])
    result = gmail_tools.search_email(app, "from:chris dinner")
    data = json.loads(result.split("\n", 1)[1])
    assert data[0]["links"] == ["https://maps.app.goo.gl/example"]
    assert data[0]["source_url"].endswith("abc")
    assert app.untrusted_read_this_run


@pytest.mark.parametrize("error,expected", [
    (GmailPermissionError("Missing Gmail scope"), "Missing Gmail scope"),
    (GmailAuthError("Expired token"), "Expired token"),
    (GmailTemporaryError("Temporary failure"), "Temporary failure"),
])
def test_read_failures_preserve_reason_and_user_specific_reconnection(error, expected):
    app = app_context()
    app.gmail_reauth_message = "Reconnect at https://example.com/signup/gmail"

    def fail(*args):
        raise error

    app.gmail = SimpleNamespace(get_message=fail)
    result = gmail_tools.read_email(app, "abc")
    assert expected in result
    assert ("/signup/gmail" in result) is not isinstance(error, GmailTemporaryError)


def test_no_email_matches_is_not_reported_as_lack_of_access():
    app = app_context()
    app.gmail = SimpleNamespace(search_messages=lambda *args: [])
    assert gmail_tools.search_email(app, "from:chris") == (
        "Gmail search completed: no matching emails. Try a different sender, subject, or date."
    )


@pytest.mark.parametrize("tool_name,arguments", [
    ("update_event", {"event_id": "e", "title": "Injected", "new_start": None, "new_end": None}),
    ("delete_event", {"event_id": "e", "confirmed": True}),
    ("remember", {"fact": "Email told me to remember this"}),
    ("save_contact", {"name": "Injected", "phone": "6581234567", "email": "x@example.com"}),
])
def test_untrusted_email_cannot_trigger_other_mutating_tools(tool_name, arguments):
    app = app_context()
    app.untrusted_read_this_run = True
    tool = next(t for t in build_assistant("m").tools if t.name == tool_name)
    args = json.dumps(arguments)
    result = asyncio.run(tool.on_invoke_tool(ToolContext(app, tool_name=tool_name,
                                                       tool_call_id="call-test", tool_arguments=args), args))
    assert result.startswith("CLARIFY:"), result
    assert app.calendar.service.calls == []
    assert app.store.list_facts() == []
    assert app.store.list_contacts() == []
