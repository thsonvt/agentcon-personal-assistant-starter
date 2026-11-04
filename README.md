# AgentCon personal assistant starter

A clean source starter based on the Pippa scheduling assistant for the AgentCon workshop. Python 3.11+; Python 3.12 recommended. Licensed under Apache-2.0.

## Start without credentials

Install Git and Python 3.11+ first. For the recommended setup, install [uv](https://docs.astral.sh/uv/). Then open a terminal and clone the starter (skip the clone if you already have it):

```bash
git clone https://github.com/thsonvt/agentcon-personal-assistant-starter.git
cd agentcon-personal-assistant-starter
```

Run all commands below from this repository folder. No `.env` file or API keys are needed for the preview.

With uv (recommended; uses the committed dependency lockfile):

```bash
uv sync --locked --extra dev
uv run --locked python scripts/preview_web_assistant.py
```

Open http://127.0.0.1:8016/preview. This existing preview uses synthetic sample data, in-memory storage and **fake assistant replies**. It does not call an AI provider, save persistent tasks, send messages, access a calendar or provide working voice. Restarting it clears its data. This gives you a working interface to inspect and extend during the workshop.

Keep the terminal running; a quiet terminal is normal. In the browser, type `Help me plan my day.` and click **Send**. A successful setup replies that this is a local interface preview using a fake assistant. Voice controls are disabled in this preview. Stop the server with **Ctrl+C**; rerun the preview command to start it again. If port 8016 is already in use, stop your earlier preview first. If your session expires, open `/preview` again; restart the server if it has been running for more than four hours.

Without uv:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python scripts/preview_web_assistant.py
```

On Windows, activate with `.venv\Scripts\Activate.ps1`. The uv commands work without activation.

The pip alternative resolves dependencies when you install; it does not use `uv.lock`. Use uv for consistent workshop dependencies.

## Extend the baseline

- `app/web_assistant.py`, `app/web_pages.py`, `app/static/web/`: browser conversation and voice UI.
- `app/agents.py`, `app/assistant_tools.py`, `app/delegate_tools.py`: agent instructions and tool boundaries.
- `app/store.py`, `app/users.py`: SQLite storage and per-user records.
- `app/tools/`: Google Calendar, Gmail, WhatsApp, Calendly and ElevenLabs adapters.
- `app/wiring.py`: connects real services; `scripts/preview_web_assistant.py` shows the fake preview wiring.
- `tests/`: behavior and security regression checks using fake services.

For a 90-minute session, start with the preview, choose one useful behavior, implement it behind a tool, then test its validation, storage and result. The source includes advanced scheduling features so existing imports and tests remain intact; you do not need to configure them all to run the preview.

## Connect your own Google Calendar for a live assistant

The source already supports reading your agenda, checking availability, and creating, moving, or deleting Google Calendar events. The preview does not use these tools. Google authorization is performed by `scripts/get_google_token.py`, not by the preview's Connections page.

**Current launcher limitation:** the live web launcher still builds the full application. In addition to Google and OpenAI, it requires Meta WhatsApp and Calendly settings, plus all six public sign-up settings listed below. Connecting Google alone will not make `run_web_local.py` start. Complete this setup before the workshop if using the current live launcher; a Google-and-OpenAI-only launcher is not implemented yet.

### 1. Prepare your private configuration

If `.env` does not exist, copy the template once. Otherwise, edit your existing file without overwriting it:

```bash
cp .env.example .env
uv sync --locked --extra dev
```

Use your own accounts and credentials. Set `OPENAI_API_KEY` to your own API key and `OPENAI_MODEL` to a model available to that API account. The starter has no shared workshop API key or proxy; API usage is billed to your account. Do not paste credentials into chat or commit `.env`.

### 2. Create Google OAuth credentials

Follow Google's [Calendar Python setup guide](https://developers.google.com/workspace/calendar/api/quickstart/python) for the Cloud project and OAuth client, then use this repository's helper rather than Google's sample program:

1. Create or select your own Google Cloud project and enable **Google Calendar API**.
2. Configure **Google Auth Platform → Branding** with your app name and contact email. For a personal Gmail account, use an **External** audience, keep the app in **Testing**, and add your own Google account under **Audience → Test users**. An organization account may require administrator approval.
3. Under **Data Access**, include `https://www.googleapis.com/auth/calendar`. The helper requests this read/write scope because the assistant can schedule and edit events.
4. Under **Clients**, create a **Desktop app** OAuth client for this local helper. Copy its client ID and client secret into `.env` as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. This is separate from the full application's Web client used in step 4 below.

### 3. Sign in to your Google account

From the repository folder, run:

```bash
uv run --locked python scripts/get_google_token.py --save-env
```

Your browser opens Google's consent screen. Select the account whose calendar you want to use, check that the app is yours, and grant Calendar access. The helper receives the callback on `http://localhost:8765/`, saves `GOOGLE_REFRESH_TOKEN` in `.env`, and reports that it saved the token without printing it. Keep port 8765 free during authorization. Stop here if consent fails; do not substitute a placeholder token.

If reusing an existing **Web application** OAuth client with this helper, its authorized redirect URI must be exactly `http://localhost:8765/`, including the trailing slash. With the recommended Desktop client, a manually configured web redirect is unnecessary.

Set `GOOGLE_CALENDAR_ID=primary` to read your main calendar. For write exercises, create a separate workshop calendar and set this variable to its Calendar ID from Google Calendar settings → **Integrate calendar**. Reads and writes both use the selected calendar; an empty workshop calendar will not show events from your main calendar. Restart the live app after changing `.env`.

Gmail access is separate and unnecessary for the agenda/meeting exercise. Only if you want email-reading features, enable Gmail API and rerun the helper with `--gmail --save-env`, granting both permissions. Calendar consent does not grant Gmail access.

### 4. Complete the current live launcher's prerequisites

These requirements come from `app/config.py` and `app/wiring.py`; do not fill them with dummy credentials:

| Configuration | Required values |
| --- | --- |
| AI | `OPENAI_API_KEY`, `OPENAI_MODEL` |
| Owner's Google account | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` |
| Meta WhatsApp | `WA_TOKEN`, `WA_PHONE_NUMBER_ID`, `WA_APP_SECRET`, `WA_VERIFY_TOKEN`, `OWNER_WHATSAPP` |
| Calendly | `CALENDLY_TOKEN`, `CALENDLY_EVENT_TYPE_URI` |
| Local storage | `DB_PATH` (for example, `./data/workshop.db`) |
| Web sign-up configuration | `GOOGLE_WEB_CLIENT_ID`, `GOOGLE_WEB_CLIENT_SECRET`, `BASE_URL`, `BOT_WHATSAPP`, `TOKEN_KEY`, `SESSION_SECRET` |

For the Web sign-up client, use the actual Web application OAuth client and register `<BASE_URL>/signup/callback`; `BASE_URL` must be your HTTPS origin. The local launcher does not serve this public OAuth callback. The six sign-up fields are needed because web chat is currently constructed inside the sign-up-enabled runtime, even though the local owner signs in through the helper above. Generate your own encryption/session keys locally and copy them into `.env`:

```bash
uv run --locked python scripts/check_setup.py --new-key
```

ElevenLabs voice is optional. Leave its settings empty for a text-only exercise. Leave advanced pilot flags disabled unless you are explicitly testing them.

Check the configured services before starting:

```bash
uv run --locked python scripts/check_setup.py
```

This contacts the configured providers. Look for a successful **Google Calendar** check reporting how many events are readable in the next seven days. A passing Google check does not mean other providers are configured. Resolve the reported startup prerequisites before continuing.

### 5. Start the live app and verify your own calendar

Stop the preview with **Ctrl+C** first; both launchers use port 8016. Then run:

```bash
uv run --locked python scripts/run_web_local.py
```

Open http://127.0.0.1:8016/app and click **Open Pippa** on the local sign-in page. This button establishes a local owner session; it does not perform Google OAuth. **Calendar changes are real.** Keep this server on loopback; do not expose it through a tunnel or public host. Background WhatsApp jobs and webhook routes are not started by this local runner.

Try these in order, checking the selected calendar in Google Calendar after each action. The assistant currently interprets schedules in **Asia/Singapore (UTC+08:00)**; use explicit dates and times for the workshop.

1. Ask: **“What is on my calendar today?”** Compare its answer with that calendar, including the date and timezone.
2. Ask: **“Find a free 30-minute slot tomorrow between 2pm and 5pm Singapore time.”** This should read availability without booking anything.
3. On your workshop calendar, ask: **“Create a personal event called AgentCon practice tomorrow from 3pm to 3:30pm Singapore time. No guests and no location.”** Resolve any conflict or clarification the assistant raises. Verify the event exists in Google Calendar.
4. Ask: **“Move AgentCon practice to 4pm tomorrow, keeping its duration.”** Check the changed time.
5. Ask: **“Delete AgentCon practice.”** Review the proposed event and confirm in a separate message. Check that it was removed.

For a real invitation, supply the exact attendee name, an email you are authorized to invite, date, time, duration, and location. Review the assistant's complete proposal before confirming. An invitation can send a real email; do not use fixture addresses. The code requests Google Meet when an invitation has no supplied location. Treat a meeting link as created only when Google returns one, and an invitation request as distinct from email delivery or attendee acceptance.

### Troubleshooting Google setup

- **Missing `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`:** run from the repository folder and fill those values in `.env`.
- **`redirect_uri_mismatch`:** use a Desktop client, or correct the Web client's exact localhost redirect above.
- **Access blocked / not a test user:** check the selected Google account against your OAuth app's test-user list and any organization restrictions.
- **Calendar API disabled / insufficient permission:** enable Calendar API in the client's project, rerun the helper, and grant Calendar access.
- **`invalid_grant` / expired token:** reconnect with `--save-env` and restart. External apps in Testing can have refresh tokens expire after seven days; see [Google's token-expiration documentation](https://developers.google.com/identity/protocols/oauth2#expiration).
- **Missing WhatsApp/Calendly settings or “complete sign-up configuration” error:** this is the current launcher coupling described in step 4, not evidence that Google sign-in failed.
- **Still seeing “fake assistant”:** you are running the preview. Stop it and launch `run_web_local.py` after completing the real configuration.

`chat.py` is the full terminal interface; `main.py` and `Dockerfile` are full server entry points. They also need real configuration. Before hosting, configure your own OAuth redirects, webhook validation, origin, privacy/contact text, and optional mailing-list settings. Example contact links are placeholders, not your deployment's privacy statement.

## Verify

Run these in a second terminal from the repository folder while the preview is running, or stop the preview first. Node.js is needed only for the JavaScript checks, not to run the preview.

```bash
uv run --locked pytest -q
node --test tests/*.cjs
```

If you used the pip setup, run `python -m pytest -q` from the activated environment instead of `uv run --locked pytest -q`.

The test suite uses mock providers and synthetic records; never send messages to fixture phone numbers or email addresses. Public URL strings in the QR golden tests are fixed inputs for independently generated matrices, not network requests.

## What was deliberately left out

Original Git history/remotes, private environment files and variants, operational documents, agent logs, outputs/screenshots, backups, databases, local caches, vendor font binaries and the original hero image. The retained styles use system fallback fonts. Fresh setup instructions are in this README; this is a source export, not a production deployment or a copy of production data.

`.gitignore` blocks these artifacts; `.dockerignore` allows only required build inputs. `.env.example` is the only environment template intended for version control. Use Git's staged-file review before publishing, and never force-add credentials.

## License

Project source: [Apache License 2.0](LICENSE). Bundled third-party CSS retains its MIT license: [third-party notices](THIRD_PARTY_NOTICES.md). Dependencies retain their own licenses.
