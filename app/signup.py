"""Landing page and sign-up flow (spec §2): Google sign-in → WhatsApp number → join code.

Two short-lived signed cookies carry state between steps: `oauth_state` (the random value
Google must echo back, against forged sign-ins) and `signup` (which pending user this browser is).
"""

import logging
import json
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import pages
from app.crypto import TokenBox, sign, unsign
from app.router import RateLimiter
from app.tools.google_oauth import GoogleOAuthError
from app.users import SignupError, SignupsFull, Users

log = logging.getLogger(__name__)
COOKIE_AGE = timedelta(minutes=30)
STATE_COOKIE = "oauth_state"
SIGNUP_COOKIE = "signup"
CSRF_COOKIE = "signup_csrf"


def _client_ip(request: Request) -> str:
    # Railway sits behind a proxy; its address is the first X-Forwarded-For entry.
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "?")


def signup_routes(
    oauth,
    users: Users,
    box: TokenBox,
    commands,
    bot_whatsapp: str,
    session_secret: str,
    now: Callable[[], datetime],
    limiter: RateLimiter | None = None,
    on_user_changed: Callable[[int], None] = lambda user_id: None,  # drop cached contexts built from old details
    test_number_mode: bool = True,
    connections_callback: Callable | None = None,
) -> APIRouter:
    router = APIRouter()
    limiter = limiter or RateLimiter(limit=10, window=timedelta(hours=1))

    def set_cookie(response: Response, name: str, value: str) -> None:
        response.set_cookie(
            name, sign(value, session_secret, now()), max_age=int(COOKIE_AGE.total_seconds()),
            httponly=True, secure=True, samesite="lax",
        )

    def read_cookie(request: Request, name: str) -> str | None:
        raw = request.cookies.get(name)
        return unsign(raw, session_secret, now(), COOKIE_AGE) if raw else None

    def pending_user(request: Request):
        value = read_cookie(request, SIGNUP_COOKIE)
        return users.get(int(value)) if value and value.isdigit() else None

    def html(body: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store", "Referrer-Policy": "same-origin"})

    def csrf_for(request: Request) -> str:
        return read_cookie(request, CSRF_COOKIE) or secrets.token_urlsafe(24)

    def valid_csrf(request: Request, value: str) -> bool:
        expected = read_cookie(request, CSRF_COOKIE)
        return bool(expected and value and secrets.compare_digest(expected, value))

    def finish_oauth(response: Response) -> Response:
        response.delete_cookie(STATE_COOKIE)
        return response

    def session_response(response: Response, user_id: int) -> Response:
        set_cookie(response, SIGNUP_COOKIE, str(user_id))
        set_cookie(response, CSRF_COOKIE, secrets.token_urlsafe(24))
        return finish_oauth(response)

    @router.get("/signup", response_class=HTMLResponse)
    def landing(request: Request):
        if connections_callback is not None:
            return RedirectResponse("/signin", status_code=303)
        if pending_user(request):
            return RedirectResponse("/signup/status", status_code=303)
        return html(pages.landing(users.seats_left(), auto_activate=users.auto_activate, test_number_mode=test_number_mode))

    @router.get("/signup/google")
    def start(request: Request, gmail: bool = False):
        if connections_callback is not None:
            return RedirectResponse("/connections/gmail" if gmail else "/connections/calendar", status_code=303)
        if not limiter.allow(_client_ip(request), now()):
            return html(pages.message("Slow down", "Too many sign-in attempts. Please try again in an hour."), 429)
        state = secrets.token_urlsafe(24)
        user = pending_user(request)
        intent = {"state": state, "gmail": gmail, "email": user.email if user else None}
        url = oauth.auth_url(state, include_gmail=True) if gmail else oauth.auth_url(state)
        response = RedirectResponse(url, status_code=302)
        set_cookie(response, STATE_COOKIE, json.dumps(intent))
        return response

    @router.get("/signup/gmail", response_class=HTMLResponse)
    def connect_gmail(request: Request):
        if connections_callback is not None:
            return RedirectResponse("/connections/gmail", status_code=303)
        user = pending_user(request)
        return html(pages.gmail_connection(expected_email=user.email if user else ""))

    @router.get("/signup/callback")
    def callback(request: Request, state: str = "", code: str = "", error: str = ""):
        if connections_callback is not None:
            response = connections_callback(request, state, code, error)
            if response is not None:
                return response
        try:
            intent = json.loads(read_cookie(request, STATE_COOKIE) or "{}")
        except (ValueError, TypeError):
            intent = {}
        expected = intent.get("state", "")
        state_valid = bool(expected and secrets.compare_digest(expected, state))
        gmail_requested = bool(state_valid and intent.get("gmail"))
        retry = pages.try_again(include_gmail=gmail_requested)
        if not state_valid:
            return finish_oauth(html(pages.message("Sign-in expired", "That sign-in link isn't valid any more.", retry), 400))
        if error:
            return finish_oauth(html(pages.message("No problem", "You didn't allow access, so nothing was saved.", retry)))
        if not code:
            return finish_oauth(html(pages.message("Sign-in expired", "That sign-in link isn't valid any more.", retry), 400))
        try:
            account = oauth.exchange(code)
            existing = users.by_email(account.email)
            if connections_callback is not None and existing and (existing.google_subject or existing.google_disconnected):
                return finish_oauth(html(pages.message("Sign in again", "Manage this Google account through Connections. Start a fresh sign-in."), 403))
            if intent.get("email") and account.email.casefold() != intent["email"].casefold():
                return finish_oauth(html(pages.message(
                    "Use the same Google account", f"This setup belongs to {intent['email']}. No connection was changed. Choose that account or sign out to start a different setup.", retry
                ), 403))
            user, is_new = users.start_signup(account.name, account.email, box.encrypt(account.refresh_token), now(),
                                             granted_scopes=getattr(account, "granted_scopes", None))
        except GoogleOAuthError as exc:
            return finish_oauth(html(pages.message("Couldn't connect Google", str(exc), retry)))
        except SignupsFull:
            return finish_oauth(html(pages.landing(0, auto_activate=users.auto_activate, test_number_mode=test_number_mode)))
        except SignupError as exc:
            return finish_oauth(html(pages.message("Beta access", str(exc)), 403))
        if not is_new:
            on_user_changed(user.id)  # a new token must be used from the next message on
        target = "/signup/status" if user.joined or user.typed_phone else "/signup/number"
        return session_response(RedirectResponse(target, status_code=303), user.id)

    @router.get("/signup/number")
    def number_page(request: Request):
        user = pending_user(request)
        if user is None:
            return RedirectResponse("/signup", status_code=303)
        if user.joined or user.status == "active":
            return RedirectResponse("/signup/status", status_code=303)
        csrf = csrf_for(request)
        response = html(pages.number_form(user.first_name, phone=user.typed_phone or "", calendly=user.calendly_url or "",
                                         csrf_token=csrf, auto_activate=users.auto_activate, test_number_mode=test_number_mode))
        set_cookie(response, CSRF_COOKIE, csrf)
        return response

    @router.post("/signup/number")
    def number_submit(request: Request, phone: str = Form(""), calendly: str = Form(""), csrf_token: str = Form("")):
        user = pending_user(request)
        if user is None:
            return RedirectResponse("/signup", status_code=303)
        if not valid_csrf(request, csrf_token):
            return html(pages.message("Form expired", "Refresh setup and try again."), 403)
        if user.joined or user.status == "active":
            return RedirectResponse("/signup/status", status_code=303)
        try:
            user = users.set_details(user.id, phone, calendly, now())
        except SignupError as exc:
            return html(pages.number_form(user.first_name, str(exc), phone, calendly, csrf_token=csrf_token,
                                         auto_activate=users.auto_activate, test_number_mode=test_number_mode), 400)
        on_user_changed(user.id)
        log.info("sign-up: user #%d finished the form", user.id)
        commands.announce_signup(user)
        return RedirectResponse("/signup/done", status_code=303)

    @router.get("/signup/done")
    def done(request: Request):
        user = pending_user(request)
        if user is None or not user.join_code:
            return RedirectResponse("/signup", status_code=303)
        csrf = csrf_for(request)
        response = html(pages.onboarding_status(user, bot_whatsapp, csrf_token=csrf,
                                                auto_activate=users.auto_activate, test_number_mode=test_number_mode, now=now()))
        set_cookie(response, CSRF_COOKIE, csrf)
        return response

    @router.get("/signup/status")
    def status(request: Request):
        user = pending_user(request)
        if user is None:
            return RedirectResponse("/signup", status_code=303)
        csrf = csrf_for(request)
        response = html(pages.onboarding_status(user, bot_whatsapp, csrf_token=csrf,
                                                auto_activate=users.auto_activate, test_number_mode=test_number_mode, now=now()))
        set_cookie(response, CSRF_COOKIE, csrf)
        return response

    @router.post("/signup/logout")
    def logout(request: Request, csrf_token: str = Form("")):
        if not valid_csrf(request, csrf_token):
            return html(pages.message("Form expired", "Refresh setup and try again."), 403)
        response = RedirectResponse("/signup", status_code=303)
        for cookie in (SIGNUP_COOKIE, STATE_COOKIE, CSRF_COOKIE):
            response.delete_cookie(cookie)
        return response

    return router
