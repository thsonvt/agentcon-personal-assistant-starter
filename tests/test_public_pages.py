"""Pages Google requires before the OAuth app can be published (SON-73): a home page and a privacy policy,
served whether or not sign-up is switched on."""

from datetime import datetime

from fastapi.testclient import TestClient

from app import wiring
from app.timeutil import SGT
from app.webhook import create_app
from app.wiring import build_runtime
from tests.fakes import FakeSender
from tests.test_multiuser_wiring import settings

NOW = datetime(2026, 10, 1, 10, tzinfo=SGT)


def client(tmp_path, monkeypatch, signup):
    monkeypatch.setattr(wiring, "build_service", lambda *a: object())
    rt = build_runtime(settings(tmp_path, signup=signup), sender=FakeSender())
    return TestClient(create_app("v", "secret", rt.store, rt.router.handle, lambda: NOW, routes=rt.routes))


def test_privacy_policy_is_always_public(tmp_path, monkeypatch):
    for signup in (False, True):
        page = client(tmp_path / str(signup), monkeypatch, signup).get("/privacy")
        assert page.status_code == 200
        text = page.text
        # What Google's reviewers and users look for: what is collected, how it's used, who processes it, how to leave.
        for needle in ("Google Calendar", "WhatsApp", "OpenAI", "delete my data", "maintainer@example.com", "does not sell"):
            assert needle in text, needle


def test_home_page_exists_even_with_sign_up_off(tmp_path, monkeypatch):
    page = client(tmp_path, monkeypatch, signup=False).get("/")
    assert page.status_code == 200
    assert "Sign in with Google" not in page.text  # nothing to sign up for yet
    assert 'href="/privacy"' in page.text


def test_sign_up_landing_links_to_the_privacy_policy(tmp_path, monkeypatch):
    page = client(tmp_path, monkeypatch, signup=True).get("/signup")
    assert page.url.path == "/signin" and 'href="/signin/google"' in page.text
    assert 'href="/privacy"' in page.text


def test_pages_show_the_app_name_google_checks():
    from app import pages

    assert "<title>Pippa" in pages.home_closed() and "<h1>Pippa</h1>" in pages.home_closed()
    assert "Pippa, a personal scheduling assistant" in pages.privacy()


def test_public_home_names_pippa_in_main_heading_for_oauth_branding(tmp_path, monkeypatch):
    import re
    text = client(tmp_path, monkeypatch, signup=False).get("/").text
    heading = re.search(r"<h1[^>]*>(.*?)</h1>", text, re.S).group(1)
    assert "Pippa" in heading


def test_waitlist_is_public_with_or_without_google_signup(tmp_path, monkeypatch):
    for signup in (False, True):
        browser = client(tmp_path / str(signup), monkeypatch, signup)
        page = browser.get("/")
        assert page.status_code == 200
        assert "Less scheduling." in page.text
        assert "On WhatsApp. Nowhere else." not in page.text
        assert 'action="/api/waitlist"' in page.text
        assert 'newsletter-form' not in page.text
        assert 'name="mailingLists"' not in page.text
        assert 'name="website"' in page.text
        assert 'id="waitlist-challenge"' in page.text
        assert 'name="userGroup"' not in page.text  # preserve existing Loops segments
        assert 'href="/signup"' in page.text
        assert browser.get("/signup").status_code == 200
        assert "Loops" in browser.get("/privacy").text


def test_landing_assets_are_served_but_other_files_are_not(tmp_path, monkeypatch):
    from app.public import ASSETS

    browser = client(tmp_path, monkeypatch, signup=False)
    for name in ASSETS:
        response = browser.get(f"/pippa-assets/{name}")
        assert response.status_code == 200, name
        assert response.content, name
    assert browser.get("/pippa-assets/.env").status_code == 404
    assert browser.get("/pippa-assets/index.html").status_code == 404


def test_google_email_requirement_is_visible_before_the_form(tmp_path, monkeypatch):
    text = client(tmp_path, monkeypatch, signup=False).get('/').text
    assert 'personal Gmail accounts and business email hosted on Google Workspace' in text
    assert text.index('id="waitlist-email-help"') < text.index('id="waitlist"')
    assert 'placeholder="you@gmail.com"' in text
    assert 'Contact Son for help.' in text
