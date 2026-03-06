"""
TUS Resumable Upload Server — endpoints under /tus/ for chunked uploads.
TUS protocol v1.0.0 with creation and termination extensions.
Authentication uses HTTP Basic Auth (via verify_basic_auth).
"""

import hashlib
import json
import logging
import uuid
from base64 import b64decode
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from pythowncloud.auth import verify_basic_auth
from pythowncloud.config import settings
from pythowncloud.helpers import get_storage, safe_path
from pythowncloud.cache import invalidate_listing_cache
import pythowncloud.db as db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tus")

# TUS protocol version and extensions
TUS_VERSION = "1.0.0"
TUS_EXTENSIONS = "creation,termination"
TUS_MAX_SIZE = 10 * 1024 * 1024 * 1024  # 10 GB


@router.options("/")
async def tus_options(_auth: str = Depends(verify_basic_auth)):
    """OPTIONS /tus/ — return TUS capabilities."""
    return Response(
        status_code=200,
        headers={
            "Tus-Resumable": TUS_VERSION,
            "Tus-Version": TUS_VERSION,
            "Tus-Extension": TUS_EXTENSIONS,
            "Tus-Max-Size": str(TUS_MAX_SIZE),
        },
    )


@router.post("/")
async def create_upload(
    request: Request,
    _auth: str = Depends(verify_basic_auth),
):
    """
    POST /tus/ — create a new resumable upload.
    Required headers: Tus-Resumable, Upload-Length, Upload-Metadata.
    Returns 201 with Location header pointing to the upload URL.
    """
    # Validate TUS version
    tus_resumable = request.headers.get("Tus-Resumable")
    if not tus_resumable:
        raise HTTPException(status_code=412, detail="Missing Tus-Resumable header")

    # Get upload size
    upload_length = request.headers.get("Upload-Length")
    if not upload_length:
        raise HTTPException(status_code=400, detail="Missing Upload-Length header")

    try:
        total_size = int(upload_length)
        if total_size <= 0 or total_size > TUS_MAX_SIZE:
            raise ValueError("Invalid size")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Upload-Length")

    # Parse metadata (filename and destination path)
    metadata_header = request.headers.get("Upload-Metadata", "")
    filename = "file"
    destination = "upload"

    if metadata_header:
        try:
            # Format: "filename base64value,destination base64value"
            pairs = metadata_header.split(",")
            for pair in pairs:
                key, b64_value = pair.strip().split(" ", 1)
                value = b64decode(b64_value).decode("utf-8")
                if key == "filename":
                    filename = value
                elif key == "destination":
                    destination = value
        except Exception:
            logger.warning("Failed to parse Upload-Metadata header", exc_info=True)

    # Generate upload ID
    upload_id = uuid.uuid4().hex

    # Ensure TUS upload directory exists
    tus_dir = settings.tus_upload_path
    tus_dir.mkdir(parents=True, exist_ok=True)

    # Create metadata file
    meta = {
        "upload_id": upload_id,
        "filename": filename,
        "destination": destination,
        "size": total_size,
        "offset": 0,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    meta_file = tus_dir / f"{upload_id}.meta"
    with open(meta_file, "w") as f:
        json.dump(meta, f)

    # Create empty part file
    part_file = tus_dir / f"{upload_id}.part"
    part_file.touch()

    logger.info(f"Created TUS upload {upload_id} for {filename} ({total_size} bytes)")

    return Response(
        status_code=201,
        headers={
            "Tus-Resumable": TUS_VERSION,
            "Location": f"/tus/{upload_id}",
        },
    )


@router.head("/{upload_id}")
async def get_upload_offset(
    upload_id: str,
    _auth: str = Depends(verify_basic_auth),
):
    """
    HEAD /tus/{upload_id} — get current upload offset.
    Returns Upload-Offset and Upload-Length headers.
    """
    tus_dir = settings.tus_upload_path
    meta_file = tus_dir / f"{upload_id}.meta"

    if not meta_file.exists():
        raise HTTPException(status_code=404, detail="Upload not found")

    try:
        with open(meta_file) as f:
            meta = json.load(f)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to read upload metadata")

    return Response(
        status_code=200,
        headers={
            "Tus-Resumable": TUS_VERSION,
            "Upload-Offset": str(meta["offset"]),
            "Upload-Length": str(meta["size"]),
            "Cache-Control": "no-store",
        },
    )


@router.patch("/{upload_id}")
async def upload_chunk(
    upload_id: str,
    request: Request,
    _auth: str = Depends(verify_basic_auth),
):
    """
    PATCH /tus/{upload_id} — upload a chunk.
    Required headers: Tus-Resumable, Upload-Offset, Content-Type: application/offset+octet-stream.
    Returns 204 No Content on success, 409 if offset mismatch.
    """
    # Validate TUS version
    tus_resumable = request.headers.get("Tus-Resumable")
    if not tus_resumable:
        raise HTTPException(status_code=412, detail="Missing Tus-Resumable header")

    # Get expected offset
    upload_offset = request.headers.get("Upload-Offset")
    if upload_offset is None:
        raise HTTPException(status_code=400, detail="Missing Upload-Offset header")

    try:
        expected_offset = int(upload_offset)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Upload-Offset")

    tus_dir = settings.tus_upload_path
    meta_file = tus_dir / f"{upload_id}.meta"
    part_file = tus_dir / f"{upload_id}.part"

    if not meta_file.exists() or not part_file.exists():
        raise HTTPException(status_code=404, detail="Upload not found")

    try:
        with open(meta_file) as f:
            meta = json.load(f)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to read upload metadata")

    # Verify offset matches
    if meta["offset"] != expected_offset:
        raise HTTPException(
            status_code=409,
            detail=f"Offset mismatch (expected {meta['offset']}, got {expected_offset})",
            headers={"Upload-Offset": str(meta["offset"])},
        )

    # Read chunk from request body and accumulate size
    total_chunk_size = 0
    with open(part_file, "ab") as f:
        async for chunk in request.stream():
            f.write(chunk)
            total_chunk_size += len(chunk)

    # Update offset in metadata
    meta["offset"] += total_chunk_size
    with open(meta_file, "w") as f:
        json.dump(meta, f)

    # Check if upload is complete
    if meta["offset"] == meta["size"]:
        # Assembly: move part file to final destination
        try:
            dest = safe_path(meta["destination"])
            dest.parent.mkdir(parents=True, exist_ok=True)

            # Move the part file to the final destination
            import shutil
            shutil.move(str(part_file), str(dest))

            # Update database
            if db.get_pool() is not None:
                try:
                    # Compute checksum of final file
                    h = hashlib.sha256()
                    with open(dest, "rb") as f:
                        while chunk := f.read(8192):
                            h.update(chunk)

                    mtime = datetime.fromtimestamp(dest.stat().st_mtime, tz=timezone.utc)
                    ext = dest.suffix.lstrip(".").lower() or None
                    await db.upsert_file(
                        path=str(dest.relative_to(get_storage())),
                        filename=dest.name,
                        extension=ext,
                        size=dest.stat().st_size,
                        checksum=h.hexdigest(),
                        is_dir=False,
                        modified_at=mtime,
                    )
                except Exception:
                    logger.warning("DB upsert failed after TUS completion of %s", meta["destination"], exc_info=True)

            # Invalidate listing cache
            try:
                rel = str(dest.relative_to(get_storage()))
                invalidate_listing_cache(rel)
            except Exception:
                pass

            # Delete metadata file
            meta_file.unlink()

            logger.info(f"Completed TUS upload {upload_id} to {meta['destination']}")

        except Exception:
            logger.error(f"Failed to finalize TUS upload {upload_id}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to finalize upload")

    return Response(
        status_code=204,
        headers={
            "Tus-Resumable": TUS_VERSION,
            "Upload-Offset": str(meta["offset"]),
        },
    )


@router.delete("/{upload_id}")
async def delete_upload(
    upload_id: str,
    _auth: str = Depends(verify_basic_auth),
):
    """
    DELETE /tus/{upload_id} — cancel a resumable upload.
    Deletes the partial file and metadata.
    """
    tus_dir = settings.tus_upload_path
    meta_file = tus_dir / f"{upload_id}.meta"
    part_file = tus_dir / f"{upload_id}.part"

    # Delete files if they exist
    meta_file.unlink(missing_ok=True)
    part_file.unlink(missing_ok=True)

    logger.info(f"Deleted TUS upload {upload_id}")

    return Response(
        status_code=204,
        headers={"Tus-Resumable": TUS_VERSION},
    )


async def cleanup_abandoned_uploads():
    """
    Clean up abandoned TUS uploads older than tus_max_age_hours.
    Called periodically or on startup.
    """
    tus_dir = settings.tus_upload_path
    if not tus_dir.exists():
        return

    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=settings.tus_max_age_hours)

    for meta_file in tus_dir.glob("*.meta"):
        try:
            with open(meta_file) as f:
                meta = json.load(f)
            created_at = datetime.fromisoformat(meta["created_at"].replace("Z", "+00:00"))
            if created_at < cutoff:
                part_file = meta_file.with_suffix(".part")
                part_file.unlink(missing_ok=True)
                meta_file.unlink(missing_ok=True)
                logger.info(f"Cleaned up abandoned TUS upload {meta.get('upload_id', 'unknown')}")
        except Exception:
            logger.warning(f"Failed to process {meta_file} during cleanup", exc_info=True)
