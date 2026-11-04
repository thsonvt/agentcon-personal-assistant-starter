"""Google Calendar wrapper. Returns plain dataclasses; raises CalendarError on failure."""

import threading
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo
from uuid import uuid4

from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from app.timeutil import SGT

SCOPES = ["https://www.googleapis.com/auth/calendar"]


class CalendarError(Exception):
    """Any failure talking to Google Calendar."""


class CalendarAuthError(CalendarError):
    """The refresh token was rejected; the owner must re-link Google."""


class EventNotFoundError(CalendarError):
    """The event id doesn't exist, or the event was already deleted."""


NOT_FOUND = "That event wasn't found; it may have been deleted already."


def _live(item: dict) -> dict:
    """Google may answer for a deleted event with status "cancelled", sometimes with only its id."""
    if item.get("status") == "cancelled" or "start" not in item:
        raise EventNotFoundError(NOT_FOUND)
    return item


@dataclass(frozen=True)
class Event:
    id: str
    title: str
    start: datetime
    end: datetime
    all_day: bool
    location: str | None = None
    meet_link: str | None = None
    description: str | None = None
    source_url: str | None = None


def build_service(client_id: str, client_secret: str, refresh_token: str):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _aware_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("Google returned a time without an offset")
    return dt


def _parse_when(when: dict, tz: ZoneInfo) -> tuple[datetime, bool]:
    if "dateTime" in when:
        dt = datetime.fromisoformat(when["dateTime"].replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # Google also allows a local dateTime with an explicit IANA timeZone.
            zone = ZoneInfo(when["timeZone"])
            first, second = dt.replace(tzinfo=zone, fold=0), dt.replace(tzinfo=zone, fold=1)
            if first.utcoffset() != second.utcoffset():
                raise ValueError("Ambiguous or nonexistent Google event time; an offset is required")
            dt = first
        return dt.astimezone(tz), False
    return datetime.combine(date.fromisoformat(when["date"]), time(0), tzinfo=tz), True


def _to_event(item: dict, tz: ZoneInfo) -> Event:
    start, all_day = _parse_when(item["start"], tz)
    end, _ = _parse_when(item["end"], tz)
    return Event(
        id=item["id"],
        title=item.get("summary") or "(no title)",
        start=start,
        end=end,
        all_day=all_day,
        location=item.get("location"),
        meet_link=item.get("hangoutLink") or next((
            point.get("uri") for point in (item.get("conferenceData") or {}).get("entryPoints", [])
            if point.get("entryPointType") == "video"
        ), None),
        description=item.get("description"),
        source_url=item.get("htmlLink"),
    )


class GoogleCalendar:
    def __init__(self, service, calendar_id: str = "primary", tz: ZoneInfo = SGT, before_request=None,
                 check_selected_calendars: bool = False):
        self.service = service
        self.calendar_id = calendar_id
        self.tz = tz
        self.before_request = before_request
        self.check_selected_calendars = check_selected_calendars
        # The SDK runs tools in parallel threads; googleapiclient's HTTP object is not thread-safe.
        self._lock = threading.Lock()

    def _execute(self, request) -> dict:
        try:
            with self._lock:
                if self.before_request is not None:
                    self.before_request()
                return request.execute()
        except RefreshError as exc:
            raise CalendarAuthError("Google Calendar access has expired.") from exc
        except HttpError as exc:
            if exc.resp.status in (404, 410):  # 410 Gone = already deleted
                raise EventNotFoundError(NOT_FOUND) from exc
            raise CalendarError(f"Google Calendar returned an error ({exc.resp.status}).") from exc
        except OSError as exc:
            raise CalendarError("Could not reach Google Calendar.") from exc

    def list_events(self, start: datetime, end: datetime, limit: int = 10) -> list[Event]:
        if limit <= 0:
            return []
        params = dict(calendarId=self.calendar_id, timeMin=start.isoformat(), timeMax=end.isoformat(),
                      singleEvents=True, orderBy="startTime", showDeleted=False, maxResults=min(limit, 2500))
        events: list[Event] = []
        seen_pages: set[str] = set()
        while True:
            result = self._execute(self.service.events().list(**params))
            for item in result.get("items", []):
                # Declining an invitation does not cancel it for other attendees.
                # Google can keep that event "confirmed" on this calendar.
                if item.get("status") == "cancelled" or any(
                    attendee.get("self") is True and attendee.get("responseStatus") == "declined"
                    for attendee in item.get("attendees", [])
                ):
                    continue
                events.append(_to_event(item, self.tz))
                if len(events) >= limit:
                    return events
            next_page = result.get("nextPageToken")
            if not next_page:
                return events
            if next_page in seen_pages:
                raise CalendarError("Google Calendar repeated a results page. Please try again.")
            seen_pages.add(next_page)
            params["pageToken"] = next_page

    def _availability_calendar_ids(self) -> list[str]:
        if not self.check_selected_calendars:
            return [self.calendar_id]
        calendar_ids = list(dict.fromkeys([self.calendar_id, "primary"]))
        params = dict(showHidden=False, showDeleted=False, maxResults=250)
        seen_pages = set()
        while True:
            result = self._execute(self.service.calendarList().list(**params))
            try:
                items = result["items"]
                if not isinstance(items, list):
                    raise ValueError("Missing calendars")
                for item in items:
                    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                        raise ValueError("Missing calendar identity")
                    if any(key in item and not isinstance(item[key], bool)
                           for key in ("selected", "hidden", "deleted", "primary")):
                        raise ValueError("Invalid calendar flags")
                    if (item.get("selected") is True and not item.get("hidden") and
                            not item.get("deleted") and not item.get("primary") and item["id"] not in calendar_ids):
                        calendar_ids.append(item["id"])
                next_page = result.get("nextPageToken")
                if next_page is not None and (not isinstance(next_page, str) or not next_page):
                    raise ValueError("Invalid page token")
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise CalendarError("Google Calendar returned incomplete calendar information. Nothing was changed.") from exc
            if not next_page:
                return calendar_ids
            if next_page in seen_pages:
                raise CalendarError("Google Calendar repeated a results page. Nothing was changed.")
            seen_pages.add(next_page)
            params["pageToken"] = next_page

    def busy_periods(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        calendar_ids = self._availability_calendar_ids()
        periods = []
        # Google accepts at most 50 calendars in one free/busy query.
        for offset in range(0, len(calendar_ids), 50):
            batch = calendar_ids[offset:offset + 50]
            result = self._execute(self.service.freebusy().query(body={
                "timeMin": start.isoformat(), "timeMax": end.isoformat(), "timeZone": str(self.tz),
                "items": [{"id": calendar_id} for calendar_id in batch],
            }))
            try:
                for calendar_id in batch:
                    calendar = result["calendars"][calendar_id]
                    if not isinstance(calendar, dict):
                        raise ValueError("Missing calendar")
                    errors = calendar.get("errors", [])
                    if not isinstance(errors, list):
                        raise ValueError("Invalid provider errors")
                    if errors:
                        # Imported feeds can expose events while refusing free/busy.
                        if (self.check_selected_calendars and
                                all(isinstance(error, dict) and error.get("reason") in ("notFound", "forbidden")
                                    for error in errors)):
                            periods.extend(self._event_busy_periods(start, end, calendar_id))
                            continue
                        raise ValueError("Provider error")
                    raw_busy = calendar["busy"]
                    if not isinstance(raw_busy, list):
                        raise ValueError("Missing busy intervals")
                    for interval in raw_busy:
                        left = _aware_datetime(interval["start"]).astimezone(self.tz)
                        right = _aware_datetime(interval["end"]).astimezone(self.tz)
                        if right <= left:
                            raise ValueError("Invalid busy interval")
                        periods.append((left, right))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise CalendarError("Google Calendar returned incomplete free/busy information. Nothing was changed.") from exc
        return periods

    def busy_periods_excluding(
        self, start: datetime, end: datetime, event_id: str
    ) -> list[tuple[datetime, datetime]]:
        """Read every overlapping event: subtracting merged free/busy loses other conflicts.

        Expand recurring instances, filter cancelled/transparent/self-declined
        events and exclude only the exact instance being moved. No result limit.
        """
        return self._event_busy_periods(start, end, self.calendar_id, event_id)

    def _event_busy_periods(
        self, start: datetime, end: datetime, calendar_id: str, event_id: str | None = None,
    ) -> list[tuple[datetime, datetime]]:
        params = dict(calendarId=calendar_id, timeMin=start.isoformat(), timeMax=end.isoformat(),
                      singleEvents=True, showDeleted=False, maxResults=2500)
        periods = []
        seen_pages = set()
        while True:
            result = self._execute(self.service.events().list(**params))
            try:
                items = result["items"]
                if not isinstance(items, list):
                    raise ValueError("Missing events")
                calendar_tz = ZoneInfo(result.get("timeZone", str(self.tz)))
                for item in items:
                    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                        raise ValueError("Missing event identity")
                    if item["id"] == event_id or item.get("status") == "cancelled":
                        continue
                    attendees = item.get("attendees", [])
                    if not isinstance(attendees, list) or not all(isinstance(a, dict) for a in attendees):
                        raise ValueError("Invalid attendees")
                    if item.get("transparency") == "transparent" or any(
                        a.get("self") is True and a.get("responseStatus") == "declined" for a in attendees
                    ):
                        continue
                    if event_id is None and "date" in item["start"] and "timeZone" not in result:
                        raise ValueError("Missing calendar timezone for all-day event")
                    left, _ = _parse_when(item["start"], calendar_tz)
                    right, _ = _parse_when(item["end"], calendar_tz)
                    if right <= left:
                        raise ValueError("Invalid event interval")
                    if left < end and right > start:
                        periods.append((left.astimezone(self.tz), right.astimezone(self.tz)))
                next_page = result.get("nextPageToken")
                if next_page is not None and (not isinstance(next_page, str) or not next_page):
                    raise ValueError("Invalid page token")
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise CalendarError("Google Calendar returned incomplete availability. Nothing was changed.") from exc
            if not next_page:
                return periods
            if next_page in seen_pages:
                raise CalendarError("Google Calendar repeated a results page. Nothing was changed.")
            seen_pages.add(next_page)
            params["pageToken"] = next_page

    def _written_event(self, item) -> Event:
        if isinstance(item, dict) and item.get("status") == "cancelled":
            raise EventNotFoundError(NOT_FOUND)
        try:
            event = _to_event(item, self.tz)
            if not isinstance(event.id, str) or not event.id or event.end <= event.start:
                raise ValueError("Incomplete written event")
            return event
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise CalendarError(
                "Google Calendar did not confirm the write result. It may have succeeded; "
                "check the calendar before retrying."
            ) from exc

    def create_event(
        self, title: str, start: datetime, end: datetime, attendee_email: str | None = None,
        location: str | None = None, event_id: str | None = None,
        attendee_emails: list[str] | None = None,
    ) -> Event:
        body: dict = {
            "summary": title,
            "start": {"dateTime": start.isoformat(), "timeZone": str(self.tz)},
            "end": {"dateTime": end.isoformat(), "timeZone": str(self.tz)},
        }
        if event_id is not None:
            body["id"] = event_id
        if location:
            body["location"] = location
        emails = list(dict.fromkeys(([attendee_email] if attendee_email else []) + (attendee_emails or [])))
        if emails:
            body["attendees"] = [{"email": email} for email in emails]
            if not location or not location.strip():
                body["conferenceData"] = {"createRequest": {
                    "requestId": uuid4().hex,
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }}
        created = self._execute(
            self.service.events().insert(
                calendarId=self.calendar_id,
                body=body,
                sendUpdates="all" if emails else "none",
                conferenceDataVersion=1,
            )
        )
        return self._written_event(created)

    def get_event(self, event_id: str) -> Event:
        item = self._execute(self.service.events().get(calendarId=self.calendar_id, eventId=event_id))
        try:
            return _to_event(_live(item), self.tz)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise CalendarError("Google Calendar returned an incomplete event. Nothing was changed.") from exc

    def update_event(
        self, event_id: str, title: str | None = None, start: datetime | None = None, end: datetime | None = None
    ) -> Event:
        """Change only the fields given. Guests get an update email."""
        body: dict = {}
        if title is not None:
            body["summary"] = title
        if start is not None:
            body["start"] = {"dateTime": start.isoformat(), "timeZone": str(self.tz)}
        if end is not None:
            body["end"] = {"dateTime": end.isoformat(), "timeZone": str(self.tz)}
        updated = self._execute(
            self.service.events().patch(
                calendarId=self.calendar_id, eventId=event_id, body=body, sendUpdates="all", conferenceDataVersion=1
            )
        )
        return self._written_event(updated)

    def delete_event(self, event_id: str) -> None:
        """Delete the event. Guests get a cancellation email."""
        self._execute(self.service.events().delete(calendarId=self.calendar_id, eventId=event_id, sendUpdates="all"))
