"""Check every connection in .env for real, without printing any secret.

    python scripts/check_setup.py
    python scripts/check_setup.py --new-key   # print a fresh TOKEN_KEY and SESSION_SECRET for sign-up

Each line is ✅ (working) or ❌ (with the reason). Safe to paste the output into a chat.
"""

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from dotenv import dotenv_values, load_dotenv  # noqa: E402

from app.config import SIGNUP_SETTINGS, ConfigError, load_settings  # noqa: E402
from app.timeutil import SGT  # noqa: E402
from app.tools.whatsapp import GRAPH_URL  # noqa: E402

failures = 0


def google_hint(exc: Exception) -> str:
    """Google's own reason, e.g. 'accessNotConfigured' = Calendar API not enabled."""
    cause = exc.__cause__
    reason = ""
    try:
        import json

        err = json.loads(cause.content)["error"]
        reason = (err.get("errors") or [{}])[0].get("reason", "") or err.get("status", "")
        message = err.get("message", "")
    except Exception:  # noqa: BLE001
        message = str(cause or "")
    hints = {
        "accessNotConfigured": "enable the Google Calendar API: console.cloud.google.com/apis/library/calendar-json.googleapis.com",
        "SERVICE_DISABLED": "enable the Google Calendar API: console.cloud.google.com/apis/library/calendar-json.googleapis.com",
        "insufficientPermissions": "the refresh token lacks the calendar scope; re-run scripts/get_google_token.py and tick the Calendar box",
        "ACCESS_TOKEN_SCOPE_INSUFFICIENT": "the refresh token lacks the calendar scope; re-run scripts/get_google_token.py and tick the Calendar box",
        "forbidden": "the signed-in account can't access GOOGLE_CALENDAR_ID; use 'primary' or a calendar you own",
    }
    hint = hints.get(reason, "")
    return f"\n     Google says: {reason or '?'}: {message[:160]}" + (f"\n     Fix: {hint}" if hint else "")


def shape(value: str) -> str:
    """Describe a secret's shape without revealing it."""
    odd = [name for ch, name in (('"', "quote"), ("'", "quote"), (" ", "space"), ("\n", "line break")) if ch in value]
    return f"{len(value)} chars" + (f", contains {', '.join(sorted(set(odd)))}" if odd else "")


def report(ok: bool, label: str, detail: str = "") -> None:
    global failures
    failures += 0 if ok else 1
    print(f"{'✅' if ok else '❌'} {label}{' — ' + detail if detail else ''}")


SIGNUP_FIXES = {
    "GOOGLE_WEB_CLIENT_ID": "the Client ID of the Web application client (Google Cloud → Clients)",
    "GOOGLE_WEB_CLIENT_SECRET": "the Client secret of that same Web application client",
    "BASE_URL": "the public address, e.g. https://example.com",
    "BOT_WHATSAPP": "the bot's own WhatsApp number with country code, e.g. 15550100100",
    "TOKEN_KEY": "generate with: python scripts/check_setup.py --new-key",
    "SESSION_SECRET": "generate with: python scripts/check_setup.py --new-key",
}


def load_for_check(env):
    """(settings, problem). A sign-up problem doesn't stop the other checks: they run with sign-up off.
    A problem with the core settings returns (None, message)."""
    try:
        return load_settings(env), None
    except ConfigError as exc:
        signup_error = str(exc)
    try:
        settings = load_settings({k: v for k, v in env.items() if k not in SIGNUP_SETTINGS})
    except ConfigError as exc:
        return None, str(exc)
    missing = [k for k in SIGNUP_SETTINGS if not env.get(k, "").strip()]
    lines = [signup_error] + [f"   {k}: {SIGNUP_FIXES[k]}" for k in missing]
    lines.append("   The bot won't start with sign-up half set up. Set these, or remove all six to keep sign-up off.")
    return settings, "\n".join(lines)


def new_keys() -> int:
    """Fresh secrets for Plan 2B sign-up. Paste them into .env / Railway; they are not stored anywhere."""
    import secrets

    from app.crypto import generate_key

    print(f"TOKEN_KEY={generate_key()}")
    print(f"SESSION_SECRET={secrets.token_urlsafe(48)}")
    print("\nKeep TOKEN_KEY safe: without it, stored Google tokens can't be read (friends would sign in again).")
    return 0


def main() -> int:
    if "--new-key" in sys.argv:
        return new_keys()
    file_values = dotenv_values(".env")
    if not file_values:
        report(False, ".env file", "not found here; run this from the instinct-ai-clone folder")
    shadowed = [k for k, v in file_values.items() if k in os.environ and v is not None and os.environ[k] != v]
    if shadowed:
        report(False, "Terminal overrides .env", f"{', '.join(shadowed)} set in this terminal with a different value, so .env is ignored for them. Run: unset {' '.join(shadowed)}")
    load_dotenv()
    s, signup_problem = load_for_check(os.environ)
    if s is None:
        report(False, ".env", signup_problem)
        return 1
    report(True, ".env", "all required values present")
    if signup_problem:
        report(False, "Sign-up settings", signup_problem)

    if len(s.owner_whatsapp) < 10:
        report(False, "OWNER_WHATSAPP", f"only {len(s.owner_whatsapp)} digits; include the country code, e.g. 65XXXXXXXX")
    else:
        report(True, "OWNER_WHATSAPP", f"ends in …{s.owner_whatsapp[-4:]} (must be the phone you'll message from)")

    try:
        from openai import OpenAI

        model = OpenAI(api_key=s.openai_api_key).models.retrieve(s.openai_model)
        report(True, "OpenAI", f"key works, model '{model.id}' found")
    except Exception as exc:  # noqa: BLE001
        report(False, "OpenAI", f"{type(exc).__name__}: check OPENAI_API_KEY and the exact OPENAI_MODEL name")

    try:
        from openai import OpenAI

        OpenAI(api_key=s.openai_api_key).models.retrieve(s.openai_transcribe_model)
        report(True, "Voice notes", f"transcription model '{s.openai_transcribe_model}' found")
    except Exception as exc:  # noqa: BLE001
        report(False, "Voice notes", f"{type(exc).__name__}: check OPENAI_TRANSCRIBE_MODEL (leave it unset for whisper-1)")

    try:
        from app.tools.calendar import GoogleCalendar, build_service

        cal = GoogleCalendar(
            build_service(s.google_client_id, s.google_client_secret, s.google_refresh_token),
            calendar_id=s.google_calendar_id,
        )
        now = datetime.now(SGT)
        events = cal.list_events(now, now + timedelta(days=7), limit=20)
        report(True, "Google Calendar", f"calendar '{s.google_calendar_id}' readable, {len(events)} event(s) in the next 7 days")
    except Exception as exc:  # noqa: BLE001
        report(False, "Google Calendar", f"{type(exc).__name__}: {exc}{google_hint(exc)}")

    uri = s.calendly_event_type_uri
    if not uri.startswith("https://api.calendly.com/event_types/"):
        report(False, "CALENDLY_EVENT_TYPE_URI", f"should start with https://api.calendly.com/event_types/ (yours starts with {uri[:24]!r}); run scripts/find_calendly_event_type.py")
    try:
        from app.tools.calendly import CalendlyClient

        report(True, "Calendly", CalendlyClient(s.calendly_token, s.calendly_event_type_uri).scheduling_url())
    except Exception as exc:  # noqa: BLE001
        cause = exc.__cause__
        extra = f" ({type(cause).__name__})" if cause else ""
        report(False, "Calendly", f"{type(exc).__name__}: {exc}{extra}; token {shape(s.calendly_token)}")

    try:
        r = httpx.get(
            f"{GRAPH_URL}/{s.wa_phone_number_id}",
            params={"fields": "display_phone_number,verified_name"},
            headers={"Authorization": f"Bearer {s.wa_token}"},
            timeout=10,
        )
        if r.status_code == 200:
            info = r.json()
            report(True, "WhatsApp token + phone number ID", f"{info.get('display_phone_number')} ({info.get('verified_name')})")
        else:
            msg = r.json().get("error", {}).get("message", r.status_code)
            hint = "" if s.wa_token.startswith("EAA") else "; WhatsApp tokens start with 'EAA', so this looks like a different value"
            report(False, "WhatsApp token + phone number ID", f"{msg} (token {shape(s.wa_token)}{hint}). Re-copy it with the copy button next to the token")
    except Exception as exc:  # noqa: BLE001
        report(False, "WhatsApp", f"{type(exc).__name__}: {exc}")

    secret = s.wa_app_secret
    looks_right = len(secret) == 32 and all(c in "0123456789abcdef" for c in secret.lower())
    detail = "looks like an app secret" if looks_right else (
        f"expected 32 characters of 0-9/a-f, got {shape(secret)}"
        + (" of digits only; that's probably the App ID" if secret.isdigit() else "")
        + ". Find it in App settings → Basic → App secret → Show"
    )
    report(looks_right, "WA_APP_SECRET format", detail)

    report(len(s.wa_verify_token) >= 8, "WA_VERIFY_TOKEN", "set" if len(s.wa_verify_token) >= 8 else "use at least 8 random characters")

    if s.signup_enabled:
        report(
            True,
            "Sign-up (Plan 2B)",
            f"on for up to {s.max_users} users. In Google Cloud, the Web client's redirect URI must be exactly "
            f"{s.base_url}/signup/callback, and the app must be 'In production'. Bot number +{s.bot_whatsapp}.",
        )
    elif not signup_problem:
        print("ℹ️  Sign-up (Plan 2B) is off. Optional: see README 'Let friends test it'.")

    print("\nAll good: next run `python chat.py`." if failures == 0 else f"\n{failures} problem(s) to fix above.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
