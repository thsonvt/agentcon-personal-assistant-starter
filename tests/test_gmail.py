import base64
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.errors import HttpError


def gmail_module():
    from app.tools import gmail
    return gmail


def part(mime, text, **kwargs):
    return {"mimeType": mime, "body": {"data": base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")}, **kwargs}


def service(message=None, error=None):
    svc = MagicMock()
    messages = svc.users.return_value.messages.return_value
    messages.get.return_value.execute.return_value = message or {"id": "a123", "payload": part("text/plain", "Dinner")}
    messages.list.return_value.execute.return_value = {"messages": [{"id": "a123"}]}
    if error:
        messages.list.return_value.execute.side_effect = error
    return svc, messages


def test_reads_nested_full_body_and_html_map_href_not_only_snippet():
    g = gmail_module()
    maps = "https://maps.app.goo.gl/aXayYUJLmeyTRC1P8?g_st=ic"
    msg = {"id": "a123", "snippet": "No location here", "payload": {
        "mimeType": "multipart/mixed", "headers": [{"name": "Subject", "value": "Dinner with Son"}, {"name": "From", "value": "Chris"}, {"name": "Date", "value": "Tue, 29 Sep 2026 18:00:00 +0800"}],
        "parts": [{"mimeType": "multipart/alternative", "parts": [part("text/plain", "Dinner at 6pm"), part("text/html", f'<p>Meet at <a href="{maps}">this restaurant</a></p><script>bad()</script>')]}]}}
    svc, messages = service(msg)
    result = g.GoogleGmail(svc).get_message("a123")
    assert result.subject == "Dinner with Son"
    assert result.sender == "Chris"
    assert "Dinner at 6pm" in result.body
    assert "this restaurant" in result.body
    assert "bad()" not in result.body
    assert maps in result.links
    assert result.source_url == "https://mail.google.com/mail/u/0/#all/a123"
    messages.get.assert_called_once_with(userId="me", id="a123", format="full")


def test_search_limits_fetches_to_five_and_empty_results_are_not_errors():
    g = gmail_module()
    svc, messages = service()
    messages.list.return_value.execute.return_value = {"messages": [{"id": str(i)} for i in range(9)]}
    assert len(g.GoogleGmail(svc).search_messages("from:chris dinner", 100)) == 5
    messages.list.assert_called_once_with(userId="me", q="from:chris dinner", maxResults=5)
    assert messages.get.call_count == 5
    messages.list.return_value.execute.return_value = {}
    assert g.GoogleGmail(svc).search_messages("no match") == []


def test_inline_calendar_and_plain_links_are_extracted_without_fetching_attachments():
    g = gmail_module()
    msg = {"id": "a123", "payload": {"mimeType": "multipart/mixed", "parts": [
        part("text/calendar", "BEGIN:VEVENT\r\nLOCATION:Restaurant\\, Singapore\r\nURL:https://maps.google.com/?q=restaurant\r\nEND:VEVENT", filename="invite.ics"),
        {"mimeType": "text/plain", "body": {"attachmentId": "external", "size": 99}},
    ]}}
    svc, messages = service(msg)
    result = g.GoogleGmail(svc).get_message("a123")
    assert "Restaurant" in result.body
    assert "https://maps.google.com/?q=restaurant" in result.links
    assert result.truncated
    messages.attachments.assert_not_called()


def test_unsafe_links_filtered_and_body_bounded():
    g = gmail_module()
    msg = {"id": "a123", "payload": part("text/html", '<a href="javascript:alert(1)">bad</a><a href="file:///etc/passwd">bad</a><a href="https://user:password@example.com">bad</a><a href="https://maps.app.goo.gl/location">map</a>' + 'x' * 50000)}
    svc, _ = service(msg)
    result = g.GoogleGmail(svc).get_message("a123")
    assert result.links == ("https://maps.app.goo.gl/location",)
    assert len(result.body) <= g.MAX_BODY_CHARS
    assert result.truncated


def test_malformed_base64_is_safe_and_marked_incomplete():
    g = gmail_module()
    svc, _ = service({"id": "a123", "payload": {"mimeType": "text/plain", "body": {"data": "%%%"}}})
    msg = g.GoogleGmail(svc).get_message("a123")
    assert msg.truncated
    assert msg.body == ""


@pytest.mark.parametrize("status,reason,error_class", [(403,"insufficientPermissions","GmailPermissionError"),(403,"accessNotConfigured","GmailServiceDisabledError"),(403,"rateLimitExceeded","GmailRateLimitError"),(429,"rateLimitExceeded","GmailRateLimitError"),(401,"authError","GmailAuthError"),(503,"backendError","GmailTemporaryError"),(404,"notFound","GmailNotFoundError"),(403,"domainPolicy","GmailError")])
def test_google_errors_distinguish_access_from_temporary_failures(status, reason, error_class):
    g = gmail_module()
    error = HttpError(SimpleNamespace(status=status, reason="private token"), json.dumps({"error": {"message": "private token", "errors": [{"reason": reason}]}}).encode())
    svc, _ = service(error=error)
    with pytest.raises(getattr(g, error_class)) as caught:
        g.GoogleGmail(svc).search_messages("dinner")
    assert "private token" not in str(caught.value)


@pytest.mark.parametrize("error,expected", [(RefreshError("private token"), "GmailAuthError"), (TransportError("private token"), "GmailTemporaryError"), (OSError("private token"), "GmailTemporaryError")])
def test_transport_and_refresh_errors_are_safe(error, expected):
    g = gmail_module()
    svc, _ = service(error=error)
    with pytest.raises(getattr(g, expected)) as caught:
        g.GoogleGmail(svc).search_messages("dinner")
    assert "private token" not in str(caught.value)


def test_build_uses_only_readonly_gmail_scope():
    g = gmail_module()
    with patch("google.oauth2.credentials.Credentials") as creds, patch("googleapiclient.discovery.build") as build:
        assert g.build_service("client", "secret", "refresh") is build.return_value
    assert creds.call_args.kwargs["scopes"] == ["https://www.googleapis.com/auth/gmail.readonly"]
    build.assert_called_once_with("gmail", "v1", credentials=creds.return_value, cache_discovery=False)


def test_google_http_transport_dns_failure_is_safe():
    from httplib2 import ServerNotFoundError
    g = gmail_module()
    svc, _ = service(error=ServerNotFoundError("private token"))
    with pytest.raises(g.GmailTemporaryError) as caught:
        g.GoogleGmail(svc).search_messages("dinner")
    assert "private token" not in str(caught.value)


def test_modern_service_disabled_reason_is_recognized():
    g = gmail_module()
    error = HttpError(SimpleNamespace(status=403, reason="Forbidden"), json.dumps({"error": {"details": [{"reason": "SERVICE_DISABLED"}]}}).encode())
    svc, _ = service(error=error)
    with pytest.raises(g.GmailServiceDisabledError):
        g.GoogleGmail(svc).search_messages("dinner")


def test_invalid_error_payload_is_safe():
    g = gmail_module()
    error = HttpError(SimpleNamespace(status=503, reason="private token"), b"not json private token")
    svc, _ = service(error=error)
    with pytest.raises(g.GmailTemporaryError) as caught:
        g.GoogleGmail(svc).search_messages("dinner")
    assert "private token" not in str(caught.value)


def test_inline_calendar_unfolds_location_link():
    g = gmail_module()
    svc, _ = service({"id": "a123", "payload": part("text/calendar", "LOCATION:https://maps.app.goo.gl/\r\n restaurant\r\n")})
    assert g.GoogleGmail(svc).get_message("a123").links == ("https://maps.app.goo.gl/restaurant",)


def test_empty_search_is_not_an_unbounded_inbox_read():
    g = gmail_module()
    svc, messages = service()
    with pytest.raises(g.GmailError):
        g.GoogleGmail(svc).search_messages("   ")
    messages.list.assert_not_called()


def test_respects_declared_text_charset():
    g = gmail_module()
    payload = {"mimeType": "text/plain", "headers": [{"name": "Content-Type", "value": "text/plain; charset=iso-8859-1"}], "body": {"data": base64.urlsafe_b64encode("Café".encode("iso-8859-1")).decode()}}
    svc, _ = service({"id": "a123", "payload": payload})
    assert g.GoogleGmail(svc).get_message("a123").body == "Café"


def test_parallel_gmail_requests_are_serialized():
    import threading
    import time
    g = gmail_module()
    active = 0
    peak = 0
    def execute():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        time.sleep(0.01)
        active -= 1
        return {"id": "a123", "payload": part("text/plain", "dinner")}
    svc, messages = service()
    messages.get.return_value.execute.side_effect = execute
    gmail = g.GoogleGmail(svc)
    workers = [threading.Thread(target=gmail.get_message, args=("a123",)) for _ in range(3)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert peak == 1


def test_refresh_invalid_scope_reports_missing_permission_not_expired_authorization():
    g = gmail_module()
    error = RefreshError("private token", {"error": "invalid_scope", "error_description": "private token"})
    svc, _ = service(error=error)
    with pytest.raises(g.GmailPermissionError) as caught:
        g.GoogleGmail(svc).search_messages("dinner")
    assert "read permission" in str(caught.value)
    assert "expired" not in str(caught.value)
    assert "private token" not in str(caught.value)


def test_refresh_invalid_grant_still_reports_expired_authorization():
    g = gmail_module()
    error = RefreshError("private token", {"error": "invalid_grant"})
    svc, _ = service(error=error)
    with pytest.raises(g.GmailAuthError):
        g.GoogleGmail(svc).search_messages("dinner")
