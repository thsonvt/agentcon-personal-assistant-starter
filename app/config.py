"""Load settings from environment variables and fail fast if any are missing."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

REQUIRED = (
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "WA_TOKEN",
    "WA_PHONE_NUMBER_ID",
    "WA_APP_SECRET",
    "WA_VERIFY_TOKEN",
    "OWNER_WHATSAPP",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_REFRESH_TOKEN",
    "CALENDLY_TOKEN",
    "CALENDLY_EVENT_TYPE_URI",
    "DB_PATH",
)


# Plan 2B sign-up (D23): all or none. Without them the bot runs exactly as before, for Pippa only.
SIGNUP_SETTINGS = (
    "GOOGLE_WEB_CLIENT_ID",
    "GOOGLE_WEB_CLIENT_SECRET",
    "BASE_URL",
    "BOT_WHATSAPP",
    "TOKEN_KEY",
    "SESSION_SECRET",
)


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    openai_model: str
    wa_token: str
    wa_phone_number_id: str
    wa_app_secret: str
    wa_verify_token: str
    owner_whatsapp: str  # digits only, e.g. "6591234567"
    google_client_id: str
    google_client_secret: str
    google_refresh_token: str
    calendly_token: str
    calendly_event_type_uri: str
    db_path: str
    google_calendar_id: str = "primary"
    openai_transcribe_model: str = "whisper-1"
    owner_name: str = "Pippa"
    owner_voice_language: str = "en"  # owner reported English notes; other users retain auto detection
    # Sign-up (Plan 2B); empty when sign-up is off.
    google_web_client_id: str = ""
    google_web_client_secret: str = ""
    base_url: str = ""
    bot_whatsapp: str = ""
    token_key: str = ""
    session_secret: str = ""
    max_users: int = 5
    beta_auto_approve: bool = False
    beta_allowed_emails: tuple[str, ...] = ()
    whatsapp_test_mode: bool = True
    event_networking_enabled: bool = False  # explicit pilot release gate
    conference_capture_enabled: bool = False
    proactive_briefs_enabled: bool = False
    capture_model: str = ''  # default to OPENAI_MODEL; must support image inputs/structured outputs
    elevenlabs_api_key: str = ''
    elevenlabs_voice_id: str = ''
    loops_api_key: str = field(default='', repr=False)
    booking_host_email_id: str = ''
    followups_enabled: bool = False
    followup_reminder_template: str = ''

    @property
    def signup_enabled(self) -> bool:
        return bool(self.token_key)


def normalize_phone(raw: str) -> str:
    """Keep digits only. Meta sends '6591234567'; people type '+65 9123 4567'."""
    return "".join(ch for ch in raw if ch.isdigit())


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    missing = [key for key in REQUIRED if not env.get(key, "").strip()]
    if missing:
        raise ConfigError("Missing required environment variables: " + ", ".join(missing))
    owner = normalize_phone(env["OWNER_WHATSAPP"])
    if not owner:
        raise ConfigError("OWNER_WHATSAPP must contain a phone number")
    signup = _signup_settings(env)
    capture_enabled = _bool_setting(env, 'CONFERENCE_CAPTURE_ENABLED', False)
    if capture_enabled and (not signup.get('session_secret') or not signup.get('base_url', '').startswith('https://')):
        raise ConfigError('CONFERENCE_CAPTURE_ENABLED requires HTTPS BASE_URL and SESSION_SECRET in the complete signup configuration')
    auto_approve = _bool_setting(env, "BETA_AUTO_APPROVE", False)
    test_mode = _bool_setting(env, "WHATSAPP_TEST_MODE", True)
    allowed = tuple(sorted({s.strip().lower() for s in env.get("BETA_ALLOWED_EMAILS", "").split(",") if s.strip()}))
    if auto_approve and not allowed:
        raise ConfigError("BETA_AUTO_APPROVE requires explicit BETA_ALLOWED_EMAILS")
    if any("@" not in email or "." not in email.rsplit("@", 1)[-1] or any(c.isspace() for c in email) or "*" in email for email in allowed):
        raise ConfigError("BETA_ALLOWED_EMAILS must contain exact email addresses, separated by commas")
    language = env.get("OWNER_VOICE_LANGUAGE", "en").strip().lower() or "auto"
    if language != "auto" and (len(language) != 2 or not language.isascii() or not language.isalpha()):
        raise ConfigError("OWNER_VOICE_LANGUAGE must be a two-letter language code or auto")
    return Settings(
        **signup,
        beta_auto_approve=auto_approve,
        beta_allowed_emails=allowed,
        whatsapp_test_mode=test_mode,
        event_networking_enabled=_bool_setting(env, "EVENT_NETWORKING_ENABLED", False),
        conference_capture_enabled=capture_enabled,
        proactive_briefs_enabled=_bool_setting(env, 'PROACTIVE_BRIEFS_ENABLED', False),
        capture_model=env.get('CAPTURE_MODEL', '').strip(),
        elevenlabs_api_key=env.get('ELEVENLABS_API_KEY', '').strip(),
        elevenlabs_voice_id=env.get('ELEVENLABS_VOICE_ID', '').strip(),
        loops_api_key=env.get('LOOPS_API_KEY', '').strip(),
        booking_host_email_id=env.get('LOOPS_BOOKING_HOST_EMAIL_ID', '').strip(),
        followups_enabled=_bool_setting(env, 'FOLLOWUPS_ENABLED', False),
        followup_reminder_template=env.get('FOLLOWUP_REMINDER_TEMPLATE', '').strip(),
        owner_name=env.get("OWNER_NAME", "").strip() or "Pippa",
        owner_voice_language=language,
        openai_api_key=env["OPENAI_API_KEY"].strip(),
        openai_model=env["OPENAI_MODEL"].strip(),
        wa_token=env["WA_TOKEN"].strip(),
        wa_phone_number_id=env["WA_PHONE_NUMBER_ID"].strip(),
        wa_app_secret=env["WA_APP_SECRET"].strip(),
        wa_verify_token=env["WA_VERIFY_TOKEN"].strip(),
        owner_whatsapp=owner,
        google_client_id=env["GOOGLE_CLIENT_ID"].strip(),
        google_client_secret=env["GOOGLE_CLIENT_SECRET"].strip(),
        google_refresh_token=env["GOOGLE_REFRESH_TOKEN"].strip(),
        calendly_token=env["CALENDLY_TOKEN"].strip(),
        calendly_event_type_uri=env["CALENDLY_EVENT_TYPE_URI"].strip(),
        db_path=env["DB_PATH"].strip(),
        google_calendar_id=env.get("GOOGLE_CALENDAR_ID", "").strip() or "primary",
        openai_transcribe_model=env.get("OPENAI_TRANSCRIBE_MODEL", "").strip() or "whisper-1",
    )


def _bool_setting(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = env.get(name, "").strip().lower()
    if not value:
        return default
    if value in ("true", "1", "yes"):
        return True
    if value in ("false", "0", "no"):
        return False
    raise ConfigError(f"{name} must be true or false")


def _signup_settings(env: Mapping[str, str]) -> dict:
    from app.crypto import valid_key

    given = {key: env.get(key, "").strip() for key in SIGNUP_SETTINGS}
    try:
        max_users = int(env.get("MAX_USERS", "").strip() or 5)
    except ValueError:
        raise ConfigError("MAX_USERS must be a whole number, e.g. 5") from None
    if not any(given.values()):
        return {"max_users": max_users}
    missing = [key for key, value in given.items() if not value]
    if missing:
        raise ConfigError("Sign-up is half set up. Also set: " + ", ".join(missing))
    if not valid_key(given["TOKEN_KEY"]):
        raise ConfigError("TOKEN_KEY isn't a valid key. Generate one with scripts/check_setup.py --new-key")
    if len(given["SESSION_SECRET"]) < 32:
        raise ConfigError("SESSION_SECRET should be at least 32 random characters.")
    if not given["BASE_URL"].startswith("https://"):
        raise ConfigError("BASE_URL must start with https://, e.g. https://your-app.up.railway.app")
    return {
        "google_web_client_id": given["GOOGLE_WEB_CLIENT_ID"],
        "google_web_client_secret": given["GOOGLE_WEB_CLIENT_SECRET"],
        "base_url": given["BASE_URL"].rstrip("/"),
        "bot_whatsapp": normalize_phone(given["BOT_WHATSAPP"]),
        "token_key": given["TOKEN_KEY"],
        "session_secret": given["SESSION_SECRET"],
        "max_users": max_users,
    }
