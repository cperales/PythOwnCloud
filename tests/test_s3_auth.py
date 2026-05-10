"""
Tests for S3 Signature V4 authentication.
Covers canonical query string building with special characters like +, spaces, etc.
"""

import hashlib
import hmac
import pytest
from datetime import datetime, timezone
from urllib.parse import quote

import pythowncloud.config as config
from pythowncloud.s3_auth import _parse_raw_query, _normalize_qs_param
from fastapi.testclient import TestClient


# ─── Settings ──────────────────────────────────────────────────────────────────

S3_ACCESS_KEY = "test-access-key"
S3_SECRET_KEY = "test-secret-key"
API_KEY = "test-api-key"


@pytest.fixture(scope="session", autouse=True)
def override_settings(tmp_path_factory):
    """Point the app at a temp directory and set S3 credentials."""
    storage = tmp_path_factory.mktemp("data")
    config.settings.storage_path = str(storage)
    config.settings.api_key = API_KEY
    config.settings.s3_access_key = S3_ACCESS_KEY
    config.settings.s3_secret_key = S3_SECRET_KEY
    return storage


@pytest.fixture(scope="session")
def client(override_settings):
    import pythowncloud.main as main
    return TestClient(main.app)


# ─── Unit tests for query param normalization ──────────────────────────────────

class TestNormalizeQsParam:
    """Test the _normalize_qs_param function."""

    def test_literal_plus_becomes_percent_encoded(self):
        """A literal + in a parameter value should be re-encoded as %2B."""
        result = _normalize_qs_param("2021+Kayak")
        assert result == "2021%2BKayak"

    def test_percent_encoded_slash_stays_encoded(self):
        """A %2F (/) should stay as %2F."""
        result = _normalize_qs_param("Fotos%2F2021%2BKayak%2F")
        assert result == "Fotos%2F2021%2BKayak%2F"

    def test_percent_encoded_space_stays_encoded(self):
        """A %20 (space) should stay as %20."""
        result = _normalize_qs_param("2021%20Kayak")
        assert result == "2021%20Kayak"

    def test_plain_alphanumeric_unchanged(self):
        """Plain alphanumeric should pass through unchanged."""
        result = _normalize_qs_param("list-type")
        assert result == "list-type"

    def test_underscore_and_tilde_unchanged(self):
        """Unreserved chars like _ and ~ should not be encoded."""
        result = _normalize_qs_param("a_b~c")
        assert result == "a_b~c"

    def test_hyphen_and_dot_unchanged(self):
        """Unreserved chars - and . should not be encoded."""
        result = _normalize_qs_param("a-b.c")
        assert result == "a-b.c"


class TestParseRawQuery:
    """Test the _parse_raw_query function."""

    def test_simple_query_string(self):
        """Parse a simple query string with no special chars."""
        pairs = _parse_raw_query("key1=value1&key2=value2")
        assert sorted(pairs) == [("key1", "value1"), ("key2", "value2")]

    def test_query_with_plus_in_value(self):
        """Parse a query string where a value contains a + that should become %2B."""
        pairs = _parse_raw_query("prefix=Fotos%2F2021+Kayak%2F")
        assert pairs == [("prefix", "Fotos%2F2021%2BKayak%2F")]

    def test_query_with_multiple_params_and_special_chars(self):
        """Parse the real query string from issue #8."""
        qs = "prefix=Fotos%2F2021+Kayak%2F&delimiter=%2F&list-type=2&max-keys=1000"
        pairs = _parse_raw_query(qs)
        # Check that + is re-encoded to %2B
        for k, v in pairs:
            if k == "prefix":
                assert v == "Fotos%2F2021%2BKayak%2F"
                assert "+" not in v
            elif k == "delimiter":
                assert v == "%2F"

    def test_query_with_missing_value(self):
        """Parse a query string where a param has no value."""
        pairs = _parse_raw_query("key1&key2=value2")
        assert ("key1", "") in pairs
        assert ("key2", "value2") in pairs

    def test_plus_is_space_mode(self):
        """With plus_is_space=True, '+' decodes to space and re-encodes as %20."""
        pairs = _parse_raw_query("prefix=2021+Kayak", plus_is_space=True)
        assert pairs == [("prefix", "2021%20Kayak")]

    def test_canonical_query_string_sorting(self):
        """Verify that after normalization, sorting works correctly."""
        qs = "z=1&a=2&m=3"
        pairs = _parse_raw_query(qs)
        sorted_pairs = sorted(pairs)
        canonical = "&".join(f"{k}={v}" for k, v in sorted_pairs)
        # Should be sorted by key name
        assert canonical == "a=2&m=3&z=1"


# ─── End-to-end S3 signature tests ─────────────────────────────────────────────

class TestS3SignatureV4:
    """Test S3 Signature V4 verification with special characters."""

    def _build_s3_request(self, client, path: str, query_string: str = "", method: str = "GET"):
        """
        Build and sign an S3 ListObjectsV2 request using AWS Signature V4.
        Returns the response from the server.
        """
        from urllib.parse import urlencode

        # Current timestamp for the request
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        region = "us-east-1"
        service = "s3"

        # Build credential scope
        credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
        credential_str = f"{S3_ACCESS_KEY}/{credential_scope}"

        # Canonical request components
        canonical_method = method
        canonical_uri = path
        canonical_query_string = query_string
        canonical_headers = f"host:testserver\nx-amz-date:{amz_date}\n"
        signed_headers = "host;x-amz-date"
        payload_hash = "UNSIGNED-PAYLOAD"

        # Build canonical request
        canonical_request = (
            f"{canonical_method}\n"
            f"{canonical_uri}\n"
            f"{canonical_query_string}\n"
            f"{canonical_headers}\n"
            f"{signed_headers}\n"
            f"{payload_hash}"
        )

        # Hash the canonical request
        canonical_hash = hashlib.sha256(canonical_request.encode()).hexdigest()

        # Build string to sign
        string_to_sign = (
            f"AWS4-HMAC-SHA256\n"
            f"{amz_date}\n"
            f"{credential_scope}\n"
            f"{canonical_hash}"
        )

        # Calculate signature
        k_date = hmac.new(
            f"AWS4{S3_SECRET_KEY}".encode(),
            date_stamp.encode(),
            hashlib.sha256,
        ).digest()
        k_region = hmac.new(k_date, region.encode(), hashlib.sha256).digest()
        k_service = hmac.new(k_region, service.encode(), hashlib.sha256).digest()
        k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
        signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

        # Build Authorization header
        auth_header = (
            f"AWS4-HMAC-SHA256 "
            f"Credential={credential_str}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )

        # Make the request
        url = path
        if canonical_query_string:
            url = f"{path}?{canonical_query_string}"

        headers = {
            "Authorization": auth_header,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
        }

        response = client.get(url, headers=headers)
        return response

    def test_list_bucket_succeeds(self, client):
        """Test a simple ListBuckets request."""
        response = self._build_s3_request(client, "/", method="GET")
        assert response.status_code == 200

    def test_list_objects_v2_without_params(self, client):
        """Test ListObjectsV2 without any query params."""
        response = self._build_s3_request(client, "/storage", query_string="list-type=2")
        assert response.status_code == 200

    def test_list_objects_v2_with_space_in_prefix(self, client, override_settings):
        """
        Test ListObjectsV2 with a prefix containing a space.
        This is the real-world case from issue #8 where S3Drive fails.
        The prefix would be "Fotos/2021 Kayak/" where the space is encoded as %20.
        """
        # Create the folder structure first (via REST API)
        # We don't actually create files, just test that the signature verifies

        # The canonical query string should have the space as %20 (not +)
        # This is what S3Drive client should send after signing correctly
        canonical_qs = "delimiter=%2F&list-type=2&max-keys=1000&prefix=Fotos%2F2021%20Kayak%2F"
        response = self._build_s3_request(client, "/storage", query_string=canonical_qs)
        # Should return 200 (successful auth) - the folder doesn't exist but auth should work
        assert response.status_code == 200
        # Response should be valid XML with empty listing
        assert "ListBucketResult" in response.text

    def test_list_objects_v2_with_literal_plus_in_prefix(self, client):
        """
        Test ListObjectsV2 where the folder name contains a literal + character.
        E.g., a folder named "Project+Data" would have prefix="Project%2BData"
        """
        canonical_qs = "delimiter=%2F&list-type=2&max-keys=1000&prefix=Project%2BData%2F"
        response = self._build_s3_request(client, "/storage", query_string=canonical_qs)
        assert response.status_code == 200
        assert "ListBucketResult" in response.text

    def _build_s3_request_split_qs(self, client, path: str, signed_qs: str, wire_qs: str):
        """
        Like _build_s3_request, but signs `signed_qs` while sending `wire_qs` over the wire.
        Models clients (e.g. S3Drive on Android) that sign per AWS spec (%20 for space)
        but emit form-style encoding (+ for space) in the actual HTTP request.
        """
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        region = "us-east-1"
        service = "s3"

        credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
        credential_str = f"{S3_ACCESS_KEY}/{credential_scope}"
        signed_headers = "host;x-amz-date"
        payload_hash = "UNSIGNED-PAYLOAD"

        canonical_request = (
            f"GET\n{path}\n{signed_qs}\n"
            f"host:testserver\nx-amz-date:{amz_date}\n\n"
            f"{signed_headers}\n{payload_hash}"
        )
        canonical_hash = hashlib.sha256(canonical_request.encode()).hexdigest()
        string_to_sign = (
            f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n{canonical_hash}"
        )
        k_date = hmac.new(f"AWS4{S3_SECRET_KEY}".encode(), date_stamp.encode(), hashlib.sha256).digest()
        k_region = hmac.new(k_date, region.encode(), hashlib.sha256).digest()
        k_service = hmac.new(k_region, service.encode(), hashlib.sha256).digest()
        k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
        signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

        headers = {
            "Authorization": (
                f"AWS4-HMAC-SHA256 Credential={credential_str}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            ),
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
        }
        return client.get(f"{path}?{wire_qs}", headers=headers)

    def test_s3drive_form_encoded_space_in_prefix(self, client):
        """
        Issue #8 follow-up: client signs `%20` per spec but sends `+` over the wire.
        Server must accept this by retrying canonicalization with `+` treated as space.
        """
        signed_qs = "delimiter=%2F&list-type=2&max-keys=1000&prefix=Fotos%2F2021%20Kayak%2F"
        wire_qs   = "delimiter=%2F&list-type=2&max-keys=1000&prefix=Fotos%2F2021+Kayak%2F"
        response = self._build_s3_request_split_qs(client, "/storage", signed_qs, wire_qs)
        assert response.status_code == 200
        assert "ListBucketResult" in response.text
