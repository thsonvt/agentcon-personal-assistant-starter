import json
import re

from app.web_pages import render_web_page


def test_web_page_escapes_identity_and_script_data():
    attack = '</script><img src=x onerror=alert(1)>&'
    html = render_web_page({'name': attack, 'email': 'a@example.com', 'csrf_token': attack,
                            'ready': True, 'voice': {'stt': False, 'tts': False}})
    assert '<img src=x' not in html
    assert '&lt;/script&gt;' in html
    payload = re.search(r'<script id="pippa-state" type="application/json">(.*?)</script>', html).group(1)
    assert '<' not in payload and '&' not in payload
    assert json.loads(payload)['csrf_token'] == attack
    assert '/pippa-web-assets/app.js' in html
    assert '/pippa-web-assets/app.css' in html
    assert re.search(r'<label[^>]*for="message"', html)
    assert 'role="status"' in html
    assert 'href="/connections/chat"' in html


def test_mobile_shell_preserves_accessible_chat_and_voice_controls():
    html = render_web_page({'name': 'Ana', 'email': 'ana@example.com', 'csrf_token': 'safe',
                            'ready': True, 'voice': {'stt': True, 'tts': True}})
    for control in ('message', 'composer', 'mic', 'handsfree', 'stop-audio', 'status',
                    'live-transcript', 'refresh', 'toggle-text', 'voice-status-details', 'voice-status-detail'):
        assert f'id="{control}"' in html
    assert re.search(r'<textarea[^>]*id="message"[^>]*rows="1"', html)
    assert re.search(r'<label[^>]*for="message"[^>]*>Your message</label>', html)
    assert re.search(r'<button[^>]*id="toggle-text"[^>]*type="button"', html)
    assert re.search(r'<button[^>]*id="send"[^>]*type="submit"', html)
    for summary in ('Settings', 'More controls', 'Voice and privacy'):
        assert re.search(rf'<summary[^>]*>{summary}</summary>', html)
    assert '<svg' in html and '✳' not in html
    assert 'href="/connections"' in html and 'href="/privacy"' in html
    assert 'viewport-fit=cover' in html
