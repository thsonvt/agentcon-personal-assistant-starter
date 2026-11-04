"""Run the real Connections UI against local fake providers: PYTHONPATH=. python scripts/preview_connections.py.

No .env, production settings, external API, or real message transport is loaded.
The SQLite database is disposable memory; restarting resets the demo.
"""

import secrets
from datetime import datetime, timezone
from html import escape
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.commands import Commands
from app.crypto import TokenBox, generate_key
from app.signup import signup_routes
from app.store import Store
from app.tools.google_oauth import CALENDAR_SCOPE, GMAIL_SCOPE, GoogleAccount
from app.users import Users, SignupError
from app.router import Router, IncomingMessage
from app.tools.whatsapp import WhatsAppError


class PreviewSender:
    def __init__(self):
        self.sent = []
        self.blocked_recipient = None

    def send_text(self, to, text):
        if to == self.blocked_recipient:
            raise WhatsAppError("Local simulator: recipient not allowed", 131030)
        self.sent.append((to, text))
        return f"preview-message-{len(self.sent)}"


class PreviewOAuth:
    def connection_auth_url(self, state, verifier, service="identity", email=None):
        return "/__preview/google?" + urlencode({"state": state, "service": service})

    def exchange_connection(self, code, verifier):
        scopes = ("openid", "email", "profile")
        if code in {"calendar", "gmail"}:
            scopes += (CALENDAR_SCOPE,)
        if code == "gmail":
            scopes += (GMAIL_SCOPE,)
        return GoogleAccount("demo@example.com", "Alex Demo", "preview-refresh" if code != "identity" else "",
                             GMAIL_SCOPE in scopes, scopes, "preview-google-subject", "preview-access")

    def refresh_connection(self, token):
        return "preview-access"

    def check_connection(self, token, service):
        return "connected", "Local test provider responded successfully."

    def revoke(self, token):
        pass


def create_preview():
    from app.connections import Connections

    now = lambda: datetime.now(timezone.utc)
    store = Store(":memory:")
    users = Users(store, max_users=5, allowed_emails={"demo@example.com"}, auto_activate=True)
    users.ensure_admin("Local organizer", "6590000000", now())
    box, oauth, sender = TokenBox(generate_key()), PreviewOAuth(), PreviewSender()
    preview_csrf = secrets.token_urlsafe(32)
    commands = Commands(users, sender, now, box.decrypt, oauth.revoke, base_url="http://localhost:8826",
                        test_number_mode=False)
    connections = Connections(oauth=oauth, users=users, box=box, commands=commands,
                              bot_whatsapp="", now=now, test_number_mode=False)

    async def no_model(_text):
        raise AssertionError("The preview join must never invoke a model")

    router = Router(owner_phone=users.get(1).phone, store=store, sender=sender,
                    run_owner=no_model, now=now, commands=commands, users=users)
    app = FastAPI()
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])
    app.include_router(connections.router)
    app.include_router(signup_routes(oauth, users, box, commands, "", "local-preview-only" * 3, now,
                                     connections_callback=connections.callback, test_number_mode=False))

    @app.middleware("http")
    async def preview_banner(request: Request, call_next):
        response = await call_next(request)
        if "text/html" in response.headers.get("content-type", ""):
            content = b"".join([chunk async for chunk in response.body_iterator]).decode()
            banner = ('<aside style="background:#fff4d7;color:#493600;padding:12px 20px;font:14px/1.5 system-ui">'
                      '<b>Local acceptance preview</b> · Real application pages, fake Google and WhatsApp. '
                      'No account permissions, calendar changes, or messages leave this server. '
                      '<a href="/__preview/activation">Simulate WhatsApp verification</a></aside>')
            content = content.replace("<body>", "<body>" + banner, 1)
            headers = {k: v for k, v in response.headers.items() if k.lower() not in {"content-length", "set-cookie"}}
            result = HTMLResponse(content, status_code=response.status_code, headers=headers)
            for key, value in response.raw_headers:
                if key.lower() == b"set-cookie":
                    result.raw_headers.append((key, value))
            return result
        return response

    @app.get("/")
    def root():
        return RedirectResponse("/signin", status_code=303)

    @app.get("/privacy")
    def privacy():
        from app.pages import privacy as privacy_page
        return HTMLResponse(privacy_page())

    @app.get("/__preview/google")
    def fake_google(state: str, service: str = "identity"):
        if service not in {"identity", "calendar", "gmail"}:
            return Response(status_code=400)
        approve = "/signup/callback?" + urlencode({"state": state, "code": service})
        decline = "/signup/callback?" + urlencode({"state": state, "error": "access_denied"})
        return HTMLResponse('<!doctype html><html><body><main style="max-width:600px;margin:60px auto;font:17px/1.7 system-ui">'
                            f'<h1>Local provider simulator</h1><p>Step: {escape(service)}. '
                            'Sample account: demo@example.com. This is not Google sign-in.</p>'
                            f'<p><a href="{escape(approve)}">Continue with sample account / grant sample permission</a></p>'
                            f'<p><a href="{escape(decline)}">Decline this step</a></p></main></body></html>')

    @app.get("/__preview/activation")
    def activation():
        user = users.by_email("demo@example.com")
        ready = bool(user and user.typed_phone and user.join_code)
        form = (f'<form method="post" action="/__preview/join"><input type="hidden" name="csrf_token" '
                f'value="{preview_csrf}"><button name="delivery" value="delivered">Simulate join and delivered reply</button> '
                '<button name="delivery" value="blocked">Simulate join with Meta reply blocked</button></form>'
                if ready else '<p>Complete sign-in, Calendar and the WhatsApp number form first.</p>')
        return HTMLResponse('<!doctype html><html><body><main style="max-width:600px;margin:60px auto;font:17px/1.7 system-ui">'
                            '<h1>Local WhatsApp simulator</h1><p>This exercises the real join validation and beta activation '
                            'against the disposable local database. It sends no message.</p>' + form +
                            '<p><a href="/connections">Back to Connections</a></p></main></body></html>')

    @app.post("/__preview/join")
    async def join(csrf_token: str = Form(""), delivery: str = Form("delivered")):
        if not secrets.compare_digest(csrf_token, preview_csrf):
            return Response(status_code=403)
        user = users.by_email("demo@example.com")
        if not user or not user.typed_phone or not user.join_code:
            return RedirectResponse("/connections", status_code=303)
        if delivery not in {"delivered", "blocked"}:
            return Response(status_code=400)
        sender.blocked_recipient = user.typed_phone if delivery == "blocked" else None
        try:
            await router.handle(IncomingMessage(sender=user.typed_phone, text="join " + user.join_code,
                                                message_id="preview-inbound-" + secrets.token_hex(8)))
        finally:
            sender.blocked_recipient = None
        row = store.conn.execute("SELECT message_id FROM onboarding_attempts WHERE user_id=? ORDER BY id DESC LIMIT 1",
                                 (user.id,)).fetchone()
        if delivery == "delivered" and row and row[0]:
            commands.receipts.record_status({"id": row[0], "recipient_id": user.typed_phone, "status": "delivered"})
        return RedirectResponse("/connections", status_code=303)

    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_preview(), host="127.0.0.1", port=8826, access_log=False)
