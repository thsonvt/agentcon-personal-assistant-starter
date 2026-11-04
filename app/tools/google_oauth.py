"""Google sign-in for friends who sign up (Plan 2B), using the "Web application" OAuth client.

Pippa's own token still comes from scripts/get_google_token.py and the Desktop client.
A refresh token only works with the client that issued it, so each user's calendar is
built with the matching client id and secret.
"""

import base64
import hashlib
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SCOPES = f"openid email profile {CALENDAR_SCOPE}"


class GoogleOAuthError(Exception):
    """Sign-in didn't give us what we need; the message is safe to show the person."""


@dataclass(frozen=True)
class GoogleAccount:
    email: str
    name: str
    refresh_token: str
    gmail_enabled: bool = False
    granted_scopes: tuple[str, ...] | None = None
    subject: str | None = None
    access_token: str | None = None


class GoogleOAuth:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, http: httpx.Client | None = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.http = http or httpx.Client(timeout=15)

    def auth_url(self, state: str, include_gmail: bool = False) -> str:
        return AUTH_URL + "?" + urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": SCOPES + (f" {GMAIL_SCOPE}" if include_gmail else ""),
                "access_type": "offline",  # we need a refresh token
                "prompt": "consent",  # ...every time, even if they signed in before
                "include_granted_scopes": "true",
                "state": state,
            }
        )

    def exchange(self, code: str) -> GoogleAccount:
        try:
            token = self.http.post(
                TOKEN_URL,
                data={
                    "code": code,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "redirect_uri": self.redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            if token.status_code != 200:
                raise GoogleOAuthError("Google didn't accept that sign-in. Please try again.")
            data = token.json()
            if CALENDAR_SCOPE not in data.get("scope", "").split():
                raise GoogleOAuthError("The bot needs access to your Google Calendar. Please sign in again and tick the calendar box.")
            if not data.get("refresh_token"):
                raise GoogleOAuthError("Google didn't give long-term access. Please try again.")
            info = self.http.get(USERINFO_URL, headers={"Authorization": f"Bearer {data['access_token']}"})
            if info.status_code != 200:
                raise GoogleOAuthError("Couldn't read your Google account details. Please try again.")
            profile = info.json()
        except httpx.HTTPError as exc:
            raise GoogleOAuthError("Couldn't reach Google. Please try again in a minute.") from exc
        if not profile.get("email") or not profile.get("email_verified"):
            raise GoogleOAuthError("Your Google account's email isn't verified.")
        email = profile["email"].strip().lower()
        return GoogleAccount(email=email, name=(profile.get("name") or email.split("@")[0]).strip(),
                             refresh_token=data["refresh_token"], gmail_enabled=GMAIL_SCOPE in data.get("scope", "").split(),
                             granted_scopes=tuple(sorted(set(data.get("scope", "").split()))))

    def connection_auth_url(self, state: str, verifier: str, service: str = "identity", email: str | None = None) -> str:
        scopes = "openid email profile"
        params = {
            "client_id": self.client_id, "redirect_uri": self.redirect_uri, "response_type": "code",
            "state": state, "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("="),
            "code_challenge_method": "S256",
        }
        if service in ("calendar", "gmail"):
            scopes += " " + (CALENDAR_SCOPE if service == "calendar" else GMAIL_SCOPE)
            params.update(access_type="offline", prompt="consent", include_granted_scopes="true")
        elif service != "identity":
            raise ValueError("Unknown Google capability")
        if email:
            params["login_hint"] = email
        params["scope"] = scopes
        return AUTH_URL + "?" + urlencode(params)

    def exchange_connection(self, code: str, verifier: str) -> GoogleAccount:
        try:
            response = self.http.post(TOKEN_URL, data={
                "code": code, "code_verifier": verifier, "client_id": self.client_id,
                "client_secret": self.client_secret, "redirect_uri": self.redirect_uri,
                "grant_type": "authorization_code",
            })
            if response.status_code != 200:
                raise GoogleOAuthError("Google didn't accept that sign-in. Please try again.")
            data = response.json()
            access = data.get("access_token")
            if not isinstance(access, str) or not access:
                raise GoogleOAuthError("Google didn't provide access. Please try again.")
            info = self.http.get(USERINFO_URL, headers={"Authorization": f"Bearer {access}"})
            if info.status_code != 200:
                raise GoogleOAuthError("Couldn't verify your Google account. Please try again.")
            profile = info.json()
            if (profile.get("email_verified") is not True or not isinstance(profile.get("sub"), str)
                    or not profile["sub"] or not isinstance(profile.get("email"), str) or not profile["email"].strip()):
                raise GoogleOAuthError("A verified Google account email and identity are required.")
            email = profile["email"].strip().lower()
            scopes = tuple(sorted(set(data.get("scope", "").split())))
            return GoogleAccount(email=email, name=(profile.get("name") or email.split("@")[0]).strip(),
                                 refresh_token=data.get("refresh_token") or "", granted_scopes=scopes,
                                 gmail_enabled=GMAIL_SCOPE in scopes, subject=profile["sub"], access_token=access)
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            raise GoogleOAuthError("Couldn't verify Google access. Please try again in a minute.") from exc

    def refresh_connection(self, refresh_token: str) -> str:
        try:
            response = self.http.post(TOKEN_URL, data={"client_id": self.client_id,
                "client_secret": self.client_secret, "refresh_token": refresh_token, "grant_type": "refresh_token"})
            if response.status_code in (400, 401) and response.json().get("error") == "invalid_grant":
                raise GoogleOAuthError("Google authorization was revoked or expired. Reconnect Google.")
            if response.status_code != 200 or not response.json().get("access_token"):
                raise GoogleOAuthError("Google access could not be checked right now. Try again later.")
            return response.json()["access_token"]
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise GoogleOAuthError("Google is temporarily unavailable. Try again later.") from exc

    def check_connection(self, access_token: str, service: str) -> tuple[str, str]:
        if service == "calendar":
            url = "https://www.googleapis.com/calendar/v3/users/me/calendarList?maxResults=1"
        elif service == "gmail":
            url = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
        else:
            raise ValueError("Unknown Google capability")
        try:
            response = self.http.get(url, headers={"Authorization": f"Bearer {access_token}"})
        except httpx.HTTPError:
            return "needs_attention", "Google is temporarily unavailable. Try checking again later."
        if response.status_code == 200:
            return "connected", "Permission granted and access checked successfully."
        if response.status_code == 401:
            return "needs_attention", "Google authorization is invalid or expired. Reconnect Google."
        if response.status_code == 403:
            # Only examine provider errors; never expose their raw text or account data.
            body = response.text.lower()
            if any(reason in body for reason in ("accessnotconfigured", "service_disabled", "servicedisabled", "api has not been used")):
                return "needs_attention", "This Google API is not enabled for Pippa. Contact the organizer."
            return "needs_attention", "Google denied this permission. Reconnect and allow access, or contact the organizer."
        return "needs_attention", "Google access could not be checked right now. Try again later."

    def revoke(self, token: str) -> None:
        try:
            response = self.http.post(REVOKE_URL, data={"token": token})
        except httpx.HTTPError as exc:
            raise GoogleOAuthError("Couldn't reach Google.") from exc
        if response.status_code != 200:
            raise GoogleOAuthError(f"Google didn't revoke the token ({response.status_code}).")
