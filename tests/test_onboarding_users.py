"""Selected early adopters activate only after consent and phone ownership checks."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from app.commands import Commands
from app.store import Store
from app.users import SignupError, SignupsFull, Users, USERS_SCHEMA
from tests.fakes import FakeSender
from tests.test_users import NOW

CALENDAR = "https://www.googleapis.com/auth/calendar"
GMAIL = "https://www.googleapis.com/auth/gmail.readonly"
PHONE = "6581112222"


def automatic(max_users=3):
    users = Users(Store(":memory:"), max_users=max_users,
                  allowed_emails={" Early@Example.com ", "second@example.com"}, auto_activate=True)
    users.ensure_admin("Son", "6591234567", NOW)
    return users


def start(users, email="early@example.com", scopes=(CALENDAR,)):
    return users.start_signup("Early", email, "encrypted", NOW, granted_scopes=scopes)[0]


def test_auto_activation_requires_nonempty_allowlist():
    with pytest.raises(ValueError, match="allow"):
        Users(Store(":memory:"), auto_activate=True)


def test_auto_eligibility_uses_normalized_email_and_actual_calendar_grant():
    users = automatic()
    assert users.is_eligible(" EARLY@example.COM ")
    assert not users.is_eligible("stranger@example.com")
    with pytest.raises(SignupError):
        start(users, "stranger@example.com")
    for scopes in (None, (), (GMAIL,)):
        with pytest.raises(SignupError, match="Calendar"):
            start(users, scopes=scopes)
    assert len(users.list()) == 1
    user = start(users)
    assert user.status == "approved" and not user.joined
    assert user.calendar_authorized is True
    assert user.gmail_authorized is False


def test_activation_requires_join_from_exact_typed_number():
    users = automatic()
    user = users.set_details(start(users).id, PHONE, None, NOW)
    assert len(user.join_code) == 6
    with pytest.raises(SignupError, match="number"):
        users.join(user.join_code, "6589990000", NOW)
    assert users.get(user.id).status == "approved"
    assert users.get(user.id).phone is None
    joined = users.join(user.join_code, "+65 8111 2222", NOW)
    assert joined.status == "active" and joined.phone == PHONE


def test_join_code_expires_at_thirty_minutes_and_can_be_reissued_before_join():
    users = automatic()
    user = users.set_details(start(users).id, PHONE, None, NOW)
    with pytest.raises(SignupError, match="expired"):
        users.join(user.join_code, PHONE, NOW + timedelta(minutes=30))
    renewed = users.set_details(user.id, PHONE, None, NOW + timedelta(minutes=31))
    assert renewed.join_code != user.join_code
    assert users.join(renewed.join_code, PHONE, NOW + timedelta(minutes=32)).status == "active"


def test_joined_active_user_cannot_change_phone_or_reissue_code():
    users = automatic()
    user = users.set_details(start(users).id, PHONE, None, NOW)
    users.join(user.join_code, PHONE, NOW)
    for number in (PHONE, "6589990000"):
        with pytest.raises(SignupError, match="linked"):
            users.set_details(user.id, number, None, NOW)
    assert users.get(user.id).phone == PHONE


def test_pending_but_joined_user_cannot_reissue_or_change_phone():
    users = Users(Store(":memory:"))
    user = users.start_signup("Early", "early@example.com", "encrypted", NOW)[0]
    user = users.set_details(user.id, PHONE, None, NOW)
    users.join(user.join_code, PHONE, NOW)
    with pytest.raises(SignupError, match="linked"):
        users.set_details(user.id, "6589990000", None, NOW)


def test_active_reconnect_at_capacity_refreshes_token_and_scopes_together():
    users = automatic(max_users=2)
    user = users.set_details(start(users).id, PHONE, None, NOW)
    users.join(user.join_code, PHONE, NOW)
    again, is_new = users.start_signup("Early", "EARLY@example.com", "new-token", NOW,
                                       granted_scopes=(CALENDAR, GMAIL))
    assert not is_new and again.id == user.id and again.status == "active"
    assert again.google_token_enc == "new-token"
    assert again.google_scopes == (CALENDAR, GMAIL)
    assert again.gmail_authorized is True
    with pytest.raises(SignupsFull):
        start(users, "second@example.com")


def test_ineligible_reconnect_does_not_replace_token_or_grants():
    users = automatic()
    user = start(users)
    with pytest.raises(SignupError, match="Calendar"):
        users.start_signup("Changed", user.email, "bad-new-token", NOW, granted_scopes=(GMAIL,))
    assert users.get(user.id).google_token_enc == "encrypted"
    assert users.get(user.id).google_scopes == (CALENDAR,)


@pytest.mark.parametrize("status", ["rejected", "disabled"])
def test_blocked_user_cannot_reconnect_or_reissue(status):
    users = automatic()
    user = users.set_details(start(users).id, PHONE, None, NOW)
    users.conn.execute("UPDATE users SET status = ? WHERE id = ?", (status, user.id))
    users.conn.commit()
    with pytest.raises(SignupError):
        start(users)
    with pytest.raises(SignupError):
        users.set_details(user.id, PHONE, None, NOW)
    assert users.get(user.id).status == status


def test_legacy_grants_unknown_and_new_grants_persist_on_reopen(tmp_path):
    store = Store(str(tmp_path / "users.db"))
    users = Users(store)
    user = users.start_signup("Early", "early@example.com", "encrypted", NOW)[0]
    assert user.google_scopes is None
    assert user.calendar_authorized is None and user.gmail_authorized is None
    users.start_signup("Early", user.email, "new-token", NOW, granted_scopes=(CALENDAR, GMAIL))
    loaded = Users(Store(str(tmp_path / "users.db"))).get(user.id)
    assert loaded.google_scopes == (CALENDAR, GMAIL)
    assert loaded.google_token_enc == "new-token"


def test_migration_retains_legacy_user_and_marks_grants_unknown():
    store = Store(":memory:")
    store.conn.executescript(USERS_SCHEMA.replace("  google_scopes TEXT,\n", ""))
    store.conn.execute(
        "INSERT INTO users (name, email, status, google_token_enc, created_at, updated_at) "
        "VALUES ('Early', 'early@example.com', 'pending', 'old-token', ?, ?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    store.conn.commit()
    user = Users(store).by_email("early@example.com")
    assert user.google_token_enc == "old-token"
    assert user.google_scopes is None and user.calendar_authorized is None


def test_legacy_mismatched_join_cannot_be_activated_by_approval():
    users = Users(Store(":memory:"))
    user = users.start_signup("Early", "early@example.com", "encrypted", NOW)[0]
    user = users.set_details(user.id, PHONE, None, NOW)
    users.conn.execute("UPDATE users SET joined = 1, phone = '6589990000' WHERE id = ?", (user.id,))
    users.conn.commit()
    with pytest.raises(SignupError, match="number"):
        users.approve(user.join_code, NOW)
    assert users.get(user.id).status != "active"


def test_parallel_signups_do_not_overbook_last_seat():
    users = automatic(max_users=2)
    def signup(email):
        try:
            return start(users, email).status
        except SignupsFull:
            return "full"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(signup, ["early@example.com", "second@example.com"]))
    assert sorted(results) == ["approved", "full"]
    assert len(users.list()) == 2


def commands(users, sender=None, test_mode=True):
    return Commands(users, sender or FakeSender(), lambda: NOW, lambda token: token,
                    lambda token: None, base_url="https://example.com", test_number_mode=test_mode)


def test_auto_announcement_has_no_manual_approval_and_distinguishes_meta_setup():
    users = automatic()
    user = users.set_details(start(users).id, PHONE, None, NOW)
    sender = FakeSender()
    commands(users, sender).announce_signup(user)
    message = sender.sent[-1][1]
    assert "approve " not in message
    assert "Meta" in message
    assert "recipient" in message.lower()


def test_active_welcome_help_status_show_actual_capabilities_and_gmail_option():
    users = automatic()
    user = users.set_details(start(users).id, PHONE, None, NOW)
    cmd = commands(users, test_mode=False)
    welcome = cmd.handle(PHONE, f"join {user.join_code}")
    assert "calendar" in welcome.lower() and "voice" in welcome.lower()
    assert "Gmail" in welcome and "/signup/gmail" in welcome
    for text in ("help", "status"):
        reply = cmd.handle(PHONE, text)
        assert "Gmail" in reply and "/signup/gmail" in reply
        assert cmd.handle("6588889999", text) is None
    users.start_signup("Early", user.email, "new-token", NOW, granted_scopes=(CALENDAR, GMAIL))
    assert "Gmail: connected" in cmd.handle(PHONE, "status")


def test_legacy_four_character_join_code_still_parses():
    users = automatic()
    user = users.set_details(start(users).id, PHONE, None, NOW)
    users.conn.execute("UPDATE users SET join_code = 'ABCD' WHERE id = ?", (user.id,))
    users.conn.commit()
    assert "You're in" in commands(users).handle(PHONE, "join ABCD")


def test_disabled_users_help_does_not_promise_signup_can_reactivate_them():
    users = automatic()
    user = users.set_details(start(users).id, PHONE, None, NOW)
    users.join(user.join_code, PHONE, NOW)
    users.disable(PHONE[-4:], NOW)
    reply = commands(users).handle(PHONE, "help")
    assert "disabled" in reply and "organizer" in reply
    assert "Finish the signup" not in reply


def legacy_joined_user(status="pending"):
    users = Users(Store(":memory:"))
    users.ensure_admin("Son", "6591234567", NOW)
    user = users.start_signup("Early", "early@example.com", "legacy-token", NOW)[0]
    user = users.set_details(user.id, PHONE, None, NOW)
    users.join(user.join_code, PHONE, NOW)
    if status != "pending":
        users.conn.execute("UPDATE users SET status = ? WHERE id = ?", (status, user.id))
        users.conn.commit()
    automatic_users = Users(users.store, max_users=2, allowed_emails={user.email}, auto_activate=True)
    return automatic_users, automatic_users.get(user.id)


def test_failed_automatic_approval_of_legacy_joined_user_does_not_mutate_status():
    users, user = legacy_joined_user()
    with pytest.raises(SignupError, match="Calendar"):
        users.approve(user.join_code, NOW)
    assert users.get(user.id) == user


@pytest.mark.parametrize("status", ["pending", "approved"])
def test_valid_reconnect_activates_verified_legacy_join_and_consumes_old_code(status):
    users, user = legacy_joined_user(status)
    assert users.seats_left() == 0
    refreshed, is_new = users.start_signup(user.name, user.email, "new-token", NOW,
                                            granted_scopes=(CALENDAR, GMAIL))
    assert not is_new and refreshed.id == user.id
    assert refreshed.status == "active" and refreshed.phone == PHONE
    assert refreshed.google_token_enc == "new-token"
    assert refreshed.google_scopes == (CALENDAR, GMAIL)
    assert refreshed.join_code is None and refreshed.join_code_expires_at is None
    assert users.by_code(user.join_code) is None


@pytest.mark.parametrize("status", ["pending", "approved"])
@pytest.mark.parametrize("typed_phone", [None, "6589990000"])
def test_valid_reconnect_recovers_unverified_legacy_join_by_requiring_fresh_proof(status, typed_phone):
    users, user = legacy_joined_user(status)
    users.conn.execute("UPDATE users SET typed_phone = ? WHERE id = ?", (typed_phone, user.id))
    users.conn.commit()
    refreshed, is_new = users.start_signup(user.name, user.email, "new-token", NOW,
                                            granted_scopes=(CALENDAR,))
    assert not is_new and refreshed.status == "approved"
    assert refreshed.phone is None and not refreshed.joined
    assert refreshed.join_code is None and refreshed.join_code_expires_at is None
    assert users.by_code(user.join_code) is None
    assert users.by_phone(PHONE) is None
    # The authenticated user can re-enter their number, but only that sender can activate it.
    fresh = users.set_details(user.id, PHONE, None, NOW)
    with pytest.raises(SignupError, match="exact WhatsApp number"):
        users.join(fresh.join_code, "6589990000", NOW)
    assert users.join(fresh.join_code, PHONE, NOW).status == "active"


def test_failed_calendar_reconnect_does_not_reset_legacy_phone_state():
    users, user = legacy_joined_user()
    users.conn.execute("UPDATE users SET typed_phone = '6589990000' WHERE id = ?", (user.id,))
    users.conn.commit()
    before = users.get(user.id)
    with pytest.raises(SignupError, match="Calendar"):
        users.start_signup(user.name, user.email, "new-token", NOW, granted_scopes=(GMAIL,))
    assert users.get(user.id) == before


@pytest.mark.parametrize("status", ["active", "disabled", "rejected"])
def test_legacy_phone_recovery_never_resets_active_or_blocked_accounts(status):
    users, user = legacy_joined_user(status)
    users.conn.execute("UPDATE users SET typed_phone = '6589990000' WHERE id = ?", (user.id,))
    users.conn.commit()
    before = users.get(user.id)
    if status == "active":
        after, _ = users.start_signup(user.name, user.email, "new-token", NOW, granted_scopes=(CALENDAR,))
        for field in ("phone", "typed_phone", "joined", "status", "join_code", "join_code_expires_at"):
            assert getattr(after, field) == getattr(before, field)
    else:
        with pytest.raises(SignupError):
            users.start_signup(user.name, user.email, "new-token", NOW, granted_scopes=(CALENDAR,))
        assert users.get(user.id) == before
