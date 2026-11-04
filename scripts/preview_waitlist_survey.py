"""Loopback-only questionnaire preview using the real signup route and fake providers.

Run: .venv/bin/uvicorn scripts.preview_waitlist_survey:create_preview --factory --host 127.0.0.1 --port 8831
No .env, real Turnstile, Loops contact, or email. All submissions are in-memory.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
import json

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.waitlist import WaitlistConfig, waitlist_routes
from app.pages import privacy

LANDING = Path(__file__).resolve().parents[1] / "app/static/landing"
SHIM = """<script>
(() => {
  let next = 0; const widgets = new Map();
  window.turnstile = {
    render(target, options) {
      const el = typeof target === 'string' ? document.querySelector(target) : target;
      const id = String(++next); widgets.set(id, options);
      if (el) el.textContent = 'Preview security check ✓ — no email is sent.';
      setTimeout(() => options.callback('preview-token-' + id), 0); return id;
    },
    reset(id) { const o = widgets.get(String(id)); if (o) setTimeout(() => o.callback('preview-token-' + (++next)), 0); },
    remove(id) { widgets.delete(String(id)); }
  };
})();
</script>"""


def create_preview():
    temporary = TemporaryDirectory(prefix="pippa-survey-preview-")
    contacts = {}
    created = []
    origin = "https://preview.pippatime.test"

    def provider(request):
        if request.url.host == "challenges.cloudflare.com":
            return httpx.Response(200, json={"success": True, "hostname": "preview.pippatime.test", "action": "waitlist"})
        if request.url.host == "dns.google":
            return httpx.Response(200, json={"Status": 0, "Answer": [{"type": 15, "data": "1 smtp.google.com."}]})
        if request.url.host != "app.loops.so":
            raise AssertionError("Unexpected preview provider")
        if request.url.path.endswith("/find"):
            contact = contacts.get(request.url.params.get("email"))
            return httpx.Response(200, json=[contact] if contact else [])
        payload = json.loads(request.content)
        if request.url.path.endswith("/create"):
            contacts[payload["email"]] = payload
            created.append(payload)
        elif request.url.path.endswith("/update"):
            contacts[payload["email"]].update(payload)
        return httpx.Response(200, json={"success": True})

    config = WaitlistConfig(enabled=True, list_id="test-list", site_key="preview-only", secret_key="preview-only", loops_api_key="preview-only",
                            origin=origin, state_path=str(Path(temporary.name) / "limits.sqlite3"), rate_secret="preview-only")
    inner = FastAPI()
    inner.include_router(waitlist_routes(config, transport=httpx.MockTransport(provider)))
    app = FastAPI()
    app.state.temporary = temporary
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])

    @app.get("/", response_class=HTMLResponse)
    def home():
        html = (LANDING / "index.html").read_text()
        banner = '<aside style="padding:10px 24px;background:#fff0cc;color:#493600;font:13px system-ui;text-align:center">Local preview · No contacts or emails are created. Use sample answers.</aside>'
        return HTMLResponse(html.replace("</head>", SHIM + "</head>").replace("<body>", "<body>" + banner))

    @app.get("/pippa-assets/{filename}")
    def asset(filename: str):
        if filename not in {p.name for p in LANDING.iterdir() if p.is_file()}:
            return Response(status_code=404)
        return FileResponse(LANDING / filename, headers={"Cache-Control": "no-store"})

    @app.api_route("/api/waitlist{tail:path}", methods=["GET", "POST"])
    async def proxy(request: Request, tail: str):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=inner), base_url=origin) as client:
            result = await client.request(request.method, "/api/waitlist" + tail, content=await request.body(),
                                          headers={"origin": origin, "content-type": request.headers.get("content-type", "")})
        return Response(content=result.content, status_code=result.status_code, media_type="application/json", headers={"Cache-Control": "no-store"})

    @app.get("/__preview/submissions")
    def submissions():
        return {"preview": True, "created": created}

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy_notice():
        return HTMLResponse(privacy())

    return app
