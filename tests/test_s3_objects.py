"""
Tests for S3 single-object reads and deletes: GetObject (including Range),
HeadObject, DeleteObject and their error paths.
"""

import os

import pytest
from fastapi.testclient import TestClient

from pythowncloud.config import settings
from test_s3_uploads import S3_ACCESS_KEY, S3_SECRET_KEY, _sign


@pytest.fixture(scope="module")
def storage(tmp_path_factory):
    """Point the app at a temp directory with S3 credentials; restore afterwards."""
    path = tmp_path_factory.mktemp("s3-objects")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "storage_path", str(path))
        mp.setattr(settings, "s3_access_key", S3_ACCESS_KEY)
        mp.setattr(settings, "s3_secret_key", S3_SECRET_KEY)
        yield path


@pytest.fixture(scope="module")
def client(storage):
    import pythowncloud.main as main
    return TestClient(main.app)


def _request(client, method, path, extra_headers=None):
    url, headers = _sign(method, path)
    headers.update(extra_headers or {})
    return client.request(method, url, headers=headers)


@pytest.fixture
def stored(storage):
    """A 64 KiB file at photos/pic.bin, recreated for every test."""
    target = storage / "photos" / "pic.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    body = os.urandom(64 * 1024)
    target.write_bytes(body)
    yield body
    target.unlink(missing_ok=True)


# ─── GetObject ─────────────────────────────────────────────────────────────────

class TestGetObject:
    def test_returns_body(self, client, stored):
        resp = _request(client, "GET", "/storage/photos/pic.bin")

        assert resp.status_code == 200
        assert resp.content == stored
        assert resp.headers["Content-Length"] == str(len(stored))

    def test_range_returns_partial_content(self, client, stored):
        resp = _request(
            client, "GET", "/storage/photos/pic.bin", {"Range": "bytes=100-199"}
        )

        assert resp.status_code == 206
        assert resp.content == stored[100:200]
        assert resp.headers["Content-Range"] == f"bytes 100-199/{len(stored)}"

    def test_open_ended_range_reads_to_the_end(self, client, stored):
        resp = _request(
            client, "GET", "/storage/photos/pic.bin", {"Range": "bytes=65000-"}
        )

        assert resp.status_code == 206
        assert resp.content == stored[65000:]

    def test_missing_key_is_404_no_such_key(self, client, storage):
        resp = _request(client, "GET", "/storage/photos/nope.bin")

        assert resp.status_code == 404
        assert "<Code>NoSuchKey</Code>" in resp.text

    def test_directory_key_is_400(self, client, stored):
        resp = _request(client, "GET", "/storage/photos")

        assert resp.status_code == 400
        assert "<Code>InvalidArgument</Code>" in resp.text

    def test_unsigned_request_is_rejected(self, client, stored):
        resp = client.get("/storage/photos/pic.bin")

        assert resp.status_code in (401, 403)
        assert resp.content != stored


# ─── HeadObject ────────────────────────────────────────────────────────────────

class TestHeadObject:
    def test_returns_size_and_last_modified(self, client, stored):
        resp = _request(client, "HEAD", "/storage/photos/pic.bin")

        assert resp.status_code == 200
        assert resp.headers["Content-Length"] == str(len(stored))
        assert resp.headers["Last-Modified"].endswith("GMT")

    def test_missing_key_is_404(self, client, storage):
        assert _request(client, "HEAD", "/storage/photos/nope.bin").status_code == 404

    def test_directory_key_is_404(self, client, stored):
        assert _request(client, "HEAD", "/storage/photos").status_code == 404


# ─── DeleteObject ──────────────────────────────────────────────────────────────

class TestDeleteObject:
    def test_deletes_file(self, client, storage, stored):
        resp = _request(client, "DELETE", "/storage/photos/pic.bin")

        assert resp.status_code == 204
        assert not (storage / "photos" / "pic.bin").exists()
        assert _request(client, "GET", "/storage/photos/pic.bin").status_code == 404

    def test_missing_key_is_still_204(self, client, storage):
        # S3 semantics: deleting a key that does not exist succeeds.
        resp = _request(client, "DELETE", "/storage/photos/never-existed.bin")

        assert resp.status_code == 204

    def test_only_the_named_file_is_removed(self, client, storage, stored):
        other = storage / "photos" / "keep.bin"
        other.write_bytes(b"keep me")

        _request(client, "DELETE", "/storage/photos/pic.bin")

        assert other.read_bytes() == b"keep me"
        other.unlink()

    def test_unsigned_delete_is_rejected_and_keeps_file(self, client, storage, stored):
        resp = client.delete("/storage/photos/pic.bin")

        assert resp.status_code in (401, 403)
        assert (storage / "photos" / "pic.bin").read_bytes() == stored
