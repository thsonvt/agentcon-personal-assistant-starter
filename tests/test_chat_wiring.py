from fastapi.testclient import TestClient
from app import wiring
from app.webhook import create_app
from app.connection_pages import render_connection_page
from tests.fakes import FakeSender
from tests.test_multiuser_wiring import settings, NOW


def test_new_chat_is_authenticated_and_existing_group_routes_remain(tmp_path, monkeypatch):
    monkeypatch.setattr(wiring, 'build_service', lambda *a: object())
    runtime = wiring.build_runtime(settings(tmp_path), sender=FakeSender())
    app = create_app('v', 'secret', runtime.store, runtime.router.handle, lambda: NOW, routes=runtime.routes)
    client = TestClient(app, follow_redirects=False)
    assert client.get('/connections/chat').status_code == 303
    assert client.get('/connections/chat').headers['location'] == '/signin'
    assert runtime.chat_scheduling.groups is runtime.group_meetings
    assert client.get('/connections/meetings').status_code == 303
    assert client.get('/health').json() == {'ok':True}
    assert client.get('/m/invalid').status_code == 404


def test_connections_links_to_both_interfaces():
    html = render_connection_page('dashboard', {'name':'Host','email':'host@example.com'})
    assert 'href="/connections/chat"' in html
    assert 'href="/connections/meetings"' in html
