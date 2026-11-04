import pytest

from app.config import REQUIRED, ConfigError, load_settings, normalize_phone


def full_env(**overrides):
    env = {key: f"value-{key.lower()}" for key in REQUIRED}
    env["OWNER_WHATSAPP"] = "+65 9123 4567"
    env.update(overrides)
    return env


def test_loads_all_required_values():
    settings = load_settings(full_env())
    assert settings.openai_model == "value-openai_model"
    assert settings.db_path == "value-db_path"


def test_missing_values_are_all_listed():
    env = full_env()
    del env["WA_TOKEN"]
    env["CALENDLY_TOKEN"] = "   "
    with pytest.raises(ConfigError) as err:
        load_settings(env)
    assert "WA_TOKEN" in str(err.value)
    assert "CALENDLY_TOKEN" in str(err.value)


def test_owner_phone_is_normalized_to_digits():
    assert load_settings(full_env()).owner_whatsapp == "6591234567"


def test_owner_phone_without_digits_is_rejected():
    with pytest.raises(ConfigError):
        load_settings(full_env(OWNER_WHATSAPP="me"))


def test_calendar_id_defaults_to_primary():
    assert load_settings(full_env()).google_calendar_id == "primary"
    assert load_settings(full_env(GOOGLE_CALENDAR_ID="test@group")).google_calendar_id == "test@group"


@pytest.mark.parametrize("raw", ["+65 9123 4567", "6591234567", "+65-9123-4567", "(65) 9123 4567"])
def test_normalize_phone(raw):
    assert normalize_phone(raw) == "6591234567"
