import re

import pytest
from fastapi.testclient import TestClient

from scripts.preview_event_networking import create_preview


def test_hosted_preview_uses_exact_https_origin_and_secure_demo_sessions(monkeypatch):
    origin = 'https://pippa-demo.example.com'
    monkeypatch.setenv('EVENT_PREVIEW_BASE_URL', origin)
    app = create_preview()
    client = TestClient(app, base_url=origin, follow_redirects=False)
    assert client.get('/health').json() == {'ok': True, 'mode': 'demo'}
    assert client.get('/health', headers={'Host': 'healthcheck.railway.app'}).status_code == 200
    start = client.get('/__preview/as/alice')
    assert start.status_code == 303
    assert 'Secure' in start.headers['set-cookie']
    page = client.get('/people/3')
    assert origin + '/people/3' in page.text
    assert 'localhost:8833' not in page.text
    assert 'Shared demo' in page.text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
    response = client.post('/people/3/requests', data={
        'csrf_token': csrf, 'event_slug': 'builders-night', 'purpose': 'coffee',
        'note': 'Test follow-up', 'idempotency_key': 'hosted-preview-test',
    }, headers={'Origin': origin, 'Sec-Fetch-Site': 'same-origin'})
    assert response.status_code == 303
    assert client.get('/health', headers={'Host': 'attacker.example.com'}).status_code == 400
    assert '/webhook' not in app.openapi()['paths']
    assert client.post('/webhook', json={}).status_code == 405  # GET-only username catch-all


@pytest.mark.parametrize('origin', [
    'http://demo.example.com', 'https://demo.example.com/path',
    'https://user:password@demo.example.com', 'https://demo.example.com?x=1',
    'not-a-url',
])
def test_public_demo_origin_must_be_an_https_origin(monkeypatch, origin):
    monkeypatch.setenv('EVENT_PREVIEW_BASE_URL', origin)
    with pytest.raises(ValueError, match='origin'):
        create_preview()


def test_local_preview_default_remains_available(monkeypatch):
    monkeypatch.delenv('EVENT_PREVIEW_BASE_URL', raising=False)
    client = TestClient(create_preview(), base_url='http://localhost:8833', follow_redirects=False)
    assert client.get('/health').status_code == 200
    assert 'Local demo' in client.get('/__preview').text
