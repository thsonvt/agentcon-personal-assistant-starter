"""Country-aware signup validation and stable, exact-number join proof."""
from datetime import datetime, timedelta

import pytest

from app.store import Store
from app.timeutil import SGT
from app.users import SignupError, Users

NOW = datetime(2026, 10, 1, 10, tzinfo=SGT)


@pytest.fixture
def signup():
    users = Users(Store(':memory:'))
    user, _ = users.start_signup('Tester', 'tester@example.com', 'token', NOW)
    return users, user


@pytest.mark.parametrize('phone,prefix,expected', [
    ('9000 0002', '+65', '6590000002'),
    ('+65 9000 0002', '+65', '6590000002'),
    ('+44 7700 900123', '+65', '447700900123'),
    ('6590000002', '', '6590000002'),
    ('(415) 555-0123', '+1', '14155550123'),
])
def test_phone_is_canonical_and_requires_real_join(signup, phone, prefix, expected):
    users, user = signup
    updated = users.set_details(user.id, phone, None, NOW, country_code=prefix)
    assert updated.typed_phone == expected
    assert updated.phone is None and not updated.joined and updated.status == 'pending'
    assert users.join(updated.join_code, expected, NOW).joined


@pytest.mark.parametrize('phone,prefix', [
    ('90000002', ''), ('90000002', '65'), ('90000002', '+0'),
    ('+65 9451594', ''), ('+65 900000022', ''), ('++6590000002', ''),
    ('call6590000002', ''), ('＋６５９４５１５９４３', ''), ('6590000002\u200b', ''),
    ('6590000002ext12', ''), ('0900000020', ''), ('+1234567890123456', ''),
])
def test_bad_phone_does_not_mutate_signup(signup, phone, prefix):
    users, user = signup
    with pytest.raises(SignupError):
        users.set_details(user.id, phone, None, NOW, country_code=prefix)
    assert users.get(user.id) == user


def test_same_canonical_number_reuses_unexpired_code_without_extending_expiry(signup):
    users, user = signup
    first = users.set_details(user.id, '+65 90000002', None, NOW)
    again = users.set_details(user.id, '90000002', None, NOW + timedelta(minutes=5), country_code='+65')
    assert (again.join_code, again.join_code_expires_at) == (first.join_code, first.join_code_expires_at)


def test_changed_or_expired_number_gets_new_code(signup):
    users, user = signup
    first = users.set_details(user.id, '+65 90000002', None, NOW)
    changed = users.set_details(user.id, '+65 94515944', None, NOW)
    assert changed.join_code != first.join_code and users.by_code(first.join_code) is None
    renewed = users.set_details(user.id, '+65 94515944', None, NOW + timedelta(minutes=30))
    assert renewed.join_code != changed.join_code


def test_national_number_cannot_bypass_canonical_phone_conflict(signup):
    users, user = signup
    users.set_details(user.id, '+65 90000002', None, NOW)
    other, _ = users.start_signup('Other', 'other@example.com', 'token', NOW)
    with pytest.raises(SignupError, match='already signed up'):
        users.set_details(other.id, '90000002', None, NOW, country_code='+65')
