"""Plan 2B: every user's contacts, facts and loop-in tasks are theirs alone, and old databases upgrade safely."""

import sqlite3
from datetime import datetime, timedelta

from app.store import OLD_SCHEMA_FOR_TESTS, Store
from app.timeutil import SGT

NOW = datetime(2026, 10, 1, 10, tzinfo=SGT)


def test_default_store_is_user_1():
    # Everything Plan 1/2 wrote belongs to Pippa, who is user #1.
    assert Store(":memory:").user_id == 1


def test_contacts_are_per_user_and_share_no_rows():
    root = Store(":memory:")
    Pippa, edmund = root.for_user(1), root.for_user(2)
    Pippa.add_contact("Zerrie", "6582220000")
    edmund.add_contact("Zerrie (school)", "6582220000")  # same phone, different user: allowed
    assert [c.name for c in Pippa.list_contacts()] == ["Zerrie"]
    assert [c.name for c in edmund.list_contacts()] == ["Zerrie (school)"]
    assert Pippa.get_contact_by_phone("6582220000").user_id == 1
    assert edmund.find_contact("zerrie").user_id == 2
    assert Pippa.get_contact(edmund.list_contacts()[0].id) is None  # can't read another user's contact by id


def test_facts_are_per_user_including_working_hours():
    root = Store(":memory:")
    Pippa, edmund = root.for_user(1), root.for_user(2)
    Pippa.add_fact("Prefers Zoom", NOW)
    Pippa.add_fact("Working hours 10:00-18:00", NOW, key="working_hours")
    edmund.add_fact("Working hours 08:00-12:00", NOW, key="working_hours")  # must not replace Pippa's
    assert [f.fact for f in Pippa.list_facts()] == ["Prefers Zoom", "Working hours 10:00-18:00"]
    assert edmund.get_fact("working_hours").fact == "Working hours 08:00-12:00"
    Pippa_fact = Pippa.list_facts()[0]
    assert edmund.delete_fact(Pippa_fact.id) is False  # can't delete someone else's fact
    assert len(Pippa.list_facts()) == 2


def test_tasks_are_per_user_and_carry_their_owner():
    root = Store(":memory:")
    edmund = root.for_user(2)
    c = edmund.add_contact("Akash", "6583330000")
    task = edmund.create_loop_in_task(c.id, "a call", 30, NOW, NOW + timedelta(days=2), NOW)
    assert task.user_id == 2
    assert root.for_user(1).get_task(task.id) is None
    assert edmund.get_task(task.id).purpose == "a call"


def test_open_invite_lookup_spans_all_users():
    # The router must find an invite whoever sent it (D25).
    root = Store(":memory:")
    edmund = root.for_user(2)
    c = edmund.add_contact("Akash", "+65 8333 0000")
    task = edmund.create_loop_in_task(c.id, "a call", 30, NOW, NOW + timedelta(days=2), NOW)
    found = root.find_open_invite("6583330000")
    assert found is not None
    contact, open_task = found
    assert (contact.user_id, open_task.id) == (2, task.id)
    edmund.set_task_status(task.id, "done", NOW)
    assert root.find_open_invite("6583330000") is None


def test_overdue_tasks_are_found_for_every_user():
    root = Store(":memory:")
    old = NOW - timedelta(days=3)
    for uid, phone in ((1, "6581110000"), (2, "6582220000")):
        s = root.for_user(uid)
        c = s.add_contact(f"C{uid}", phone)
        s.create_loop_in_task(c.id, "a call", 30, old, NOW + timedelta(days=2), old)
    assert sorted(t.user_id for t in root.tasks_waiting_since(NOW - timedelta(hours=48))) == [1, 2]


def _old_database(path):
    """A database exactly as Plan 1/2 left it: no user_id columns, Pippa's rows only."""
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA_FOR_TESTS)
    conn.execute("INSERT INTO contacts (name, phone, email) VALUES ('Edmund', '6581234567', 'e@example.com')")
    conn.execute("INSERT INTO profile_facts (key, fact, created_at) VALUES (NULL, 'Prefers Zoom', ?)", (NOW.isoformat(),))
    conn.execute(
        "INSERT INTO loop_in_tasks (contact_id, purpose, duration_min, window_start, window_end, status, created_at, updated_at) "
        "VALUES (1, 'a call', 30, ?, ?, 'waiting', ?, ?)",
        (NOW.isoformat(), (NOW + timedelta(days=2)).isoformat(), NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute("INSERT INTO seen_messages VALUES ('wamid.1', ?)", (NOW.isoformat(),))
    conn.commit()
    conn.close()


def test_old_database_upgrades_with_everything_owned_by_Pippa(tmp_path):
    # Review Focus: the Railway database must survive the upgrade with nothing lost.
    path = str(tmp_path / "agent.db")
    _old_database(path)
    store = Store(path)
    assert [(c.name, c.user_id, c.email) for c in store.list_contacts()] == [("Edmund", 1, "e@example.com")]
    assert [f.fact for f in store.list_facts()] == ["Prefers Zoom"]
    task = store.get_open_task(store.list_contacts()[0].id)
    assert (task.purpose, task.user_id) == ("a call", 1)
    assert store.mark_message_seen("wamid.1", NOW) is False  # dedupe history kept
    assert store.for_user(2).list_contacts() == []


def test_upgrade_is_safe_to_run_twice(tmp_path):
    path = str(tmp_path / "agent.db")
    _old_database(path)
    Store(path)
    store = Store(path)  # second startup
    assert len(store.list_contacts()) == 1
    assert len(store.list_facts()) == 1
    # And the new per-user uniqueness holds: the same phone can now be saved by another user.
    store.for_user(2).add_contact("Edmund (work)", "6581234567")
    assert len(store.list_contacts()) == 1


def test_status_changes_and_recent_bookings_stay_with_their_user():
    root = Store(":memory:")
    edmund = root.for_user(2)
    c = edmund.add_contact("Akash", "6583330000")
    task = edmund.create_loop_in_task(c.id, "a call", 30, NOW, NOW + timedelta(days=2), NOW)
    assert root.mark_negotiating(task.id, NOW) is None  # Pippa's view can't touch Edmund's task
    assert edmund.get_task(task.id).status == "waiting"
    assert edmund.mark_negotiating(task.id, NOW).status == "negotiating"
    edmund.set_task_status(task.id, "done", NOW)
    assert root.recent_closed_task(c.id, NOW - timedelta(days=7)) is None
    contact, booked = root.find_recent_booking("6583330000", NOW - timedelta(days=7))
    assert (contact.user_id, booked.id) == (2, task.id)
    assert root.find_recent_booking("6583330000", NOW + timedelta(minutes=1)) is None
