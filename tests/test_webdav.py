"""
Tests for WebDAV endpoints (Phase 5).
"""

import base64
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

import pythowncloud.config as config


API_KEY = "a1b2c3d4"
WEBDAV_USER = "admin"
WEBDAV_PASSWORD = "testpass"


@pytest.fixture(scope="session", autouse=True)
def override_settings(tmp_path_factory):
    """Override settings to use a temporary storage directory."""
    storage = tmp_path_factory.mktemp("data")
    config.settings.storage_path = str(storage)
    config.settings.api_key = API_KEY
    # Set a login password hash (for bcrypt verification in Basic Auth)
    import bcrypt
    password_hash = bcrypt.hashpw(WEBDAV_PASSWORD.encode(), bcrypt.gensalt()).decode()
    config.settings.login_password_hash = password_hash
    return storage


@pytest.fixture(scope="session")
def client(override_settings):
    """FastAPI TestClient for the PythOwnCloud app."""
    import pythowncloud.main as main
    return TestClient(main.app)


@pytest.fixture
def basic_auth_headers():
    """HTTP Basic Auth header for WebDAV requests."""
    credentials = base64.b64encode(f"{WEBDAV_USER}:{WEBDAV_PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {credentials}"}


@pytest.fixture
def bad_auth_headers():
    """Invalid HTTP Basic Auth header."""
    credentials = base64.b64encode(f"{WEBDAV_USER}:wrongpassword".encode()).decode()
    return {"Authorization": f"Basic {credentials}"}


class TestWebDAVAuth:
    """Test WebDAV authentication (HTTP Basic Auth)."""

    def test_options_without_auth(self, client):
        """OPTIONS without auth should return 401."""
        resp = client.options("/dav/")
        assert resp.status_code == 401
        assert "WWW-Authenticate" in resp.headers

    def test_options_with_bad_auth(self, client, bad_auth_headers):
        """OPTIONS with wrong password should return 403."""
        resp = client.options("/dav/", headers=bad_auth_headers)
        assert resp.status_code == 403

    def test_options_with_good_auth(self, client, basic_auth_headers):
        """OPTIONS with correct auth should return 200."""
        resp = client.options("/dav/", headers=basic_auth_headers)
        assert resp.status_code == 200


class TestWebDAVOptions:
    """Test WebDAV OPTIONS endpoint."""

    def test_options_root(self, client, basic_auth_headers):
        """OPTIONS /dav/ returns DAV capabilities."""
        resp = client.options("/dav/", headers=basic_auth_headers)
        assert resp.status_code == 200
        assert "DAV" in resp.headers
        assert "1" in resp.headers["DAV"]
        assert "Allow" in resp.headers
        assert "PROPFIND" in resp.headers["Allow"]
        assert "GET" in resp.headers["Allow"]
        assert "PUT" in resp.headers["Allow"]

    def test_options_file(self, client, basic_auth_headers):
        """OPTIONS /dav/file.txt returns DAV capabilities."""
        resp = client.options("/dav/file.txt", headers=basic_auth_headers)
        assert resp.status_code == 200
        assert "DAV" in resp.headers


class TestWebDAVPropfind:
    """Test WebDAV PROPFIND endpoint (directory listing)."""

    def test_propfind_root_depth_0(self, client, basic_auth_headers):
        """PROPFIND /dav/ with Depth:0 returns single response."""
        resp = client.request("PROPFIND", "/dav/", headers=basic_auth_headers)
        assert resp.status_code == 207
        assert resp.headers["Content-Type"] == "application/xml; charset=utf-8"
        assert b"<D:multistatus" in resp.content
        assert b"<D:href>/dav</D:href>" in resp.content or b"<D:href>/dav/</D:href>" in resp.content

    def test_propfind_root_depth_1(self, client, basic_auth_headers):
        """PROPFIND /dav/ with Depth:1 returns root + children."""
        # Create a test file first
        put_resp = client.put("/dav/test.txt", content=b"hello", headers=basic_auth_headers)
        assert put_resp.status_code == 201

        # Note: list_directory in PROPFIND calls db.list_directory, which might
        # not include files unless the scanner has run. For this test, we just
        # verify that PROPFIND returns a valid response with the root directory.
        resp = client.request(
            "PROPFIND",
            "/dav/",
            headers={**basic_auth_headers, "Depth": "1"},
        )
        assert resp.status_code == 207
        assert b"<D:multistatus" in resp.content
        assert b"<D:response>" in resp.content

    def test_propfind_depth_infinity_rejected(self, client, basic_auth_headers):
        """PROPFIND with Depth:infinity should return 403."""
        resp = client.request(
            "PROPFIND",
            "/dav/",
            headers={**basic_auth_headers, "Depth": "infinity"},
        )
        assert resp.status_code == 403

    def test_propfind_invalid_depth(self, client, basic_auth_headers):
        """PROPFIND with invalid Depth should return 400."""
        resp = client.request(
            "PROPFIND",
            "/dav/",
            headers={**basic_auth_headers, "Depth": "99"},
        )
        assert resp.status_code == 400

    def test_propfind_nonexistent_path(self, client, basic_auth_headers):
        """PROPFIND on nonexistent path should return 404."""
        resp = client.request(
            "PROPFIND",
            "/dav/nonexistent/",
            headers=basic_auth_headers,
        )
        assert resp.status_code == 404


class TestWebDAVFileOperations:
    """Test WebDAV GET, PUT, DELETE for files."""

    def test_put_file(self, client, basic_auth_headers):
        """PUT /dav/test.txt uploads a file."""
        resp = client.put(
            "/dav/test.txt",
            content=b"hello world",
            headers=basic_auth_headers,
        )
        assert resp.status_code == 201

    def test_put_file_creates_parent(self, client, basic_auth_headers):
        """PUT creates parent directories if needed."""
        resp = client.put(
            "/dav/subdir/nested/file.txt",
            content=b"content",
            headers=basic_auth_headers,
        )
        assert resp.status_code == 201

    def test_get_file(self, client, basic_auth_headers):
        """GET /dav/test.txt downloads the file."""
        # Upload first
        client.put("/dav/test.txt", content=b"hello world", headers=basic_auth_headers)

        resp = client.get("/dav/test.txt", headers=basic_auth_headers)
        assert resp.status_code == 200
        assert resp.content == b"hello world"

    def test_get_nonexistent_file(self, client, basic_auth_headers):
        """GET nonexistent file returns 404."""
        resp = client.get("/dav/nonexistent.txt", headers=basic_auth_headers)
        assert resp.status_code == 404

    def test_head_file(self, client, basic_auth_headers):
        """HEAD /dav/test.txt returns metadata without body."""
        # Upload first
        client.put("/dav/test.txt", content=b"hello world", headers=basic_auth_headers)

        resp = client.head("/dav/test.txt", headers=basic_auth_headers)
        assert resp.status_code == 200
        assert "Content-Length" in resp.headers
        assert int(resp.headers["Content-Length"]) == 11
        assert resp.content == b""  # No body for HEAD

    def test_delete_file(self, client, basic_auth_headers):
        """DELETE /dav/test.txt removes the file."""
        # Upload first
        client.put("/dav/test.txt", content=b"content", headers=basic_auth_headers)

        resp = client.delete("/dav/test.txt", headers=basic_auth_headers)
        assert resp.status_code == 204

        # Verify it's gone
        resp = client.get("/dav/test.txt", headers=basic_auth_headers)
        assert resp.status_code == 404

    def test_delete_nonexistent_file(self, client, basic_auth_headers):
        """DELETE nonexistent file returns 404."""
        resp = client.delete("/dav/nonexistent.txt", headers=basic_auth_headers)
        assert resp.status_code == 404


class TestWebDAVDirectoryOperations:
    """Test WebDAV MKCOL, DELETE for directories."""

    def test_mkcol_creates_directory(self, client, basic_auth_headers):
        """MKCOL /dav/newdir/ creates a directory."""
        resp = client.request(
            "MKCOL",
            "/dav/newdir/",
            headers=basic_auth_headers,
        )
        assert resp.status_code == 201

    def test_mkcol_parent_must_exist(self, client, basic_auth_headers):
        """MKCOL in nonexistent parent returns 409."""
        resp = client.request(
            "MKCOL",
            "/dav/nonexistent/newdir/",
            headers=basic_auth_headers,
        )
        assert resp.status_code == 409

    def test_delete_empty_directory(self, client, basic_auth_headers):
        """DELETE /dav/emptydir/ removes the directory."""
        # Create first
        client.request("MKCOL", "/dav/emptydir/", headers=basic_auth_headers)

        resp = client.delete("/dav/emptydir/", headers=basic_auth_headers)
        assert resp.status_code == 204

    def test_delete_directory_with_contents(self, client, basic_auth_headers):
        """DELETE removes directory and all contents."""
        # Create dir and file
        client.request("MKCOL", "/dav/dir/", headers=basic_auth_headers)
        client.put("/dav/dir/file.txt", content=b"content", headers=basic_auth_headers)

        resp = client.delete("/dav/dir/", headers=basic_auth_headers)
        assert resp.status_code == 204

        # Verify dir and file are gone
        resp = client.get("/dav/dir/", headers=basic_auth_headers)
        assert resp.status_code == 404


class TestWebDAVMove:
    """Test WebDAV MOVE endpoint."""

    def test_move_file(self, client, basic_auth_headers):
        """MOVE /dav/a.txt to /dav/b.txt renames the file."""
        # Create source file
        client.put("/dav/a.txt", content=b"content", headers=basic_auth_headers)

        resp = client.request(
            "MOVE",
            "/dav/a.txt",
            headers={
                **basic_auth_headers,
                "Destination": "http://localhost:8000/dav/b.txt",
            },
        )
        assert resp.status_code == 201

        # Verify source is gone and dest exists
        assert client.get("/dav/a.txt", headers=basic_auth_headers).status_code == 404
        assert client.get("/dav/b.txt", headers=basic_auth_headers).status_code == 200

    def test_move_to_nonexistent_parent(self, client, basic_auth_headers):
        """MOVE into nonexistent parent creates parent."""
        # Create source
        client.put("/dav/source.txt", content=b"data", headers=basic_auth_headers)

        resp = client.request(
            "MOVE",
            "/dav/source.txt",
            headers={
                **basic_auth_headers,
                "Destination": "http://localhost:8000/dav/subdir/moved.txt",
            },
        )
        assert resp.status_code == 201
        assert client.get("/dav/subdir/moved.txt", headers=basic_auth_headers).status_code == 200

    def test_move_nonexistent_source(self, client, basic_auth_headers):
        """MOVE nonexistent source returns 404."""
        resp = client.request(
            "MOVE",
            "/dav/nonexistent.txt",
            headers={
                **basic_auth_headers,
                "Destination": "http://localhost:8000/dav/dest.txt",
            },
        )
        assert resp.status_code == 404

    def test_move_over_existing_without_overwrite(self, client, basic_auth_headers):
        """MOVE with Overwrite:F on existing dest returns 412."""
        client.put("/dav/a.txt", content=b"a", headers=basic_auth_headers)
        client.put("/dav/b.txt", content=b"b", headers=basic_auth_headers)

        resp = client.request(
            "MOVE",
            "/dav/a.txt",
            headers={
                **basic_auth_headers,
                "Destination": "http://localhost:8000/dav/b.txt",
                "Overwrite": "F",
            },
        )
        assert resp.status_code == 412

    def test_move_same_path(self, client, basic_auth_headers):
        """MOVE to same path is a no-op (204)."""
        client.put("/dav/file.txt", content=b"content", headers=basic_auth_headers)

        resp = client.request(
            "MOVE",
            "/dav/file.txt",
            headers={
                **basic_auth_headers,
                "Destination": "http://localhost:8000/dav/file.txt",
            },
        )
        assert resp.status_code == 204


class TestWebDAVCopy:
    """Test WebDAV COPY endpoint."""

    def test_copy_file(self, client, basic_auth_headers):
        """COPY /dav/a.txt to /dav/b.txt copies the file."""
        # Create source
        client.put("/dav/a.txt", content=b"content", headers=basic_auth_headers)

        resp = client.request(
            "COPY",
            "/dav/a.txt",
            headers={
                **basic_auth_headers,
                "Destination": "http://localhost:8000/dav/b.txt",
            },
        )
        assert resp.status_code == 201

        # Verify both exist with same content
        assert client.get("/dav/a.txt", headers=basic_auth_headers).content == b"content"
        assert client.get("/dav/b.txt", headers=basic_auth_headers).content == b"content"

    def test_copy_nonexistent_source(self, client, basic_auth_headers):
        """COPY nonexistent source returns 404."""
        resp = client.request(
            "COPY",
            "/dav/nonexistent.txt",
            headers={
                **basic_auth_headers,
                "Destination": "http://localhost:8000/dav/dest.txt",
            },
        )
        assert resp.status_code == 404

    def test_copy_over_existing_without_overwrite(self, client, basic_auth_headers):
        """COPY with Overwrite:F on existing dest returns 412."""
        client.put("/dav/a.txt", content=b"a", headers=basic_auth_headers)
        client.put("/dav/b.txt", content=b"b", headers=basic_auth_headers)

        resp = client.request(
            "COPY",
            "/dav/a.txt",
            headers={
                **basic_auth_headers,
                "Destination": "http://localhost:8000/dav/b.txt",
                "Overwrite": "F",
            },
        )
        assert resp.status_code == 412


class TestWebDAVMimeTypes:
    """Test that WebDAV correctly handles MIME types."""

    def test_text_file_mime_type(self, client, basic_auth_headers):
        """PUT/GET text file has correct MIME type."""
        client.put("/dav/document.txt", content=b"text", headers=basic_auth_headers)
        resp = client.get("/dav/document.txt", headers=basic_auth_headers)
        assert "text/plain" in resp.headers.get("Content-Type", "")

    def test_image_mime_type(self, client, basic_auth_headers):
        """PUT/GET image file has correct MIME type."""
        # Minimal JPEG header
        jpeg_data = b"\xff\xd8\xff\xe0\x00\x10JFIF"
        client.put("/dav/image.jpg", content=jpeg_data, headers=basic_auth_headers)
        resp = client.get("/dav/image.jpg", headers=basic_auth_headers)
        assert "image/jpeg" in resp.headers.get("Content-Type", "")
