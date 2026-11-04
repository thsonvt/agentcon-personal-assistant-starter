# AgentCon personal assistant starter

Connect your own Google account, see your real Calendar events, and ask the assistant to help schedule meetings. Python 3.11+ (3.12 recommended). The local workshop app does **not** require WhatsApp, Calendly, a public website, or public sign-up configuration.

## 1. Install and start

Install Git, Python, and [uv](https://docs.astral.sh/uv/), then open a terminal:

```bash
git clone https://github.com/thsonvt/agentcon-personal-assistant-starter.git
cd agentcon-personal-assistant-starter
uv sync --locked --extra dev
```

If you already cloned the repo, enter that folder and run `git pull` instead of cloning again. Copy the template once; if `.env` already exists, edit it without overwriting it:

```bash
cp .env.example .env
```

## 2. Set up Google once

Each participant uses their own Google Cloud project and personal Gmail account. This initial Google setup is required; after it, connecting is a button click inside the app.

1. In [Google Cloud Console](https://console.cloud.google.com/), create or select your project and enable **Google Calendar API**.
2. In **Google Auth Platform → Branding**, provide an app name and support/contact email. Under **Audience**, select **External**, keep the app in **Testing**, and add your Gmail address as a **test user**.
3. Under **Data Access**, add `https://www.googleapis.com/auth/calendar`. Calendar read/write access lets the assistant read and schedule events; the app also requests basic identity information to identify your connected account.
4. Under **Clients**, create a **Web application** OAuth client. Add this exact **Authorized redirect URI** (no trailing slash):

```text
http://127.0.0.1:8016/signup/callback
```

5. Copy the client ID and secret into your private `.env`:

```dotenv
GOOGLE_CLIENT_ID=your-web-client-id
GOOGLE_CLIENT_SECRET=your-web-client-secret
GOOGLE_CALENDAR_ID=primary
DB_PATH=./data/workshop.db
```

Use a Web application client for this browser flow. The older terminal helper's Desktop client and `localhost:8765` callback are a separate flow and are not needed here. Google's [consent setup](https://developers.google.com/workspace/guides/configure-oauth-consent) and [Web OAuth documentation](https://developers.google.com/identity/protocols/oauth2/web-server) explain these settings; loopback redirects are allowed over HTTP.

## 3. Click Connect Google and view your events

Stop any earlier preview with **Ctrl+C**, then run:

```bash
uv run --locked python scripts/run_web_local.py
```

Open [http://127.0.0.1:8016/connections](http://127.0.0.1:8016/connections). Open the local app if prompted, then click **Connect Google**. Choose your Gmail account, verify the app name is yours, and grant Calendar access. Google returns you to the app.

Open **Calendar** to see events read directly from Google. No AI key is required for this view. Refresh it after changing an event in Google Calendar. The app uses **Asia/Singapore (UTC+08:00)** for dates and times. An empty day is shown as empty, not populated with demo events.

For a quick real-account check, create a harmless event in Google Calendar for today, then refresh the app and compare its title and time. Only a successful real-account read proves your connection works; automated tests use fake providers.

Keep the terminal running. **Ctrl+C** stops the server. Google authorization is stored encrypted locally and reused after restart. The database and its generated encryption-key file live under `data/` by default and are ignored by Git. Keep both private and together; do not commit them. Each database belongs to one Google account. To use another account, choose a different `DB_PATH` and restart.

## 4. Enable conversational scheduling (optional)

To chat with the assistant and schedule meetings, add your own OpenAI API key and a model available to your API account to `.env`, then restart the launcher:

```dotenv
OPENAI_API_KEY=your-own-api-key
OPENAI_MODEL=your-available-model
```

Keep the key local; do not paste it into chat or Git. API usage is billed to your API account. The starter does not supply a shared workshop key or proxy. Calendar details used to answer chat requests are sent to the configured model provider.

Open **Chat** or **Schedule a meeting**; both lead to the real assistant. Try:

1. **“What is on my calendar today?”** Compare the answer against Google Calendar.
2. **“Find a free 30-minute slot tomorrow between 2pm and 5pm Singapore time.”** This reads availability without booking.
3. **“Create a personal event called AgentCon practice tomorrow from 3pm to 3:30pm Singapore time. No guests and no location.”** Resolve any conflict or clarification. Verify the event exists in Google Calendar.
4. **“Move AgentCon practice to 4pm tomorrow, keeping its duration.”** Verify the new time.
5. **“Delete AgentCon practice.”** Review the proposal and confirm in a separate message. Verify removal.

For write exercises, create a separate workshop calendar and set `GOOGLE_CALENDAR_ID` to its Calendar ID from Google Calendar settings → **Integrate calendar**, then restart. Reads and writes both use that selected calendar; it will not show events from your main calendar.

For an invitation, supply the exact attendee name, authorized email, date, time, duration, and location. Review the complete proposal before confirming. Invitations may send real email; do not invite fixture addresses. The existing tool requests Google Meet if no location was supplied, and reports a link only if Google returns one. Invitation creation does not prove email delivery or attendee acceptance.

A Gmail account gives access to Google Calendar, but reading inbox emails is a separate permission. This local workshop flow requests Calendar access, not Gmail inbox access. WhatsApp outreach, Calendly links, group booking pages, and background reminders are not part of this launcher.

## Speak instead of typing

With Google connected and your OpenAI key/model configured, open **Conversation**, click **Start conversation**, and allow the browser to use your microphone. Ask “What is on my calendar today?” Pippa uses GPT-Live for listening and spoken replies, while the existing calendar assistant checks your events. Headphones help prevent speaker audio feeding back into the microphone.

The voice model defaults to the following; your reasoning model remains `OPENAI_MODEL`:

```dotenv
OPENAI_VOICE_MODEL=gpt-live-1
```

**Stop conversation** turns off the microphone and ends the voice session. **Stop speaking** interrupts the current reply while leaving the conversation active. Sessions end after five minutes; start another explicitly if needed. If the browser blocks speaker playback or reports a connection error, check the message under **Connection details**. Do not automatically repeat a calendar request after an uncertain result.

Voice can read your calendar and prepare a booking or deletion proposal. For calendar changes, stop voice, review the details in chat, and **type** your confirmation. To move or rename an event, type the exact change. Spoken “yes” does not authorize a write: live transcripts can contain partial sentences and corrections. This preserves an explicit confirmation step while using the same calendar tools.

Audio goes to OpenAI during a live session. Voice duration and backend model usage are billed separately to your API account, including the provider's WebRTC initialization charge. The project key stays on the local server. See the [GPT-Live connection and billing notes](https://developers.openai.com/api/docs/guides/voice-webrtc?api=live). The app does not request provider recording storage. It saves the delegated requests and backend replies in your local chat history; live captions are not a complete recording.

ElevenLabs is not required for **Start conversation**. **Record voice** (record, review transcript, send) and replaying saved replies still use the optional ElevenLabs settings. They remain unavailable if those settings are empty.

## Troubleshooting

- **Google settings missing:** fill in `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in the repository's `.env`, then restart. The app can show setup guidance before those values are configured.
- **`redirect_uri_mismatch`:** use a Web application client and register exactly `http://127.0.0.1:8016/signup/callback`. `localhost`, a different port, and a trailing slash are different values.
- **Access blocked / not a test user:** add the Gmail address you selected to your OAuth app's test-user list. Organization accounts may have additional administrator restrictions.
- **Calendar API disabled / permission missing:** enable Calendar API in the client's project, then reconnect and grant Calendar access.
- **Expired or revoked access:** use **Reconnect Google**. External apps in Testing can have refresh tokens expire after seven days; see [Google's token-expiration documentation](https://developers.google.com/identity/protocols/oauth2#expiration).
- **No events:** confirm the displayed date/timezone, selected calendar, and signed-in Google account. Refresh after adding a test event. A provider error is not an empty calendar.
- **Chat asks for AI settings:** add both OpenAI values and restart; Calendar still works without them.
- **Port 8016 is busy:** stop the earlier server before starting this one. Use the `127.0.0.1` URLs above, not `localhost`.
- **“Local preview only” banner:** you launched `preview_web_assistant.py`. Stop it and run `run_web_local.py` instead.

This is a single-owner local application. Do not expose it through a tunnel, bind it to a public network, or share your database/key files. It is not a hosted multi-user service.

## Preview without a Google account

For interface-only development, stop the real app and run:

```bash
uv run --locked python scripts/preview_web_assistant.py
```

Open http://127.0.0.1:8016/preview. This separate preview uses fake replies and in-memory synthetic data. It has no live Calendar, AI, or voice connection; restarting clears its data.

## Without uv

From the repository folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python scripts/run_web_local.py
```

On Windows, use `python` if `python3` is unavailable, and activate with `.venv\Scripts\Activate.ps1`. The uv path does not require activation and uses the committed lockfile; pip resolves dependencies at install time.

## Verify and extend

Run in a second terminal from the repository folder (Node.js is needed only for JavaScript checks):

```bash
uv run --locked pytest -q
node --test tests/*.cjs
```

With pip, use `python -m pytest -q` in your activated environment. Tests use fake providers; never send messages to fixture numbers or addresses.

- `app/workshop.py`: local Google connection, Calendar view, and assistant wiring.
- `app/web_assistant.py`, `app/web_pages.py`, `app/static/web/`: browser chat and voice UI.
- `app/agents.py`, `app/assistant_tools.py`: agent tools and booking confirmation rules.
- `app/tools/`: provider adapters; `app/store.py`, `app/users.py`: local records.
- `app/wiring.py`, `main.py`, `chat.py`, `Dockerfile`: advanced full runtime with its own additional configuration requirements. `scripts/check_setup.py` checks that full runtime, not the minimal workshop launcher.

The full hosted/WhatsApp application remains available for advanced projects. Its settings below the workshop section of `.env.example` are unnecessary for the local exercise. Before hosting, configure your own OAuth redirects, validation, privacy/contact text, and services.

## License and source export

Project source: [Apache License 2.0](LICENSE). Bundled third-party CSS retains its MIT license: [third-party notices](THIRD_PARTY_NOTICES.md). Dependencies retain their own licenses.

This source starter is based on Pippa. Original Git history, private credentials, operational documents, logs, outputs, backups, databases, caches, vendor font binaries, and the original hero image were excluded. Retained styles use fallback fonts. `.gitignore` excludes private/generated files; `.dockerignore` limits full-server build inputs. This repository is a source export, not a production deployment or production-data copy.
