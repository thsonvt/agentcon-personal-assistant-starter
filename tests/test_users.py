"""Plan 2B: sign-ups, approval and join codes (D24)."""

from datetime import datetime, timedelta

import pytest

from app.store import Store
from app.timeutil import SGT
from app.users import CODE_ALPHABET, SignupError, SignupsFull, UnknownCode, Users

NOW = datetime(2026, 10, 1, 10, tzinfo=SGT)


@pytest.fixture
def users():
    u = Users(Store(":memory:"), max_users=3)
    u.ensure_admin("Pippa", "6591234567", NOW)
    return u


def signup(users, name="Edmund Lim", email="edmund@example.com", phone="+65 8123 4567", link=None):
    user, is_new = users.start_signup(name, email, "enc-token", NOW)
    return users.set_details(user.id, phone, link, NOW), is_new


def test_admin_is_user_1_and_active(users):
    Pippa = users.get(1)
    assert (Pippa.name, Pippa.phone, Pippa.status, Pippa.is_admin) == ("Pippa", "6591234567", "active", True)
    users.ensure_admin("Pippa Le", "6591234567", NOW)  # every startup: updates, never duplicates
    assert [u.name for u in users.list()] == ["Pippa Le"]


def test_signup_creates_a_pending_user_with_a_join_code(users):
    user, is_new = signup(users)
    assert is_new
    assert (user.status, user.typed_phone, user.phone, user.joined) == ("pending", "6581234567", None, False)
    assert len(user.join_code) == 6 and set(user.join_code) <= set(CODE_ALPHABET)
    assert "0" not in CODE_ALPHABET and "O" not in CODE_ALPHABET and "1" not in CODE_ALPHABET and "I" not in CODE_ALPHABET


def test_same_email_signing_in_again_replaces_the_token(users):
    # The re-auth path: expired Google access is fixed by signing in again.
    first, _ = signup(users)
    again, is_new = users.start_signup("Edmund Lim", "EDMUND@example.com", "new-token", NOW)
    assert not is_new and again.id == first.id
    assert users.get(first.id).google_token_enc == "new-token"


def test_approve_then_join_activates(users):
    user, _ = signup(users)
    assert users.approve(user.join_code, NOW).status == "approved"
    joined = users.join(user.join_code, "6581234567", NOW)
    assert (joined.status, joined.phone, joined.joined) == ("active", "6581234567", True)
    assert users.by_phone("+65 8123 4567").id == user.id


def test_join_then_approve_activates(users):
    user, _ = signup(users)
    assert users.join(user.join_code, "6581234567", NOW).status == "pending"
    assert users.approve(user.join_code, NOW).status == "active"


def test_join_rejects_a_sender_different_from_the_entered_number(users):
    user, _ = signup(users, phone="6581234567")
    with pytest.raises(SignupError, match="exact WhatsApp number"):
        users.join(user.join_code, "6589990000", NOW)
    assert users.get(user.id).phone is None


def test_codes_work_in_any_case_and_with_spaces(users):
    user, _ = signup(users)
    assert users.join(f"  {user.join_code.lower()} ", "6581234567", NOW).joined


def test_code_is_single_use(users):
    user, _ = signup(users)
    users.join(user.join_code, "6581234567", NOW)
    with pytest.raises(SignupError, match="already been used"):
        users.join(user.join_code, "6589990000", NOW)


def test_code_expires_after_thirty_minutes(users):
    user, _ = signup(users)
    with pytest.raises(SignupError, match="expired"):
        users.join(user.join_code, "6581234567", NOW + timedelta(minutes=30))


def test_unknown_code(users):
    with pytest.raises(UnknownCode):
        users.join("ZZZZ", "6581234567", NOW)
    with pytest.raises(UnknownCode):
        users.approve("ZZZZ", NOW)


def test_rejected_user_cannot_join(users):
    user, _ = signup(users)
    users.reject(user.join_code, NOW)
    with pytest.raises(SignupError, match="not approved"):
        users.join(user.join_code, "6581234567", NOW)


def test_number_already_linked_to_someone_else_is_refused(users):
    with pytest.raises(SignupError, match="already"):
        signup(users, phone="+65 9123 4567")  # Pippa's number
    first, _ = signup(users)
    users.join(first.join_code, "6581234567", NOW)
    with pytest.raises(SignupError, match="already"):
        signup(users, name="Other", email="other@gmail.com", phone="6581234567")


@pytest.mark.parametrize("phone", ["12345", "+65 8123 4567 8901 234", "call me"])
def test_bad_numbers_are_refused(users, phone):
    with pytest.raises(SignupError, match="country code"):
        signup(users, phone=phone)


def test_booking_link_must_be_https(users):
    with pytest.raises(SignupError, match="https"):
        signup(users, link="calendly.com/edmund")
    user, _ = signup(users, link="https://calendly.com/edmund/30min")
    assert user.calendly_url == "https://calendly.com/edmund/30min"


def test_signups_stop_at_max_users(users):
    # max_users=3: Pippa + 2. Rejected sign-ups free their seat.
    signup(users, email="a@gmail.com", phone="6581110000")
    b, _ = signup(users, email="b@gmail.com", phone="6582220000")
    assert users.seats_left() == 0
    with pytest.raises(SignupsFull):
        users.start_signup("C", "c@gmail.com", "tok", NOW)
    users.reject(b.join_code, NOW)
    assert users.seats_left() == 1
    # A returning user can always sign in again, even when full.
    signup(users, email="x@gmail.com", phone="6583330000")
    users.start_signup("A", "a@gmail.com", "tok2", NOW)


def test_disable_by_last_four_digits(users):
    user, _ = signup(users, phone="6581112222")
    users.join(user.join_code, "6581112222", NOW)
    users.approve(user.join_code, NOW)
    assert users.disable("2222", NOW).status == "disabled"
    with pytest.raises(SignupError, match="admin"):
        users.disable(users.get(1).phone[-4:], NOW)  # the admin can't be disabled


def test_delete_removes_the_user_and_all_their_rows(users):
    user, _ = signup(users)
    users.join(user.join_code, "6581234567", NOW)
    users.approve(user.join_code, NOW)
    mine = users.store.for_user(user.id)
    c = mine.add_contact("Akash", "6583330000")
    mine.create_loop_in_task(c.id, "a call", 30, NOW, NOW + timedelta(days=1), NOW)
    mine.add_fact("Likes mornings", NOW)
    Pippa = users.store.for_user(1)
    Pippa.add_fact("Prefers Zoom", NOW)
    users.delete(user.id)
    assert users.get(user.id) is None
    assert mine.list_contacts() == [] and mine.list_facts() == []
    assert [f.fact for f in Pippa.list_facts()] == ["Prefers Zoom"]  # nobody else's data touched
    with pytest.raises(SignupError, match="admin"):
        users.delete(1)
