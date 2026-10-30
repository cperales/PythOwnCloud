"""
Tests for S3 upload handling: correctness of hashing/assembly (PutObject,
UploadPart, CompleteMultipartUpload) and the concurrent-upload cap.
"""

import asyncio
import hashlib
import hmac
import os
import re
from datetime import datetime, timezone
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

import pythowncloud.routers.s3 as s3
from pythowncloud.config import settings

S3_ACCESS_KEY = "test-access-key"
S3_SECRET_KEY = "test-secret-key"


# ─── Fixtures / helpers ────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def storage(tmp_path_factory):
    """Point the app at a temp directory with S3 credentials; restore afterwards."""
    path = tmp_path_factory.mktemp("s3-uploads")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "storage_path", str(path))
        mp.setattr(settings, "s3_access_key", S3_ACCESS_KEY)
        mp.setattr(settings, "s3_secret_key", S3_SECRET_KEY)
        yield path


@pytest.fixture(scope="module")
def client(storage):
    import pythowncloud.main as main
    return TestClient(main.app)


def _sign(method: str, path: str, params=()):
    """Sign an S3 request (SigV4, UNSIGNED-PAYLOAD). Returns (url, headers)."""
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]
    region, service = "us-east-1", "s3"
    scope = f"{date_stamp}/{region}/{service}/aws4_request"

    query = "&".join(
        f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in sorted(params)
    )
    canonical_request = (
        f"{method}\n{path}\n{query}\n"
        f"host:testserver\nx-amz-date:{amz_date}\n\n"
        f"host;x-amz-date\nUNSIGNED-PAYLOAD"
    )
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n"
        f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    )

    key = f"AWS4{S3_SECRET_KEY}".encode()
    for part in (date_stamp, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()

    headers = {
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={S3_ACCESS_KEY}/{scope}, "
            f"SignedHeaders=host;x-amz-date, Signature={signature}"
        ),
        "x-amz-date": amz_date,
        "x-amz-content-sha256": "UNSIGNED-PAYLOAD",
    }
    return (f"{path}?{query}" if query else path), headers


def _put(client, path, body, params=()):
    url, headers = _sign("PUT", path, params)
    return client.put(url, content=body, headers=headers)


def _post(client, path, params, body=b""):
    url, headers = _sign("POST", path, params)
    return client.post(url, content=body, headers=headers)


def _initiate(client, key) -> str:
    resp = _post(client, f"/storage/{key}", [("uploads", "")])
    assert resp.status_code == 200
    return re.search(r"<UploadId>([^<]+)</UploadId>", resp.text).group(1)


def _complete_xml(etags: dict[int, str]) -> bytes:
    parts = "".join(
        f"<Part><ETag>{etag}</ETag><PartNumber>{n}</PartNumber></Part>"
        for n, etag in sorted(etags.items())
    )
    return (
        '<CompleteMultipartUpload xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"{parts}</CompleteMultipartUpload>"
    ).encode()


# ─── PutObject ─────────────────────────────────────────────────────────────────

class TestPutObject:
    def test_put_stores_body_and_returns_md5_etag(self, client, storage):
        body = os.urandom(3 * 1024 * 1024 + 123)
        resp = _put(client, "/storage/put/roundtrip.bin", body)

        assert resp.status_code == 200
        assert resp.headers["ETag"] == f'"{hashlib.md5(body).hexdigest()}"'
        assert (storage / "put" / "roundtrip.bin").read_bytes() == body


# ─── Multipart ─────────────────────────────────────────────────────────────────

class TestMultipartUpload:
    def _upload_parts(self, client, key, upload_id, bodies):
        etags = {}
        for n, body in enumerate(bodies, start=1):
            resp = _put(
                client, f"/storage/{key}", body,
                [("partNumber", str(n)), ("uploadId", upload_id)],
            )
            assert resp.status_code == 200
            assert resp.headers["ETag"] == f'"{hashlib.md5(body).hexdigest()}"'
            etags[n] = resp.headers["ETag"]
        return etags

    def test_round_trip_assembles_parts(self, client, storage):
        key = "mp/big.bin"
        bodies = [os.urandom(1024 * 1024 + 7), os.urandom(512 * 1024), os.urandom(4321)]
        upload_id = _initiate(client, key)
        etags = self._upload_parts(client, key, upload_id, bodies)

        resp = _post(
            client, f"/storage/{key}", [("uploadId", upload_id)], _complete_xml(etags)
        )

        assembled = b"".join(bodies)
        assert resp.status_code == 200
        assert (storage / key).read_bytes() == assembled
        assert f"{hashlib.md5(assembled).hexdigest()}-3" in resp.text

        # parts, metadata and the temp file are cleaned up
        uploads_dir = storage / ".uploads"
        assert not list(uploads_dir.glob(f"{upload_id}*"))
        assert not (storage / "mp" / "big.bin.tmp").exists()

    def test_complete_with_missing_part_file_is_400(self, client, storage):
        key = "mp/missing.bin"
        upload_id = _initiate(client, key)
        etags = self._upload_parts(
            client, key, upload_id, [os.urandom(2048), os.urandom(2048)]
        )
        (storage / ".uploads" / f"{upload_id}.part.2").unlink()

        resp = _post(
            client, f"/storage/{key}", [("uploadId", upload_id)], _complete_xml(etags)
        )

        assert resp.status_code == 400
        assert "InvalidPartOrder" in resp.text
        assert not (storage / key).exists()
        assert not (storage / "mp" / "missing.bin.tmp").exists()


# ─── Concurrent-upload cap ─────────────────────────────────────────────────────

class TestUploadCap:
    @pytest.fixture
    def no_free_slots(self, monkeypatch):
        monkeypatch.setattr(s3, "_upload_slots", asyncio.Semaphore(0))
        monkeypatch.setattr(settings, "s3_upload_queue_timeout_seconds", 0.1)

    def test_put_object_gets_slow_down_when_full(self, client, storage, no_free_slots):
        resp = _put(client, "/storage/cap/busy.bin", b"payload")

        assert resp.status_code == 503
        assert "<Code>SlowDown</Code>" in resp.text
        assert resp.headers["Retry-After"] == "5"
        assert not (storage / "cap" / "busy.bin").exists()

    def test_upload_part_gets_slow_down_when_full(self, client, storage, monkeypatch):
        upload_id = _initiate(client, "cap/busy-mp.bin")
        monkeypatch.setattr(s3, "_upload_slots", asyncio.Semaphore(0))
        monkeypatch.setattr(settings, "s3_upload_queue_timeout_seconds", 0.1)

        resp = _put(
            client, "/storage/cap/busy-mp.bin", b"payload",
            [("partNumber", "1"), ("uploadId", upload_id)],
        )

        assert resp.status_code == 503
        assert "<Code>SlowDown</Code>" in resp.text
        assert not (storage / ".uploads" / f"{upload_id}.part.1").exists()

    def test_slot_times_out_when_full(self, monkeypatch):
        monkeypatch.setattr(s3, "_upload_slots", asyncio.Semaphore(1))
        monkeypatch.setattr(settings, "s3_upload_queue_timeout_seconds", 0.05)

        async def scenario():
            async with s3._upload_slot():
                with pytest.raises(s3.UploadBusy):
                    async with s3._upload_slot():
                        pass
            # first holder released it, so it can be taken again
            async with s3._upload_slot():
                pass

        asyncio.run(scenario())

    def test_slot_released_when_body_raises(self, monkeypatch):
        sem = asyncio.Semaphore(1)
        monkeypatch.setattr(s3, "_upload_slots", sem)

        async def scenario():
            with pytest.raises(RuntimeError):
                async with s3._upload_slot():
                    raise RuntimeError("client went away")
            assert not sem.locked()

        asyncio.run(scenario())

    def test_never_exceeds_cap(self, monkeypatch):
        monkeypatch.setattr(s3, "_upload_slots", asyncio.Semaphore(2))
        monkeypatch.setattr(settings, "s3_upload_queue_timeout_seconds", 5)
        running = peak = 0

        async def upload():
            nonlocal running, peak
            async with s3._upload_slot():
                running += 1
                peak = max(peak, running)
                await asyncio.sleep(0.02)
                running -= 1

        async def scenario():
            await asyncio.gather(*(upload() for _ in range(6)))

        asyncio.run(scenario())
        assert peak == 2
