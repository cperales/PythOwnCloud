"""
Tests for TUS resumable upload endpoints (Phase 5).
"""

import base64
import json
import pytest
from pathlib import Path

import pythowncloud.config as config


API_KEY = "a1b2c3d4"
TUS_USER = "admin"
TUS_PASSWORD = "testpass"


@pytest.fixture(scope="session", autouse=True)
def override_settings(tmp_path_factory):
    """Override settings to use a temporary storage directory."""
    storage = tmp_path_factory.mktemp("data")
    config.settings.storage_path = str(storage)
    config.settings.api_key = API_KEY
    # Set a login password hash for TUS Basic Auth
    import bcrypt
    password_hash = bcrypt.hashpw(TUS_PASSWORD.encode(), bcrypt.gensalt()).decode()
    config.settings.login_password_hash = password_hash
    return storage


@pytest.fixture(scope="session")
def client(override_settings):
    """FastAPI TestClient for the PythOwnCloud app."""
    from fastapi.testclient import TestClient
    import pythowncloud.main as main
    return TestClient(main.app)


@pytest.fixture
def basic_auth_headers():
    """HTTP Basic Auth header for TUS requests."""
    credentials = base64.b64encode(f"{TUS_USER}:{TUS_PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {credentials}"}


@pytest.fixture
def bad_auth_headers():
    """Invalid HTTP Basic Auth header."""
    credentials = base64.b64encode(f"{TUS_USER}:wrongpassword".encode()).decode()
    return {"Authorization": f"Basic {credentials}"}


class TestTUSAuth:
    """Test TUS authentication (HTTP Basic Auth)."""

    def test_options_without_auth(self, client):
        """OPTIONS without auth should return 401."""
        resp = client.options("/tus/")
        assert resp.status_code == 401

    def test_options_with_bad_auth(self, client, bad_auth_headers):
        """OPTIONS with wrong password should return 403."""
        resp = client.options("/tus/", headers=bad_auth_headers)
        assert resp.status_code == 403

    def test_options_with_good_auth(self, client, basic_auth_headers):
        """OPTIONS with correct auth should return 200."""
        resp = client.options("/tus/", headers=basic_auth_headers)
        assert resp.status_code == 200


class TestTUSOptions:
    """Test TUS OPTIONS endpoint."""

    def test_options_returns_tus_headers(self, client, basic_auth_headers):
        """OPTIONS /tus/ returns TUS protocol headers."""
        resp = client.options("/tus/", headers=basic_auth_headers)
        assert resp.status_code == 200
        assert "Tus-Resumable" in resp.headers
        assert resp.headers["Tus-Resumable"] == "1.0.0"
        assert "Tus-Version" in resp.headers
        assert "Tus-Extension" in resp.headers
        assert "creation" in resp.headers["Tus-Extension"]
        assert "Tus-Max-Size" in resp.headers


class TestTUSCreateUpload:
    """Test TUS upload creation (POST /tus/)."""

    def test_create_upload(self, client, basic_auth_headers):
        """POST /tus/ creates a new upload and returns Location header."""
        resp = client.post(
            "/tus/",
            headers={
                **basic_auth_headers,
                "Tus-Resumable": "1.0.0",
                "Upload-Length": "100",
            },
        )
        assert resp.status_code == 201
        assert "Location" in resp.headers
        assert "/tus/" in resp.headers["Location"]
        assert resp.headers["Tus-Resumable"] == "1.0.0"

    def test_create_upload_with_metadata(self, client, basic_auth_headers):
        """POST /tus/ with metadata (filename, destination)."""
        filename_b64 = base64.b64encode(b"myfile.txt").decode()
        destination_b64 = base64.b64encode(b"uploads/myfile.txt").decode()
        metadata = f"filename {filename_b64},destination {destination_b64}"

        resp = client.post(
            "/tus/",
            headers={
                **basic_auth_headers,
                "Tus-Resumable": "1.0.0",
                "Upload-Length": "1000",
                "Upload-Metadata": metadata,
            },
        )
        assert resp.status_code == 201
        assert "Location" in resp.headers

    def test_create_upload_missing_tus_resumable(self, client, basic_auth_headers):
        """POST /tus/ without Tus-Resumable header returns 412."""
        resp = client.post(
            "/tus/",
            headers={
                **basic_auth_headers,
                "Upload-Length": "100",
            },
        )
        assert resp.status_code == 412

    def test_create_upload_missing_upload_length(self, client, basic_auth_headers):
        """POST /tus/ without Upload-Length header returns 400."""
        resp = client.post(
            "/tus/",
            headers={
                **basic_auth_headers,
                "Tus-Resumable": "1.0.0",
            },
        )
        assert resp.status_code == 400

    def test_create_upload_invalid_upload_length(self, client, basic_auth_headers):
        """POST /tus/ with invalid Upload-Length returns 400."""
        resp = client.post(
            "/tus/",
            headers={
                **basic_auth_headers,
                "Tus-Resumable": "1.0.0",
                "Upload-Length": "invalid",
            },
        )
        assert resp.status_code == 400

    def test_create_upload_exceeds_max_size(self, client, basic_auth_headers):
        """POST /tus/ with size > TUS-Max-Size returns 400."""
        huge_size = "99999999999999999999"
        resp = client.post(
            "/tus/",
            headers={
                **basic_auth_headers,
                "Tus-Resumable": "1.0.0",
                "Upload-Length": huge_size,
            },
        )
        assert resp.status_code == 400


class TestTUSGetOffset:
    """Test TUS get offset (HEAD /tus/{id})."""

    def test_head_upload(self, client, basic_auth_headers):
        """HEAD /tus/{id} returns current offset."""
        # Create upload
        create_resp = client.post(
            "/tus/",
            headers={
                **basic_auth_headers,
                "Tus-Resumable": "1.0.0",
                "Upload-Length": "100",
            },
        )
        upload_url = create_resp.headers["Location"]
        upload_id = upload_url.split("/")[-1]

        # Get offset
        resp = client.head(f"/tus/{upload_id}", headers=basic_auth_headers)
        assert resp.status_code == 200
        assert "Upload-Offset" in resp.headers
        assert resp.headers["Upload-Offset"] == "0"
        assert "Upload-Length" in resp.headers
        assert resp.headers["Upload-Length"] == "100"
        assert resp.headers["Tus-Resumable"] == "1.0.0"

    def test_head_nonexistent_upload(self, client, basic_auth_headers):
        """HEAD /tus/{nonexistent} returns 404."""
        resp = client.head("/tus/nonexistent123", headers=basic_auth_headers)
        assert resp.status_code == 404


class TestTUSUploadChunk:
    """Test TUS chunk upload (PATCH /tus/{id})."""

    def test_patch_single_chunk_completes(self, client, basic_auth_headers):
        """PATCH with single chunk = total size completes the upload."""
        # Create upload
        create_resp = client.post(
            "/tus/",
            headers={
                **basic_auth_headers,
                "Tus-Resumable": "1.0.0",
                "Upload-Length": "11",
                "Upload-Metadata": (
                    f"filename {base64.b64encode(b'test.txt').decode()},"
                    f"destination {base64.b64encode(b'test.txt').decode()}"
                ),
            },
        )
        upload_id = create_resp.headers["Location"].split("/")[-1]

        # Upload single chunk (entire file)
        resp = client.patch(
            f"/tus/{upload_id}",
            content=b"hello world",
            headers={
                **basic_auth_headers,
                "Tus-Resumable": "1.0.0",
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
            },
        )
        assert resp.status_code == 204
        assert resp.headers["Upload-Offset"] == "11"

        # Verify file was created on disk
        from pathlib import Path
        storage = Path(config.settings.storage_path)
        assert (storage / "test.txt").exists()
        assert (storage / "test.txt").read_bytes() == b"hello world"

    def test_patch_multiple_chunks(self, client, basic_auth_headers):
        """PATCH in two chunks accumulates correctly."""
        # Create upload for 10 bytes
        filename_b64 = base64.b64encode(b"multipart.txt").decode()
        destination_b64 = base64.b64encode(b"multipart.txt").decode()
        metadata = f"filename {filename_b64},destination {destination_b64}"

        create_resp = client.post(
            "/tus/",
            headers={
                **basic_auth_headers,
                "Tus-Resumable": "1.0.0",
                "Upload-Length": "10",
                "Upload-Metadata": metadata,
            },
        )
        upload_id = create_resp.headers["Location"].split("/")[-1]

        # Upload first chunk (5 bytes)
        resp1 = client.patch(
            f"/tus/{upload_id}",
            content=b"hello",
            headers={
                **basic_auth_headers,
                "Tus-Resumable": "1.0.0",
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
            },
        )
        assert resp1.status_code == 204
        assert resp1.headers["Upload-Offset"] == "5"

        # Upload second chunk (5 bytes) - should complete
        resp2 = client.patch(
            f"/tus/{upload_id}",
            content=b"world",
            headers={
                **basic_auth_headers,
                "Tus-Resumable": "1.0.0",
                "Upload-Offset": "5",
                "Content-Type": "application/offset+octet-stream",
            },
        )
        assert resp2.status_code == 204
        assert resp2.headers["Upload-Offset"] == "10"

        # Verify file was created correctly
        from pathlib import Path
        storage = Path(config.settings.storage_path)
        assert (storage / "multipart.txt").exists()
        assert (storage / "multipart.txt").read_bytes() == b"helloworld"

    def test_patch_offset_mismatch(self, client, basic_auth_headers):
        """PATCH with wrong Upload-Offset returns 409."""
        # Create and try to upload with wrong offset
        create_resp = client.post(
            "/tus/",
            headers={
                **basic_auth_headers,
                "Tus-Resumable": "1.0.0",
                "Upload-Length": "10",
            },
        )
        upload_id = create_resp.headers["Location"].split("/")[-1]

        # Try to upload at offset 5 when expected is 0
        resp = client.patch(
            f"/tus/{upload_id}",
            content=b"data",
            headers={
                **basic_auth_headers,
                "Tus-Resumable": "1.0.0",
                "Upload-Offset": "5",  # Wrong!
                "Content-Type": "application/offset+octet-stream",
            },
        )
        assert resp.status_code == 409
        assert "Upload-Offset" in resp.headers

    def test_patch_missing_tus_resumable(self, client, basic_auth_headers):
        """PATCH without Tus-Resumable header returns 412."""
        create_resp = client.post(
            "/tus/",
            headers={
                **basic_auth_headers,
                "Tus-Resumable": "1.0.0",
                "Upload-Length": "10",
            },
        )
        upload_id = create_resp.headers["Location"].split("/")[-1]

        resp = client.patch(
            f"/tus/{upload_id}",
            content=b"data",
            headers={
                **basic_auth_headers,
                # Missing Tus-Resumable
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
            },
        )
        assert resp.status_code == 412

    def test_patch_missing_upload_offset(self, client, basic_auth_headers):
        """PATCH without Upload-Offset header returns 400."""
        create_resp = client.post(
            "/tus/",
            headers={
                **basic_auth_headers,
                "Tus-Resumable": "1.0.0",
                "Upload-Length": "10",
            },
        )
        upload_id = create_resp.headers["Location"].split("/")[-1]

        resp = client.patch(
            f"/tus/{upload_id}",
            content=b"data",
            headers={
                **basic_auth_headers,
                "Tus-Resumable": "1.0.0",
                # Missing Upload-Offset
                "Content-Type": "application/offset+octet-stream",
            },
        )
        assert resp.status_code == 400

    def test_patch_nonexistent_upload(self, client, basic_auth_headers):
        """PATCH to nonexistent upload returns 404."""
        resp = client.patch(
            "/tus/nonexistent123",
            content=b"data",
            headers={
                **basic_auth_headers,
                "Tus-Resumable": "1.0.0",
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
            },
        )
        assert resp.status_code == 404


class TestTUSDeleteUpload:
    """Test TUS upload deletion (DELETE /tus/{id})."""

    def test_delete_upload(self, client, basic_auth_headers):
        """DELETE /tus/{id} removes the upload."""
        # Create upload
        create_resp = client.post(
            "/tus/",
            headers={
                **basic_auth_headers,
                "Tus-Resumable": "1.0.0",
                "Upload-Length": "100",
            },
        )
        upload_id = create_resp.headers["Location"].split("/")[-1]

        # Delete it
        resp = client.delete(f"/tus/{upload_id}", headers=basic_auth_headers)
        assert resp.status_code == 204
        assert resp.headers["Tus-Resumable"] == "1.0.0"

        # Verify metadata file is gone
        tus_dir = config.settings.tus_upload_path
        assert not (tus_dir / f"{upload_id}.meta").exists()
        assert not (tus_dir / f"{upload_id}.part").exists()

    def test_delete_nonexistent_upload(self, client, basic_auth_headers):
        """DELETE nonexistent upload returns 204 (no error)."""
        resp = client.delete("/tus/nonexistent123", headers=basic_auth_headers)
        # TUS spec: DELETE can succeed even if upload doesn't exist
        assert resp.status_code == 204

    def test_delete_partially_uploaded(self, client, basic_auth_headers):
        """DELETE removes partial upload that was started."""
        # Create and partially upload
        create_resp = client.post(
            "/tus/",
            headers={
                **basic_auth_headers,
                "Tus-Resumable": "1.0.0",
                "Upload-Length": "100",
            },
        )
        upload_id = create_resp.headers["Location"].split("/")[-1]

        # Upload first chunk
        client.patch(
            f"/tus/{upload_id}",
            content=b"hello",
            headers={
                **basic_auth_headers,
                "Tus-Resumable": "1.0.0",
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
            },
        )

        # Delete it
        resp = client.delete(f"/tus/{upload_id}", headers=basic_auth_headers)
        assert resp.status_code == 204

        # Verify files are gone
        tus_dir = config.settings.tus_upload_path
        assert not (tus_dir / f"{upload_id}.meta").exists()
        assert not (tus_dir / f"{upload_id}.part").exists()


class TestTUSLargeFiles:
    """Test TUS with realistic large file scenarios."""

    def test_upload_1mb_file(self, client, basic_auth_headers):
        """TUS can handle uploading a 1 MB file in chunks."""
        # Create upload
        chunk_size = 256 * 1024  # 256 KB
        total_size = 1024 * 1024  # 1 MB
        num_chunks = total_size // chunk_size

        create_resp = client.post(
            "/tus/",
            headers={
                **basic_auth_headers,
                "Tus-Resumable": "1.0.0",
                "Upload-Length": str(total_size),
                "Upload-Metadata": (
                    f"filename {base64.b64encode(b'largefile.bin').decode()},"
                    f"destination {base64.b64encode(b'largefile.bin').decode()}"
                ),
            },
        )
        upload_id = create_resp.headers["Location"].split("/")[-1]

        # Upload in chunks
        for i in range(num_chunks):
            offset = i * chunk_size
            chunk_data = b"\x00" * chunk_size
            resp = client.patch(
                f"/tus/{upload_id}",
                content=chunk_data,
                headers={
                    **basic_auth_headers,
                    "Tus-Resumable": "1.0.0",
                    "Upload-Offset": str(offset),
                    "Content-Type": "application/offset+octet-stream",
                },
            )
            assert resp.status_code == 204
            assert resp.headers["Upload-Offset"] == str(offset + chunk_size)

        # Verify file was created
        from pathlib import Path
        storage = Path(config.settings.storage_path)
        largefile = storage / "largefile.bin"
        assert largefile.exists()
        assert largefile.stat().st_size == total_size
