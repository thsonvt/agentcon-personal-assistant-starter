from datetime import datetime, timedelta

import pytest

from app.store import OpenTaskExists, Store
from app.timeutil import SGT

NOW = datetime(2026, 9, 28, 10, tzinfo=SGT)


@pytest.fixture
def store():
    return Store(":memory:")


def test_fresh_db_has_all_tables(store):
    assert {"contacts", "profile_facts", "loop_in_tasks", "seen_messages", "unknown_replies"} <= store.table_names()


def test_file_db_creates_parent_folder(tmp_path):
    Store(str(tmp_path / "nested" / "agent.db"))
    assert (tmp_path / "nested" / "agent.db").exists()


def test_add_and_find_contact_by_any_phone_format(store):
    store.add_contact("Edmund", "+65 8123 4567", "edmund@example.com")
    found = store.get_contact_by_phone("6581234567")
    assert found is not None
    assert found.name == "Edmund"
    assert found.email == "edmund@example.com"


def test_adding_same_phone_updates_name_keeps_email(store):
    store.add_contact("Ed", "6581234567", "edmund@example.com")
    store.add_contact("Edmund Lim", "+6581234567")
    found = store.get_contact_by_phone("6581234567")
    assert found.name == "Edmund Lim"
    assert found.email == "edmund@example.com"


def test_unknown_phone_returns_none(store):
    assert store.get_contact_by_phone("6500000000") is None


def test_only_one_open_task_per_contact(store):
    contact = store.add_contact("Edmund", "6581234567")
    store.create_loop_in_task(contact.id, "a 30-minute call", 30, NOW, NOW + timedelta(days=7), NOW)
    with pytest.raises(OpenTaskExists):
        store.create_loop_in_task(contact.id, "another call", 30, NOW, NOW + timedelta(days=7), NOW)


def test_open_task_is_returned(store):
    contact = store.add_contact("Edmund", "6581234567")
    created = store.create_loop_in_task(contact.id, "a call", 30, NOW, NOW + timedelta(days=7), NOW)
    task = store.get_open_task(contact.id)
    assert task == created
    assert task.status == "waiting"


def test_duplicate_message_id_detected(store):
    assert store.mark_message_seen("wamid.1", NOW) is True
    assert store.mark_message_seen("wamid.1", NOW) is False
    assert store.mark_message_seen("wamid.2", NOW) is True


def test_unknown_reply_at_most_once_per_day(store):
    assert store.should_send_unknown_reply("6590000000", NOW) is True
    assert store.should_send_unknown_reply("6590000000", NOW + timedelta(hours=23)) is False
    assert store.should_send_unknown_reply("6590000000", NOW + timedelta(hours=24)) is True
