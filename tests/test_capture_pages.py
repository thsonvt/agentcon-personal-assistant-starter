import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.capture_pages import CapturePages


NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


class Captures:
    def __init__(self):
        self.conn = sqlite3.connect(':memory:', check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('CREATE TABLE capture_sessions (id TEXT PRIMARY KEY, user_id INTEGER)')
        self.conn.executemany('INSERT INTO capture_sessions VALUES (?, ?)', [('one', 1), ('two', 2)])
        self.sessions = {key: dict(id=key, user_id=user, title='Talk <script>alert(1)</script>', state='ready',
            created_at=NOW.isoformat(), expires_at=(NOW + timedelta(days=7)).isoformat(), link_version=1,
            summary_json=json.dumps({'takeaways': [{'text': '<b>Takeaway</b>', 'source_ids': [1]}],
                                     'observations': [], 'questions': []})) for key, user in [('one', 1), ('two', 2)]}
        self.sources = [dict(id=1, session_id='one', user_id=1, kind='image', status='ready', included=True,
            category='observation', original_text='<img src=x onerror=alert(1)>', extracted_text='Slide words',
            media=b'picture', mime_type='image/png'),
            dict(id=2, session_id='one', user_id=1, kind='audio', status='ready', included=True,
            category='observation', original_text='', extracted_text='Voice transcript', media=b'voice', mime_type='audio/ogg'),
            dict(id=3, session_id='two', user_id=2, kind='image', status='ready', included=True,
            category='observation', original_text='Other secret', extracted_text='', media=b'secret', mime_type='image/png'),
            dict(id=4, session_id='one', user_id=1, kind='text', status='failed', included=False,
            category='review', original_text='Review source', extracted_text='', media=None, mime_type=None)]

    def get(self, user_id, session_id):
        session = self.sessions.get(session_id)
        return session if session and session['user_id'] == user_id else None

    def items(self, user_id, session_id, include_media=True):
        rows = [row for row in self.sources if row['user_id'] == user_id and row['session_id'] == session_id]
        return rows if include_media else [dict(row, media=None, has_media=bool(row.get('media'))) for row in rows]

    def item(self, user_id, item_id):
        return next((row for row in self.sources if row['user_id'] == user_id and row['id'] == item_id), None)


@pytest.fixture
def setup():
    captures = Captures()
    users = SimpleNamespace(get=lambda user_id: SimpleNamespace(status='active', is_admin=False))
    time = [NOW]
    pages = CapturePages(captures, users, lambda: time[0], 'https://pippa.test', 'test-secret')
    app = FastAPI()
    app.include_router(pages.router)
    return captures, users, time, pages, TestClient(app)


def path(pages, session):
    value = urlsplit(pages.link(session))
    return value.path + '?' + value.query


def assert_private(response):
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert 'noindex' in response.headers['x-robots-tag']
    assert "default-src 'none'" in response.headers['content-security-policy']


def test_page_shows_escaped_notes_provenance_and_status(setup):
    captures, _, _, pages, client = setup
    response = client.get(path(pages, captures.sessions['one']))
    assert response.status_code == 200
    assert '&lt;script&gt;' in response.text and '<script>' not in response.text
    assert '&lt;b&gt;Takeaway&lt;/b&gt;' in response.text
    assert '&lt;img src=x onerror=alert(1)&gt;' in response.text
    assert 'href="#item-1"' in response.text
    assert 'Voice transcript' in response.text and '<audio' in response.text
    assert 'Review source' in response.text and 'Failed' in response.text
    assert 'anyone with this link' in response.text.lower()
    assert 'Other secret' not in response.text
    assert_private(response)


def test_media_requires_same_capability_and_never_exposes_other_session(setup):
    captures, _, _, pages, client = setup
    token = urlsplit(pages.link(captures.sessions['one'])).query
    response = client.get('/captures/one/items/1?' + token)
    assert response.status_code == 200 and response.content == b'picture'
    assert response.headers['content-type'] == 'image/png'
    assert_private(response)
    for url in ['/captures/one/items/3?' + token, '/captures/two/items/3?' + token,
                '/captures/one/items/1', '/captures/two?' + token]:
        denied = client.get(url)
        assert denied.status_code == 404 and b'secret' not in denied.content
        assert_private(denied)


@pytest.mark.parametrize('reason', ['missing', 'tampered', 'expired', 'disabled', 'deleted', 'version'])
def test_invalid_access_denied_privately(setup, reason):
    captures, users, time, pages, client = setup
    url = path(pages, captures.sessions['one'])
    if reason == 'missing':
        url = '/captures/one'
    elif reason == 'tampered':
        url += 'x'
    elif reason == 'expired':
        time[0] = NOW + timedelta(days=7)
    elif reason == 'disabled':
        users.get = lambda user_id: SimpleNamespace(status='disabled', is_admin=True)
    elif reason == 'deleted':
        users.get = lambda user_id: None
    else:
        captures.sessions['one']['link_version'] += 1
    response = client.get(url)
    assert response.status_code == 404
    assert_private(response)


def test_bad_summary_citation_is_not_rendered(setup):
    captures, _, _, pages, client = setup
    captures.sessions['one']['summary_json'] = json.dumps({'takeaways': [
        {'text': 'Invalid source secret', 'source_ids': [3]},
        {'text': 'Failed source fact', 'source_ids': [4]}], 'observations': [], 'questions': []})
    response = client.get(path(pages, captures.sessions['one']))
    assert 'Invalid source secret' not in response.text and 'Failed source fact' not in response.text
    assert 'summary' in response.text.lower()


def test_no_write_routes_and_secret_required(setup):
    captures, users, _, _, client = setup
    assert client.post('/captures/one').status_code == 405
    with pytest.raises(ValueError):
        CapturePages(captures, users, lambda: NOW, 'https://pippa.test', '')


@pytest.mark.parametrize('token', ['nonsense', '1.abc', '9999999999999999999999999999999999999.x', '123.é', 'x' * 101])
def test_malformed_tokens_get_private_not_found(setup, token):
    _, _, _, _, client = setup
    response = client.get('/captures/one', params={'token': token})
    assert response.status_code == 404
    assert_private(response)


def test_malicious_or_missing_media_mime_is_not_served(setup):
    captures, _, _, pages, client = setup
    captures.sources[0]['mime_type'] = 'text/html'
    token = urlsplit(pages.link(captures.sessions['one'])).query
    response = client.get('/captures/one/items/1?' + token)
    assert response.status_code == 404
    assert_private(response)


def test_media_invalid_item_id_gets_private_error(setup):
    captures, _, _, pages, client = setup
    token = urlsplit(pages.link(captures.sessions['one'])).query
    response = client.get('/captures/one/items/not-a-number?' + token)
    assert response.status_code == 404
    assert_private(response)


def test_real_store_link_correction_and_deletion(tmp_path):
    from app.store import Store
    from app.capture_store import CaptureStore
    store = Store(str(tmp_path / 'captures.db'))
    captures = CaptureStore(store, lambda: NOW)
    users = SimpleNamespace(get=lambda uid: SimpleNamespace(status='active', is_admin=False))
    pages = CapturePages(captures, users, lambda: NOW, 'https://pippa.test', 'secret')
    app = FastAPI()
    app.include_router(pages.router)
    client = TestClient(app)
    session = captures.start(1, '6590000001', 'Real store notes')
    item = captures.add_item(1, session['id'], 'm1', 'text', text='Evidence')
    captures.complete_item(1, item['id'], 'Evidence', 'observation')
    captures.finish(1, session['id'])
    captures.save_summary(1, session['id'], {'takeaways': [{'text': 'Supported point', 'source_ids': [item['id']]}], 'observations': [], 'questions': []})
    url = path(pages, captures.get(1, session['id']))
    response = client.get(url)
    assert response.status_code == 200 and 'Supported point' in response.text
    captures.set_included(1, session['id'], item['id'], False)
    current = captures.get(1, session['id'])
    response = client.get(path(pages, current))
    assert response.status_code == 200 and 'Supported point' not in response.text
    captures.delete(1, session['id'])
    response = client.get(path(pages, current))
    assert response.status_code == 404
    assert_private(response)
    captures.conn.close()
    store.conn.close()


def test_ready_empty_capture_explains_absence_without_retry(setup):
    captures, _, _, pages, client = setup
    captures.sources = []
    captures.sessions['one']['summary_json'] = json.dumps({'takeaways': [], 'observations': [], 'questions': []})
    response = client.get(path(pages, captures.sessions['one']))
    assert response.status_code == 200
    assert 'No included, readable sources to summarize' in response.text
    assert 'The summary is unavailable' not in response.text
    assert 'Send “retry capture”' not in response.text


def test_ready_all_excluded_capture_explains_absence_and_keeps_originals(setup):
    captures, _, _, pages, client = setup
    for item in captures.sources:
        item['included'] = False
    captures.sessions['one']['summary_json'] = json.dumps({'takeaways': [], 'observations': [], 'questions': []})
    response = client.get(path(pages, captures.sessions['one']))
    assert 'No included, readable sources to summarize' in response.text
    assert 'Voice transcript' in response.text
    assert 'The summary is unavailable' not in response.text


def test_empty_summary_with_usable_sources_still_requires_retry(setup):
    captures, _, _, pages, client = setup
    captures.sessions['one']['summary_json'] = json.dumps({'takeaways': [], 'observations': [], 'questions': []})
    response = client.get(path(pages, captures.sessions['one']))
    assert 'The summary is unavailable' in response.text
    assert 'No included, readable sources to summarize' not in response.text


def test_page_requests_metadata_without_media_blobs(setup):
    captures, _, _, pages, client = setup
    original_items = captures.items
    calls = []

    def metadata_only(user_id, session_id, include_media=True):
        calls.append(include_media)
        assert include_media is False, 'HTML must not load all original media bytes'
        return original_items(user_id, session_id, include_media=False)

    captures.items = metadata_only
    response = client.get(path(pages, captures.sessions['one']))
    assert response.status_code == 200
    assert '<img src="/captures/one/items/1?' in response.text
    assert '<audio controls' in response.text
    assert calls == [False]


def test_media_fetches_one_scoped_item_without_loading_session_blobs(setup):
    captures, _, _, pages, client = setup
    original_item = captures.item
    calls = []

    def one_item(user_id, item_id):
        calls.append((user_id, item_id))
        return original_item(user_id, item_id)

    def forbid_items(*args, **kwargs):
        pytest.fail('A media request must not fetch the session item list')

    captures.item = one_item
    captures.items = forbid_items
    token = urlsplit(pages.link(captures.sessions['one'])).query
    response = client.get('/captures/one/items/1?' + token)
    assert response.status_code == 200 and response.content == b'picture'
    assert calls == [(1, 1)]


def test_media_rejects_another_session_owned_by_same_user(setup):
    captures, _, _, pages, client = setup
    captures.sources.append(dict(captures.sources[0], id=5, session_id='another-session', media=b'same owner secret'))
    token = urlsplit(pages.link(captures.sessions['one'])).query
    response = client.get('/captures/one/items/5?' + token)
    assert response.status_code == 404
    assert b'same owner secret' not in response.content
    assert_private(response)


def test_text_source_does_not_repeat_identical_extraction(setup):
    captures, _, _, pages, client = setup
    captures.sources.append(dict(id=6, session_id='one', user_id=1, kind='text', status='ready', included=True,
        category='observation', original_text='Unique original observation', extracted_text='Unique original observation',
        media=None, mime_type=None))
    response = client.get(path(pages, captures.sessions['one']))
    assert response.text.count('Unique original observation') == 1
    captures.sources[-1]['extracted_text'] = 'Different processed observation'
    response = client.get(path(pages, captures.sessions['one']))
    assert 'Unique original observation' in response.text and 'Different processed observation' in response.text


def test_expiry_is_readable_utc_and_preserves_instant(setup):
    captures, _, time, pages, client = setup
    time[0] = NOW + timedelta(days=1)
    captures.sessions['one']['expires_at'] = '2026-10-10T19:43:25.345678+08:00'
    response = client.get(path(pages, captures.sessions['one']))
    assert '10 Oct 2026, 11:43 UTC' in response.text
    assert '<time datetime="2026-10-10T11:43:25+00:00">' in response.text
    assert 'until 2026-10-10T' not in response.text
