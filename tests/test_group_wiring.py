from fastapi.testclient import TestClient
from app import wiring
from app.webhook import create_app
from app.connection_pages import render_connection_page
from tests.fakes import FakeSender
from tests.test_multiuser_wiring import settings, NOW, EDMUND, world, send


def test_private_meetings_routes_require_auth_and_do_not_shadow_health(tmp_path, monkeypatch):
    monkeypatch.setattr(wiring, "build_service", lambda *a: object())
    runtime = wiring.build_runtime(settings(tmp_path), sender=FakeSender())
    client = TestClient(create_app("v", "secret", runtime.store, runtime.router.handle, lambda: NOW, routes=runtime.routes),follow_redirects=False)
    assert client.get("/connections/meetings").status_code == 303
    assert client.get("/health").json() == {"ok": True}
    assert client.get("/webhook", params={"hub.mode":"subscribe","hub.verify_token":"v","hub.challenge":"x"}).text == "x"
    assert client.get("/m/invalid").status_code in (400,404)


def test_signed_in_chat_context_has_scoped_group_service(world):
    runtime,user,_,runs = world
    send(runtime,EDMUND,"help arrange a call","group-help")
    assert runtime.group_meetings is not None
    assert runs[-1]["app"].group_meetings is runtime.group_meetings
    assert runs[-1]["app"].store.user_id == user.id


def test_connections_has_private_meeting_entrypoint():
    html=render_connection_page("dashboard",{"name":"Host","email":"host@example.com"})
    assert 'href="/connections/meetings"' in html
