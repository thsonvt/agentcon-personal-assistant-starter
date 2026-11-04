"""Server-verified waitlist signup. Provider credentials never reach the browser."""

from dataclasses import dataclass, field
import hashlib
import hmac
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
import unicodedata
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect
import httpx

from app.email_eligibility import UNSUPPORTED_DOMAINS, known_unsupported, check_google_hosting

SUCCESS_MESSAGE = "Thanks for joining Pippa! Your signup has been saved."
ERROR_MESSAGE = "We couldn’t process your signup. Please try again later."
EMAIL_MESSAGES = {
    "invalid_survey": "Please check your answers and try again.",
    "invalid_email": "Please enter a complete email address, such as you@gmail.com.",
    "unsupported_email_provider": "Pippa currently supports personal Gmail and Google Workspace work email only. Please use a supported address.",
    "workspace_unverified": "We couldn't confirm Google Workspace for this domain. Use a personal Gmail address, or contact us if your company uses Google Workspace.",
    "email_check_unavailable": "We couldn't check your work email right now. Please try again shortly.",
}
SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
LOOPS = "https://app.loops.so/api/v1/contacts"
SURVEY_FIELDS = {"surveyVersion", "firstName", "schedulingTools", "schedulingToolsOther", "schedulingChallenge", "pippaWish"}
FORM_FIELDS = {"email", "cf-turnstile-response", "website"} | SURVEY_FIELDS
SCHEDULING_TOOLS = {
    "google_calendar": "Google Calendar", "outlook": "Outlook",
    "apple_calendar": "Apple Calendar", "calendly": "Calendly",
    "cal_com": "Cal.com", "whatsapp": "WhatsApp", "manual": "Manual", "other": "Other",
}


class _LoopsLookupLogFilter(logging.Filter):
    def filter(self, record):
        # httpx INFO includes the entire request URL, including the email query.
        # Suppress only this provider lookup; retain all other HTTP request logs.
        return LOOPS + "/find" not in record.getMessage()


_httpx_logger = logging.getLogger("httpx")
if not any(item.name == "pippa.waitlist.contact-lookup" for item in _httpx_logger.filters):
    _httpx_logger.addFilter(_LoopsLookupLogFilter("pippa.waitlist.contact-lookup"))


@dataclass(frozen=True)
class WaitlistConfig:
    enabled: bool = False
    site_key: str = ""
    secret_key: str = field(default="", repr=False)
    loops_api_key: str = field(default="", repr=False)
    list_id: str = ""
    origin: str = "https://example.com"
    state_path: str = "agents.db.waitlist.sqlite3"
    rate_secret: str = field(default="", repr=False)
    trusted_proxy_cidrs: tuple[str, ...] = ()
    client_ip_source: str = "socket"
    beta_allowed_emails: tuple[str, ...] = ()

    @classmethod
    def from_env(cls):
        return cls(
            enabled=os.getenv("WAITLIST_ENABLED", "false").lower() == "true",
            site_key=os.getenv("TURNSTILE_SITE_KEY", ""),
            secret_key=os.getenv("TURNSTILE_SECRET_KEY", ""),
            loops_api_key=os.getenv("LOOPS_API_KEY", ""),
            list_id=os.getenv("LOOPS_WAITLIST_LIST_ID", ""),
            origin=os.getenv("WAITLIST_ORIGIN", "https://example.com").rstrip("/"),
            state_path=os.getenv("WAITLIST_STATE_PATH", os.getenv("DB_PATH", "agents.db") + ".waitlist.sqlite3"),
            rate_secret=os.getenv("WAITLIST_RATE_SECRET") or os.getenv("SESSION_SECRET", ""),
            trusted_proxy_cidrs=tuple(value.strip() for value in os.getenv("WAITLIST_TRUSTED_PROXY_CIDRS", "").split(",") if value.strip()),
            client_ip_source=os.getenv("WAITLIST_CLIENT_IP_SOURCE", "socket"),
            beta_allowed_emails=tuple(sorted({value.strip().lower() for value in os.getenv("BETA_ALLOWED_EMAILS", "").split(",") if value.strip()})),
        )

    @property
    def ready(self):
        try:
            parsed = urlsplit(self.origin)
            for cidr in self.trusted_proxy_cidrs:
                ipaddress.ip_network(cidr)
        except ValueError:
            return False
        return bool(self.enabled and self.site_key and self.secret_key and self.loops_api_key
                    and self.list_id and self.rate_secret and self.state_path
                    and self.client_ip_source in {"socket", "railway"}
                    and parsed.scheme == "https" and parsed.hostname
                    and not parsed.username and not parsed.password and not parsed.path
                    and not parsed.query and not parsed.fragment)


def _client_ip(request: Request, config: WaitlistConfig):
    """Use the configured edge contract; default to the socket peer.

    request.client must still be the socket peer: disable ASGI proxy-header
    rewriting, or its earlier trust decision defeats this boundary. Railway mode
    requires exclusive ingress through Railway's edge and verified X-Real-IP
    replacement. Socket mode trusts XFF only behind explicitly configured CIDRs.
    """
    peer = request.client.host if request.client else "unknown"
    if config.client_ip_source == "railway":
        addresses = request.headers.getlist("x-real-ip")
        if len(addresses) != 1:
            return peer
        address = addresses[0]
        if not address or len(address) > 45 or address != address.strip() or "%" in address or "," in address:
            return peer
        try:
            return str(ipaddress.ip_address(address))
        except ValueError:
            return peer
    networks = [ipaddress.ip_network(cidr) for cidr in config.trusted_proxy_cidrs]
    try:
        current = ipaddress.ip_address(peer)
        if not any(current in network for network in networks):
            return str(current)
        forwarded = request.headers.get("x-forwarded-for", "")
        if not forwarded or len(forwarded) > 2048:
            return str(current)
        chain = [ipaddress.ip_address(value.strip()) for value in forwarded.split(",")]
        for address in reversed(chain):
            if not any(current in network for network in networks):
                break
            current = address
        return str(current)
    except ValueError:
        return peer


class _Limiter:
    def __init__(self, config):
        self.config = config

    def _key(self, value):
        return hmac.new(self.config.rate_secret.encode(), value.encode(), hashlib.sha256).hexdigest()

    def consume(self, scopes, now):
        """One transaction checks every budget before adding any hits."""
        Path(self.config.state_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.config.state_path, timeout=2) as db:
            db.execute("CREATE TABLE IF NOT EXISTS waitlist_hits (bucket TEXT, key TEXT, at REAL)")
            db.execute("CREATE INDEX IF NOT EXISTS waitlist_hits_lookup ON waitlist_hits(bucket, key, at)")
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM waitlist_hits WHERE at <= ?", (now - 3600,))
            for bucket, value, windows in scopes:
                key = self._key(bucket + ":" + value)
                for seconds, maximum in windows:
                    count = db.execute("SELECT COUNT(*) FROM waitlist_hits WHERE bucket = ? AND key = ? AND at > ?", (bucket, key, now - seconds)).fetchone()[0]
                    if count >= maximum:
                        return False
            for bucket, value, _ in scopes:
                db.execute("INSERT INTO waitlist_hits VALUES (?, ?, ?)", (bucket, self._key(bucket + ":" + value), now))
            return True


def _email(value):
    if not value or len(value) > 254 or not value.isascii() or value.count("@") != 1:
        return None
    local, domain = value.rsplit("@", 1)
    if not local or len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local:
        return None
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local):
        return None
    labels = domain.split(".")
    if len(labels) < 2 or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels):
        return None
    return local + "@" + domain.lower()


def _survey_profile(fields):
    """Validate the versioned survey before any provider request.

    An absent survey is a supported legacy email-only request. Only this
    explicit mapping may become contact properties; operational fields never
    come from the public form.
    """
    if not SURVEY_FIELDS.intersection(fields):
        return {}
    if fields.get("surveyVersion") != ["1"]:
        raise ValueError("Invalid survey version")
    raw_name = fields.get("firstName", [""])[0]
    name = raw_name.strip()
    if not 1 <= len(name) <= 80 or any(unicodedata.category(char) in {"Cc", "Cs", "Zl", "Zp"} for char in raw_name):
        raise ValueError("Invalid first name")
    tools = json.loads(fields.get("schedulingTools", ["[]"])[0])
    if (not isinstance(tools, list) or len(tools) > 8
            or any(not isinstance(tool, str) or tool not in SCHEDULING_TOOLS for tool in tools)
            or len(set(tools)) != len(tools)):
        raise ValueError("Invalid scheduling tools")
    profile = {"firstName": name, "pippaSurveyVersion": "1"}
    if tools:
        profile["pippaSchedulingTools"] = ", ".join(SCHEDULING_TOOLS[tool] for tool in tools)
    for source, target, maximum in (
        ("schedulingToolsOther", "pippaSchedulingToolsOther", 120),
        ("schedulingChallenge", "pippaSchedulingChallenge", 600),
        ("pippaWish", "pippaWish", 600),
    ):
        value = fields.get(source, [""])[0].strip()
        if len(value) > maximum or (source == "schedulingToolsOther" and value and "other" not in tools):
            raise ValueError("Invalid survey answer")
        if value:
            profile[target] = value
    return profile


async def _enroll(client, config, email, profile=None):
    headers = {"Authorization": "Bearer " + config.loops_api_key}
    found = await client.get(LOOPS + "/find", params={"email": email}, headers=headers)
    found.raise_for_status()
    contacts = found.json()
    if not isinstance(contacts, list) or len(contacts) > 1:
        return False
    if contacts:
        contact = contacts[0]
        if not isinstance(contact, dict) or not isinstance(contact.get("mailingLists"), dict):
            return False
        membership = contact.get("mailingLists", {}).get(config.list_id)
        # An existing unsubscribe is a decision, never an invitation to resubscribe.
        if contact.get("subscribed") is False or membership is False or membership is True:
            return True
        if contact.get("subscribed") is not True:
            return False
        response = await client.put(LOOPS + "/update", headers=headers, json={"email": email, "mailingLists": {config.list_id: True}})
    else:
        # This is a routing hint for new-contact welcome emails, never a grant of
        # access. Existing contact properties and subscriptions remain untouched.
        response = await client.post(LOOPS + "/create", headers=headers, json={
            "email": email, "source": "Pippa waitlist", "subscribed": True,
            "mailingLists": {config.list_id: True},
            "pippaSignInEligible": email.lower() in config.beta_allowed_emails,
            **(profile or {}),
        })
        if response.status_code == 409:
            # Another request may have created the contact after our first read.
            # Reconcile once without repeating a write or overriding an opt-out.
            refreshed = await client.get(LOOPS + "/find", params={"email": email}, headers=headers)
            refreshed.raise_for_status()
            contacts = refreshed.json()
            if not isinstance(contacts, list) or len(contacts) != 1:
                return False
            contact = contacts[0]
            if not isinstance(contact, dict) or not isinstance(contact.get("mailingLists"), dict):
                return False
            membership = contact["mailingLists"].get(config.list_id)
            return contact.get("subscribed") is False or membership is False or (contact.get("subscribed") is True and membership is True)
    response.raise_for_status()
    result = response.json()
    return isinstance(result, dict) and result.get("success") is True


def waitlist_routes(config=None, *, transport=None, now=time.time):
    config = config if config is not None else WaitlistConfig.from_env()
    router = APIRouter()
    limiter = _Limiter(config)

    def error(status=503, code=None):
        headers = {"Cache-Control": "no-store"}
        if status == 429:
            headers["Retry-After"] = "600"
        payload = {"success": False, "message": EMAIL_MESSAGES.get(code, ERROR_MESSAGE)}
        if code:
            payload["code"] = code
        return JSONResponse(payload, status_code=status, headers=headers)

    @router.get("/api/waitlist/config")
    def configuration():
        public = {"enabled": config.ready}
        if config.ready:
            public["siteKey"] = config.site_key
            public["unsupportedDomains"] = sorted(UNSUPPORTED_DOMAINS)
        return JSONResponse(public, headers={"Cache-Control": "no-store"})

    @router.post("/api/waitlist")
    async def signup(request: Request):
        if not config.ready:
            return error()
        if request.headers.get("origin") != config.origin:
            return error(403)
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
            return error(400)
        peer = _client_ip(request, config)
        try:
            if not limiter.consume((("ip", peer, ((600, 20), (3600, 60))), ("global", "all", ((3600, 200),))), now()):
                return error(429)
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > 24 * 1024:
                    return error(400)
                body.extend(chunk)
            try:
                fields = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True, errors="strict", max_num_fields=len(FORM_FIELDS))
            except (ValueError, UnicodeError):
                return error(400)
            if set(fields) - FORM_FIELDS or any(len(values) != 1 for values in fields.values()):
                return error(400)
            try:
                profile = _survey_profile(fields)
            except (ValueError, RecursionError):
                return error(400, "invalid_survey")
            email = _email(fields.get("email", [""])[0].strip())
            token = fields.get("cf-turnstile-response", [""])[0]
            if not email:
                return error(400, "invalid_email")
            domain = email.rsplit("@", 1)[1]
            if known_unsupported(domain):
                return error(422, "unsupported_email_provider")
            if not 1 <= len(token) <= 2048 or fields.get("website", [""])[0]:
                return error(400)
            async with httpx.AsyncClient(transport=transport, timeout=10, follow_redirects=False) as client:
                verified = await client.post(SITEVERIFY, data={"secret": config.secret_key, "response": token})
                verified.raise_for_status()
                result = verified.json()
                if not isinstance(result, dict) or result.get("success") is not True or result.get("hostname") != urlsplit(config.origin).hostname or result.get("action") != "waitlist":
                    return error(403)
                hosting = await check_google_hosting(client, domain)
                if hosting == "unavailable":
                    return error(503, "email_check_unavailable")
                if hosting != "supported":
                    return error(422, "workspace_unverified")
                # A failed challenge must not exhaust somebody else's email budget.
                if not limiter.consume((("email", email.casefold(), ((3600, 3),)),), now()):
                    return error(429)
                if not await _enroll(client, config, email, profile):
                    return error()
            return JSONResponse({"success": True, "message": SUCCESS_MESSAGE}, headers={"Cache-Control": "no-store"})
        except (httpx.HTTPError, ValueError, sqlite3.Error, OSError, ClientDisconnect):
            # Do not log provider response bodies, emails, addresses, or credentials.
            return error()

    return router
