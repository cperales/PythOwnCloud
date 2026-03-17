"""
S3-compatible API router — endpoints under /s3/ for resumable multipart uploads.

Implements AWS Signature V4 auth and a subset of S3 operations:
- Single-object: GET, PUT, HEAD, DELETE
- Bucket listing: ListObjectsV2
- Multipart upload: CreateMultipartUpload, UploadPart, CompleteMultipartUpload, AbortMultipartUpload
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import shutil
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from starlette.requests import ClientDisconnect

from pythowncloud.auth import verify_basic_auth
from pythowncloud.cache import invalidate_listing_cache
from pythowncloud.config import settings
from pythowncloud.helpers import get_storage, safe_path
import pythowncloud.db as db
import pythowncloud.thumbnails as thumbnails
from pythowncloud.s3_auth import verify_s3_auth
from pythowncloud.s3_xml import (
    build_abort_multipart,
    build_complete_multipart,
    build_copy_object,
    build_error,
    build_initiate_multipart,
    build_list_buckets,
    build_list_objects_v2,
    build_list_parts,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Per-upload locks to prevent race conditions on concurrent part uploads
_upload_locks: dict[str, asyncio.Lock] = {}


# ─── List Buckets ──────────────────────────────────────────────────────────

@router.get("/")
async def list_buckets(_auth: str = Depends(verify_s3_auth)):
    """GET /s3/ — ListBuckets (returns single hardcoded bucket 'storage')."""
    return Response(
        content=build_list_buckets(settings.s3_access_key),
        media_type="application/xml",
        status_code=200,
    )


# ─── Head Bucket ──────────────────────────────────────────────────────────

@router.head("/storage")
async def head_bucket(_auth: str = Depends(verify_s3_auth)):
    """HEAD /s3/storage — HeadBucket (return 200 if bucket exists)."""
    return Response(status_code=200)


@router.get("/storage")
async def get_bucket(request: Request, _auth: str = Depends(verify_s3_auth)):
    """GET /s3/storage — ListObjectsV2 or GetBucketVersioning."""
    # Handle ?versioning query (return empty/disabled versioning config)
    if "versioning" in request.query_params:
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?>'
            '<VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/"/>',
            media_type="application/xml",
        )
    return await _list_objects_v2(request)


# ─── Single-Object Operations ──────────────────────────────────────────────

@router.put("/storage/{key:path}")
async def put_object(key: str, request: Request, _auth: str = Depends(verify_s3_auth)):
    """
    PUT /s3/storage/{key} — Upload a file (single request or as part of multipart).

    Handles:
    - Simple PUT (no partNumber): single-file upload
    - PUT with partNumber + uploadId: multipart part upload
    - PUT with trailing slash: create empty directory
    """
    # CopyObject (x-amz-copy-source header present)
    if request.headers.get("x-amz-copy-source"):
        return await _copy_object(key, request)

    # Check for multipart parameters
    part_number_str = request.query_params.get("partNumber")
    upload_id = request.query_params.get("uploadId")

    # Multipart part upload
    if part_number_str and upload_id:
        return await _upload_part(key, upload_id, int(part_number_str), request)

    # Simple PUT: single-file upload
    now = datetime.now(timezone.utc)
    logger.info("S3 PUT received [%02d:%02d:%02d]: key=%s, content-length=%s", now.hour, now.minute, now.second, key, request.headers.get("content-length"))
    try:
        target = safe_path(key)

        # Check if this is an empty directory (trailing slash)
        if key.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            # Record in DB as directory
            if db.get_pool() is not None:
                await db.upsert_file(
                    path=str(target.relative_to(get_storage())),
                    filename=target.name or "storage",
                    extension=None,
                    size=0,
                    checksum="",
                    is_dir=True,
                    modified_at=datetime.now(tz=timezone.utc),
                )
            return Response(status_code=200, headers={"ETag": '""'})

        # Regular file upload
        target.parent.mkdir(parents=True, exist_ok=True)

        size = 0
        h_sha256 = hashlib.sha256()
        h_md5 = hashlib.md5()
        try:
            with open(target, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        target.unlink(missing_ok=True)
                        return Response(
                            content=build_error("EntityTooLarge", "Your proposed upload exceeds the maximum allowed size", key=key),
                            media_type="application/xml",
                            status_code=400,
                        )
                    f.write(chunk)
                    h_sha256.update(chunk)
                    h_md5.update(chunk)
        except ClientDisconnect:
            target.unlink(missing_ok=True)
            logger.warning("Client disconnected during S3 PUT for %s", key)
            return Response(status_code=400)

        # Record in DB
        if db.get_pool() is not None:
            try:
                rel_path = str(target.relative_to(get_storage()))
                mtime = datetime.fromtimestamp(target.stat().st_mtime, tz=timezone.utc)
                ext = target.suffix.lstrip(".").lower() or None
                await db.upsert_file(
                    path=rel_path,
                    filename=target.name,
                    extension=ext,
                    size=size,
                    checksum=h_sha256.hexdigest(),
                    is_dir=False,
                    modified_at=mtime,
                    md5=h_md5.hexdigest(),
                )
            except Exception:
                logger.warning("DB upsert failed for S3 PUT %s", key, exc_info=True)

        # Invalidate cache and generate thumbnail
        try:
            rel = str(target.relative_to(get_storage()))
            invalidate_listing_cache(rel)
            thumbnails.record_upload()
            ext = target.suffix.lstrip(".").lower()
            if thumbnails.is_thumbable(ext) and not thumbnails.should_defer_thumbnail():
                try:
                    thumbnails.invalidate_thumbnail(rel)
                    await thumbnails.ensure_thumbnail(rel, ext)
                except Exception:
                    logger.warning("Thumbnail generation failed for %s", key, exc_info=True)
            elif thumbnails.is_thumbable(ext):
                thumbnails.invalidate_thumbnail(rel)
        except Exception:
            pass

        # Return success with MD5 as ETag
        return Response(
            status_code=200,
            headers={"ETag": f'"{h_md5.hexdigest()}"'},
        )

    except HTTPException as e:
        return Response(
            content=build_error("AccessDenied", str(e.detail), key=key),
            media_type="application/xml",
            status_code=e.status_code,
        )
    except Exception as e:
        logger.error("S3 PUT error: %s", e, exc_info=True)
        return Response(
            content=build_error("InternalError", "Failed to upload file"),
            media_type="application/xml",
            status_code=500,
        )


async def _copy_object(dst_key: str, request: Request) -> Response:
    """Handle CopyObject: PUT with x-amz-copy-source header."""
    copy_source = urllib.parse.unquote(request.headers["x-amz-copy-source"])
    src_key = copy_source.lstrip("/")
    if src_key.startswith("storage/"):
        src_key = src_key[len("storage/"):]

    src = safe_path(src_key)
    dst = safe_path(dst_key)

    if not src.exists():
        return Response(
            content=build_error("NoSuchKey", "The source key does not exist", src_key),
            status_code=404,
            media_type="application/xml",
        )

    same_file = src.resolve() == dst.resolve()
    if not same_file:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))

    mtime_raw = request.headers.get("x-amz-meta-mtime")
    if mtime_raw:
        mtime = datetime.fromtimestamp(float(mtime_raw), tz=timezone.utc)
        mtime_ts = mtime.timestamp()
        os.utime(dst, (mtime_ts, mtime_ts))
    else:
        mtime = datetime.fromtimestamp(dst.stat().st_mtime, tz=timezone.utc)

    if same_file:
        row = await db.get_file_row(str(dst.relative_to(get_storage())))
        md5 = row["md5"] if row and row.get("md5") else hashlib.md5(dst.read_bytes()).hexdigest()
        checksum = row.get("checksum", "") if row else ""
    else:
        h_sha256 = hashlib.sha256()
        h_md5 = hashlib.md5()
        with open(dst, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h_sha256.update(chunk)
                h_md5.update(chunk)
        md5 = h_md5.hexdigest()
        checksum = h_sha256.hexdigest()

    if db.get_pool() is not None:
        rel = str(dst.relative_to(get_storage()))
        await db.upsert_file(
            path=rel,
            filename=dst.name,
            extension=dst.suffix.lstrip(".").lower(),
            size=dst.stat().st_size,
            checksum=checksum,
            is_dir=False,
            modified_at=mtime,
            md5=md5,
        )
        invalidate_listing_cache(rel)

    logger.info(
        "S3 CopyObject [%02d:%02d:%02d]: %s -> %s (mtime=%s)",
        mtime.hour, mtime.minute, mtime.second, src_key, dst_key, mtime_raw,
    )

    return Response(
        content=build_copy_object(md5, mtime),
        status_code=200,
        media_type="application/xml",
    )


@router.get("/storage/{key:path}")
async def get_object(key: str, request: Request, _auth: str = Depends(verify_s3_auth)):
    """
    GET /s3/storage/{key} — Download a file or list parts.
    GET /s3/storage/{key}?uploadId=X — ListParts for multipart upload.
    """
    try:
        # Check for multipart list-parts first
        upload_id = request.query_params.get("uploadId")
        if upload_id:
            return await _list_parts(key, upload_id)

        # Check if this is bucket listing (no key or empty key)
        if not key or key == "":
            return await _list_objects_v2(request)

        target = safe_path(key)
        if not target.exists():
            return Response(
                content=build_error("NoSuchKey", "The specified key does not exist.", key=key),
                media_type="application/xml",
                status_code=404,
            )
        if target.is_dir():
            return Response(
                content=build_error("InvalidArgument", "Key is a directory"),
                media_type="application/xml",
                status_code=400,
            )
        return FileResponse(target)
    except HTTPException as e:
        return Response(
            content=build_error("AccessDenied", str(e.detail), key=key),
            media_type="application/xml",
            status_code=e.status_code,
        )
    except Exception as e:
        logger.error("S3 GET error: %s", e, exc_info=True)
        return Response(
            content=build_error("InternalError", "Failed to download file"),
            media_type="application/xml",
            status_code=500,
        )


@router.head("/storage/{key:path}")
async def head_object(key: str, _auth: str = Depends(verify_s3_auth)):
    """HEAD /s3/storage/{key} — Get file metadata."""
    try:
        target = safe_path(key)
        if not target.exists():
            return Response(status_code=404)
        if target.is_dir():
            return Response(status_code=404)

        stat = target.stat()
        return Response(
            status_code=200,
            headers={
                "Content-Length": str(stat.st_size),
                "Last-Modified": datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).strftime("%a, %d %b %Y %H:%M:%S GMT"),
            },
        )
    except HTTPException:
        return Response(status_code=403)
    except Exception as e:
        logger.error("S3 HEAD error: %s", e, exc_info=True)
        return Response(status_code=500)


@router.delete("/storage/{key:path}")
async def delete_object(key: str, request: Request, _auth: str = Depends(verify_s3_auth)):
    """
    DELETE /s3/storage/{key} — Delete a file or abort multipart upload.

    Handles:
    - DELETE without uploadId: delete file
    - DELETE with uploadId: abort multipart upload
    """
    upload_id = request.query_params.get("uploadId")

    if upload_id:
        # Abort multipart upload
        return await _abort_multipart(key, upload_id)

    # Delete file
    try:
        target = safe_path(key)
        if not target.exists():
            return Response(
                status_code=204,  # S3 returns 204 even if file doesn't exist
            )

        if target.is_dir():
            # Delete directory recursively
            import shutil
            shutil.rmtree(target)
            if db.get_pool() is not None:
                try:
                    rel = str(target.relative_to(get_storage()))
                    await db.delete_directory_rows(rel)
                except Exception:
                    pass
        else:
            # Delete file
            target.unlink()
            if db.get_pool() is not None:
                try:
                    rel = str(target.relative_to(get_storage()))
                    await db.delete_file_row(rel)
                except Exception:
                    pass

        # Invalidate cache
        try:
            rel = str(target.relative_to(get_storage()))
            invalidate_listing_cache(rel)
        except Exception:
            pass

        return Response(status_code=204)
    except HTTPException as e:
        return Response(status_code=e.status_code)
    except Exception as e:
        logger.error("S3 DELETE error: %s", e, exc_info=True)
        return Response(status_code=500)


# ─── Multipart Upload ──────────────────────────────────────────────────────

@router.post("/storage/{key:path}")
async def post_object(key: str, request: Request, _auth: str = Depends(verify_s3_auth)):
    """
    POST /s3/storage/{key} — Initiate or complete multipart upload.

    Handles:
    - POST with ?uploads: InitiateMultipartUpload
    - POST with ?uploadId=X: CompleteMultipartUpload
    - GET with ?uploadId=X: ListParts (handled by separate route)
    """
    uploads_param = request.query_params.get("uploads")
    upload_id = request.query_params.get("uploadId")

    if uploads_param == "":
        # InitiateMultipartUpload
        return await _initiate_multipart(key)
    elif upload_id:
        # CompleteMultipartUpload
        return await _complete_multipart(key, upload_id, request)
    else:
        return Response(
            content=build_error("InvalidArgument", "Invalid POST parameters"),
            media_type="application/xml",
            status_code=400,
        )


# ─── Multipart Helper Functions ────────────────────────────────────────────

async def _initiate_multipart(key: str) -> Response:
    """POST ?uploads — create a new multipart upload."""
    try:
        upload_id = f"s3-{uuid.uuid4().hex}"

        # Create metadata file
        uploads_dir = settings.tus_upload_path
        uploads_dir.mkdir(parents=True, exist_ok=True)

        meta = {
            "upload_id": upload_id,
            "bucket": "storage",
            "key": key,
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
            "parts": {},
        }

        meta_file = uploads_dir / f"{upload_id}.meta"
        with open(meta_file, "w") as f:
            json.dump(meta, f)

        logger.info(f"Initiated multipart upload {upload_id} for {key}")

        return Response(
            content=build_initiate_multipart("storage", key, upload_id),
            media_type="application/xml",
            status_code=200,
        )
    except Exception as e:
        logger.error("Multipart init error: %s", e, exc_info=True)
        return Response(
            content=build_error("InternalError", "Failed to initiate multipart upload"),
            media_type="application/xml",
            status_code=500,
        )


async def _upload_part(key: str, upload_id: str, part_number: int, request: Request) -> Response:
    """PUT ?partNumber=N&uploadId=X — upload a part."""
    try:
        uploads_dir = settings.tus_upload_path
        meta_file = uploads_dir / f"{upload_id}.meta"

        if not meta_file.exists():
            return Response(
                content=build_error("NoSuchUpload", "The specified upload does not exist."),
                media_type="application/xml",
                status_code=404,
            )

        # Stream part body and compute MD5
        part_file = uploads_dir / f"{upload_id}.part.{part_number}"
        size = 0
        h_md5 = hashlib.md5()

        try:
            with open(part_file, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        part_file.unlink(missing_ok=True)
                        return Response(
                            content=build_error("EntityTooLarge", "Your proposed upload exceeds the maximum allowed size"),
                            media_type="application/xml",
                            status_code=400,
                        )
                    f.write(chunk)
                    h_md5.update(chunk)
        except ClientDisconnect:
            part_file.unlink(missing_ok=True)
            logger.warning("Client disconnected during S3 part upload for %s", upload_id)
            return Response(status_code=400)

        # Record part info in metadata — use a per-upload lock to prevent
        # concurrent UploadPart requests from overwriting each other's writes
        etag = f'"{h_md5.hexdigest()}"'
        if upload_id not in _upload_locks:
            _upload_locks[upload_id] = asyncio.Lock()
        async with _upload_locks[upload_id]:
            with open(meta_file) as f:
                meta = json.load(f)
            meta["parts"][str(part_number)] = {
                "size": size,
                "etag": etag,
            }
            with open(meta_file, "w") as f:
                json.dump(meta, f)

        logger.info(f"Uploaded part {part_number} for {upload_id} ({size} bytes)")

        return Response(
            status_code=200,
            headers={"ETag": etag},
        )
    except Exception as e:
        logger.error("Part upload error: %s", e, exc_info=True)
        return Response(
            content=build_error("InternalError", "Failed to upload part"),
            media_type="application/xml",
            status_code=500,
        )


async def _complete_multipart(key: str, upload_id: str, request: Request) -> Response:
    """POST ?uploadId=X — complete the multipart upload."""
    try:
        uploads_dir = settings.tus_upload_path
        meta_file = uploads_dir / f"{upload_id}.meta"

        if not meta_file.exists():
            return Response(
                content=build_error("NoSuchUpload", "The specified upload does not exist."),
                media_type="application/xml",
                status_code=404,
            )

        # Read metadata
        with open(meta_file) as f:
            meta = json.load(f)

        # Parse CompleteMultipartUpload XML from request body
        body = await request.body()
        logger.warning("CompleteMultipartUpload body (%d bytes): %r", len(body), body[:500] if body else b"")
        try:
            root = ET.fromstring(body)
            parts_from_request = {}

            # Try with S3 namespace first, then fallback to no namespace
            NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
            parts_elems = root.findall(f".//{NS}Part")
            if not parts_elems:
                parts_elems = root.findall(".//Part")


            for part_elem in parts_elems:
                # Try namespace-qualified first, then fallback
                pn_text = part_elem.findtext(f"{NS}PartNumber") or part_elem.findtext("PartNumber")
                etag_text = part_elem.findtext(f"{NS}ETag") or part_elem.findtext("ETag")
                if pn_text and etag_text:
                    part_number = int(pn_text)
                    parts_from_request[part_number] = etag_text
        except Exception as e:
            logger.warning("Failed to parse CompleteMultipartUpload XML: %s", e)
            return Response(
                content=build_error("InvalidArgument", "Invalid request body"),
                media_type="application/xml",
                status_code=400,
            )

        # Verify all parts exist and ETags match
        for part_num, etag in parts_from_request.items():
            part_key = str(part_num)
            if part_key not in meta["parts"]:
                return Response(
                    content=build_error("InvalidPartOrder", f"Part {part_num} not found"),
                    media_type="application/xml",
                    status_code=400,
                )
            # Normalize ETags by stripping quotes (client may send with or without quotes)
            stored_etag = meta["parts"][part_key]["etag"].strip('"')
            client_etag = etag.strip('"')
            if stored_etag != client_etag:
                logger.debug(
                    "ETag mismatch for part %d: stored=%s, client=%s",
                    part_num, stored_etag, client_etag
                )
                return Response(
                    content=build_error("InvalidPartOrder", f"ETag mismatch for part {part_num}"),
                    media_type="application/xml",
                    status_code=400,
                )

        # Concatenate parts to temp file with incremental SHA256
        target = safe_path(key)
        target.parent.mkdir(parents=True, exist_ok=True)

        temp_file = target.parent / f"{target.name}.tmp"
        h_sha256 = hashlib.sha256()
        h_md5_combined = hashlib.md5()
        total_size = 0

        try:
            with open(temp_file, "wb") as f:
                for part_num in sorted(parts_from_request.keys()):
                    part_file = uploads_dir / f"{upload_id}.part.{part_num}"
                    if not part_file.exists():
                        temp_file.unlink(missing_ok=True)
                        return Response(
                            content=build_error("InvalidPartOrder", f"Part {part_num} file not found"),
                            media_type="application/xml",
                            status_code=400,
                        )

                    with open(part_file, "rb") as pf:
                        while chunk := pf.read(8192):
                            f.write(chunk)
                            h_sha256.update(chunk)
                            h_md5_combined.update(chunk)
                            total_size += len(chunk)
        except Exception as e:
            temp_file.unlink(missing_ok=True)
            logger.error("Failed to concatenate parts: %s", e, exc_info=True)
            return Response(
                content=build_error("InternalError", "Failed to concatenate parts"),
                media_type="application/xml",
                status_code=500,
            )

        # Move temp file to final destination (atomic)
        try:
            import shutil
            shutil.move(str(temp_file), str(target))
        except Exception as e:
            temp_file.unlink(missing_ok=True)
            logger.error("Failed to move file to final location: %s", e, exc_info=True)
            return Response(
                content=build_error("InternalError", "Failed to finalize upload"),
                media_type="application/xml",
                status_code=500,
            )

        # Build ETag for multipart response and storage
        # For multipart uploads, ETag is "md5-of-part-md5s-partcount"
        multipart_etag = f"{h_md5_combined.hexdigest()}-{len(parts_from_request)}"

        # Upsert to DB
        if db.get_pool() is not None:
            try:
                rel_path = str(target.relative_to(get_storage()))
                mtime = datetime.fromtimestamp(target.stat().st_mtime, tz=timezone.utc)
                ext = target.suffix.lstrip(".").lower() or None
                await db.upsert_file(
                    path=rel_path,
                    filename=target.name,
                    extension=ext,
                    size=total_size,
                    checksum=h_sha256.hexdigest(),
                    is_dir=False,
                    modified_at=mtime,
                    md5=multipart_etag,
                )
            except Exception:
                logger.warning("DB upsert failed after S3 multipart completion of %s", key, exc_info=True)

        # Generate thumbnail
        try:
            rel = str(target.relative_to(get_storage()))
            invalidate_listing_cache(rel)
            ext = target.suffix.lstrip(".").lower()
            thumbnails.record_upload()
            if thumbnails.is_thumbable(ext) and not thumbnails.should_defer_thumbnail():
                try:
                    thumbnails.invalidate_thumbnail(rel)
                    await thumbnails.ensure_thumbnail(rel, ext)
                except Exception:
                    logger.warning("Thumbnail generation failed for %s", key, exc_info=True)
            elif thumbnails.is_thumbable(ext):
                thumbnails.invalidate_thumbnail(rel)
        except Exception:
            pass

        # Clean up parts, metadata, and lock
        for part_num in parts_from_request.keys():
            part_file = uploads_dir / f"{upload_id}.part.{part_num}"
            part_file.unlink(missing_ok=True)
        meta_file.unlink(missing_ok=True)
        _upload_locks.pop(upload_id, None)

        # Format ETag for response (add quotes)
        etag = f'"{multipart_etag}"'

        logger.info(f"Completed multipart upload {upload_id} to {key}")

        return Response(
            content=build_complete_multipart("storage", key, etag, location=f"/s3/storage/{key}"),
            media_type="application/xml",
            status_code=200,
        )
    except Exception as e:
        logger.error("Multipart complete error: %s", e, exc_info=True)
        return Response(
            content=build_error("InternalError", "Failed to complete multipart upload"),
            media_type="application/xml",
            status_code=500,
        )


async def _abort_multipart(key: str, upload_id: str) -> Response:
    """DELETE ?uploadId=X — abort the multipart upload."""
    try:
        uploads_dir = settings.tus_upload_path
        meta_file = uploads_dir / f"{upload_id}.meta"

        if not meta_file.exists():
            return Response(status_code=204)  # S3 returns 204 even if not found

        # Read metadata to find all parts
        with open(meta_file) as f:
            meta = json.load(f)

        # Delete all part files
        for part_num in meta.get("parts", {}).keys():
            part_file = uploads_dir / f"{upload_id}.part.{part_num}"
            part_file.unlink(missing_ok=True)

        # Delete metadata and lock
        meta_file.unlink(missing_ok=True)
        _upload_locks.pop(upload_id, None)

        logger.info(f"Aborted multipart upload {upload_id}")

        return Response(status_code=204)
    except Exception as e:
        logger.error("Multipart abort error: %s", e, exc_info=True)
        return Response(status_code=500)


async def _list_parts(key: str, upload_id: str) -> Response:
    """GET ?uploadId=X — list all parts of a multipart upload."""
    try:
        uploads_dir = settings.tus_upload_path
        meta_file = uploads_dir / f"{upload_id}.meta"

        if not meta_file.exists():
            return Response(
                content=build_error("NoSuchUpload", "The specified upload does not exist."),
                media_type="application/xml",
                status_code=404,
            )

        with open(meta_file) as f:
            meta = json.load(f)

        # Build parts list
        parts = []
        for part_num_str, part_info in sorted(meta.get("parts", {}).items(), key=lambda x: int(x[0])):
            parts.append({
                "part_number": int(part_num_str),
                "size": part_info["size"],
                "etag": part_info["etag"],
                "modified_at": datetime.now(tz=timezone.utc),
            })

        return Response(
            content=build_list_parts("storage", key, upload_id, parts),
            media_type="application/xml",
            status_code=200,
        )
    except Exception as e:
        logger.error("List parts error: %s", e, exc_info=True)
        return Response(
            content=build_error("InternalError", "Failed to list parts"),
            media_type="application/xml",
            status_code=500,
        )


async def _list_objects_v2(request: Request) -> Response:
    """GET /s3/storage?list-type=2[&prefix=...&delimiter=...] — list objects."""
    try:
        raw_prefix = request.query_params.get("prefix", "")
        delimiter = request.query_params.get("delimiter", "/")
        max_keys = int(request.query_params.get("max-keys", "1000"))

        # Separate raw prefix (for XML response) from normalized prefix (for DB queries)
        prefix_for_db = raw_prefix.lstrip("/").rstrip("/")

        # Decode keyset cursor from continuation-token query param
        continuation_tok = request.query_params.get("continuation-token", "")
        after_key = ""
        if continuation_tok:
            try:
                after_key = base64.urlsafe_b64decode(
                    continuation_tok.encode() + b"=="   # safe re-padding
                ).decode()
            except Exception:
                after_key = ""

        # Get listing from DB
        if not delimiter:
            # Flat listing (no delimiter): recursively list all files under prefix
            objects = await db.list_all_under(
                prefix_for_db, after_key=after_key, limit=max_keys + 1
            ) if prefix_for_db else await db.list_all_under(
                "", after_key=after_key, limit=max_keys + 1
            )

            # Check truncation: we fetched max_keys + 1 to detect if more results exist
            is_truncated = len(objects) > max_keys
            if is_truncated:
                objects = objects[:max_keys]

            # Compute next continuation token: base64-encode the last object's path
            next_token: str | None = None
            if is_truncated:
                next_token = base64.urlsafe_b64encode(
                    objects[-1]["path"].encode()
                ).decode().rstrip("=")

            key_count = len(objects)
            common_prefixes = []
        else:
            # Delimited listing: only direct children
            if prefix_for_db:
                items = await db.list_directory(prefix_for_db)
            else:
                items = await db.list_directory("")

            # Split into objects (files) and common_prefixes (directories)
            objects = []
            common_prefixes = set()

            for item in items:
                if item["is_dir"]:
                    # Add as common prefix (with trailing slash)
                    prefix_key = item["path"] + "/" if not item["path"].endswith("/") else item["path"]
                    common_prefixes.add(prefix_key)
                else:
                    objects.append(item)

            # Convert to list and sort
            common_prefixes = sorted(list(common_prefixes))

            # Apply max_keys and truncation (delimited path does not support pagination yet)
            is_truncated = len(objects) > max_keys
            if is_truncated:
                objects = objects[:max_keys]

            next_token = None
            key_count = len(objects) + len(common_prefixes)

        # Build response
        return Response(
            content=build_list_objects_v2(
                bucket="storage",
                prefix=raw_prefix if raw_prefix else "",
                delimiter=delimiter if delimiter else None,
                objects=objects,
                common_prefixes=common_prefixes,
                key_count=key_count,
                max_keys=max_keys,
                is_truncated=is_truncated,
                next_continuation_token=next_token,
            ),
            media_type="application/xml",
            status_code=200,
        )
    except Exception as e:
        logger.error("ListObjectsV2 error: %s", e, exc_info=True)
        return Response(
            content=build_error("InternalError", "Failed to list objects"),
            media_type="application/xml",
            status_code=500,
        )
