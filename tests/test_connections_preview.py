"""The local acceptance harness is explicit, isolated and never loads production settings."""

def test_preview_has_its_own_google_and_message_transport():
    from scripts.preview_connections import PreviewOAuth, PreviewSender
    from app.tools.google_oauth import GMAIL_SCOPE
    oauth = PreviewOAuth()
    assert oauth.connection_auth_url("state", "verifier", "gmail").startswith("/__preview/google?")
    assert GMAIL_SCOPE in oauth.exchange_connection("gmail", "verifier").granted_scopes
    sender = PreviewSender()
    sender.send_text("6581234567", "hello")
    assert sender.sent == [("6581234567", "hello")]


def test_preview_sender_distinguishes_acceptance_from_meta_block():
    import pytest
    from scripts.preview_connections import PreviewSender
    from app.tools.whatsapp import WhatsAppError
    sender = PreviewSender()
    assert sender.send_text('6581234567', 'hello')
    sender.blocked_recipient = '6581234567'
    with pytest.raises(WhatsAppError) as err:
        sender.send_text('6581234567', 'hello')
    assert err.value.code == 131030
