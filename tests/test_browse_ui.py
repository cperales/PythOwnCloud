"""
Tests for the web file browser markup (/browse/).

Regression: media links used inline onclick="openX({{ path|tojson }}, ...)".
tojson emits double-quoted strings, which closed the double-quoted attribute and
left a handler like `openVideoPlayer(`, so clicking a media file did nothing.
"""

import re

import pytest
from fastapi.testclient import TestClient

import pythowncloud.config as config

API_KEY = "test-secret-key-browse-ui"

# name -> expected data-kind
MEDIA_FILES = {
    "photo.jpg": "image",
    "clip.mp4": "video",
    "song.mp3": "audio",
    "Tom & \"Jerry's\" clip.mp4": "video",
}


@pytest.fixture
def browse_html(tmp_path):
    """Return the HTML of /browse/ for a storage dir holding MEDIA_FILES."""
    import pythowncloud.main as main
    from pythowncloud.passwords import hash_password

    storage = tmp_path / "data"
    storage.mkdir()
    for name in MEDIA_FILES:
        (storage / name).write_bytes(b"x")

    saved = (
        config.settings.storage_path,
        config.settings.db_path_dir,
        config.settings.api_key,
        config.settings.login_password_hash,
    )
    try:
        config.settings.storage_path = str(storage)
        config.settings.db_path_dir = str(tmp_path)
        config.settings.api_key = API_KEY
        config.settings.login_password_hash = hash_password("hunter2")

        with TestClient(main.app) as client:
            client.post("/api/scan", headers={"X-API-Key": API_KEY})
            r = client.post("/login", data={"password": "hunter2"}, follow_redirects=False)
            assert r.status_code == 303
            page = client.get("/browse/")
            assert page.status_code == 200
            yield page.text
    finally:
        (
            config.settings.storage_path,
            config.settings.db_path_dir,
            config.settings.api_key,
            config.settings.login_password_hash,
        ) = saved


class TestMediaLinks:
    def test_no_inline_handler_with_double_quoted_args(self, browse_html):
        """`onclick="openX("..."` is the exact markup that broke clicks."""
        assert not re.search(r'onclick="open(Lightbox|AudioPlayer|VideoPlayer)\("', browse_html)

    def test_media_links_carry_data_attributes(self, browse_html):
        for name, kind in MEDIA_FILES.items():
            links = re.findall(r'<a [^>]*class="media-link"[^>]*>', browse_html)
            matching = [
                tag for tag in links
                if f'data-kind="{kind}"' in tag and _attr(tag, "data-name") == name
            ]
            assert len(matching) == 1, f"no media link for {name!r}"
            assert _attr(matching[0], "data-path") == name


def _attr(tag: str, attr: str) -> str:
    """Read an HTML attribute value from a tag and unescape it."""
    import html

    m = re.search(rf'{attr}="([^"]*)"', tag)
    assert m, f"{attr} missing in {tag}"
    return html.unescape(m.group(1))
