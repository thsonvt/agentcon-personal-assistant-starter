"""WhatsApp Business Cloud API: send messages, show "typing…", download media."""

import httpx
from urllib.parse import urlsplit, quote

GRAPH_URL = "https://graph.facebook.com/v25.0"
MAX_TEXT = 4096
MAX_MEDIA_BYTES = 16 * 1024 * 1024  # WhatsApp's own limit for audio
WINDOW_CLOSED_CODE = 131047  # Meta: more than 24 hours since the customer last replied


class WhatsAppError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class WindowClosedError(WhatsAppError):
    """Free-form messages are not allowed; a template is needed."""


def clip(body: str, limit: int = MAX_TEXT) -> str:
    return body if len(body) <= limit else body[: limit - 1] + "…"


class WhatsAppClient:
    def __init__(self, token: str, phone_number_id: str, http: httpx.Client | None = None, base_url: str = GRAPH_URL):
        self.token = token
        self.base_url = base_url
        self.url = f"{base_url}/{phone_number_id}/messages"
        self.http = http or httpx.Client(timeout=15)

    def send_text(self, to: str, body: str) -> str:
        return self._message_id(self._post({"to": to, "type": "text", "text": {"body": clip(body)}}))

    def mark_read_typing(self, message_id: str) -> None:
        """Blue ticks on Pippa's message plus "typing…" in his chat.

        WhatsApp shows the indicator until the reply arrives or 25 seconds pass, whichever is first.
        """
        self._post({"status": "read", "message_id": message_id, "typing_indicator": {"type": "text"}})

    def send_template(self, to: str, name: str, params: list[str], language: str = "en") -> str:
        return self._message_id(self._post(
            {
                "to": to,
                "type": "template",
                "template": {
                    "name": name,
                    "language": {"code": language},
                    "components": [
                        {"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}
                    ],
                },
            }
        ))

    @staticmethod
    def _message_id(response: object) -> str:
        messages = response.get("messages") if isinstance(response, dict) else None
        first = messages[0] if isinstance(messages, list) and messages else None
        message_id = first.get("id") if isinstance(first, dict) else None
        if not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 512:
            raise WhatsAppError("WhatsApp returned an invalid send response.")
        return message_id

    def download_media(self, media_id: str) -> tuple[bytes, str]:
        """Fetch an incoming voice note or file: returns (bytes, mime type).

        Two steps: ask Graph for a short-lived URL (valid 5 minutes), then download it with the same token.
        """
        auth = {"Authorization": f"Bearer {self.token}"}
        try:
            meta = self.http.get(f"{self.base_url}/{quote(media_id, safe='')}", headers=auth)
            if meta.status_code >= 400:
                raise WhatsAppError(f"WhatsApp couldn't find that media ({meta.status_code}).")
            info = meta.json()
            if not isinstance(info, dict) or not isinstance(info.get('url'), str):
                raise WhatsAppError('WhatsApp returned invalid media metadata.')
            url = urlsplit(info['url'])
            host = (url.hostname or '').lower()
            if (url.scheme != 'https' or url.username or url.password or url.port not in (None, 443)
                    or not any(host == domain or host.endswith('.' + domain)
                               for domain in ('fbsbx.com', 'fbcdn.net', 'facebook.com'))):
                raise WhatsAppError('WhatsApp returned an unsupported media URL.')
            if int(info.get("file_size") or 0) > MAX_MEDIA_BYTES:
                raise WhatsAppError("That file is too large.")
            # Do not trust only file_size: enforce the bound while receiving bytes,
            # and never forward the token through a redirect to another origin.
            with self.http.stream('GET', info['url'], headers=auth, follow_redirects=False) as media:
                if not 200 <= media.status_code < 300:
                    raise WhatsAppError(f"WhatsApp media download failed ({media.status_code}).")
                content = bytearray()
                for chunk in media.iter_bytes():
                    if len(content) + len(chunk) > MAX_MEDIA_BYTES:
                        raise WhatsAppError('That file is too large.')
                    content.extend(chunk)
                mime = info.get('mime_type') or media.headers.get('content-type', 'application/octet-stream')
                if not isinstance(mime, str):
                    raise WhatsAppError('WhatsApp returned invalid media metadata.')
                return bytes(content), mime
        except httpx.HTTPError as exc:
            raise WhatsAppError("Could not download the media from WhatsApp.") from exc
        except (ValueError, TypeError) as exc:
            raise WhatsAppError('WhatsApp returned invalid media metadata.') from exc

    def _post(self, payload: dict) -> object:
        try:
            response = self.http.post(
                self.url,
                json={"messaging_product": "whatsapp", **payload},
                headers={"Authorization": f"Bearer {self.token}"},
            )
        except httpx.HTTPError as exc:
            raise WhatsAppError("Could not reach WhatsApp.") from exc
        try:
            data = response.json()
        except ValueError:
            raise WhatsAppError(f"WhatsApp returned an invalid response ({response.status_code}).") from None
        if response.status_code >= 400 or (isinstance(data, dict) and "error" in data):
            error = data.get("error") if isinstance(data, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            if not isinstance(code, int) or isinstance(code, bool) or not 0 <= code <= 999999999:
                code = None
            if code == WINDOW_CLOSED_CODE:
                raise WindowClosedError("The 24-hour reply window has closed.", code)
            raise WhatsAppError(f"WhatsApp returned an error ({response.status_code}).", code)
        return data
