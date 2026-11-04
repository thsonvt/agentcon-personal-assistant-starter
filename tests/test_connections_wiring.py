"""Real runtime exposes account management and cannot reuse a disconnected client's cache."""

import pytest
from fastapi.testclient import TestClient
from app.tools.calendar import CalendarAuthError
from app.tools.gmail import GmailAuthError

from app import wiring
from app.webhook import create_app
from tests.fakes import FakeSender
from tests.test_multiuser_wiring import NOW, EDMUND, settings, world, send


def test_runtime_exposes_connections_only_with_web_oauth_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(wiring, "build_service", lambda *a: object())
    runtime = wiring.build_runtime(settings(tmp_path), sender=FakeSender())
    app = create_app("v", "secret", runtime.store, runtime.router.handle, lambda: NOW, routes=runtime.routes)
    browser = TestClient(app, base_url="https://testserver", follow_redirects=False)
    response = browser.get("/signin")
    assert response.status_code == 200
    assert "Google" in response.text
    assert browser.get("/connections").status_code == 303
    assert browser.get("/connections").headers["location"] == "/signin"


def test_stale_cached_context_cannot_run_after_token_removed(world):
    runtime, user, _, runs = world
    send(runtime, EDMUND, "what is on my calendar?", "before")
    assert user.id in runtime.user_contexts
    runtime.users._update(user.id, NOW, google_token_enc=None)
    send(runtime, EDMUND, "and tomorrow?", "after")
    assert len(runs) == 1


def test_known_missing_calendar_permission_cannot_reuse_cached_context(world):
    runtime, user, _, runs = world
    send(runtime, EDMUND, "what is on my calendar?", "before")
    runtime.users._update(user.id, NOW, google_scopes='["openid", "email"]')
    send(runtime, EDMUND, "and tomorrow?", "after")
    assert len(runs) == 1


@pytest.mark.parametrize("service,error_type", [("calendar", CalendarAuthError), ("gmail", GmailAuthError)])
@pytest.mark.parametrize("change", ["disconnect", "replace"])
def test_inflight_context_cannot_execute_provider_request_after_disconnect(world, service, error_type, change):
    runtime, user, _, runs = world
    send(runtime, EDMUND, "check my schedule", "before")
    retained = getattr(runs[-1]["app"], service)
    if change == "disconnect":
        runtime.users.disconnect_google(user.id, NOW)
    else:
        runtime.users._update(user.id, NOW, google_token_enc=runtime.box.encrypt("new-refresh-token"))
    calls = []

    class Request:
        def execute(self):
            calls.append("network")
            return {}

    with pytest.raises(error_type):
        retained._execute(Request())
    assert calls == []


def test_public_home_exposes_returning_user_sign_in(tmp_path, monkeypatch):
    monkeypatch.setattr(wiring, "build_service", lambda *a: object())
    runtime = wiring.build_runtime(settings(tmp_path), sender=FakeSender())
    app = create_app("v", "secret", runtime.store, runtime.router.handle, lambda: NOW, routes=runtime.routes)
    assert 'href="/signin"' in TestClient(app).get("/").text
