"""
Tests for S3 ListObjectsV2 against a real database: flat vs delimited listings,
pagination, folders without an is_dir row, and clearing stale rows with DELETE.
"""

import re

import pytest
from fastapi.testclient import TestClient

from pythowncloud.config import settings
from test_s3_uploads import S3_ACCESS_KEY, S3_SECRET_KEY, _put, _sign

KEYS = [
    "photos/a.jpg",
    "photos/b.jpg",
    "photos/c.jpg",
    "photos/2024/d.jpg",
    "photos/2025/e.jpg",
    "photos/2025/deep/f.jpg",
    "photos_old/g.jpg",
    "photosXold/h.jpg",
    "top.txt",
]


@pytest.fixture(scope="module")
def storage(tmp_path_factory):
    """Point the app and its database at a temp directory; restore afterwards."""
    path = tmp_path_factory.mktemp("s3-listing")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "storage_path", str(path))
        mp.setattr(settings, "db_path_dir", str(path))
        mp.setattr(settings, "s3_access_key", S3_ACCESS_KEY)
        mp.setattr(settings, "s3_secret_key", S3_SECRET_KEY)
        yield path


@pytest.fixture(scope="module")
def client(storage):
    """TestClient as a context manager, so lifespan opens the database."""
    import pythowncloud.main as main

    async def no_scan():
        return {}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(main, "run_scan", no_scan)
        with TestClient(main.app) as test_client:
            for key in KEYS:
                assert _put(test_client, f"/storage/{key}", b"x").status_code == 200
            yield test_client


def _list(client, **params):
    query = [(k.replace("_", "-"), v) for k, v in params.items()]
    url, headers = _sign("GET", "/storage", query)
    resp = client.get(url, headers=headers)
    assert resp.status_code == 200
    return resp.text


def _keys(xml):
    return re.findall(r"<Key>([^<]+)</Key>", xml)


def _prefixes(xml):
    return re.findall(r"<CommonPrefixes><Prefix>([^<]+)</Prefix>", xml)


def _walk(client, **params):
    """Follow continuation tokens; returns every page's (keys + prefixes)."""
    pages = []
    token = None
    while True:
        request = dict(params, continuation_token=token) if token else params
        xml = _list(client, **request)
        pages.append(_keys(xml) + _prefixes(xml))
        match = re.search(r"<NextContinuationToken>([^<]+)<", xml)
        if not match:
            assert "<IsTruncated>false</IsTruncated>" in xml
            return pages
        token = match.group(1)


class TestFlatListing:
    def test_no_delimiter_recurses(self, client):
        xml = _list(client, prefix="photos/")

        assert _keys(xml) == sorted(k for k in KEYS if k.startswith("photos/"))
        assert _prefixes(xml) == []
        assert "<Delimiter>" not in xml

    def test_prefix_is_not_a_like_pattern(self, client):
        assert _keys(_list(client, prefix="photos_old/")) == ["photos_old/g.jpg"]

    def test_paginates(self, client):
        pages = _walk(client, prefix="photos/", max_keys="2")

        assert len(pages) == 3
        assert sum(pages, []) == sorted(k for k in KEYS if k.startswith("photos/"))


class TestDelimitedListing:
    def test_lists_direct_children_only(self, client):
        xml = _list(client, prefix="photos/", delimiter="/")

        assert _keys(xml) == ["photos/a.jpg", "photos/b.jpg", "photos/c.jpg"]
        assert _prefixes(xml) == ["photos/2024/", "photos/2025/"]

    def test_folders_without_a_row_appear_at_the_root(self, client):
        xml = _list(client, delimiter="/")

        assert _keys(xml) == ["top.txt"]
        assert _prefixes(xml) == ["photos/", "photosXold/", "photos_old/"]

    def test_prefix_is_not_a_like_pattern(self, client):
        xml = _list(client, prefix="photos_old/", delimiter="/")

        assert _keys(xml) == ["photos_old/g.jpg"]

    def test_pages_through_every_entry_exactly_once(self, client):
        pages = _walk(client, prefix="photos/", delimiter="/", max_keys="2")

        assert [len(page) for page in pages] == [2, 2, 1]
        assert sorted(sum(pages, [])) == [
            "photos/2024/", "photos/2025/", "photos/a.jpg", "photos/b.jpg", "photos/c.jpg",
        ]

    def test_max_keys_zero_returns_an_empty_page(self, client):
        xml = _list(client, prefix="photos/", delimiter="/", max_keys="0")

        assert _keys(xml) == []
        assert _prefixes(xml) == []
        assert "<IsTruncated>false</IsTruncated>" in xml

