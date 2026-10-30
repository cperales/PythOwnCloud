"""
AWS Signature V4 verification for S3-compatible API.

Verifies that incoming S3 requests are signed with the correct HMAC-SHA256 signature.
Uses only stdlib: hmac, hashlib, urllib.parse.
"""

import hashlib
import hmac
import logging
from urllib.parse import unquote, unquote_plus, quote

from fastapi import Request
from fastapi.exceptions import HTTPException

from pythowncloud.config import settings

logger = logging.getLogger(__name__)


def _normalize_qs_param(raw: str, plus_is_space: bool = False) -> str:
    """
    Normalize a query parameter per AWS Signature V4 / RFC 3986.
    Decodes percent-encoded values, then re-encodes using only RFC 3986 unreserved chars as safe.

    With plus_is_space=False (default), '+' is treated as a literal '+' character and
    re-encoded as '%2B'. This matches boto3 / AWS Java v2 SDK.
    With plus_is_space=True, '+' is treated as a space and re-encoded as '%20'. This matches
    clients that use form-style encoding for query strings (e.g. S3Drive on Android).
    """
    decoded = unquote_plus(raw) if plus_is_space else unquote(raw)
    return quote(decoded, safe="~")


def _canonical_uri(path: str) -> str:
    """
    Normalize path for Signature V4: URI-encode each segment according to RFC 3986.
    Per AWS spec, unreserved characters (A-Z a-z 0-9 - _ . ~) are not encoded.
    """
    if not path:
        return "/"
    # Split by /, encode each segment, then rejoin
    segments = path.split("/")
    encoded = "/".join(quote(seg, safe="") for seg in segments)
    if not encoded.startswith("/"):
        encoded = "/" + encoded
    return encoded


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
    # Normalize path using AWS Signature V4 spec
    path = _canonical_uri(path)

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


def _parse_raw_query(raw_query: str, plus_is_space: bool = False) -> list[tuple[str, str]]:
    """
    Split raw percent-encoded query string into normalized key=value pairs.
    Each key and value is decoded then re-encoded per AWS Signature V4 / RFC 3986.
    See _normalize_qs_param for the plus_is_space flag.
    """
    pairs = []
    for part in raw_query.split("&"):
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            pairs.append((
                _normalize_qs_param(k, plus_is_space),
                _normalize_qs_param(v, plus_is_space),
            ))
        else:
            pairs.append((_normalize_qs_param(part, plus_is_space), ""))
    return pairs


def _build_canonical_query_string(
    raw_query: str, is_presigned: bool, plus_is_space: bool
) -> str:
    pairs = _parse_raw_query(raw_query, plus_is_space=plus_is_space)
    if is_presigned:
        pairs = [(k, v) for k, v in pairs if k != "X-Amz-Signature"]
    return "&".join(f"{k}={v}" for k, v in sorted(pairs))


async def verify_s3_auth(request: Request) -> str:
    """
    FastAPI dependency to verify AWS Signature V4 auth.
    Supports both Authorization header auth and pre-signed URL query auth.
    Returns the access key if valid, raises 403 otherwise.
    """
    logger.debug(
        "S3 auth check: %s %s | auth-present=%s | x-amz-date=%s",
        request.method,
        request.url.path,
        bool(request.headers.get("Authorization")),
        request.headers.get("x-amz-date", "(none)"),
    )

    qs_params = dict(_parse_raw_query(request.url.query))
    is_presigned = "X-Amz-Signature" in qs_params

    if is_presigned:
        # Pre-signed URL auth: all auth params are in the query string
        credential = qs_params.get("X-Amz-Credential", "")
        signed_headers = qs_params.get("X-Amz-SignedHeaders", "")
        provided_signature = qs_params.get("X-Amz-Signature", "")
        amz_date = qs_params.get("X-Amz-Date", "")

        if not all([credential, signed_headers, provided_signature, amz_date]):
            raise HTTPException(status_code=403, detail="Missing pre-signed URL parameters")

        # Pre-signed requests always use UNSIGNED-PAYLOAD
        payload_hash = "UNSIGNED-PAYLOAD"

    else:
        # Header-based auth
        auth_header = request.headers.get("Authorization", "")
        if not auth_header or not auth_header.startswith("AWS4-HMAC-SHA256 "):
            raise HTTPException(status_code=403, detail="Missing or invalid Authorization header")

        auth_parts = auth_header[17:].split(",")
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

        amz_date = request.headers.get("x-amz-date", "")
        content_sha256 = request.headers.get("x-amz-content-sha256", "UNSIGNED-PAYLOAD")

        if not amz_date:
            raise HTTPException(status_code=403, detail="Missing x-amz-date header")

        # AWS Signature V4 Chunked Transfer Encoding (streaming signatures)
        # When x-amz-content-sha256 = STREAMING-AWS4-HMAC-SHA256-PAYLOAD, the request body
        # is sent with AWS chunked framing (not standard HTTP chunked encoding).
        # We verify the Authorization header seed signature using this literal value as the payload hash.
        if content_sha256 == "STREAMING-AWS4-HMAC-SHA256-PAYLOAD":
            logger.info("S3 streaming signature detected for %s %s (will verify seed signature)", request.method, request.url.path)

        payload_hash = content_sha256

    # Parse credential: ACCESS_KEY/DATESTAMP/REGION/s3/aws4_request
    # URL-decode credential in case it's percent-encoded (common in presigned URLs)
    decoded_credential = unquote(credential)
    credential_parts = decoded_credential.split("/")
    if len(credential_parts) != 5:
        raise HTTPException(status_code=403, detail="Invalid credential format")

    access_key, date_stamp, region, _service, _req_type = credential_parts

    if access_key != settings.s3_access_key:
        raise HTTPException(status_code=403, detail="SignatureDoesNotMatch")

    if not amz_date.startswith(date_stamp):
        raise HTTPException(status_code=403, detail="Date mismatch")

    # Build signed headers dict from request
    # URL-decode signed headers in case it's percent-encoded (semicolon = %3B)
    decoded_signed_headers = unquote(signed_headers)
    headers_to_sign = {}
    for header_name in decoded_signed_headers.split(";"):
        header_value = request.headers.get(header_name)
        if header_value is not None:
            headers_to_sign[header_name] = header_value

    # Build and hash canonical request. Some clients (e.g. S3Drive on Android) use
    # form-style encoding for spaces (`+` on the wire) but sign per AWS spec (`%20`).
    # Try the strict literal-`+` interpretation first; if it doesn't match and the raw
    # query contains `+`, retry treating `+` as space.
    path = request.url.path
    signing_key = _sign_key(settings.s3_secret_key, date_stamp, region, "s3")
    raw_query = request.url.query

    def _compute(plus_is_space: bool) -> str:
        query_string = _build_canonical_query_string(raw_query, is_presigned, plus_is_space)
        canonical, _ = _canonical_request(
            request.method, path, query_string, headers_to_sign, payload_hash
        )
        canonical_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        string_to_sign = (
            f"AWS4-HMAC-SHA256\n"
            f"{amz_date}\n"
            f"{date_stamp}/{region}/s3/aws4_request\n"
            f"{canonical_hash}"
        )
        return hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    matched = hmac.compare_digest(_compute(False), provided_signature)
    if not matched and "+" in raw_query:
        matched = hmac.compare_digest(_compute(True), provided_signature)

    if not matched:
        logger.warning(
            "S3 signature mismatch for key %s (presigned=%s) — %s %s",
            access_key, is_presigned, request.method, path,
        )
        raise HTTPException(status_code=403, detail="SignatureDoesNotMatch")

    logger.debug("S3 auth verified for key %s (presigned=%s)", access_key, is_presigned)
    return access_key
