"""The B2B marketing page must work without an account or provider credentials."""

from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.public import public_routes


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        for key in ("href", "src"):
            if key in attrs:
                self.urls.append(attrs[key])


@pytest.mark.parametrize("signup_enabled", [False, True])
def test_b2b_page_is_public_and_links_resolve(signup_enabled):
    app = FastAPI()
    app.include_router(public_routes(signup_enabled))
    browser = TestClient(app)
    page = browser.get("/b2b")
    assert page.status_code == 200
    assert "Pippa for business" in page.text
    assert "Illustrative workflow" in page.text
    assert "design partners" in page.text
    links = Links()
    links.feed(page.text)
    assert any(url.startswith("mailto:maintainer@example.com?") for url in links.urls)
    for url in links.urls:
        target = urlsplit(url)
        if url.startswith("#"):
            assert target.fragment in links.ids
        elif url.startswith("/"):
            assert browser.get(url).status_code == 200, url
    assert 'href="/b2b"' in browser.get("/").text
    assert browser.get("/pippa-assets/b2b.html").status_code == 404
