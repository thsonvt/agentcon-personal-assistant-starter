"""FastAPI app that receives WhatsApp webhooks. No AI code lives here."""

import asyncio
import contextlib
import hashlib
import logging
import hmac
import json
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.config import normalize_phone
from app.router import IncomingMessage
from app.store import Store

IGNORED_TYPES = {"reaction", "system", "unsupported"}  # nothing to answer

log = logging.getLogger(__name__)


def _records(value) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _values(payload):
    """Skip malformed envelope members while preserving valid sibling events."""
    if not isinstance(payload, dict):
        return
    for entry in _records(payload.get("entry")):
        for change in _records(entry.get("changes")):
            value = change.get("value")
            if isinstance(value, dict):
                yield value


def count_statuses(payload: dict) -> int:
    return sum(len(_records(value.get("statuses"))) for value in _values(payload))


def extract_statuses(payload: dict) -> list[dict]:
    """Bound the callback to known delivery fields; ignore incomplete provider events."""
    statuses = []
    for value in _values(payload):
        for status in _records(value.get("statuses")):
            message_id, recipient, state = status.get("id"), status.get("recipient_id"), status.get("status")
            if (not isinstance(message_id, str) or not 0 < len(message_id) <= 512
                    or not isinstance(recipient, str) or not 0 < len(recipient) <= 32
                    or state not in ("sent", "delivered", "read", "failed")):
                continue
            errors = status.get("errors")
            first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
            code = first.get("code")
            safe_code = code if isinstance(code, int) and not isinstance(code, bool) and 0 <= code <= 999999999 else None
            statuses.append({"id": message_id, "recipient_id": recipient, "status": state,
                             "errors": [{"code": safe_code}] if safe_code is not None else []})
    return statuses


def verify_signature(body: bytes, header: str | None, app_secret: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def contact_cards_text(cards: list) -> str | None:
    """Shared WhatsApp contacts as text the agent can act on, one line per card."""
    lines = []
    for card in _records(cards):
        name_data = card.get("name")
        name = name_data.get("formatted_name") if isinstance(name_data, dict) else None
        name = name.strip() if isinstance(name, str) and name.strip() else "Unnamed contact"
        phones = _records(card.get("phones"))
        digits = next((normalize_phone(p["wa_id"]) for p in phones if isinstance(p.get("wa_id"), str) and p["wa_id"]), "")
        digits = digits or next((normalize_phone(p["phone"]) for p in phones if isinstance(p.get("phone"), str) and p["phone"]), "")
        email = next((e["email"] for e in _records(card.get("emails")) if isinstance(e.get("email"), str) and e["email"]), None)
        parts = [f"[contact card] {name}", f"phone +{digits}" if digits else "no phone number"]
        if email:
            parts.append(f"email {email}")
        lines.append(", ".join(parts))
    return "\n".join(lines) or None


def extract_messages(payload: dict) -> list[IncomingMessage]:
    """Pull user messages out of a webhook payload. Status updates (sent/delivered/read) are ignored."""
    messages = []
    for value in _values(payload):
        for m in _records(value.get("messages")):
            kind, sender, message_id = m.get("type"), m.get("from"), m.get("id")
            if (not isinstance(kind, str) or kind in IGNORED_TYPES
                    or not isinstance(sender, str) or not 0 < len(sender) <= 32
                    or not isinstance(message_id, str) or not 0 < len(message_id) <= 512):
                continue
            text, audio_id, image_id, caption = None, None, None, None
            if kind == "text":
                content = m.get("text")
                text = content.get("body") if isinstance(content, dict) else None
                if not isinstance(text, str):
                    continue
            if kind == "contacts":
                text = contact_cards_text(m.get("contacts"))
            if kind == "audio":
                audio = m.get("audio")
                audio_id = audio.get("id") if isinstance(audio, dict) else None
                if audio_id is not None and (not isinstance(audio_id, str) or not 0 < len(audio_id) <= 512):
                    continue
            if kind == 'image':
                content = m.get('image')
                image_id = content.get('id') if isinstance(content, dict) else None
                caption = content.get('caption') if isinstance(content, dict) else None
                if (not isinstance(image_id, str) or not 0 < len(image_id) <= 512
                        or (caption is not None and not isinstance(caption, str))):
                    continue
            stamp = m.get('timestamp')
            source_timestamp = stamp if isinstance(stamp, str) and stamp.isascii() and stamp.isdigit() and len(stamp) <= 16 else None
            messages.append(IncomingMessage(sender=sender, text=text, message_id=message_id, audio_id=audio_id,
                image_id=image_id, caption=caption, source_timestamp=source_timestamp, kind=kind))
    return messages


def create_app(
    verify_token: str,
    app_secret: str,
    store: Store,
    handle: Callable[[IncomingMessage], Awaitable[None]],
    now: Callable[[], datetime],
    background_jobs: Sequence[Callable[[], Awaitable[None]]] = (),
    routes: Sequence[APIRouter] = (),  # Plan 2B: the landing and sign-up pages, when enabled
    status_handler: Callable[[dict], None] | None = None,
    capture_ingest: Callable[[IncomingMessage], bool] | None = None,
) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        tasks = [asyncio.create_task(job()) for job in background_jobs]
        yield
        for task in tasks:
            task.cancel()

    app = FastAPI(lifespan=lifespan)

    if capture_ingest is not None:
        from app.capture_pages import PRIVATE_HEADERS

        @app.middleware('http')
        async def protect_capture_responses(request, call_next):
            response = await call_next(request)
            if request.url.path == '/captures' or request.url.path.startswith('/captures/'):
                response.headers.update(PRIVATE_HEADERS)
            return response

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/webhook")
    def verify(request: Request):
        params = request.query_params
        if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == verify_token:
            return PlainTextResponse(params.get("hub.challenge", ""))
        return PlainTextResponse("Forbidden", status_code=403)

    @app.post("/webhook")
    async def receive(request: Request, background: BackgroundTasks):
        body = await request.body()
        if not verify_signature(body, request.headers.get("X-Hub-Signature-256"), app_secret):
            return JSONResponse({"error": "bad signature"}, status_code=403)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return JSONResponse({"error": "bad json"}, status_code=400)
        messages = extract_messages(payload)
        log.info("webhook: %d message(s), %d status update(s)", len(messages), count_statuses(payload))
        if status_handler is not None:
            for status in extract_statuses(payload):
                status_handler(status)
        for msg in messages:
            # Capture owns durable deduplication. Persistence failure must fail this
            # delivery so Meta can retry, rather than acknowledging an unsaved note.
            if capture_ingest is not None and capture_ingest(msg):
                continue
            if store.mark_message_seen(msg.message_id, now()):
                background.add_task(handle, msg)
            else:
                log.info("webhook: duplicate delivery of %s ignored", msg.message_id)
        return {"status": "ok"}

    # Fixed infrastructure endpoints must precede username catch-all routes.
    for extra in routes:
        app.include_router(extra)
    return app
