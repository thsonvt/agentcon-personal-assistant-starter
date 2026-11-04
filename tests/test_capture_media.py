import httpx
import pytest

from app.tools.whatsapp import WhatsAppClient, WhatsAppError


def test_actual_stream_size_is_enforced_when_metadata_understates_it(monkeypatch):
    from app.tools import whatsapp
    monkeypatch.setattr(whatsapp, 'MAX_MEDIA_BYTES', 8)
    yielded = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            for block in (b'12345', b'67890', b'never-read'):
                yielded.append(block)
                yield block

    def handler(request):
        if request.url.host == 'graph.facebook.com':
            return httpx.Response(200, json={'url':'https://lookaside.fbsbx.com/media', 'file_size':1, 'mime_type':'image/png'})
        return httpx.Response(200, stream=Stream())
    client = WhatsAppClient('token', 'id', http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(WhatsAppError, match='too large'):
        client.download_media('media')
    assert b'never-read' not in yielded


@pytest.mark.parametrize('metadata', [{}, None, [], {'url':'http://localhost/private'},
    {'url':'https://attacker.example/private'}, {'url':'https://lookaside.fbsbx.com/file', 'file_size':'bad'}])
def test_bad_media_metadata_is_rejected_without_token_exposure(metadata):
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=metadata)
    client = WhatsAppClient('secret-token', 'id', http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(WhatsAppError) as exc:
        client.download_media('media')
    assert len(seen) == 1 and 'secret-token' not in str(exc.value)
