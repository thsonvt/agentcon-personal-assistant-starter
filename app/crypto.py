"""Encrypt stored Google tokens, and sign short-lived browser cookies.

Tokens: Fernet (AES + HMAC) with TOKEN_KEY, so a copy of the database alone can't open anyone's calendar.
Cookies: "<value>|<unix time>|<hmac>", checked in constant time and rejected after max_age.
"""

import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken


def generate_key() -> str:
    return Fernet.generate_key().decode()


def valid_key(key: str) -> bool:
    try:
        Fernet(key.encode())
        return True
    except (ValueError, TypeError):
        return False


class TokenBox:
    def __init__(self, key: str):
        self._fernet = Fernet(key.encode())

    def encrypt(self, secret: str) -> str:
        return self._fernet.encrypt(secret.encode()).decode()

    def decrypt(self, sealed: str) -> str:
        try:
            return self._fernet.decrypt(sealed.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("Stored token can't be decrypted with this TOKEN_KEY.") from exc


def _mac(message: str, secret: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def sign(value: str, secret: str, now: datetime) -> str:
    message = f"{value}|{int(now.timestamp())}"
    return f"{message}|{_mac(message, secret)}"


def unsign(token: str, secret: str, now: datetime, max_age: timedelta) -> str | None:
    """The signed value, or None if the token is forged, altered or too old."""
    try:
        value, stamp, mac = token.rsplit("|", 2)
        issued = datetime.fromtimestamp(int(stamp), tz=timezone.utc)
    except ValueError:
        return None
    if not hmac.compare_digest(mac, _mac(f"{value}|{stamp}", secret)):
        return None
    if now - issued > max_age or issued - now > timedelta(minutes=1):
        return None
    return value
