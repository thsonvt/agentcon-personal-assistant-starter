"""Read-only Gmail access. Message content is untrusted data, never instructions.

Reads full MIME bodies, including HTML hrefs and inline calendar invitations.
Links are returned as evidence only: this module never fetches their destinations.
"""

import base64
import binascii
import json
import re
import threading
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from urllib.parse import quote, urlsplit

from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.errors import HttpError
from httplib2 import HttpLib2Error

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
MAX_BODY_CHARS = 12000
MAX_DECODED_BYTES = 128000
MAX_LINKS = 30


class GmailError(Exception):
    """Safe public-facing Gmail failure."""


class GmailAuthError(GmailError):
    """Credentials expired or revoked."""


class GmailPermissionError(GmailError):
    """Gmail read permission is missing from the grant."""


class GmailServiceDisabledError(GmailError):
    """Gmail API is disabled for the application."""


class GmailRateLimitError(GmailError):
    """Gmail quota or request rate exceeded."""


class GmailTemporaryError(GmailError):
    """Transient transport or service failure."""


class GmailNotFoundError(GmailError):
    """Message no longer exists or is unavailable."""


@dataclass(frozen=True)
class EmailMessage:
    id: str
    subject: str
    sender: str
    date: str
    body: str
    links: tuple[str, ...]
    source_url: str
    truncated: bool = False


def build_service(client_id: str, client_secret: str, refresh_token: str):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None, refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id, client_secret=client_secret, scopes=SCOPES,
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _safe_link(value: str) -> str | None:
    """Allow displayable web links only. This is not permission to fetch URLs."""
    if len(value) > 2048 or any(ord(c) < 33 for c in value):
        return None
    try:
        url = urlsplit(value)
        if url.scheme in {"http", "https"} and url.hostname and not url.username and not url.password:
            return value
    except ValueError:
        pass
    return None


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.links: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if self.hidden:
            return
        if tag in {"p", "div", "br", "li", "tr"}:
            self.text.append("\n")
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value:
                    self.links.append(value)

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        elif not self.hidden and tag in {"p", "div", "li", "tr"}:
            self.text.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def _message(item: dict) -> EmailMessage:
    payload = item.get("payload", {})
    headers = {h["name"].lower(): h.get("value", "") for h in payload.get("headers", []) if "name" in h}
    bodies: list[str] = []
    links: list[str] = []
    truncated = False
    remaining = MAX_DECODED_BYTES
    pending = [payload]
    count = 0
    while pending and count < 100:
        part = pending.pop()
        count += 1
        pending.extend(reversed(part.get("parts", [])[:100]))
        if len(part.get("parts", [])) > 100:
            truncated = True
        mime = part.get("mimeType", "").lower()
        if mime not in {"text/plain", "text/html", "text/calendar"}:
            continue
        # Ordinary file attachments are outside the message body. Inline .ics is useful.
        if part.get("filename") and mime != "text/calendar":
            continue
        body = part.get("body", {})
        data = body.get("data", "")
        if body.get("attachmentId") and not data:
            truncated = True
            continue
        if not data:
            continue
        max_encoded = (remaining // 3) * 4
        if len(data) > max_encoded:
            data = data[:max_encoded]
            truncated = True
        try:
            raw = base64.b64decode(data + "=" * (-len(data) % 4), altchars=b"-_", validate=True)
        except (ValueError, binascii.Error):
            truncated = True
            continue
        remaining -= len(raw)
        content_type = Message()
        for header in part.get("headers", []):
            if header.get("name", "").lower() == "content-type":
                content_type["content-type"] = header.get("value", "")
                break
        try:
            text = raw.decode(content_type.get_content_charset() or "utf-8", errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")
        if mime == "text/html":
            parser = _HTMLText()
            parser.feed(text)
            text = "".join(parser.text)
            links.extend(parser.links)
        elif mime == "text/calendar":
            # RFC 5545 folded lines can split URLs or location strings.
            text = re.sub(r"\r?\n[ \t]", "", text)
        links.extend(re.findall(r'https?://[^\s<>"\']+', text))
        bodies.append(text.strip())
    if pending:
        truncated = True
    safe_links = tuple(dict.fromkeys(link for raw in links if (link := _safe_link(raw))))
    text = "\n\n".join(dict.fromkeys(body for body in bodies if body))
    truncated |= len(text) > MAX_BODY_CHARS or len(safe_links) > MAX_LINKS
    message_id = item.get("id", "")
    return EmailMessage(
        id=message_id, subject=headers.get("subject", "(no subject)")[:1000],
        sender=headers.get("from", "")[:1000], date=headers.get("date", "")[:200],
        body=text[:MAX_BODY_CHARS], links=safe_links[:MAX_LINKS],
        source_url="https://mail.google.com/mail/u/0/#all/" + quote(message_id, safe=""),
        truncated=truncated,
    )


def _http_error(exc: HttpError) -> GmailError:
    status = exc.resp.status
    try:
        error = json.loads(exc.content).get("error", {})
        reasons = {e.get("reason") for e in error.get("errors", [])}
        reasons.update(e.get("reason") for e in error.get("details", []))
    except (ValueError, TypeError, AttributeError):
        reasons = set()
    if status == 401:
        return GmailAuthError("Gmail authorization has expired or been revoked. Reconnect Google.")
    if status == 429 or reasons & {"rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded", "quotaExceeded"}:
        return GmailRateLimitError("Gmail is temporarily rate limited. Try again later.")
    if reasons & {"accessNotConfigured", "SERVICE_DISABLED"}:
        return GmailServiceDisabledError("The Gmail API is disabled for this application. The operator must enable it.")
    if reasons & {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}:
        return GmailPermissionError("This Google connection lacks Gmail read permission. Reconnect and grant Gmail read access.")
    if status == 404:
        return GmailNotFoundError("That email was not found; it may have been deleted.")
    if status >= 500:
        return GmailTemporaryError("Gmail is temporarily unavailable. Try again later.")
    return GmailError(f"Gmail could not complete the request (HTTP {status}).")


class GoogleGmail:
    def __init__(self, service, before_request=None):
        self.service = service
        self.before_request = before_request
        self._lock = threading.Lock()

    def _execute(self, request) -> dict:
        try:
            # The SDK can invoke tools concurrently; googleapiclient transport cannot.
            with self._lock:
                if self.before_request is not None:
                    self.before_request()
                return request.execute()
        except RefreshError as exc:
            # Calendar-only grants can fail at the token endpoint before Gmail
            # returns a 403. Inspect structured metadata, never expose its text.
            if any(isinstance(arg, dict) and arg.get("error") == "invalid_scope" for arg in exc.args):
                raise GmailPermissionError(
                    "This Google connection lacks Gmail read permission. Reconnect and grant Gmail read access."
                ) from exc
            if exc.retryable:
                raise GmailTemporaryError("Google authorization is temporarily unavailable. Try again later.") from exc
            raise GmailAuthError("Gmail authorization has expired or been revoked. Reconnect Google.") from exc
        except HttpError as exc:
            raise _http_error(exc) from exc
        except (TransportError, HttpLib2Error, OSError) as exc:
            raise GmailTemporaryError("Could not reach Gmail. Try again later.") from exc

    def search_messages(self, query: str, limit: int = 5) -> list[EmailMessage]:
        if not query.strip():
            raise GmailError("Provide a specific email search query.")
        limit = max(1, min(limit, 5))
        result = self._execute(self.service.users().messages().list(userId="me", q=query, maxResults=limit))
        return [self.get_message(item["id"]) for item in result.get("messages", [])[:limit]]

    def get_message(self, message_id: str) -> EmailMessage:
        item = self._execute(self.service.users().messages().get(userId="me", id=message_id, format="full"))
        return _message(item)
