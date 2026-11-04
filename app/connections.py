"""Authenticated connection management; Google callback URI remains /signup/callback.

Opaque sessions and single-use OAuth attempts live in SQLite. Identity-only sign-in
never replaces integration credentials. Provider checks read metadata, not messages.
"""
import hashlib
import json
import secrets
import re
import threading
from datetime import datetime, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.connection_pages import render_connection_page
from app.router import RateLimiter
from app.signup import _client_ip
from app.tools.google_oauth import GoogleOAuthError
from app.users import CALENDAR_SCOPE, GMAIL_SCOPES, SignupError

SESSION_COOKIE = "pippa_session"
OAUTH_COOKIE = "pippa_oauth"
EVENT_BROWSER_COOKIE = "pippa_event_browser"
SESSION_AGE = timedelta(days=30)
OAUTH_AGE = timedelta(minutes=10)
_LOCK = threading.RLock()


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


class _BoundedEventLimiter:
    """Local burst protection with expiring entries and a hard memory ceiling."""
    def __init__(self, limit, max_keys=4096):
        self.limit, self.max_keys = limit, max_keys
        self.hits = {}

    def allow(self, key, now):
        cutoff = now - timedelta(hours=1)
        for old_key in list(self.hits):
            recent = [stamp for stamp in self.hits[old_key] if stamp > cutoff]
            if recent:
                self.hits[old_key] = recent
            else:
                del self.hits[old_key]
        if key not in self.hits and len(self.hits) >= self.max_keys:
            return False
        recent = self.hits.setdefault(key, [])
        if len(recent) >= self.limit:
            return False
        recent.append(now)
        return True


class Connections:
    def __init__(self, oauth, users, box, commands, bot_whatsapp, now,
                 on_user_changed=lambda user_id: None, test_number_mode=True, receipts=None, event_access=None):
        self.oauth, self.users, self.box, self.commands = oauth, users, box, commands
        self.bot_whatsapp, self.now = bot_whatsapp, now
        self.on_user_changed, self.test_number_mode = on_user_changed, test_number_mode
        self.receipts = receipts if receipts is not None else getattr(commands, "receipts", None)
        self.conn = users.conn
        self.event_access = event_access
        self.event_browser_limiter = _BoundedEventLimiter(10)
        self.event_ip_limiter = _BoundedEventLimiter(300)
        self.limiter = RateLimiter(limit=20, window=timedelta(hours=1))
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS connection_sessions (
                token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                csrf TEXT NOT NULL, expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS connection_oauth (
                state_hash TEXT PRIMARY KEY, browser_hash TEXT NOT NULL,
                verifier TEXT NOT NULL, service TEXT NOT NULL,
                session_hash TEXT, user_id INTEGER, expires_at TEXT NOT NULL
            );
        """)
        if "event_slug" not in {row[1] for row in self.conn.execute("PRAGMA table_info(connection_oauth)")}:
            self.conn.execute("ALTER TABLE connection_oauth ADD COLUMN event_slug TEXT")
        self.conn.commit()
        self.router = APIRouter()
        self._routes()

    def _session(self, token):
        if not token:
            return None
        row = self.conn.execute("SELECT * FROM connection_sessions WHERE token_hash=? AND expires_at>?",
                                (_hash(token), self.now().isoformat())).fetchone()
        if row is None:
            return None
        user = self.users.get(row["user_id"])
        if user is None or user.is_admin or user.status in ("disabled", "rejected"):
            return None
        return dict(row)

    def _auth(self, request):
        session = self._session(request.cookies.get(SESSION_COOKIE))
        return (self.users.get(session["user_id"]), session) if session else (None, None)

    def _redirect(self, target, status=303):
        return RedirectResponse(target, status_code=status, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    def _onboarding_payload(self, user):
        onboarding = self.receipts.view(user) if self.receipts is not None else {}
        expired = bool(user.join_code_expires_at and self.now() >= datetime.fromisoformat(user.join_code_expires_at))
        payload = {"onboarding": onboarding, "joined": user.verified_join, "active": user.status == "active",
                   "beta_approved": user.status in ("approved", "active"), "join_expired": expired}
        # Only the revision leaves the server; never expose join codes, cookies or tokens in this JSON endpoint.
        revision = {**payload, "generation": user.join_code, "expires": user.join_code_expires_at,
                    "phone": user.typed_phone, "calendar": user.calendar_status}
        payload["revision"] = hashlib.sha256(json.dumps(revision, sort_keys=True).encode()).hexdigest()
        return payload

    def _page(self, view, user=None, session=None, status=200, **extra):
        data = {"bot_whatsapp": self.bot_whatsapp, "auto_activate": self.users.auto_activate,
                "test_number_mode": self.test_number_mode, "seats_left": self.users.seats_left()}
        if user:
            onboarding = self._onboarding_payload(user)
            data.update(email=user.email, name=user.name, csrf_token=session["csrf"] if session else "",
                        calendar_status=user.calendar_status, calendar_detail=user.calendar_detail,
                        gmail_status=user.gmail_status, gmail_detail=user.gmail_detail,
                        checked_at=user.google_checked_at, phone=(user.phone if user.verified_join else user.typed_phone) or "",
                        joined=user.verified_join, active=user.status == "active",
                        beta_approved=user.status in ("approved", "active"), calendar_ready=user.calendar_ready,
                        approval_pending=user.status == "pending" and not self.users.auto_activate, join_code=user.join_code,
                        join_code_expires_at=user.join_code_expires_at,
                        onboarding=onboarding["onboarding"], onboarding_revision=onboarding["revision"],
                        join_expired=bool(user.join_code_expires_at and self.now() >= datetime.fromisoformat(user.join_code_expires_at)),
                        booking_url=user.calendly_url or "", revocation_status=user.google_revocation_status)
            if not user.google_token_enc or user.google_disconnected:
                data.update(calendar_status="not_connected", gmail_status="not_connected")
        data.update(extra)
        return HTMLResponse(render_connection_page(view, data), status_code=status,
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                     "X-Content-Type-Options": "nosniff"})

    def _cookie(self, response, name, value, age):
        response.set_cookie(name, value, max_age=int(age.total_seconds()), secure=True, httponly=True, samesite="lax")

    def _clear_oauth(self, response):
        response.delete_cookie(OAUTH_COOKIE, secure=True, httponly=True, samesite="lax")
        return response

    def _start(self, request, service, event_slug=None):
        if event_slug is not None and (
            service != "identity" or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", event_slug)
            or not self.event_access or not self.event_access.get_event(event_slug)
        ):
            return self._page("error", status=400, error="This event sign-in link is not available.")
        user, session = self._auth(request)
        if service != "identity" and not user:
            return self._redirect("/signin")
        if service != "identity" and user.google_revocation_status == "pending":
            return self._page("error", user, session, status=409, error="Google revocation is still in progress. Try reconnecting shortly. If this persists, contact the organizer and review Pippa in your Google account permissions.")
        if service != "identity" and user.event_only:
            try:
                self.users.check_event_upgrade(user)
            except SignupError as exc:
                return self._page("error", user, session, status=403, error=str(exc))
        event_browser = None
        if event_slug is not None:
            event_browser = request.cookies.get(EVENT_BROWSER_COOKIE, "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", event_browser):
                event_browser = secrets.token_urlsafe(32)
            # Use the transport peer for coarse protection. Unverified forwarding
            # headers cannot let a client manufacture new network identities.
            peer = request.client.host if request.client else "unknown"
            with _LOCK:
                allowed = (self.event_browser_limiter.allow(_hash(event_browser), self.now())
                           and self.event_ip_limiter.allow(peer, self.now()))
        else:
            allowed = self.limiter.allow(_client_ip(request), self.now())
        if not allowed:
            return self._page("error", status=429, error="Too many sign-in attempts. Please try again in an hour.")
        state, browser, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        with _LOCK:
            if service != "identity" and self.users.get(user.id).google_revocation_status == "pending":
                return self._page("error", user, session, status=409, error="Google revocation is still in progress. Try reconnecting shortly. If this persists, contact the organizer and review Pippa in your Google account permissions.")
            self.conn.execute("DELETE FROM connection_oauth WHERE expires_at<=?", (self.now().isoformat(),))
            self.conn.execute("DELETE FROM connection_sessions WHERE expires_at<=?", (self.now().isoformat(),))
            self.conn.execute("INSERT INTO connection_oauth "
                              "(state_hash,browser_hash,verifier,service,session_hash,user_id,expires_at,event_slug) "
                              "VALUES (?,?,?,?,?,?,?,?)", (
                _hash(state), _hash(browser), verifier, service,
                session["token_hash"] if session and service != "identity" else None,
                user.id if user and service != "identity" else None, (self.now() + OAUTH_AGE).isoformat(), event_slug))
            self.conn.commit()
        response = self._redirect(self.oauth.connection_auth_url(state, verifier, service, user.email if user and service != "identity" else None), 302)
        self._cookie(response, OAUTH_COOKIE, browser, OAUTH_AGE)
        if event_browser:
            self._cookie(response, EVENT_BROWSER_COOKIE, event_browser, SESSION_AGE)
        return response

    def callback(self, request, state="", code="", error=""):
        # State lookup routes the shared callback without consuming unrelated legacy flows.
        with _LOCK:
            intent = self.conn.execute("SELECT * FROM connection_oauth WHERE state_hash=?", (_hash(state),)).fetchone()
            if intent is None:
                if request.cookies.get(OAUTH_COOKIE) or not request.cookies.get("oauth_state"):
                    return self._clear_oauth(self._page("error", status=400, error="That sign-in has expired. Please try again."))
                return None
            self.conn.execute("DELETE FROM connection_oauth WHERE state_hash=?", (_hash(state),))
            self.conn.commit()
        browser = request.cookies.get(OAUTH_COOKIE, "")
        if (intent["expires_at"] <= self.now().isoformat() or not browser
                or not secrets.compare_digest(intent["browser_hash"], _hash(browser))):
            return self._clear_oauth(self._page("error", status=400, error="That sign-in has expired. Please try again."))
        user, session = self._auth(request)
        if intent["service"] != "identity" and (not session or intent["session_hash"] != session["token_hash"] or intent["user_id"] != user.id):
            return self._clear_oauth(self._page("error", status=403, error="Sign in again before changing this connection."))
        if error:
            return self._clear_oauth(self._page(intent["service"] if intent["service"] != "identity" else "welcome", user, session,
                                               notice="You did not allow access. No connection was changed."))
        if not code:
            return self._clear_oauth(self._page("error", status=400, error="Google did not return a sign-in code. Please try again."))
        try:
            account = self.oauth.exchange_connection(code, intent["verifier"])
            if intent["service"] == "identity":
                event_slug = intent["event_slug"]
                if event_slug:
                    if (not self.event_access or not self.event_access.get_event(event_slug)
                            or not self.event_access.eligible_for_claim(event_slug, account.email.strip().lower())):
                        raise SignupError("Your verified email does not match a checked-in attendee. Contact the check-in team.")
                    user, is_new = self.users.sign_in_event_identity(account.subject, account.name, account.email, self.now())
                else:
                    user, is_new = self.users.sign_in_identity(account.subject, account.name, account.email, self.now())
                token = secrets.token_urlsafe(32)
                with _LOCK:
                    old_token = request.cookies.get(SESSION_COOKIE)
                    if old_token:
                        self.conn.execute("DELETE FROM connection_sessions WHERE token_hash=?", (_hash(old_token),))
                    self.conn.execute("INSERT INTO connection_sessions VALUES (?,?,?,?)",
                                      (_hash(token), user.id, secrets.token_urlsafe(32), (self.now() + SESSION_AGE).isoformat()))
                    self.conn.commit()
                target = f"/events/{event_slug}/claim" if event_slug else ("/connections/calendar" if is_new else "/connections")
                response = self._redirect(target)
                self._cookie(response, SESSION_COOKIE, token, SESSION_AGE)
                return self._clear_oauth(response)
            if account.subject != user.google_subject or account.email != user.email:
                return self._clear_oauth(self._page("error", user, session, status=403,
                    error="Use the same Google account shown in Connections. No connection was changed."))
            return self._clear_oauth(self._save_grant(intent["service"], account, user, session))
        except (GoogleOAuthError, SignupError, ValueError) as exc:
            return self._clear_oauth(self._page("error", user, session, status=403 if isinstance(exc, SignupError) else 400,
                                               error=str(exc) if not isinstance(exc, ValueError) or isinstance(exc, SignupError) else "Stored access could not be read. Reconnect Google."))

    def _save_grant(self, service, account, user, session):
        scopes = set(account.granted_scopes or ())
        requested = CALENDAR_SCOPE in scopes if service == "calendar" else bool(GMAIL_SCOPES & scopes)
        if not requested:
            return self._page(service, user, session, notice="The requested permission was not granted. No connection was changed.")
        # Refuse replacing a token with a narrower grant; preserve both services on reconnect.
        existing = set(user.google_scopes or ())
        if user.google_token_enc and not user.google_disconnected and not (existing - {"openid", "email", "profile"}).issubset(scopes):
            return self._page(service, user, session, error="Google returned fewer permissions than your existing connection. No access was replaced. Retry and allow the existing permissions too.")
        if not account.refresh_token and not user.google_token_enc:
            return self._page(service, user, session, error="Google did not provide long-term access. Reconnect and allow access.")
        import json
        fields = dict(google_token_enc=self.box.encrypt(account.refresh_token) if account.refresh_token else user.google_token_enc,
                      google_scopes=json.dumps(sorted(scopes)), google_disconnected=0, google_revocation_status=None,
                      google_checked_at=self.now().isoformat())
        # A newly issued refresh token is checked directly; access-token success alone
        # must not mask a broken credential that the WhatsApp worker will actually use.
        try:
            access = self.oauth.refresh_connection(account.refresh_token or self.box.decrypt(user.google_token_enc))
        except GoogleOAuthError as exc:
            return self._page(service, user, session, error=str(exc))
        for capability, allowed in (("calendar", CALENDAR_SCOPE in scopes), ("gmail", bool(GMAIL_SCOPES & scopes))):
            status, detail = self.oauth.check_connection(access, capability) if allowed else ("not_connected", "Permission has not been granted.")
            fields[capability + "_status"], fields[capability + "_detail"] = status, detail
        if (self.users.auto_activate and fields["calendar_status"] == "connected"
                and user.status in ("pending", "approved")):
            self.users._check_automatic_eligibility(user.email, scopes)
            verified_join = user.joined and user.phone and user.phone == user.typed_phone
            fields["status"] = "active" if verified_join else "approved"
        self.users.commit_connection(user.id, user.google_connection_version, self.now(), **fields)
        self.on_user_changed(user.id)
        if fields[service + "_status"] != "connected":
            return self._redirect("/connections/" + service)
        target = "/connections" if user.joined else ("/connections/gmail" if service == "calendar" else "/connections/whatsapp")
        return self._redirect(target)

    def _post_auth(self, request, csrf):
        user, session = self._auth(request)
        if not user:
            return None, None, self._redirect("/signin")
        if not csrf or not secrets.compare_digest(session["csrf"], csrf):
            return user, session, self._page("error", user, session, status=403, error="Form expired. Refresh Connections and try again.")
        return user, session, None

    def _routes(self):
        @self.router.get("/signin")
        def signin(request: Request):
            user, _ = self._auth(request)
            return self._redirect("/connections") if user else self._page("welcome")

        @self.router.get("/signin/google")
        def google_signin(request: Request, event: str | None = None):
            return self._start(request, "identity", event_slug=event)

        @self.router.get("/connections/google")
        def google_connect(request: Request, service: str = ""):
            if service not in ("calendar", "gmail"):
                return self._page("error", status=400, error="Choose Calendar or Gmail.")
            return self._start(request, service)

        @self.router.get("/connections/whatsapp/status")
        def whatsapp_status(request: Request):
            user, _ = self._auth(request)
            headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
            if user is None:
                return JSONResponse({"error": "sign_in_required"}, status_code=401, headers=headers)
            return JSONResponse(self._onboarding_payload(user), headers=headers)

        for path, view in (("/connections", "dashboard"), ("/connections/calendar", "calendar"),
                           ("/connections/gmail", "gmail"), ("/connections/whatsapp", "whatsapp"),
                           ("/connections/booking", "booking"), ("/connections/disconnect", "disconnect")):
            def make_page(selected):
                def page(request: Request):
                    user, session = self._auth(request)
                    return self._page(selected, user, session) if user else self._redirect("/signin")
                return page
            self.router.add_api_route(path, make_page(view), methods=["GET"])

        @self.router.post("/connections/booking")
        def booking(request: Request, csrf_token: str = Form(""), booking_url: str = Form("")):
            user, session, error = self._post_auth(request, csrf_token)
            if error is not None: return error
            try:
                self.users.set_booking(user.id, booking_url, self.now())
            except SignupError as exc:
                return self._page("booking", user, session, status=400, error=str(exc), booking_url=booking_url)
            self.on_user_changed(user.id)
            return self._redirect("/connections")

        @self.router.post("/connections/whatsapp")
        def whatsapp(request: Request, csrf_token: str = Form(""), phone: str = Form(""), country_code: str = Form("")):
            user, session, error = self._post_auth(request, csrf_token)
            if error is not None: return error
            try:
                updated = self.users.set_details(user.id, phone, user.calendly_url, self.now(), country_code=country_code)
            except SignupError as exc:
                return self._page("whatsapp", user, session, status=400, error=str(exc), phone_input=phone, country_code=country_code)
            self.on_user_changed(user.id)
            self.commands.announce_signup(updated)
            return self._redirect("/connections/whatsapp")

        @self.router.post("/connections/check")
        def check(request: Request, csrf_token: str = Form("")):
            user, session, error = self._post_auth(request, csrf_token)
            if error is not None: return error
            if not user.google_token_enc or user.google_disconnected:
                return self._redirect("/connections")
            fields = {"google_checked_at": self.now().isoformat()}
            try:
                access = self.oauth.refresh_connection(self.box.decrypt(user.google_token_enc))
                for capability, allowed in (("calendar", user.calendar_authorized), ("gmail", user.gmail_authorized)):
                    if allowed is True:
                        status, detail = self.oauth.check_connection(access, capability)
                    elif allowed is None:
                        status, detail = "unknown", "Reconnect to verify the permissions of this older connection."
                    else:
                        status, detail = "not_connected", "Permission has not been granted."
                    fields[capability + "_status"], fields[capability + "_detail"] = status, detail
            except (GoogleOAuthError, ValueError) as exc:
                for capability, allowed in (("calendar", user.calendar_authorized), ("gmail", user.gmail_authorized)):
                    if allowed is not False:
                        fields[capability + "_status"] = "needs_attention"
                        fields[capability + "_detail"] = str(exc) if isinstance(exc, GoogleOAuthError) else "Stored access could not be read. Reconnect Google."
            try:
                self.users.commit_connection(user.id, user.google_connection_version, self.now(), **fields)
            except SignupError:
                return self._redirect("/connections")
            self.on_user_changed(user.id)
            return self._redirect("/connections")

        @self.router.post("/connections/disconnect")
        def disconnect(request: Request, csrf_token: str = Form("")):
            user, session, error = self._post_auth(request, csrf_token)
            if error is not None: return error
            with _LOCK:
                current = self.users.get(user.id)
                if current.google_revocation_status == "pending":
                    return self._redirect("/connections")
                disconnected = self.users.disconnect_google(user.id, self.now())
            self.on_user_changed(user.id)
            revocation = "not_needed"
            if user.google_token_enc:
                try:
                    self.oauth.revoke(self.box.decrypt(user.google_token_enc))
                    revocation = "succeeded"
                except (GoogleOAuthError, ValueError):
                    revocation = "failed"
            self.users.finish_google_disconnect(user.id, disconnected.google_connection_version, revocation, self.now())
            return self._redirect("/connections")

        @self.router.post("/connections/logout")
        def logout(request: Request, csrf_token: str = Form("")):
            user, session, error = self._post_auth(request, csrf_token)
            if error is not None: return error
            with _LOCK:
                self.conn.execute("DELETE FROM connection_sessions WHERE token_hash=?", (session["token_hash"],))
                self.conn.execute("DELETE FROM connection_oauth WHERE session_hash=?", (session["token_hash"],))
                self.conn.commit()
            response = self._redirect("/signin")
            response.delete_cookie(SESSION_COOKIE, secure=True, httponly=True, samesite="lax")
            return self._clear_oauth(response)
