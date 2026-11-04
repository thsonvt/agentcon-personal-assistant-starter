"""Conservative Google mail-hosting checks for the early-access waitlist.

MX records indicate a domain's mail route, not ownership of an individual
mailbox or permission to use Calendar. Google OAuth remains authoritative for
account access. Domains behind mail gateways or CNAMEs need manual review.
"""

import re
from typing import Literal

import httpx


PERSONAL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})
UNSUPPORTED_DOMAINS = frozenset({
    "yahoo.com", "yahoo.co.uk", "yahoo.com.sg", "yahoo.com.vn",
    "yahoo.com.au", "yahoo.co.in", "yahoo.in", "yahoo.co.jp", "yahoo.ca",
    "yahoo.fr", "yahoo.de", "yahoo.it", "yahoo.es", "yahoo.com.br",
    "yahoo.com.hk", "yahoo.com.tw", "yahoo.co.id", "yahoo.com.my",
    "yahoo.com.ph", "yahoo.co.nz", "ymail.com", "rocketmail.com",
    "aol.com", "aim.com", "icloud.com", "me.com", "mac.com",
    "outlook.com", "outlook.co.uk", "hotmail.com", "hotmail.co.uk",
    "hotmail.fr", "hotmail.de", "live.com", "live.co.uk", "live.com.au",
    "msn.com", "proton.me", "protonmail.com", "pm.me", "tuta.com",
    "tutanota.com", "tutanota.de", "gmx.com", "gmx.net", "gmx.de",
    "mail.com", "fastmail.com", "hey.com", "zoho.com",
})
GOOGLE_MX = frozenset({
    "smtp.google.com", "aspmx.l.google.com",
    *(f"alt{i}.aspmx.l.google.com" for i in range(1, 5)),
    *(f"aspmx{i}.googlemail.com" for i in range(2, 6)),
})
Eligibility = Literal["supported", "unsupported", "unverified", "unavailable"]
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)


def _domain(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.lower().removesuffix(".")
    labels = name.split(".")
    if (len(name) > 253 or len(labels) < 2 or labels[-1].isdigit()
            or any(not _LABEL.fullmatch(label) for label in labels)):
        return None
    return name


def known_unsupported(domain: str) -> bool:
    """Match explicit provider domains and their subdomains at label boundaries."""
    name = _domain(domain)
    return name is not None and any(
        name == provider or name.endswith("." + provider)
        for provider in UNSUPPORTED_DOMAINS
    )


def _from_dns(payload: object, domain: str) -> Eligibility:
    if not isinstance(payload, dict):
        return "unavailable"
    status = payload.get("Status")
    questions = payload.get("Question")
    if (type(status) is not int or payload.get("TC") is not False
            or not isinstance(questions, list) or len(questions) != 1):
        return "unavailable"
    question = questions[0]
    if (not isinstance(question, dict) or question.get("type") != 15
            or _domain(question.get("name")) != domain):
        return "unavailable"
    if status not in (0, 3):
        return "unavailable"
    answers = payload.get("Answer", [])
    if not isinstance(answers, list) or len(answers) > 100:
        return "unavailable"
    mx = []
    has_cname = False
    for record in answers:
        if (not isinstance(record, dict) or type(record.get("type")) is not int
                or not _domain(record.get("name"))
                or not isinstance(record.get("data"), str)):
            return "unavailable"
        if "TTL" in record and (type(record["TTL"]) is not int or record["TTL"] < 0):
            return "unavailable"
        # CNAMEs and records for other names cannot prove this domain's route.
        if record["type"] == 5:
            if not _domain(record["data"]):
                return "unavailable"
            has_cname = True
        if record["type"] != 15:
            continue
        fields = record["data"].split()
        if (len(fields) != 2 or not re.fullmatch(r"[0-9]{1,5}", fields[0])
                or int(fields[0]) > 65535):
            return "unavailable"
        priority, target = int(fields[0]), _domain(fields[1])
        if target is None and not (priority == 0 and fields[1] == "."):
            return "unavailable"
        if _domain(record["name"]) == domain:
            mx.append((priority, target))
    if status == 3 or has_cname or not mx or any(target is None for _, target in mx):
        return "unverified"
    priority = min(preference for preference, _ in mx)
    primary_targets = {target for preference, target in mx if preference == priority}
    return "supported" if primary_targets <= GOOGLE_MX else "unverified"


async def check_google_hosting(client: httpx.AsyncClient, domain: str) -> Eligibility:
    """Check only the domain through a fixed resolver; never send an email address.

    Unknown routes are unverified; transport/schema failures are unavailable.
    Neither result permits enrollment. No redirects or user-selected hosts are
    contacted, and resolver requests disclose no client subnet to DNS servers.
    """
    name = _domain(domain)
    if name is None:
        return "unverified"
    if name in PERSONAL_DOMAINS:
        return "supported"
    if known_unsupported(name):
        return "unsupported"
    try:
        response = await client.get(
            "https://dns.google/resolve",
            params={"name": name, "type": "MX", "edns_client_subnet": "0.0.0.0/0"},
            headers={"accept": "application/dns-json"},
            timeout=3.0,
            follow_redirects=False,
        )
        if response.status_code != 200 or len(response.content) > 65536:
            return "unavailable"
        return _from_dns(response.json(), name)
    except (httpx.HTTPError, ValueError, UnicodeError):
        return "unavailable"
