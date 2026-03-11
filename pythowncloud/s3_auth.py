"""
AWS Signature V4 verification for S3-compatible API.

Verifies that incoming S3 requests are signed with the correct HMAC-SHA256 signature.
Uses only stdlib: hmac, hashlib, urllib.parse.
"""

import hashlib
import hmac
import logging
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from fastapi import Request
from fastapi.exceptions import HTTPException

from pythowncloud.config import settings

logger = logging.getLogger(__name__)


def _sign_key(secret_key: str, date_stamp: str, region_name: str, service_name: str) -> bytes:
    """
    Derive the signature key for AWS Signature V4.
    Follows: HMAC(HMAC(HMAC(HMAC("AWS4" + secret_key, date_stamp), region_name), service_name), "aws4_request")
    """
    k_date = hmac.new(
        ("AWS4" + secret_key).encode("utf-8"),
        date_stamp.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    k_region = hmac.new(k_date, region_name.encode("utf-8"), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service_name.encode("utf-8"), hashlib.sha256).digest()
    k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
    return k_signing


def _canonical_request(
    method: str,
    path: str,
    query_string: str,
    headers_dict: dict,
    payload_hash: str,
) -> str:
    """
    Build the canonical request string for Signature V4.
    Format:
    CanonicalRequest =
      HTTPMethod + '\n' +
      CanonicalURI + '\n' +
      CanonicalQueryString + '\n' +
      CanonicalHeaders + '\n' +
      SignedHeaders + '\n' +
      HashedPayload
    """
    # Normalize path: remove double slashes, ensure starts with /
    if not path.startswith("/"):
        path = "/" + path
    path = "/".join(p for p in path.split("/") if p or path.startswith("/"))

    # Canonical headers: sorted, lowercase names, trimmed values, each on own line + newline
    canonical_headers_list = []
    signed_headers_list = []
    for name, value in sorted(headers_dict.items()):
        name_lower = name.lower()
        canonical_headers_list.append(f"{name_lower}:{value.strip()}")
        signed_headers_list.append(name_lower)

    canonical_headers = "\n".join(canonical_headers_list) + "\n"
    signed_headers = ";".join(signed_headers_list)

    # Build canonical request
    canonical_request = (
        f"{method}\n"
        f"{path}\n"
        f"{query_string}\n"
        f"{canonical_headers}\n"
        f"{signed_headers}\n"
        f"{payload_hash}"
    )
    return canonical_request, signed_headers


async def verify_s3_auth(request: Request) -> str:
    """
    FastAPI dependency to verify AWS Signature V4 auth.
    Returns the access key if valid, raises 403 otherwise.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header:
        raise HTTPException(status_code=403, detail="Missing Authorization header")

    # Parse Authorization header: "AWS4-HMAC-SHA256 Credential=KEY/DATE/REGION/s3/aws4_request, SignedHeaders=..., Signature=..."
    if not auth_header.startswith("AWS4-HMAC-SHA256 "):
        raise HTTPException(status_code=403, detail="Invalid authorization scheme")

    auth_parts = auth_header[17:].split(",")  # Skip "AWS4-HMAC-SHA256 "
    auth_dict = {}
    for part in auth_parts:
        if "=" in part:
            key, value = part.split("=", 1)
            auth_dict[key.strip()] = value.strip()

    credential = auth_dict.get("Credential", "")
    signed_headers = auth_dict.get("SignedHeaders", "")
    provided_signature = auth_dict.get("Signature", "")

    if not all([credential, signed_headers, provided_signature]):
        raise HTTPException(status_code=403, detail="Invalid Authorization header format")

    # Parse credential: ACCESS_KEY/DATESTAMP/REGION/s3/aws4_request
    credential_parts = credential.split("/")
    if len(credential_parts) != 5:
        raise HTTPException(status_code=403, detail="Invalid credential format")

    access_key, date_stamp, region, service, request_type = credential_parts

    # Verify access key
    if access_key != settings.s3_access_key:
        raise HTTPException(status_code=403, detail="SignatureDoesNotMatch")

    # Get x-amz-date and x-amz-content-sha256 headers
    amz_date = request.headers.get("x-amz-date", "")
    content_sha256 = request.headers.get("x-amz-content-sha256", "")

    if not amz_date or not content_sha256:
        raise HTTPException(status_code=403, detail="Missing x-amz-date or x-amz-content-sha256 header")

    # Verify date format matches (YYYYMMDD from x-amz-date YYYYMMDDTHHMMSSZ)
    if not amz_date.startswith(date_stamp):
        raise HTTPException(status_code=403, detail="Date mismatch")

    # Build signed headers dict from request
    headers_to_sign = {}
    for header_name in signed_headers.split(";"):
        header_value = request.headers.get(header_name)
        if header_value is not None:
            headers_to_sign[header_name] = header_value

    # Compute payload hash
    if content_sha256 == "UNSIGNED-PAYLOAD":
        payload_hash = "UNSIGNED-PAYLOAD"
    else:
        # For signed payload, content_sha256 should be the hex hash
        payload_hash = content_sha256

    # Build canonical query string (AWS Sig V4: sorted key=value pairs)
    # Raw query string is already percent-encoded by the client, so we just
    # split, sort, and rejoin — no re-encoding (that would double-encode %2F etc.)
    query_string = ""
    if request.url.query:
        raw_pairs = []
        for part in request.url.query.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                raw_pairs.append((k, v))
            else:
                raw_pairs.append((part, ""))
        query_string = "&".join(f"{k}={v}" for k, v in sorted(raw_pairs))

    # Build canonical request
    path = request.url.path
    canonical, signed = _canonical_request(
        request.method,
        path,
        query_string,
        headers_to_sign,
        payload_hash,
    )

    # Hash the canonical request
    canonical_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # Build string to sign
    algorithm = "AWS4-HMAC-SHA256"
    string_to_sign = (
        f"{algorithm}\n"
        f"{amz_date}\n"
        f"{date_stamp}/{region}/s3/aws4_request\n"
        f"{canonical_hash}"
    )

    # Derive signing key
    signing_key = _sign_key(settings.s3_secret_key, date_stamp, region, "s3")

    # Compute signature
    computed_signature = hmac.new(
        signing_key,
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    # Compare signatures
    if not hmac.compare_digest(computed_signature, provided_signature):
        logger.warning(
            "Signature mismatch for key %s: expected %s, got %s",
            access_key,
            computed_signature,
            provided_signature,
        )
        raise HTTPException(status_code=403, detail="SignatureDoesNotMatch")

    logger.debug("S3 auth verified for key %s", access_key)
    return access_key
