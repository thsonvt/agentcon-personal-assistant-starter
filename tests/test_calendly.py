import httpx
import pytest

from app.tools.calendly import CalendlyClient, CalendlyError

URI = "https://api.calendly.com/event_types/ABC"


def client_with(handler):
    return CalendlyClient("tok", URI, http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_returns_scheduling_url_and_caches_it():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer tok"
        assert str(request.url) == URI
        return httpx.Response(200, json={"resource": {"scheduling_url": "https://calendly.com/Pippa/30min"}})

    client = client_with(handler)
    assert client.scheduling_url() == "https://calendly.com/Pippa/30min"
    assert client.scheduling_url() == "https://calendly.com/Pippa/30min"
    assert len(calls) == 1


def test_error_status_raises_readable_error():
    client = client_with(lambda request: httpx.Response(401, json={"message": "Unauthenticated"}))
    with pytest.raises(CalendlyError, match="401"):
        client.scheduling_url()


def test_network_failure_raises_readable_error():
    def handler(request):
        raise httpx.ConnectError("down")

    with pytest.raises(CalendlyError, match="reach"):
        client_with(handler).scheduling_url()
