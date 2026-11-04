from fastapi.testclient import TestClient
from scripts.preview_web_assistant import build_preview


def test_local_preview_uses_fake_replies_and_needs_no_credentials():
    client = TestClient(build_preview(), base_url='http://127.0.0.1:8016')
    page = client.get('/preview')
    assert page.status_code == 200 and 'Pippa' in page.text
    import json
    import re
    data = json.loads(re.search(r'id="pippa-state" type="application/json">(.*?)</script>', page.text).group(1))
    assert data['voice'] == {'stt': False, 'tts': False}
    result = client.post('/api/web/message', json=dict(message='hello', revision=0, voice=False,
        request_id='preview-test'), headers={'X-CSRF-Token': data['csrf_token']})
    assert result.status_code == 200
    assert 'fake assistant' in result.json()['messages'][-1]['content']
