"""
Test S3 API endpoints.
"""

import hashlib
import time
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

import pythowncloud.config as config
import pythowncloud.main as main


@pytest.fixture(scope="session", autouse=True)
def override_s3_settings(tmp_path_factory):
    """Point the app at a temp directory and set known S3 credentials."""
    storage = tmp_path_factory.mktemp("data")
    config.settings.storage_path = str(storage)
    config.settings.s3_access_key = "test-access-key"
    config.settings.s3_secret_key = "test-secret"
    config.settings.max_upload_bytes = 2000000  # 2MB
    return storage


@pytest.fixture(scope="session")
def client(override_s3_settings):
    """Configure TestClient for S3 API (use API key, no Signature V4)."""
    # Bypass S3 auth dependency for tests
    from pythowncloud.s3_auth import verify_s3_auth

    main.app.dependency_overrides[verify_s3_auth] = lambda: "test-access-key"
    return TestClient(main.app)


class TestS3SingleObject:
    """PUT, GET, DELETE - simple operations that don't require presigned URLs."""

    def test_upload_with_api_key(self, client):
        """PUT /s3/storage/{key} with API key - direct upload."""
        content = b"hello from S3"
        r = client.put("/s3/storage/test_upload.txt", data=content, headers={})
        assert r.status_code == 200
        assert "ETag" in r.headers
        assert r.headers["ETag"].startswith('"')

    def test_get_object_returns_data(self, client):
        """GET /s3/storage/{key} — download file."""
        client.put("/s3/storage/get_me.txt", data=b"test content", headers={})
        r = client.get("/s3/storage/get_me.txt")
        assert r.status_code == 200
        assert r.content == b"test content"

    def test_delete_file(self, client):
        """DELETE /s3/storage/{key} — delete file."""
        client.put("/s3/storage/delete.txt", data=b"delete", headers={})
        r = client.delete("/s3/storage/delete.txt", headers={})
        assert r.status_code == 204

    def test_get_nonexistent_file(self, client):
        """GET nonexistent file — 404."""
        r = client.get("/s3/storage/nonexistent.txt", headers={})
        assert r.status_code == 404
