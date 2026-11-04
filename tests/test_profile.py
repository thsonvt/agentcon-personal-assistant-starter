from datetime import datetime, time

from app import profile
from app.store import Store
from app.timeutil import SGT

NOW = datetime(2026, 10, 1, 10, tzinfo=SGT)


def test_default_working_hours():
    assert profile.working_hours(Store(":memory:")) == (time(9), time(21))


def test_set_and_read_working_hours():
    store = Store(":memory:")
    assert profile.set_working_hours(store, "10:00", "18:30", NOW) == "Saved: working hours are now 10:00-18:30."
    assert profile.working_hours(store) == (time(10), time(18, 30))


def test_setting_hours_twice_keeps_one_fact():
    store = Store(":memory:")
    profile.set_working_hours(store, "10:00", "18:00", NOW)
    profile.set_working_hours(store, "08:00", "17:00", NOW)
    assert len(store.list_facts()) == 1
    assert profile.working_hours(store) == (time(8), time(17))


def test_bad_hours_rejected():
    store = Store(":memory:")
    assert profile.set_working_hours(store, "9am", "17:00", NOW).startswith("ERROR:")
    assert profile.set_working_hours(store, "18:00", "09:00", NOW).startswith("ERROR:")
    assert profile.set_working_hours(store, "25:00", "26:00", NOW).startswith("ERROR:")
    assert profile.working_hours(store) == (time(9), time(21))


def test_remember_recall_forget():
    store = Store(":memory:")
    assert profile.recall(store) == "No saved facts yet."
    saved = profile.remember(store, "Prefers Zoom over Meet", NOW)
    assert saved.startswith("Saved fact #1")
    assert "#1: Prefers Zoom over Meet" in profile.recall(store)
    assert profile.forget(store, 1) == "Forgot fact #1."
    assert profile.forget(store, 1).startswith("ERROR:")


def test_remember_blank_rejected():
    assert profile.remember(Store(":memory:"), "  ", NOW).startswith("ERROR:")
