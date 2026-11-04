"""Local UI preview with fake assistant replies; no credentials or provider calls.

Run: .venv/bin/python scripts/preview_web_assistant.py
Open http://127.0.0.1:8016/preview. This is not the production entry point.
"""

import hashlib
import json
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse

from app.connections import Connections
from app.crypto import TokenBox, generate_key
from app.store import Store
from app.tools.elevenlabs import ElevenLabsVoice
from app.users import CALENDAR_SCOPE, Users
from app.web_assistant import WebAssistant


def build_preview():
    now = lambda: datetime.now(timezone.utc)
    users = Users(Store(':memory:'))
    users.ensure_admin('Preview owner', '6590000000', now())
    box = TokenBox(generate_key())
    user, _ = users.sign_in_identity('preview', 'Son', 'preview@example.com', now())
    users._update(user.id, now(), status='approved', google_token_enc=box.encrypt('preview-only'),
                  google_scopes=json.dumps([CALENDAR_SCOPE]), calendar_status='connected')
    connections = Connections(SimpleNamespace(), users, box, SimpleNamespace(), '', now)
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    users.conn.execute('INSERT INTO connection_sessions VALUES (?,?,?,?)',
        (hashlib.sha256(token.encode()).hexdigest(), user.id, csrf, (now() + timedelta(hours=4)).isoformat()))
    users.conn.commit()

    async def reply(user, text):
        return ('This is the local interface preview, using a fake assistant. '
                'The real app connects this conversation to Pippa’s existing Calendar and assistant tools. '
                'No calendar action or provider call was made.')

    web = WebAssistant(connections, reply, ElevenLabsVoice(), 'http://127.0.0.1:8016')
    app = FastAPI()
    app.include_router(web.router)

    @app.get('/preview')
    def login():
        response = RedirectResponse('/app', status_code=303)
        response.set_cookie('pippa_session', token, httponly=True, samesite='lax')
        return response

    @app.get('/connections')
    @app.get('/connections/chat')
    @app.get('/signin')
    def preview_notice():
        return HTMLResponse('<p>Local preview only. No Google or ElevenLabs connection is used.</p>'
                            '<p><a href="/preview">Return to Pippa preview</a></p>')

    return app


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(build_preview(), host='127.0.0.1', port=8016, log_level='warning')
