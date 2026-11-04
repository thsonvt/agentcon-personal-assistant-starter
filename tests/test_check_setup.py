"""scripts/check_setup.py: a half-finished sign-up is reported on its own line; the other checks still run."""

import importlib.util
from pathlib import Path

from app.config import REQUIRED

spec = importlib.util.spec_from_file_location("check_setup", Path(__file__).parent.parent / "scripts" / "check_setup.py")
check_setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_setup)


def core_env():
    env = {key: "x" for key in REQUIRED}
    env["OWNER_WHATSAPP"] = "6591234567"
    return env


def test_half_set_sign_up_still_loads_the_core_settings():
    env = core_env() | {"BASE_URL": "https://example.com", "GOOGLE_WEB_CLIENT_ID": "id", "GOOGLE_WEB_CLIENT_SECRET": "s"}
    settings, problem = check_setup.load_for_check(env)
    assert settings is not None and not settings.signup_enabled
    assert "BOT_WHATSAPP" in problem and "--new-key" in problem and "won't start" in problem
    assert "GOOGLE_WEB_CLIENT_ID:" not in problem  # only the missing ones get a fix line


def test_complete_or_absent_sign_up_has_no_problem():
    settings, problem = check_setup.load_for_check(core_env())
    assert settings is not None and problem is None


def test_core_problems_are_still_fatal():
    settings, problem = check_setup.load_for_check({"BASE_URL": "https://example.com"})
    assert settings is None and "Missing required" in problem
