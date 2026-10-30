"""
Tests for S3 bucket operations: CreateBucket (PUT /storage), including the
trailing-slash variant, and that the bucket route never shadows object uploads.
"""

import pytest
from fastapi.testclient import TestClient

from pythowncloud.config import settings
from test_s3_uploads import S3_ACCESS_KEY, S3_SECRET_KEY, _sign


@pytest.fixture(scope="module")
def storage(tmp_path_factory):
    """Point the app at a temp directory with S3 credentials; restore afterwards."""
    path = tmp_path_factory.mktemp("s3-bucket")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "storage_path", str(path))
        mp.setattr(settings, "s3_access_key", S3_ACCESS_KEY)
        mp.setattr(settings, "s3_secret_key", S3_SECRET_KEY)
        yield path


@pytest.fixture(scope="module")
def client(storage):
    import pythowncloud.main as main
    return TestClient(main.app)


def _request(client, method, path, **kwargs):
    url, headers = _sign(method, path)
    return client.request(method, url, headers=headers, **kwargs)


class TestCreateBucket:
    def test_returns_200_with_location(self, client):
        resp = _request(client, "PUT", "/storage")

        assert resp.status_code == 200
        assert resp.headers["Location"] == "/storage"

    def test_is_idempotent(self, client, storage):
        before = sorted(p.name for p in storage.iterdir())

        for _ in range(2):
            assert _request(client, "PUT", "/storage").status_code == 200

        assert sorted(p.name for p in storage.iterdir()) == before

    def test_accepts_location_constraint_body(self, client):
        body = (
            b'<CreateBucketConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            b"<LocationConstraint>eu-west-1</LocationConstraint>"
            b"</CreateBucketConfiguration>"
        )

        resp = _request(client, "PUT", "/storage", content=body)

        assert resp.status_code == 200

    def test_trailing_slash_does_not_500(self, client):
        """PUT /storage/ lands in put_object with an empty key."""
        resp = _request(client, "PUT", "/storage/")

        assert resp.status_code == 200
        assert resp.headers["Location"] == "/storage"

    def test_requires_auth(self, client):
        assert client.put("/storage").status_code == 403

    def test_head_and_get_bucket_still_work(self, client):
        assert _request(client, "HEAD", "/storage").status_code == 200
        assert _request(client, "GET", "/storage").status_code == 200


class TestBucketRouting:
    def test_put_storage_does_not_swallow_object_uploads(self, client, storage):
        resp = _request(client, "PUT", "/storage/bucket-routing.txt", content=b"hello")

        assert resp.status_code == 200
        assert (storage / "bucket-routing.txt").read_bytes() == b"hello"
