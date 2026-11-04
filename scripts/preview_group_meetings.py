"""Disposable local group-coordination preview; no real API credentials or sends.

uvicorn scripts.preview_group_meetings:create_preview --factory --port 8832
"""
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.connections import Connections
from app.crypto import TokenBox, generate_key
from app.group_meetings import GroupMeetings
from app.group_privacy import install_private_meeting_log_filter
from app.pages import privacy
from app.store import Store
from app.tools.calendar import CalendarError, Event, EventNotFoundError
from app.users import Users, CALENDAR_SCOPE


class DemoCalendar:
    def __init__(self):
        self.events = {}
        self.attendees = {}

    def busy_periods(self, start, end):
        return [(e.start, e.end) for e in self.events.values() if e.start < end and e.end > start]

    def create_event(self, title, start, end, attendee_email=None, location=None, event_id=None, attendee_emails=None):
        if event_id in self.events:
            raise CalendarError("Duplicate demo event")
        self.events[event_id] = Event(event_id, title, start, end, False, location=location,
                                      source_url="http://localhost:8832/__preview/events")
        self.attendees[event_id] = attendee_emails or ([attendee_email] if attendee_email else [])
        return self.events[event_id]

    def get_event(self, event_id):
        if event_id not in self.events:
            raise EventNotFoundError("No demo event")
        return self.events[event_id]


def create_preview():
    now = lambda: datetime.now(timezone.utc)
    store, box = Store(":memory:"), TokenBox(generate_key())
    users = Users(store)
    users.ensure_admin("Preview admin", "6590000000", now())
    user, _ = users.sign_in_identity("preview-host", "Son (demo)", "host@example.com", now())
    users._update(user.id, now(), status="approved", google_token_enc=box.encrypt("fake-refresh"),
                  google_scopes=json.dumps([CALENDAR_SCOPE]), calendar_status="connected")
    connections = Connections(None, users, box, SimpleNamespace(), "", now, test_number_mode=False)
    calendar = DemoCalendar()
    groups = GroupMeetings(connections, users, lambda uid: calendar, now, "http://localhost:8832", "preview-group" * 4)
    install_private_meeting_log_filter()
    app = FastAPI()
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])

    @app.middleware("http")
    async def banner(request, call_next):
        response = await call_next(request)
        if "text/html" not in response.headers.get("content-type", ""):
            return response
        content = b"".join([chunk async for chunk in response.body_iterator]).decode()
        notice = ('<aside style="padding:12px 20px;background:#fff0cc;color:#493600;font:13px system-ui">'
                  '<strong>Local demo — fake Google Calendar.</strong> No real invitations or messages. '
                  '<a href="/__preview/host">Demo host sign-in</a></aside>')
        headers = {k: v for k, v in response.headers.items() if k.lower() not in ("content-length", "set-cookie")}
        result = HTMLResponse(content.replace("<body>", "<body>" + notice, 1), status_code=response.status_code, headers=headers)
        for key, value in response.raw_headers:
            if key.lower() == b"set-cookie":
                result.raw_headers.append((key, value))
        return result

    @app.get("/")
    def home(): return RedirectResponse("/__preview/host")

    @app.get("/__preview/host")
    def host():
        token = secrets.token_urlsafe(32)
        users.conn.execute("INSERT INTO connection_sessions VALUES (?,?,?,?)", (
            hashlib.sha256(token.encode()).hexdigest(), user.id, secrets.token_urlsafe(32),
            (now() + timedelta(hours=4)).isoformat()))
        users.conn.commit()
        response = RedirectResponse("/connections/meetings", status_code=303)
        response.set_cookie("pippa_session", token, httponly=True, samesite="lax")
        return response

    @app.get("/__preview/events")
    def events():
        return {"simulated": True, "count": len(calendar.events),
                "attendees": list(calendar.attendees.values())}

    @app.get("/privacy")
    def privacy_page(): return HTMLResponse(privacy())

    app.include_router(connections.router)
    app.include_router(groups.router)
    return app
