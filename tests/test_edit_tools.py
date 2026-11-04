"""Plan 2A: update_event and the two-step delete_event tool."""

from datetime import datetime

import httpx

from app import assistant_tools as t
from app.store import Store
from app.timeutil import SGT
from app.tools.calendar import GoogleCalendar
from app.tools.calendly import CalendlyClient
from tests.fakes import FakeCalendarService

NOW = datetime(2026, 9, 28, 14, 0, tzinfo=SGT)


def event(event_id="e1", title="Physics revision", start="2026-09-29T16:30:00+08:00", end="2026-09-29T17:30:00+08:00"):
    return {"id": event_id, "summary": title, "start": {"dateTime": start}, "end": {"dateTime": end}}


ALL_DAY = {"id": "e9", "summary": "Holiday", "start": {"date": "2026-09-30"}, "end": {"date": "2026-10-01"}}


def make_app(*items, exc=None):
    svc = FakeCalendarService(items=[dict(i) for i in items], exc=exc)
    app = t.AppContext(
        calendar=GoogleCalendar(svc),
        calendly=CalendlyClient("tok", "https://api.calendly.com/event_types/X", http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))),
        store=Store(":memory:"),
        now=lambda: NOW,
    )
    return app, svc


def next_run(app):
    """What run_agent does at the start of each of Pippa's messages."""
    app.run_no += 1
    app.completed_writes.clear()


# --- update_event ---------------------------------------------------------------

def test_new_start_alone_keeps_the_duration():
    app, svc = make_app(event())
    out = t.update_event(app, "e1", new_start="2026-09-30T10:00")
    assert out == "Updated: Physics revision, now Wed 30 Sep, 10:00am (2026)–11:00am (Asia/Singapore, UTC+08:00; 60 min); location not specified [id: e1]"
    assert svc.calls[-1][0] == "patch"


def test_new_end_alone_keeps_the_start():
    app, _ = make_app(event())
    out = t.update_event(app, "e1", new_end="2026-09-29T18:30")
    assert "Tue 29 Sep, 4:30pm (2026)–6:30pm" in out


def test_rename_only_sends_just_the_title():
    app, svc = make_app(event())
    out = t.update_event(app, "e1", title="  Physics exam prep ")
    assert out.startswith("Updated: Physics exam prep, now Tue 29 Sep, 4:30pm (2026)–5:30pm")
    assert svc.calls[-1][1]["body"] == {"summary": "Physics exam prep"}


def test_update_with_nothing_to_change_is_an_error():
    app, svc = make_app(event())
    assert t.update_event(app, "e1").startswith("ERROR: Nothing to change")
    assert svc.calls == []


def test_update_rejects_blank_title_past_time_and_bad_range():
    app, svc = make_app(event())
    assert t.update_event(app, "e1", title="  ").startswith("ERROR:")
    assert "past" in t.update_event(app, "e1", new_start="2026-09-27T10:00", new_end="2026-09-27T11:00")
    assert t.update_event(app, "e1", new_start="2026-09-30T11:00", new_end="2026-09-30T10:00").startswith("ERROR:")
    assert [c for c in svc.calls if c[0] == "patch"] == []


def test_moving_an_all_day_event_needs_both_times():
    # Review Focus: "keep the duration" of an all-day event would make a 24-hour meeting.
    app, svc = make_app(ALL_DAY)
    out = t.update_event(app, "e9", new_start="2026-09-30T10:00")
    assert "all-day" in out and out.startswith("ERROR:")
    assert [c for c in svc.calls if c[0] == "patch"] == []


def test_update_unknown_event_says_not_found():
    app, _ = make_app(event())
    assert "wasn't found" in t.update_event(app, "nope", title="x")


def test_update_is_recorded_as_a_write():
    app, _ = make_app(event())
    t.update_event(app, "e1", title="New")
    assert app.completed_writes[0].startswith("Updated: New")


# --- delete_event ---------------------------------------------------------------

def test_first_delete_call_only_asks():
    app, svc = make_app(event())
    out = t.delete_event(app, "e1", confirmed=False)
    assert out.startswith("CONFIRM:")
    assert "Physics revision, Tue 29 Sep, 4:30pm–5:30pm" in out
    assert [c[0] for c in svc.calls] == ["get"]
    assert app.completed_writes == []


def test_confirmed_in_the_next_message_deletes():
    app, svc = make_app(event())
    t.delete_event(app, "e1", confirmed=False)
    next_run(app)  # Pippa replies "yes"
    out = t.delete_event(app, "e1", confirmed=True)
    assert out == "Deleted: Physics revision, Tue 29 Sep, 4:30pm–5:30pm"
    assert svc.calls[-1][0] == "delete"
    assert app.completed_writes == [out]
    assert app.pending_deletes == {}


def test_model_cannot_confirm_its_own_question_in_the_same_run():
    # The core safety rule (D19): only Pippa's next message can confirm.
    app, svc = make_app(event())
    t.delete_event(app, "e1", confirmed=False)
    out = t.delete_event(app, "e1", confirmed=True)
    assert out.startswith("CONFIRM:")
    assert "delete" not in [c[0] for c in svc.calls]


def test_confirmed_without_asking_first_only_asks():
    app, svc = make_app(event())
    assert t.delete_event(app, "e1", confirmed=True).startswith("CONFIRM:")
    assert "delete" not in [c[0] for c in svc.calls]


def test_confirmation_goes_stale_after_another_message():
    # Review Focus: "delete gym?" → "wait, what else is on Friday?" → "ok yes" must ask again.
    app, svc = make_app(event())
    t.delete_event(app, "e1", confirmed=False)
    next_run(app)  # Pippa talks about something else
    next_run(app)  # ...then says yes
    assert t.delete_event(app, "e1", confirmed=True).startswith("CONFIRM:")
    assert "delete" not in [c[0] for c in svc.calls]


def test_confirming_one_event_does_not_delete_another():
    app, svc = make_app(event("e1"), event("e2", title="Gym"))
    t.delete_event(app, "e1", confirmed=False)
    next_run(app)
    assert t.delete_event(app, "e2", confirmed=True).startswith("CONFIRM:")
    assert "delete" not in [c[0] for c in svc.calls]


def test_several_events_can_be_confirmed_together():
    app, _ = make_app(event("e1"), event("e2", title="Gym"))
    t.delete_event(app, "e1", confirmed=False)
    t.delete_event(app, "e2", confirmed=False)
    next_run(app)
    assert t.delete_event(app, "e1", confirmed=True).startswith("Deleted: Physics revision")
    assert t.delete_event(app, "e2", confirmed=True).startswith("Deleted: Gym")


def test_event_deleted_meanwhile_in_google_says_not_found():
    # Review Focus: Pippa deleted it in the Google Calendar app between the question and "yes".
    app, svc = make_app(event())
    t.delete_event(app, "e1", confirmed=False)
    svc.items.clear()
    svc.deleted.add("e1")
    next_run(app)
    out = t.delete_event(app, "e1", confirmed=True)
    assert out.startswith("ERROR:") and "deleted already" in out
    assert app.completed_writes == []


def test_delete_needs_an_id():
    app, _ = make_app()
    assert t.delete_event(app, " ", confirmed=False).startswith("ERROR: Which event?")


def test_moving_an_all_day_event_with_both_times_is_refused_too():
    # Final review: giving both times must not turn the holiday into a 24-hour timed meeting.
    app, svc = make_app(ALL_DAY)
    out = t.update_event(app, "e9", new_start="2026-10-02T00:00", new_end="2026-10-03T00:00")
    assert out.startswith("ERROR:") and "all-day" in out
    assert [c for c in svc.calls if c[0] == "patch"] == []


def test_renaming_an_all_day_event_still_works():
    app, _ = make_app(ALL_DAY)
    assert t.update_event(app, "e9", title="Family holiday").startswith("Updated: Family holiday")


def test_extending_a_meeting_already_under_way():
    # Final review: "I'm running over, push this to 3pm" at 2pm for a 1:30pm meeting.
    app, svc = make_app(event(start="2026-09-28T13:30:00+08:00", end="2026-09-28T14:30:00+08:00"))
    out = t.update_event(app, "e1", new_end="2026-09-28T15:00")
    assert out == "Updated: Physics revision, now Mon 28 Sep, 1:30pm (2026)–3:00pm (Asia/Singapore, UTC+08:00; 90 min); location not specified [id: e1]"
    assert set(svc.calls[-1][1]["body"]) == {"end"}  # the start isn't touched


def test_new_end_in_the_past_is_refused():
    app, svc = make_app(event(start="2026-09-28T12:00:00+08:00", end="2026-09-28T13:00:00+08:00"))
    out = t.update_event(app, "e1", new_end="2026-09-28T13:30")
    assert out.startswith("ERROR:") and "past" in out
    assert [c for c in svc.calls if c[0] == "patch"] == []
