"""Personal pages use the signed-in host's Calendar independently of WhatsApp."""
import pytest
from fastapi.testclient import TestClient

from app import wiring
from app.webhook import create_app
from app.tools.calendar import CalendarAuthError
from app.users import CALENDAR_SCOPE
from tests.fakes import FakeSender, FakeCalendarService
from tests.test_multiuser_wiring import NOW, settings


def runtime_for(tmp_path, monkeypatch):
    monkeypatch.setattr(wiring, "build_service", lambda *args: FakeCalendarService())
    return wiring.build_runtime(settings(tmp_path), sender=FakeSender())


def test_runtime_registers_authenticated_booking_settings(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path, monkeypatch)
    app = create_app("v", "secret", runtime.store, runtime.router.handle, lambda: NOW, routes=runtime.routes)
    response = TestClient(app, follow_redirects=False).get("/connections/scheduling")
    assert response.status_code == 303
    assert response.headers["location"] == "/signin"


def test_username_routes_do_not_shadow_health_or_meta_verification(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path, monkeypatch)
    app = create_app("v", "secret", runtime.store, runtime.router.handle, lambda: NOW, routes=runtime.routes)
    browser = TestClient(app)
    health = browser.get("/health")
    assert health.status_code == 200 and health.json() == {"ok": True}
    webhook = browser.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "v", "hub.challenge": "123"})
    assert webhook.status_code == 200 and webhook.text == "123"


def test_approved_calendar_host_does_not_need_whatsapp_or_gmail(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path, monkeypatch)
    user, _ = runtime.users.sign_in_identity("public-host", "Public host", "host@example.com", NOW)
    runtime.users._update(user.id, NOW, status="approved", calendar_status="connected",
                          google_token_enc=runtime.box.encrypt("host-token"),
                          google_scopes='["' + CALENDAR_SCOPE + '"]')
    calendar = runtime.public_booking.calendar_for_user(user.id)
    assert calendar.check_selected_calendars is True
    assert calendar is not runtime.app_ctx.calendar
    assert calendar.busy_periods(NOW, NOW.replace(hour=NOW.hour + 1)) == []
    assert runtime.users.get(user.id).phone is None


def test_runtime_uses_configured_transactional_host_sender(tmp_path, monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr(wiring, 'build_service', lambda *args: FakeCalendarService())
    config = replace(settings(tmp_path), loops_api_key='secret', booking_host_email_id='booking-template')
    runtime = wiring.build_runtime(config, sender=FakeSender())
    assert runtime.public_booking.host_email.ready


@pytest.mark.parametrize("change", ["disabled", "disconnect", "replace"])
def test_retained_public_calendar_is_rechecked_before_network(tmp_path, monkeypatch, change):
    runtime = runtime_for(tmp_path, monkeypatch)
    user, _ = runtime.users.sign_in_identity("public-host", "Public host", "host@example.com", NOW)
    runtime.users._update(user.id, NOW, status="approved", calendar_status="connected",
                          google_token_enc=runtime.box.encrypt("host-token"),
                          google_scopes='["' + CALENDAR_SCOPE + '"]')
    calendar = runtime.public_booking.calendar_for_user(user.id)
    if change == "disabled":
        runtime.users._update(user.id, NOW, status="disabled")
    elif change == "disconnect":
        runtime.users.disconnect_google(user.id, NOW)
    else:
        runtime.users._update(user.id, NOW, google_token_enc=runtime.box.encrypt("replacement"))
    calls = []
    class Request:
        def execute(self):
            calls.append("network")
            return {}
    with pytest.raises(CalendarAuthError):
        calendar._execute(Request())
    assert calls == []


def test_connections_dashboard_links_to_native_booking_settings():
    from app.connection_pages import render_connection_page
    html = render_connection_page("dashboard", {"name": "Host", "email": "host@example.com"})
    assert 'href="/connections/scheduling"' in html
