"""Hand-written fakes shared by several test files."""

import httplib2
from googleapiclient.errors import HttpError


def http_error(status: int) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b"{}")


class _Request:
    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc

    def execute(self):
        if self._exc:
            raise self._exc
        return self._result


class _Events:
    def __init__(self, svc):
        self.svc = svc

    def list(self, **kwargs):
        self.svc.calls.append(("list", kwargs))
        return _Request({"items": self.svc.items}, self.svc.exc)

    def insert(self, **kwargs):
        self.svc.calls.append(("insert", kwargs))
        return _Request({"id": "evt_new", **kwargs["body"]}, self.svc.exc)

    def _find(self, event_id):
        return next((item for item in self.svc.items if item["id"] == event_id), None)

    def _missing(self, event_id):
        # Google answers 410 Gone for an event that was deleted, 404 for an id it never had.
        return http_error(410 if event_id in self.svc.deleted else 404)

    def get(self, **kwargs):
        self.svc.calls.append(("get", kwargs))
        item = self._find(kwargs["eventId"])
        return _Request(item, self.svc.exc or (None if item else self._missing(kwargs["eventId"])))

    def patch(self, **kwargs):
        # Known events keep the fields the patch doesn't change. With no items set up at all,
        # any id is accepted, so simple tests needn't build an event first.
        self.svc.calls.append(("patch", kwargs))
        item = self._find(kwargs["eventId"])
        if item is None and (self.svc.items or kwargs["eventId"] in self.svc.deleted):
            return _Request(None, self.svc.exc or self._missing(kwargs["eventId"]))
        base = item or {"id": kwargs["eventId"], "summary": "Moved event"}
        return _Request({**base, **kwargs["body"]}, self.svc.exc)

    def delete(self, **kwargs):
        self.svc.calls.append(("delete", kwargs))
        item = self._find(kwargs["eventId"])
        if self.svc.exc or item is None:
            return _Request(None, self.svc.exc or self._missing(kwargs["eventId"]))
        self.svc.items.remove(item)
        self.svc.deleted.add(kwargs["eventId"])
        return _Request("")


class _Freebusy:
    def __init__(self, svc):
        self.svc = svc

    def query(self, body):
        self.svc.calls.append(("freebusy", body))
        calendars = {
            item["id"]: self.svc.busy_by_calendar.get(item["id"], {"busy": self.svc.busy})
            for item in body["items"]
        }
        return _Request({"calendars": calendars}, self.svc.exc)


class _CalendarList:
    def __init__(self, svc):
        self.svc = svc

    def list(self, **kwargs):
        self.svc.calls.append(("calendarList", kwargs))
        return _Request({"items": self.svc.calendar_items}, self.svc.exc)


class FakeCalendarService:
    """Mimics service.events().list(...).execute() etc. from googleapiclient."""

    def __init__(self, items=None, busy=None, exc=None, calendar_items=None, busy_by_calendar=None):
        self.items = items or []
        self.busy = busy or []
        self.exc = exc
        self.calendar_items = calendar_items if calendar_items is not None else []
        self.busy_by_calendar = busy_by_calendar or {}
        self.calls = []
        self.deleted: set[str] = set()

    def events(self):
        return _Events(self)

    def freebusy(self):
        return _Freebusy(self)

    def calendarList(self):
        return _CalendarList(self)


class FakeSender:
    """Stands in for WhatsAppClient in router tests."""

    def __init__(self, exc=None, typing_exc=None):
        self.sent: list[tuple[str, str]] = []
        self.templates: list[tuple[str, str, list[str]]] = []
        self.typing: list[str] = []  # message ids marked read with "typing…"
        self.exc = exc
        self.typing_exc = typing_exc

    def mark_read_typing(self, message_id: str) -> None:
        if self.typing_exc:
            raise self.typing_exc
        self.typing.append(message_id)

    def send_text(self, to: str, body: str) -> str:
        if self.exc:
            raise self.exc
        self.sent.append((to, body))
        return f"wamid.out{len(self.sent)}"

    def send_template(self, to: str, name: str, params: list[str], language: str = "en") -> str:
        if self.exc:
            raise self.exc
        self.templates.append((to, name, params))
        return f"wamid.tpl{len(self.templates)}"
