"""Plan 2B: sign-up settings, encrypted Google tokens and signed cookies."""

from datetime import datetime, timedelta

import pytest

from app.config import REQUIRED, SIGNUP_SETTINGS, ConfigError, load_settings
from app.crypto import TokenBox, generate_key, sign, unsign
from app.timeutil import SGT

NOW = datetime(2026, 10, 1, 10, tzinfo=SGT)


def base_env(**extra):
    env = {key: "x" for key in REQUIRED} | {"OWNER_WHATSAPP": "6591234567"}
    return env | extra


def signup_env(**overrides):
    values = {
        "GOOGLE_WEB_CLIENT_ID": "web-id",
        "GOOGLE_WEB_CLIENT_SECRET": "web-secret",
        "BASE_URL": "https://bot.example.com/",
        "BOT_WHATSAPP": "+1 555 000 1234",
        "TOKEN_KEY": generate_key(),
        "SESSION_SECRET": "s" * 32,
    }
    return base_env(**(values | overrides))


def test_signup_is_off_without_its_settings():
    # Today's Railway settings must keep working unchanged.
    settings = load_settings(base_env())
    assert settings.signup_enabled is False
    assert settings.owner_name == "Pippa"
    assert settings.max_users == 5


def test_signup_is_on_with_all_its_settings():
    settings = load_settings(signup_env(OWNER_NAME="Pippa Le", MAX_USERS="4"))
    assert settings.signup_enabled
    assert settings.base_url == "https://bot.example.com"  # trailing slash trimmed
    assert settings.bot_whatsapp == "15550001234"
    assert (settings.owner_name, settings.max_users) == ("Pippa Le", 4)


def test_half_configured_signup_names_what_is_missing():
    env = signup_env()
    del env["TOKEN_KEY"]
    del env["BASE_URL"]
    with pytest.raises(ConfigError) as err:
        load_settings(env)
    assert "TOKEN_KEY" in str(err.value) and "BASE_URL" in str(err.value)
    assert set(SIGNUP_SETTINGS) >= {"TOKEN_KEY", "BASE_URL", "SESSION_SECRET"}


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"TOKEN_KEY": "not-a-key"}, "TOKEN_KEY"),
        ({"SESSION_SECRET": "short"}, "SESSION_SECRET"),
        ({"BASE_URL": "http://bot.example.com"}, "https"),
        ({"MAX_USERS": "lots"}, "MAX_USERS"),
    ],
)
def test_bad_signup_settings_are_explained(overrides, message):
    with pytest.raises(ConfigError, match=message):
        load_settings(signup_env(**overrides))


def test_token_round_trips_only_with_the_right_key():
    box = TokenBox(generate_key())
    sealed = box.encrypt("1//refresh-token")
    assert "refresh" not in sealed
    assert box.decrypt(sealed) == "1//refresh-token"
    with pytest.raises(ValueError):
        TokenBox(generate_key()).decrypt(sealed)


def test_signed_value_round_trips_and_rejects_tampering_and_age():
    token = sign("user:7", "secret", NOW)
    assert unsign(token, "secret", NOW + timedelta(minutes=29), max_age=timedelta(minutes=30)) == "user:7"
    assert unsign(token, "other-secret", NOW, max_age=timedelta(minutes=30)) is None
    assert unsign(token.replace("user:7", "user:1"), "secret", NOW, max_age=timedelta(minutes=30)) is None
    assert unsign(token, "secret", NOW + timedelta(minutes=31), max_age=timedelta(minutes=30)) is None
    assert unsign("garbage", "secret", NOW, max_age=timedelta(minutes=30)) is None
