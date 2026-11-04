"""Calendly: read-only lookup of the owner's booking link."""

import httpx


class CalendlyError(Exception):
    """Any failure talking to Calendly."""


class CalendlyClient:
    def __init__(self, token: str, event_type_uri: str, http: httpx.Client | None = None):
        self.token = token
        self.event_type_uri = event_type_uri
        self.http = http or httpx.Client(timeout=10)
        self._cached_url: str | None = None

    def scheduling_url(self) -> str:
        if self._cached_url:
            return self._cached_url
        try:
            response = self.http.get(
                self.event_type_uri, headers={"Authorization": f"Bearer {self.token}"}
            )
        except httpx.HTTPError as exc:
            raise CalendlyError("Could not reach Calendly.") from exc
        if response.status_code != 200:
            raise CalendlyError(f"Calendly returned an error ({response.status_code}).")
        self._cached_url = response.json()["resource"]["scheduling_url"]
        return self._cached_url


class BookingLink:
    """For users without Calendly API access: the booking link they pasted at sign-up, if any."""

    def __init__(self, url: str | None):
        self.url = url

    def scheduling_url(self) -> str:
        if not self.url:
            raise CalendlyError("No booking link has been set. It can be added by signing up again.")
        return self.url
