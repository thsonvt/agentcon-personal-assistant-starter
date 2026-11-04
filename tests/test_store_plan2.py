from datetime import datetime, timedelta

import pytest

from app.store import AmbiguousContact, Store
from app.timeutil import SGT

NOW = datetime(2026, 10, 1, 10, tzinfo=SGT)


@pytest.fixture
def store():
    s = Store(":memory:")
    s.add_contact("Edmund Lim", "6581234567", "edmund@example.com")
    s.add_contact("Eze", "6581110000")
    s.add_contact("Zerrie", "6582220000")
    return s


def test_list_contacts_sorted_by_name(store):
    assert [c.name for c in store.list_contacts()] == ["Edmund Lim", "Eze", "Zerrie"]


def test_find_contact_exact_any_case(store):
    assert store.find_contact("eze").name == "Eze"


def test_find_contact_unique_partial(store):
    assert store.find_contact("edmund").phone == "6581234567"


def test_find_contact_ambiguous_partial(store):
    # "e" appears in all three names
    with pytest.raises(AmbiguousContact):
        store.find_contact("e")


def test_find_contact_missing(store):
    assert store.find_contact("Akash") is None
    assert store.find_contact("  ") is None


def test_task_status_changes_and_event_id(store):
    c = store.find_contact("Eze")
    task = store.create_loop_in_task(c.id, "a call", 30, NOW, NOW + timedelta(days=3), NOW)
    store.set_task_status(task.id, "negotiating", NOW)
    done = store.set_task_status(task.id, "done", NOW, event_id="evt1")
    assert done.status == "done"
    assert done.event_id == "evt1"
    assert store.get_open_task(c.id) is None


def test_bad_status_rejected(store):
    c = store.find_contact("Eze")
    task = store.create_loop_in_task(c.id, "a call", 30, NOW, NOW + timedelta(days=3), NOW)
    with pytest.raises(ValueError):
        store.set_task_status(task.id, "finished", NOW)


def test_tasks_waiting_since_only_old_waiting_ones(store):
    eze, zer = store.find_contact("Eze"), store.find_contact("Zerrie")
    old = store.create_loop_in_task(eze.id, "a call", 30, NOW, NOW + timedelta(days=5), NOW - timedelta(hours=50))
    store.create_loop_in_task(zer.id, "a call", 30, NOW, NOW + timedelta(days=5), NOW - timedelta(hours=10))
    due = store.tasks_waiting_since(NOW - timedelta(hours=48))
    assert [t.id for t in due] == [old.id]


def test_keyed_fact_replaces_previous(store):
    store.add_fact("Working hours 09:00-21:00", NOW, key="working_hours")
    store.add_fact("Working hours 10:00-18:00", NOW, key="working_hours")
    store.add_fact("Prefers Zoom", NOW)
    assert [f.fact for f in store.list_facts()] == ["Working hours 10:00-18:00", "Prefers Zoom"]
    assert store.get_fact("working_hours").fact == "Working hours 10:00-18:00"


def test_delete_fact(store):
    fact = store.add_fact("Prefers Zoom", NOW)
    assert store.delete_fact(fact.id) is True
    assert store.delete_fact(fact.id) is False
    assert store.list_facts() == []
