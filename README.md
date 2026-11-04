# AgentCon personal assistant starter

A clean source starter based on the Pippa scheduling assistant for the AgentCon workshop. Python 3.11+; Python 3.12 recommended. Licensed under Apache-2.0.

## Start without credentials

With [uv](https://docs.astral.sh/uv/):

```bash
uv sync --locked --extra dev
uv run --locked python scripts/preview_web_assistant.py
```

Open http://127.0.0.1:8016/preview. This existing preview uses synthetic sample data, in-memory storage and **fake assistant replies**. It does not call an AI provider, save persistent tasks, send messages, access a calendar or provide working voice. Restarting it clears its data. This gives you a working interface to inspect and extend during the workshop.

Without uv:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python scripts/preview_web_assistant.py
```

On Windows, activate with `.venv\Scripts\Activate.ps1`. The uv commands work without activation.

## Extend the baseline

- `app/web_assistant.py`, `app/web_pages.py`, `app/static/web/`: browser conversation and voice UI.
- `app/agents.py`, `app/assistant_tools.py`, `app/delegate_tools.py`: agent instructions and tool boundaries.
- `app/store.py`, `app/users.py`: SQLite storage and per-user records.
- `app/tools/`: Google Calendar, Gmail, WhatsApp, Calendly and ElevenLabs adapters.
- `app/wiring.py`: connects real services; `scripts/preview_web_assistant.py` shows the fake preview wiring.
- `tests/`: behavior and security regression checks using fake services.

For a 90-minute session, start with the preview, choose one useful behavior, implement it behind a tool, then test its validation, storage and result. The source includes advanced scheduling features so existing imports and tests remain intact; you do not need to configure them all to run the preview.

## Connect your own services (optional)

```bash
cp .env.example .env
```

Fill in **your own** values. Never use the presenter's production credentials. `app/config.py` lists the required settings for the full runtime: OpenAI model/API key, Meta WhatsApp settings, Google OAuth client/refresh token, Calendly settings, owner number and database path. Google and Meta setup may take longer than the workshop; it is not required for the fake preview. ElevenLabs voice is optional.

The starter uses the existing official OpenAI client; it does not contain a shared workshop proxy endpoint or key. Provider access and charges belong to your own account.

```bash
uv run --locked python scripts/run_web_local.py
```

Open http://127.0.0.1:8016/app. **This is the real runtime and can change your calendar.** Use a separate test calendar and your own test accounts. The loopback login is for local development; do not expose this runner through a tunnel or public host.

`chat.py` is the terminal interface. `main.py` and `Dockerfile` are the full server entry points, not the preview. All require the appropriate real configuration. Before hosting a copy, configure your own OAuth redirects, webhook validation, origin, privacy/contact text and optional mailing-list settings. Example contact links are placeholders; the supplied pages are not your deployment's privacy statement. Optional sign-up settings must be supplied together; generate your own encryption and session keys with `scripts/check_setup.py --new-key`.

## Verify

```bash
uv run --locked pytest -q
node --test tests/*.cjs
```

The test suite uses mock providers and synthetic records; never send messages to fixture phone numbers or email addresses. Public URL strings in the QR golden tests are fixed inputs for independently generated matrices, not network requests.

## What was deliberately left out

Original Git history/remotes, private environment files and variants, operational documents, agent logs, outputs/screenshots, backups, databases, local caches, vendor font binaries and the original hero image. The retained styles use system fallback fonts. Fresh setup instructions are in this README; this is a source export, not a production deployment or a copy of production data.

`.gitignore` blocks these artifacts; `.dockerignore` allows only required build inputs. `.env.example` is the only environment template intended for version control. Use Git's staged-file review before publishing, and never force-add credentials.

## License

Project source: [Apache License 2.0](LICENSE). Bundled third-party CSS retains its MIT license: [third-party notices](THIRD_PARTY_NOTICES.md). Dependencies retain their own licenses.
