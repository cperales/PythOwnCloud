"""
S3 XML response builders for S3-compatible API.

Uses xml.etree.ElementTree to build valid S3 XML responses.
"""

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional


def _format_iso8601(dt) -> str:
    """Format datetime (or ISO string) as ISO 8601 with Z suffix."""
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def build_list_buckets(owner: str) -> str:
    """Build ListBuckets XML response (single bucket: storage)."""
    root = ET.Element("ListAllMyBucketsResult")
    root.set("xmlns", "http://s3.amazonaws.com/doc/2006-03-01/")

    owner_elem = ET.SubElement(root, "Owner")
    ET.SubElement(owner_elem, "ID").text = owner
    ET.SubElement(owner_elem, "DisplayName").text = owner

    buckets = ET.SubElement(root, "Buckets")
    bucket = ET.SubElement(buckets, "Bucket")
    ET.SubElement(bucket, "Name").text = "storage"
    ET.SubElement(bucket, "CreationDate").text = _format_iso8601(
        datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    )

    return ET.tostring(root, encoding="unicode")


def build_list_objects_v2(
    bucket: str,
    prefix: str,
    delimiter: Optional[str],
    objects: list,
    common_prefixes: list,
    key_count: int,
    max_keys: int = 1000,
    is_truncated: bool = False,
) -> str:
    """
    Build ListObjectsV2 XML response.

    Args:
        bucket: Bucket name
        prefix: Current prefix
        delimiter: Current delimiter (or None for flat listing)
        objects: List of file objects (dicts with: path, modified_at, size, checksum)
        common_prefixes: List of directory prefixes (strings)
        key_count: Number of items in Contents + CommonPrefixes
        max_keys: Max keys parameter (default 1000)
        is_truncated: Whether more results are available
    """
    root = ET.Element("ListBucketResult")
    root.set("xmlns", "http://s3.amazonaws.com/doc/2006-03-01/")

    ET.SubElement(root, "Name").text = bucket
    ET.SubElement(root, "Prefix").text = prefix if prefix else ""
    if delimiter:
        ET.SubElement(root, "Delimiter").text = delimiter
    ET.SubElement(root, "KeyCount").text = str(key_count)
    ET.SubElement(root, "MaxKeys").text = str(max_keys)
    ET.SubElement(root, "IsTruncated").text = "true" if is_truncated else "false"

    # Contents: individual files
    for obj in objects:
        contents = ET.SubElement(root, "Contents")
        ET.SubElement(contents, "Key").text = obj["path"]
        ET.SubElement(contents, "LastModified").text = _format_iso8601(obj["modified_at"])
        # Use md5 field for ETag if available, fallback to truncated checksum
        etag = obj.get("md5", "") or obj["checksum"][:16]
        ET.SubElement(contents, "ETag").text = f'"{etag}"'
        ET.SubElement(contents, "Size").text = str(obj["size"])
        ET.SubElement(contents, "StorageClass").text = "STANDARD"

    # CommonPrefixes: subdirectories
    for prefix_str in common_prefixes:
        cp = ET.SubElement(root, "CommonPrefixes")
        ET.SubElement(cp, "Prefix").text = prefix_str

    return ET.tostring(root, encoding="unicode")


def build_error(code: str, message: str, key: Optional[str] = None, request_id: str = "req-001") -> str:
    """Build S3 error XML response."""
    root = ET.Element("Error")
    ET.SubElement(root, "Code").text = code
    ET.SubElement(root, "Message").text = message
    if key:
        ET.SubElement(root, "Key").text = key
    ET.SubElement(root, "RequestId").text = request_id

    return ET.tostring(root, encoding="unicode")


def build_initiate_multipart(bucket: str, key: str, upload_id: str) -> str:
    """Build InitiateMultipartUpload XML response."""
    root = ET.Element("InitiateMultipartUploadResult")
    root.set("xmlns", "http://s3.amazonaws.com/doc/2006-03-01/")

    ET.SubElement(root, "Bucket").text = bucket
    ET.SubElement(root, "Key").text = key
    ET.SubElement(root, "UploadId").text = upload_id

    return ET.tostring(root, encoding="unicode")


def build_complete_multipart(bucket: str, key: str, etag: str, location: str = "") -> str:
    """Build CompleteMultipartUpload XML response."""
    root = ET.Element("CompleteMultipartUploadResult")
    root.set("xmlns", "http://s3.amazonaws.com/doc/2006-03-01/")

    if location:
        ET.SubElement(root, "Location").text = location
    ET.SubElement(root, "Bucket").text = bucket
    ET.SubElement(root, "Key").text = key
    ET.SubElement(root, "ETag").text = etag

    return ET.tostring(root, encoding="unicode")


def build_list_parts(bucket: str, key: str, upload_id: str, parts: list) -> str:
    """
    Build ListParts XML response.

    Args:
        bucket: Bucket name
        key: Object key
        upload_id: Multipart upload ID
        parts: List of parts (dicts with: part_number, size, etag, modified_at)
    """
    root = ET.Element("ListPartsResult")
    root.set("xmlns", "http://s3.amazonaws.com/doc/2006-03-01/")

    ET.SubElement(root, "Bucket").text = bucket
    ET.SubElement(root, "Key").text = key
    ET.SubElement(root, "UploadId").text = upload_id

    for part in parts:
        part_elem = ET.SubElement(root, "Part")
        ET.SubElement(part_elem, "PartNumber").text = str(part["part_number"])
        ET.SubElement(part_elem, "LastModified").text = _format_iso8601(part.get("modified_at", datetime.now(tz=timezone.utc)))
        ET.SubElement(part_elem, "ETag").text = part["etag"]
        ET.SubElement(part_elem, "Size").text = str(part["size"])

    return ET.tostring(root, encoding="unicode")


def build_abort_multipart() -> str:
    """Build empty response for AbortMultipartUpload (204 No Content returns no body)."""
    return ""


def build_copy_object(etag: str, last_modified: datetime) -> str:
    """Build CopyObject XML response."""
    root = ET.Element("CopyObjectResult")
    root.set("xmlns", "http://s3.amazonaws.com/doc/2006-03-01/")

    ET.SubElement(root, "LastModified").text = _format_iso8601(last_modified)
    ET.SubElement(root, "ETag").text = f'"{etag}"'

    return ET.tostring(root, encoding="unicode")
