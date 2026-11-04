import json
import re
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.tools.google_oauth import CALENDAR_SCOPE, GoogleAccount

ORIGIN = 'http://127.0.0.1:8016'


def setup(tmp_path, monkeypatch, **extra):
    from app import workshop
    env = {'DB_PATH': str(tmp_path / 'workshop.db'), 'GOOGLE_CLIENT_ID': 'test-client',
           'GOOGLE_CLIENT_SECRET': 'test-secret', **extra}
    account = GoogleAccount('participant@gmail.com', 'Participant', 'private-refresh',
                            granted_scopes=(CALENDAR_SCOPE,), subject='google-person')
    calls = []
    monkeypatch.setattr(workshop.GoogleOAuth, 'exchange_connection', lambda self, code, verifier: (calls.append((code, verifier)) or account))
    app = workshop.build_workshop_app(env)
    client = TestClient(app, base_url=ORIGIN)
    return workshop, app, client, env, calls


def login(client):
    page = client.get('/signin')
    nonce = re.search(r'name="nonce" value="([^"]+)"', page.text).group(1)
    response = client.post('/signin', data={'nonce': nonce}, headers={'Origin': ORIGIN})
    assert response.status_code == 200
    return re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)


def begin(client, csrf):
    response = client.post('/connections/google', data={'csrf': csrf}, headers={'Origin': ORIGIN}, follow_redirects=False)
    assert response.status_code == 302
    return parse_qs(urlsplit(response.headers['location']).query)


def connect(client, csrf):
    params = begin(client, csrf)
    response = client.get('/signup/callback', params={'state': params['state'][0], 'code': 'consent-code'}, follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == '/calendar'
    return params


def test_missing_settings_starts_into_actionable_setup(tmp_path):
    from app.workshop import build_workshop_app
    client = TestClient(build_workshop_app({'DB_PATH': str(tmp_path / 'workshop.db')}), base_url=ORIGIN)
    csrf = login(client)
    page = client.get('/connections')
    assert 'GOOGLE_CLIENT_ID' in page.text and 'GOOGLE_CLIENT_SECRET' in page.text
    assert 'WhatsApp' not in page.text
    response = client.post('/connections/google', data={'csrf': csrf}, headers={'Origin': ORIGIN})
    assert response.status_code == 503
    assert client.get('/connections/chat').url.path == '/app'


def test_oauth_roundtrip_encrypts_and_reuses_token_after_restart(tmp_path, monkeypatch):
    workshop, app, client, env, calls = setup(tmp_path, monkeypatch)
    params = connect(client, login(client))
    assert params['code_challenge_method'] == ['S256']
    assert params['redirect_uri'] == [ORIGIN + '/signup/callback']
    assert CALENDAR_SCOPE in params['scope'][0]
    assert 'gmail.readonly' not in params['scope'][0]
    owner = app.state.workshop.users.get(1)
    assert owner.email == 'participant@gmail.com' and owner.google_subject == 'google-person'
    assert owner.google_token_enc != 'private-refresh'
    assert app.state.workshop.box.decrypt(owner.google_token_enc) == 'private-refresh'
    assert 'private-refresh' not in client.get('/connections').text
    assert len(calls) == 1 and len(calls[0][1]) >= 43
    assert client.get('/signup/callback', params={'state': params['state'][0], 'code': 'again'}).status_code == 400
    restarted = workshop.build_workshop_app(env)
    assert restarted.state.workshop.box.decrypt(restarted.state.workshop.users.get(1).google_token_enc) == 'private-refresh'
    assert (tmp_path / 'workshop.db.key').stat().st_mode & 0o777 == 0o600


def test_oauth_state_is_browser_bound_single_use_and_expires(tmp_path, monkeypatch):
    workshop, app, client, env, calls = setup(tmp_path, monkeypatch)
    csrf = login(client)
    first = begin(client, csrf)['state'][0]
    stranger = TestClient(app, base_url=ORIGIN)
    assert stranger.get('/signup/callback', params={'state': first, 'code': 'x'}).status_code == 400
    assert client.get('/signup/callback', params={'state': first, 'code': 'x'}).status_code == 400
    second = begin(client, csrf)['state'][0]
    now = workshop.sgt_now()
    monkeypatch.setattr(workshop, 'sgt_now', lambda: now + timedelta(minutes=11))
    assert client.get('/signup/callback', params={'state': second, 'code': 'x'}).status_code == 400
    assert not calls


@pytest.mark.parametrize('changes', [
    {'granted_scopes': ()}, {'refresh_token': ''}, {'subject': None},
])
def test_callback_rejects_incomplete_grants(tmp_path, monkeypatch, changes):
    workshop, app, client, env, calls = setup(tmp_path, monkeypatch)
    values = dict(email='participant@gmail.com', name='Participant', refresh_token='secret',
                  subject='person', granted_scopes=(CALENDAR_SCOPE,)) | changes
    monkeypatch.setattr(workshop.GoogleOAuth, 'exchange_connection', lambda *args: GoogleAccount(**values))
    state = begin(client, login(client))['state'][0]
    assert client.get('/signup/callback', params={'state': state, 'code': 'x'}).status_code == 400
    assert not app.state.workshop.users.get(1).google_token_enc


def test_denied_consent_and_account_switch_preserve_connection(tmp_path, monkeypatch):
    workshop, app, client, env, calls = setup(tmp_path, monkeypatch)
    csrf = login(client)
    connect(client, csrf)
    token = app.state.workshop.users.get(1).google_token_enc
    state = begin(client, csrf)['state'][0]
    denied = client.get('/signup/callback', params={'state': state, 'error': 'access_denied'})
    assert denied.status_code == 400 and 'No connection was changed' in denied.text
    other = GoogleAccount('other@gmail.com', 'Other', 'other-secret', subject='different', granted_scopes=(CALENDAR_SCOPE,))
    monkeypatch.setattr(workshop.GoogleOAuth, 'exchange_connection', lambda *args: other)
    state = begin(client, csrf)['state'][0]
    response = client.get('/signup/callback', params={'state': state, 'code': 'x'})
    assert response.status_code == 400 and 'different Google account' in response.text
    assert app.state.workshop.users.get(1).google_token_enc == token


def test_local_auth_csrf_origin_and_host_guards(tmp_path, monkeypatch):
    workshop, app, client, env, calls = setup(tmp_path, monkeypatch)
    assert client.get('/calendar', follow_redirects=False).status_code == 303
    assert client.get('/connections', headers={'Host': 'evil.example'}).status_code == 400
    assert client.post('/connections/google', data={'csrf': 'x'}, headers={'Origin': ORIGIN}, follow_redirects=False).status_code == 303
    csrf = login(client)
    assert client.post('/connections/google', data={'csrf': csrf}, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.post('/connections/google', data={'csrf': 'wrong'}, headers={'Origin': ORIGIN}).status_code == 403
    assert client.post('/api/web/message', json={}, headers={'Origin': 'https://evil.example', 'X-CSRF-Token': csrf}).status_code == 403


def test_calendar_reads_fresh_selected_calendar_and_escapes_values(tmp_path, monkeypatch):
    from app.tools.calendar import Event
    workshop, app, client, env, calls = setup(tmp_path, monkeypatch, GOOGLE_CALENDAR_ID='workshop-calendar')
    connect(client, login(client))
    read = []
    monkeypatch.setattr(workshop, 'build_service', lambda *args: object())
    def events(calendar, start, end, limit=100):
        read.append((calendar.calendar_id, start, end))
        return [Event('event', '<script>unsafe</script>', start, start + timedelta(hours=1), False)]
    monkeypatch.setattr(workshop.GoogleCalendar, 'list_events', events)
    first = client.get('/calendar')
    second = client.get('/calendar')
    assert first.status_code == second.status_code == 200
    assert len(read) == 2 and read[0][0] == 'workshop-calendar'
    assert '<script>unsafe</script>' not in first.text and '&lt;script&gt;unsafe&lt;/script&gt;' in first.text
    assert 'Asia/Singapore' in first.text
    from app.tools.calendar import CalendarAuthError
    monkeypatch.setattr(workshop.GoogleCalendar, 'list_events', lambda *a, **kw: (_ for _ in ()).throw(CalendarAuthError('private provider error')))
    failed = client.get('/calendar')
    assert failed.status_code == 502 and 'Reconnect Google' in failed.text
    assert 'unsafe' not in failed.text and 'private provider error' not in failed.text


def test_chat_uses_existing_agent_only_allowed_tools_and_no_extra_providers(tmp_path, monkeypatch):
    workshop, app, client, env, calls = setup(tmp_path, monkeypatch, OPENAI_API_KEY='test-key', OPENAI_MODEL='test-model')
    csrf = login(client)
    connect(client, csrf)
    monkeypatch.setattr(workshop, 'build_service', lambda *args: object())
    seen = []
    async def run(agent, context, session, text):
        seen.append((agent, context, session, text))
        return 'Calendar checked'
    monkeypatch.setattr(workshop, 'run_agent', run)
    response = client.post('/api/web/message', json={'message': 'Show my day', 'revision': 0, 'voice': False, 'request_id': 'workshop-message'}, headers={'Origin': ORIGIN, 'X-CSRF-Token': csrf})
    assert response.status_code == 200 and response.json()['messages'][-1]['content'] == 'Calendar checked'
    agent, context, session, text = seen[0]
    assert {tool.name for tool in agent.tools} == workshop.CALENDAR_TOOLS
    assert context.whatsapp is None and context.gmail is None and context.group_meetings is None
    assert context.store.user_id == 1 and session.session_id == '1:workshop'


def test_calendar_connection_does_not_require_ai_key_and_chat_explains_missing_key(tmp_path, monkeypatch):
    workshop, app, client, env, calls = setup(tmp_path, monkeypatch)
    csrf = login(client)
    connect(client, csrf)
    response = client.post('/api/web/message', json={'message': 'hello', 'revision': 0, 'voice': False, 'request_id': 'no-ai-key-needed'}, headers={'Origin': ORIGIN, 'X-CSRF-Token': csrf})
    assert response.status_code == 200
    assert 'OPENAI_API_KEY' in response.json()['messages'][-1]['content']
    assert 'fake assistant' not in response.text


def test_changed_oauth_credentials_or_key_fail_closed(tmp_path, monkeypatch):
    workshop, app, client, env, calls = setup(tmp_path, monkeypatch)
    connect(client, login(client))
    with pytest.raises(ValueError, match='credentials'):
        workshop.build_workshop_app(env | {'GOOGLE_CLIENT_ID': 'other-client'})
    from app.crypto import generate_key
    with pytest.raises(ValueError, match='key|TOKEN_KEY'):
        workshop.build_workshop_app(env | {'TOKEN_KEY': generate_key()})
    (tmp_path / 'workshop.db.key').unlink()
    with pytest.raises(ValueError, match='key'):
        workshop.build_workshop_app(env)


def test_existing_full_database_and_symlink_key_are_not_repurposed(tmp_path, monkeypatch):
    from app.store import Store
    from app.users import Users
    from app.workshop import build_workshop_app, sgt_now
    database = str(tmp_path / 'existing.db')
    original = Users(Store(database))
    original.ensure_admin('Existing owner', '6590000000', sgt_now())
    with pytest.raises(ValueError, match='full application'):
        build_workshop_app({'DB_PATH': database})
    assert original.get(1).name == 'Existing owner'
    other = tmp_path / 'do-not-read.key'
    other.write_text('private-value')
    (tmp_path / 'workshop.db.key').symlink_to(other)
    with pytest.raises(ValueError, match='symbolic link'):
        build_workshop_app({'DB_PATH': str(tmp_path / 'workshop.db')})
    assert other.read_text() == 'private-value'


def test_google_failure_does_not_expose_provider_details(tmp_path, monkeypatch):
    from app.tools.google_oauth import GoogleOAuthError
    workshop, app, client, env, calls = setup(tmp_path, monkeypatch)
    state = begin(client, login(client))['state'][0]
    def failure(*args):
        raise GoogleOAuthError('private-secret-from-provider')
    monkeypatch.setattr(workshop.GoogleOAuth, 'exchange_connection', failure)
    result = client.get('/signup/callback', params={'state': state, 'code': 'private-code'})
    assert result.status_code == 400
    assert 'private-secret-from-provider' not in result.text and 'private-code' not in result.text
    assert 'role="alert"' in result.text
    assert not app.state.workshop.users.get(1).google_token_enc


def test_calendar_empty_and_invalid_date_are_distinct_from_failure(tmp_path, monkeypatch):
    workshop, app, client, env, calls = setup(tmp_path, monkeypatch)
    connect(client, login(client))
    monkeypatch.setattr(workshop, 'build_service', lambda *args: object())
    monkeypatch.setattr(workshop.GoogleCalendar, 'list_events', lambda *a, **kw: [])
    response = client.get('/calendar', params={'day': '2026-11-05'})
    assert response.status_code == 200 and 'No events on this date' in response.text
    assert client.get('/calendar', params={'day': 'not-a-date'}).status_code == 400
    def failure(*args, **kwargs):
        raise RuntimeError('private-transport-failure')
    monkeypatch.setattr(workshop.GoogleCalendar, 'list_events', failure)
    failed = client.get('/calendar')
    assert failed.status_code == 502 and 'No events' not in failed.text
    assert 'private-transport-failure' not in failed.text


def test_restart_without_unix_fchmod(tmp_path, monkeypatch):
    workshop, app, client, env, calls = setup(tmp_path, monkeypatch)
    connect(client, login(client))
    monkeypatch.delattr(workshop.os, 'fchmod', raising=False)
    restarted = workshop.build_workshop_app(env)
    assert restarted.state.workshop.box.decrypt(restarted.state.workshop.users.get(1).google_token_enc) == 'private-refresh'


def test_provider_value_error_is_not_reported_as_invalid_input_date(tmp_path, monkeypatch):
    workshop, app, client, env, calls = setup(tmp_path, monkeypatch)
    connect(client, login(client))
    monkeypatch.setattr(workshop, 'build_service', lambda *args: object())
    def failure(*args, **kwargs):
        raise ValueError('malformed private Google data')
    monkeypatch.setattr(workshop.GoogleCalendar, 'list_events', failure)
    response = client.get('/calendar', params={'day': '2026-11-05'})
    assert response.status_code == 502 and 'Calendar unavailable' in response.text
    assert 'valid date' not in response.text and 'private Google' not in response.text


def test_legacy_database_is_rejected_before_any_schema_or_byte_changes(tmp_path):
    import sqlite3
    from app.workshop import build_workshop_app
    database = tmp_path / 'legacy.db'
    with sqlite3.connect(database) as conn:
        conn.execute('CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)')
        conn.execute("INSERT INTO users VALUES (1, 'Private legacy owner')")
    before = database.read_bytes()
    with pytest.raises(ValueError, match='full application'):
        build_workshop_app({'DB_PATH': str(database)})
    assert database.read_bytes() == before
    assert not (tmp_path / 'legacy.db.key').exists()
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [('users',)]


def test_workshop_pages_share_chat_shell_and_calendar_navigation(tmp_path, monkeypatch):
    _, _, client, _, _ = setup(tmp_path, monkeypatch)
    signin = client.get('/signin')
    assert 'class="app-shell"' in signin.text
    assert '/pippa-web-assets/app.css' in signin.text
    assert 'class="brand"' in signin.text
    login(client)
    for path in ('/connections', '/app'):
        html = client.get(path).text
        assert 'class="app-shell"' in html
        assert '/pippa-web-assets/app.css' in html
        assert 'href="/calendar"' in html
        assert 'href="/connections"' in html
        assert '<summary>Settings</summary>' in html
