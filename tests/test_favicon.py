"""Tests for the site icon: /favicon.ico, the static icon files and the <link> tags."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import pythowncloud.main as main
    return TestClient(main.app)


def test_favicon_ico_is_served_without_auth(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG")


@pytest.mark.parametrize(
    "path,content_type",
    [
        ("/static/favicon.svg", "image/svg+xml"),
        ("/static/favicon-32.png", "image/png"),
        ("/static/apple-touch-icon.png", "image/png"),
    ],
)
def test_static_icons_are_served(client, path, content_type):
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(content_type)


def test_login_page_links_the_icons(client):
    html = client.get("/login").text
    assert 'rel="icon" type="image/svg+xml" href="/static/favicon.svg"' in html
    assert 'rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32.png"' in html
    assert 'rel="apple-touch-icon" href="/static/apple-touch-icon.png"' in html
