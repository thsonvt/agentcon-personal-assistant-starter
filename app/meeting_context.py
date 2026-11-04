"""Bounded, user-scoped meeting evidence. Source text is untrusted data."""

import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.assistant_tools import AppContext
from app.tools.calendar import Event
from app.tools.gmail import (
    EmailMessage, GmailAuthError, GmailError, GmailPermissionError,
    GmailRateLimitError, GmailServiceDisabledError, GmailTemporaryError,
)


def _title(value: str) -> str:
    value = re.sub(r"^(?:(?:re|fwd?):\s*)+", "", value.strip(), flags=re.I)
    return " ".join(value.casefold().split())


def _source(source_id: str, kind: str, text: str, limit: int, url=None, truncated=False) -> dict:
    result = {"id": source_id, "kind": kind, "text": text[:limit],
              "truncated": truncated or len(text) > limit}
    if url:
        result["url"] = url
    return result


def _calendar_url(value: str | None) -> str | None:
    if not value or len(value) > 2048 or any(not char.isprintable() or char.isspace() for char in value):
        return None
    try:
        url = urlsplit(value)
        if (url.scheme == "https" and not url.username and not url.password and url.port in (None, 443)
                and (url.hostname == "calendar.google.com"
                     or url.hostname == "www.google.com" and url.path.startswith("/calendar/"))):
            return value
    except ValueError:
        pass
    return None


def _gmail_failure(error: GmailError) -> str:
    if isinstance(error, GmailAuthError):
        return "Gmail authorization expired or revoked; reconnect Google with Gmail read permission."
    if isinstance(error, GmailPermissionError):
        return "Gmail read permission denied; reconnect Google with Gmail read permission."
    if isinstance(error, GmailRateLimitError):
        return "Gmail is temporarily rate limited; try again later."
    if isinstance(error, GmailServiceDisabledError):
        return "The Gmail API is disabled; the operator must enable it."
    if isinstance(error, GmailTemporaryError):
        return "Gmail is temporarily unavailable; try again later."
    return "Gmail invitation context is unavailable."


def _google_invitation(subject: str) -> tuple[str, datetime] | None:
    match = re.fullmatch(
        r"Invitation:\s*(?P<title>.+?)\s+@\s+(?P<weekday>Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
        r"(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
        r"(?P<day>\d{1,2}),\s+(?P<year>\d{4})\s+(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<period>am|pm)"
        r"(?:\s*[–-]\s*\d{1,2}(?::\d{2})?\s*(?:am|pm))?\s*"
        r"\(GMT(?P<sign>[+-])(?P<offset_hour>\d{1,2})(?::(?P<offset_minute>\d{2}))?\)(?:\s+.*)?",
        subject.strip()[:1000], re.I,
    )
    if not match:
        return None
    fields = match.groupdict()
    hour, offset_minute = int(fields["hour"]), int(fields["offset_minute"] or 0)
    if not 1 <= hour <= 12 or offset_minute >= 60:
        return None
    offset = timedelta(hours=int(fields["offset_hour"]), minutes=offset_minute)
    if fields["sign"] == "-":
        offset = -offset
    try:
        parsed = datetime(
            int(fields["year"]),
            ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec").index(fields["month"].lower()) + 1,
            int(fields["day"]), hour % 12 + (12 if fields["period"].lower() == "pm" else 0),
            int(fields["minute"] or 0), tzinfo=timezone(offset),
        )
    except ValueError:
        return None
    if ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[parsed.weekday()] != fields["weekday"].lower():
        return None
    return fields["title"], parsed


def _matches(message: EmailMessage, event: Event) -> bool:
    body = message.body[:12000]
    body = re.sub(r"\r?\n[ \t]", "", body)
    if (re.match(r"^(?:cancell?ed|cancellation):", _title(message.subject))
            or re.search(r"^STATUS:CANCELLED\s*$", body, re.M | re.I)):
        return False
    invitation = _google_invitation(message.subject)
    if invitation and _title(invitation[0]) != _title(event.title):
        return False
    summaries = re.findall(r"^SUMMARY:(.*)$", body, re.M)
    titles = {_title(value.rstrip("\r")) for value in summaries}
    if titles and titles != {_title(event.title)}:
        return False
    if not titles and not invitation and _title(message.subject) != _title(event.title):
        return False
    occurrences = [invitation[1]] if invitation else []
    try:
        for params, value in re.findall(r"^DTSTART([^:\r\n]*):([^\r\n]+)", body, re.M):
            if value.endswith("Z"):
                parsed = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            else:
                zone = re.fullmatch(r";TZID=([^;]+)", params)
                if not zone:
                    return False
                parsed = datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=ZoneInfo(zone[1]))
                if parsed.replace(fold=0).utcoffset() != parsed.replace(fold=1).utcoffset():
                    return False
            occurrences.append(parsed)
        if occurrences and set(occurrences) != {event.start}:
            return False
        for value in re.findall(
            r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?", body
        ):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            # Explicit local ISO times use the calendar occurrence's timezone.
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=event.start.tzinfo)
                if parsed.replace(fold=0).utcoffset() != parsed.replace(fold=1).utcoffset():
                    return False
            occurrences.append(parsed)
    except (ValueError, ZoneInfoNotFoundError):
        return False
    # shortcut: only ISO, inline ICS and explicit Google invitation subjects; extend with verified fixtures.
    return event.start in occurrences and set(occurrences) <= {event.start, event.end}


class MeetingContext:
    def __init__(self, context_for_user: Callable[[int], AppContext]):
        self.context_for_user = context_for_user

    def __call__(self, user_id: int, event: Event) -> dict:
        app = self.context_for_user(user_id)
        if app.store.user_id != user_id:
            raise ValueError("Meeting context must use the requesting user's scoped store.")
        calendar_text = (f"Title: {event.title}\nStart: {event.start.isoformat()}\n"
                         f"End: {event.end.isoformat()}\nLocation: {event.location or '(not supplied)'}\n"
                         f"Meeting link: {event.meet_link or '(not supplied)'}\n"
                         f"Description: {event.description or '(not supplied)'}")
        sources = [_source(f"calendar:{event.id}", "calendar", calendar_text, 4000, _calendar_url(event.source_url))]
        limitations = ["Calendar does not provide attendee identity; email matches establish title and occurrence only."]
        facts = app.store.list_facts()
        sources.extend(_source(f"fact:{fact.id}", "fact", fact.fact, 1000) for fact in facts[:20])
        if len(facts) > 20:
            limitations.append("Saved facts are limited to the first 20.")
        if app.gmail is None:
            limitations.append("Gmail is not connected; invitation context is unavailable.")
        else:
            # Quoted alphanumeric title terms cannot add Gmail operators or expand inbox scope.
            terms = " ".join(re.sub(r"[^\w\s-]", "", event.title[:200]).split())
            if not terms:
                limitations.append("The event title cannot form a bounded Gmail search.")
            else:
                now = app.now()
                after, before = now.date() - timedelta(days=59), now.date() + timedelta(days=1)
                query = f'subject:"{terms}" after:{after:%Y/%m/%d} before:{before:%Y/%m/%d}'
                try:
                    messages = app.gmail.search_messages(query, 3)
                except GmailError as exc:
                    limitations.append(_gmail_failure(exc))
                else:
                    for message in messages[:3]:
                        if not _matches(message, event):
                            limitations.append(f"Email {message.id} did not unambiguously match this title and occurrence.")
                            continue
                        known_url = "https://mail.google.com/mail/u/0/#all/" + quote(message.id, safe="")
                        url = message.source_url if message.source_url == known_url else None
                        text = f"Subject: {message.subject}\nFrom: {message.sender}\nDate: {message.date}\n{message.body}"
                        sources.append(_source(f"email:{message.id}", "email", text, 4000, url, message.truncated))
                    limitations.append("Gmail search covers 60 days and at most 3 messages; matching supports ISO date/time, inline ICS and English Google Invitation subjects with explicit GMT offsets only.")
        if not any(source["kind"] == "email" for source in sources):
            limitations.append("No unambiguous invitation matched; meeting purpose is unconfirmed unless Calendar/saved facts establish it.")
        if any(source["truncated"] for source in sources):
            limitations.append("Some source text is truncated; omitted content is unknown.")
        return {"sources": sources, "limitations": limitations}
