"""
Tests for PythOwnCloud API — Phase 1.
Covers all endpoints from the API Reference in README.md.

Uses FastAPI's TestClient (synchronous) with a temporary directory as storage,
so no Docker or running server is needed.
"""

import hashlib
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

# ─── App setup with temp storage ───────────────────────────────────────────────

import pythowncloud.config as config  # imported before main so we can override settings

API_KEY = "test-secret-key"


@pytest.fixture(scope="session", autouse=True)
def override_settings(tmp_path_factory):
    """Point the app at a temp directory and set a known API key."""
    storage = tmp_path_factory.mktemp("data")
    config.settings.storage_path = str(storage)
    config.settings.api_key = API_KEY

    import pythowncloud.main as main
    main.STORAGE = Path(storage)
    return storage


@pytest.fixture(scope="session")
def client(override_settings):
    import pythowncloud.main as main
    return TestClient(main.app)


@pytest.fixture()
def auth(client):
    """Return headers with a valid API key."""
    return {"X-API-Key": API_KEY}


# ─── Health ─────────────────────────────────────────────────────────────────────

class TestHealth:
    def test_returns_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "storage" in body
        assert body["writable"] is True


# ─── Authentication ──────────────────────────────────────────────────────────────

class TestAuth:
    def test_missing_key_returns_401(self, client):
        r = client.get("/files/")
        assert r.status_code == 401
        assert "Missing" in r.json()["detail"]

    def test_wrong_key_returns_403(self, client):
        r = client.get("/files/", headers={"X-API-Key": "wrong"})
        assert r.status_code == 403
        assert "Invalid" in r.json()["detail"]


# ─── List files ──────────────────────────────────────────────────────────────────

class TestListFiles:
    def test_list_root(self, client, auth):
        r = client.get("/files/", headers=auth)
        assert r.status_code == 200
        body = r.json()
        assert "path" in body
        assert "items" in body
        assert "total" in body

    def test_list_subdirectory(self, client, auth):
        # Create a dir and upload a file into it first
        client.post("/mkdir/photos/2025", headers=auth)
        client.put(
            "/files/photos/2025/shot.txt",
            headers=auth,
            files={"file": ("shot.txt", b"pixel", "text/plain")},
        )

        r = client.get("/files/photos/2025/", headers=auth)
        assert r.status_code == 200
        body = r.json()
        assert body["path"] == "photos/2025"
        assert any(item["name"] == "shot.txt" for item in body["items"])

    def test_list_nonexistent_returns_404(self, client, auth):
        r = client.get("/files/does/not/exist/", headers=auth)
        assert r.status_code == 404


# ─── Download a file ─────────────────────────────────────────────────────────────

class TestDownloadFile:
    def test_download_existing_file(self, client, auth):
        content = b"hello from POC"
        client.put(
            "/files/documents/hello.txt",
            headers=auth,
            files={"file": ("hello.txt", content, "text/plain")},
        )

        r = client.get("/files/documents/hello.txt", headers=auth)
        assert r.status_code == 200
        assert r.content == content

    def test_download_nonexistent_returns_404(self, client, auth):
        r = client.get("/files/documents/ghost.txt", headers=auth)
        assert r.status_code == 404


# ─── Upload a file ───────────────────────────────────────────────────────────────

class TestUploadFile:
    def test_upload_returns_metadata(self, client, auth):
        content = b"upload test content"
        r = client.put(
            "/files/documents/upload_test.txt",
            headers=auth,
            files={"file": ("upload_test.txt", content, "text/plain")},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["path"] == "documents/upload_test.txt"
        assert body["size"] == len(content)
        assert body["message"] == "uploaded"
        # Verify checksum matches
        expected = hashlib.sha256(content).hexdigest()
        assert body["checksum"] == expected

    def test_upload_creates_parent_dirs(self, client, auth):
        r = client.put(
            "/files/new/nested/dir/file.txt",
            headers=auth,
            files={"file": ("file.txt", b"nested", "text/plain")},
        )
        assert r.status_code == 200

    def test_overwrite_existing_file(self, client, auth):
        path = "/files/documents/overwrite_me.txt"
        client.put(path, headers=auth, files={"file": ("f.txt", b"v1", "text/plain")})
        client.put(path, headers=auth, files={"file": ("f.txt", b"v2", "text/plain")})

        r = client.get(path, headers=auth)
        assert r.content == b"v2"


# ─── Delete a file ───────────────────────────────────────────────────────────────

class TestDeleteFile:
    def test_delete_existing_file(self, client, auth):
        client.put(
            "/files/documents/to_delete.txt",
            headers=auth,
            files={"file": ("to_delete.txt", b"bye", "text/plain")},
        )
        r = client.delete("/files/documents/to_delete.txt", headers=auth)
        assert r.status_code == 200
        body = r.json()
        assert body["message"] == "deleted"
        assert body["size"] == 3

        # Confirm it's gone
        r2 = client.get("/files/documents/to_delete.txt", headers=auth)
        assert r2.status_code == 404

    def test_delete_nonexistent_returns_404(self, client, auth):
        r = client.delete("/files/documents/nope.txt", headers=auth)
        assert r.status_code == 404

    def test_delete_directory_is_refused(self, client, auth):
        client.post("/mkdir/protected_dir", headers=auth)
        r = client.delete("/files/protected_dir", headers=auth)
        assert r.status_code == 400


# ─── Create a directory ──────────────────────────────────────────────────────────

class TestMkdir:
    def test_create_directory(self, client, auth):
        r = client.post("/mkdir/photos/2025/march", headers=auth)
        assert r.status_code == 200
        body = r.json()
        assert body["path"] == "photos/2025/march"
        assert body["message"] == "created"

    def test_create_nested_parents(self, client, auth):
        r = client.post("/mkdir/a/b/c/d", headers=auth)
        assert r.status_code == 200

    def test_create_existing_directory_is_idempotent(self, client, auth):
        client.post("/mkdir/idempotent_dir", headers=auth)
        r = client.post("/mkdir/idempotent_dir", headers=auth)
        assert r.status_code == 200


# ─── Security ────────────────────────────────────────────────────────────────────

class TestSecurity:
    def test_path_traversal_is_blocked(self, client, auth):
        r = client.get("/files/../../etc/passwd", headers=auth)
        assert r.status_code in (403, 404)

    def test_path_traversal_on_upload_is_blocked(self, client, auth):
        r = client.put(
            "/files/../../etc/evil.txt",
            headers=auth,
            files={"file": ("evil.txt", b"bad", "text/plain")},
        )
        assert r.status_code in (403, 404)


# ─── Move File ────────────────────────────────────────────────────────────────

class TestMoveFile:
    def test_move_file(self, client, auth):
        # Create files and directory
        client.put(
            "/files/source.txt",
            headers=auth,
            files={"file": ("source.txt", b"hello", "text/plain")},
        )
        client.post("/mkdir/dest_dir", headers=auth)

        # Move file to directory
        r = client.post(
            "/files/move",
            headers=auth,
            json={"source": "source.txt", "destination": "dest_dir/source.txt"},
        )
        assert r.status_code == 200
        assert r.json()["message"] == "moved"

        # Original file should not exist
        r = client.get("/files/source.txt", headers=auth)
        assert r.status_code == 404

        # New location should exist
        r = client.get("/files/dest_dir/source.txt", headers=auth)
        assert r.status_code == 200
        assert r.content == b"hello"

    def test_move_nonexistent_returns_404(self, client, auth):
        r = client.post(
            "/files/move",
            headers=auth,
            json={"source": "nonexistent.txt", "destination": "new.txt"},
        )
        assert r.status_code == 404

    def test_move_to_existing_returns_409(self, client, auth):
        client.put(
            "/files/a.txt",
            headers=auth,
            files={"file": ("a.txt", b"a", "text/plain")},
        )
        client.put(
            "/files/b.txt",
            headers=auth,
            files={"file": ("b.txt", b"b", "text/plain")},
        )

        r = client.post(
            "/files/move",
            headers=auth,
            json={"source": "a.txt", "destination": "b.txt"},
        )
        assert r.status_code == 409

    def test_move_to_same_path_is_noop(self, client, auth):
        client.put(
            "/files/same.txt",
            headers=auth,
            files={"file": ("same.txt", b"same", "text/plain")},
        )

        r = client.post(
            "/files/move",
            headers=auth,
            json={"source": "same.txt", "destination": "same.txt"},
        )
        assert r.status_code == 200
        assert r.json()["message"] == "same path"
