"""
WebDAV Server — endpoints under /dav/ for native file manager clients.
Reuses existing file operation logic (safe_path, upsert_file, delete_file_row, etc.)
Authentication uses HTTP Basic Auth (via verify_basic_auth).
"""

import hashlib
import logging
import mimetypes
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from starlette.requests import ClientDisconnect

from pythowncloud.auth import verify_basic_auth
from pythowncloud.helpers import get_storage, safe_path
from pythowncloud.cache import invalidate_listing_cache
from pythowncloud.webdav_xml import build_propfind_response
import pythowncloud.db as db
import pythowncloud.thumbnails as thumbnails

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dav")


@router.api_route("/", methods=["OPTIONS"])
@router.api_route("/{path:path}", methods=["OPTIONS"])
async def options_handler(path: str = "", _auth: str = Depends(verify_basic_auth)):
    """OPTIONS /dav/* — return DAV capabilities."""
    return Response(
        status_code=200,
        headers={
            "Allow": "OPTIONS, GET, HEAD, PUT, DELETE, MKCOL, MOVE, COPY, PROPFIND",
            "DAV": "1, 2",
            "MS-Author-Via": "DAV",
        },
    )


async def _build_propfind_response(
    rel_path: str,
    depth_header: str,
) -> Response:
    """
    Helper to build PROPFIND response for a path.
    rel_path: storage-relative path (or empty for root)
    depth_header: "0" or "1"
    """
    # Validate path
    target = safe_path(rel_path) if rel_path else get_storage()
    if not target.exists():
        raise HTTPException(status_code=404, detail="Not found")

    # Get metadata for the target itself
    if target.is_dir():
        # Directory: fetch from DB
        rel = str(target.relative_to(get_storage())) if target != get_storage() else ""
        target_row = await db.get_file_row(rel) if rel else None
        if not target_row:
            # Fallback: build a minimal row for the root or missing dir
            target_row = {
                "path": rel,
                "filename": target.name or "root",
                "size": 0,
                "is_dir": 1,
                "modified_at": datetime.fromtimestamp(
                    target.stat().st_mtime, tz=timezone.utc
                ),
                "checksum": "",
            }
    else:
        # File: fetch from DB
        rel = str(target.relative_to(get_storage()))
        target_row = await db.get_file_row(rel)
        if not target_row:
            # Fallback: build from filesystem
            mtime = datetime.fromtimestamp(target.stat().st_mtime, tz=timezone.utc)
            target_row = {
                "path": rel,
                "filename": target.name,
                "size": target.stat().st_size,
                "is_dir": 0,
                "modified_at": mtime,
                "checksum": "",
            }

    # Determine the href for the target
    if rel_path:
        base_href = f"/dav/{rel_path}"
        if target.is_dir() and not base_href.endswith("/"):
            base_href += "/"
    else:
        base_href = "/dav"

    # Build response
    if depth_header == "0":
        # Return only the target itself
        xml = build_propfind_response(
            items=[],
            base_href=base_href,
            include_self=True,
            self_item=target_row,
        )
    else:  # depth_header == "1"
        # Return target + direct children
        if target.is_dir():
            rel_for_db = str(target.relative_to(get_storage())) if target != get_storage() else ""
            children = await db.list_directory(rel_for_db)
        else:
            children = []

        xml = build_propfind_response(
            items=children,
            base_href=base_href,
            include_self=True,
            self_item=target_row,
        )

    return Response(
        content=xml,
        status_code=207,
        media_type="application/xml; charset=utf-8",
    )


@router.api_route("/", methods=["PROPFIND"])
async def propfind_root(
    request: Request,
    _auth: str = Depends(verify_basic_auth),
):
    """PROPFIND /dav/ (root directory)."""
    depth_header = request.headers.get("Depth", "0").strip()
    if depth_header not in ("0", "1", "infinity"):
        raise HTTPException(status_code=400, detail="Invalid Depth header")
    if depth_header == "infinity":
        raise HTTPException(status_code=403, detail="Depth: infinity not allowed")

    return await _build_propfind_response("", depth_header)


@router.api_route("/{path:path}", methods=["PROPFIND"])
async def propfind_route(
    path: str,
    request: Request,
    _auth: str = Depends(verify_basic_auth),
):
    """PROPFIND /dav/{path} endpoint."""
    depth_header = request.headers.get("Depth", "0").strip()
    if depth_header not in ("0", "1", "infinity"):
        raise HTTPException(status_code=400, detail="Invalid Depth header")
    if depth_header == "infinity":
        raise HTTPException(status_code=403, detail="Depth: infinity not allowed")

    return await _build_propfind_response(path, depth_header)


@router.get("/")
async def get_root(_auth: str = Depends(verify_basic_auth)):
    """GET /dav/ — list root directory (return 403 to prevent browser access)."""
    raise HTTPException(status_code=403, detail="Use PROPFIND for directory listing")


@router.get("/{file_path:path}")
async def get_file(
    file_path: str,
    _auth: str = Depends(verify_basic_auth),
):
    """GET /dav/{file_path} — download a file."""
    target = safe_path(file_path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Not found")
    if target.is_dir():
        raise HTTPException(status_code=400, detail="Cannot GET a directory")

    media_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    return FileResponse(path=str(target), media_type=media_type, filename=target.name)


@router.head("/{file_path:path}")
async def head_file(
    file_path: str,
    _auth: str = Depends(verify_basic_auth),
):
    """HEAD /dav/{file_path} — get file metadata without body."""
    target = safe_path(file_path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Not found")
    if target.is_dir():
        raise HTTPException(status_code=400, detail="Cannot HEAD a directory")

    stat = target.stat()
    media_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    return Response(
        status_code=200,
        headers={
            "Content-Length": str(stat.st_size),
            "Content-Type": media_type,
            "Last-Modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime(
                "%a, %d %b %Y %H:%M:%S GMT"
            ),
        },
    )


@router.put("/{file_path:path}")
async def upload_file(
    file_path: str,
    request: Request,
    _auth: str = Depends(verify_basic_auth),
):
    """PUT /dav/{file_path} — upload a file (WebDAV client sends raw binary)."""
    target = safe_path(file_path)
    if target.is_dir():
        raise HTTPException(status_code=400, detail="Cannot overwrite a directory")

    target.parent.mkdir(parents=True, exist_ok=True)

    size = 0
    h = hashlib.sha256()
    try:
        with open(target, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
                h.update(chunk)
                size += len(chunk)
    except ClientDisconnect:
        logger.warning("Client disconnected during WebDAV upload of %s", file_path)
        target.unlink(missing_ok=True)
        return Response(status_code=400, detail="Client disconnected during upload")

    # Update database
    if db.get_pool() is not None:
        try:
            mtime = datetime.fromtimestamp(target.stat().st_mtime, tz=timezone.utc)
            ext = target.suffix.lstrip(".").lower() or None
            await db.upsert_file(
                path=str(target.relative_to(get_storage())),
                filename=target.name,
                extension=ext,
                size=size,
                checksum=h.hexdigest(),
                is_dir=False,
                modified_at=mtime,
            )
        except Exception:
            logger.warning("DB upsert failed after WebDAV upload of %s", file_path, exc_info=True)

    # Generate thumbnail if needed
    rel = str(target.relative_to(get_storage()))
    ext_lower = target.suffix.lstrip(".").lower()
    thumbnails.record_upload()
    if thumbnails.is_thumbable(ext_lower) and not thumbnails.should_defer_thumbnail():
        try:
            thumbnails.invalidate_thumbnail(rel)
            await thumbnails.ensure_thumbnail(rel, ext_lower)
        except Exception:
            logger.warning("Thumbnail generation failed for %s", file_path, exc_info=True)
    elif thumbnails.is_thumbable(ext_lower):
        thumbnails.invalidate_thumbnail(rel)

    invalidate_listing_cache(rel)
    return Response(status_code=201)


@router.delete("/{file_path:path}")
async def delete_item(
    file_path: str,
    _auth: str = Depends(verify_basic_auth),
):
    """DELETE /dav/{file_path} — delete a file or directory."""
    target = safe_path(file_path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Not found")

    rel = str(target.relative_to(get_storage()))

    if target.is_dir():
        # Delete directory and all contents
        shutil.rmtree(target)
        if db.get_pool() is not None:
            try:
                await db.delete_directory_rows(rel)
            except Exception:
                logger.warning("DB delete failed after WebDAV dir removal of %s", file_path, exc_info=True)
    else:
        # Delete file
        target.unlink()
        try:
            thumbnails.invalidate_thumbnail(rel)
        except Exception:
            logger.warning("Thumbnail cleanup failed for %s", file_path, exc_info=True)
        if db.get_pool() is not None:
            try:
                await db.delete_file_row(rel)
            except Exception:
                logger.warning("DB delete failed after WebDAV file removal of %s", file_path, exc_info=True)

    invalidate_listing_cache(rel)
    return Response(status_code=204)


@router.api_route("/{dir_path:path}", methods=["MKCOL"])
async def make_directory(
    dir_path: str,
    _auth: str = Depends(verify_basic_auth),
):
    """MKCOL /dav/{dir_path} — create a directory."""
    target = safe_path(dir_path)

    # Check if parent exists (WebDAV spec: parent must exist)
    if not target.parent.exists():
        raise HTTPException(status_code=409, detail="Parent directory does not exist")

    # Create directory if it doesn't exist
    target.mkdir(exist_ok=True)

    # Update database
    if db.get_pool() is not None:
        rel = str(target.relative_to(get_storage()))
        try:
            mtime = datetime.fromtimestamp(target.stat().st_mtime, tz=timezone.utc)
            await db.upsert_file(
                path=rel,
                filename=target.name,
                extension=None,
                size=0,
                checksum="",
                is_dir=True,
                modified_at=mtime,
            )
        except Exception:
            logger.warning("DB upsert failed after WebDAV MKCOL of %s", dir_path, exc_info=True)

    rel = str(target.relative_to(get_storage()))
    invalidate_listing_cache(rel)
    return Response(status_code=201)


@router.api_route("/{file_path:path}", methods=["MOVE"])
async def move_item(
    file_path: str,
    request: Request,
    _auth: str = Depends(verify_basic_auth),
):
    """MOVE /dav/{file_path} — move/rename a file or directory (Destination header)."""
    source = safe_path(file_path)
    if not source.exists():
        raise HTTPException(status_code=404, detail="Source not found")

    # Parse Destination header
    destination_header = request.headers.get("Destination")
    if not destination_header:
        raise HTTPException(status_code=400, detail="Missing Destination header")

    # Strip scheme/host/port from destination URL to get path
    # Example: "http://localhost:8000/dav/new/path" -> "/dav/new/path"
    parsed = urlparse(destination_header)
    dest_path = parsed.path
    if not dest_path.startswith("/dav/"):
        raise HTTPException(status_code=400, detail="Invalid Destination path")
    dest_rel_path = dest_path[5:].lstrip("/")

    dest = safe_path(dest_rel_path)

    # Check if source and dest are the same
    if source.resolve() == dest.resolve():
        return Response(status_code=204)

    # Check if destination already exists
    overwrite_header = request.headers.get("Overwrite", "T").upper()
    if dest.exists():
        if overwrite_header != "T":
            raise HTTPException(status_code=412, detail="Destination exists (Overwrite: F)")

    # Ensure parent of destination exists
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Move on filesystem
    shutil.move(str(source), str(dest))

    # Update database
    storage = get_storage()
    rel_source = str(source.relative_to(storage))
    rel_dest = str(dest.relative_to(storage))

    if db.get_pool() is not None:
        try:
            if source.is_dir():
                await db.move_directory_rows(rel_source, rel_dest)
            else:
                await db.move_file_row(rel_source, rel_dest)
        except Exception:
            logger.warning("DB move failed for WebDAV %s -> %s", rel_source, rel_dest, exc_info=True)

    # Move thumbnail if applicable
    try:
        ext = Path(rel_dest).suffix.lstrip(".").lower()
        if thumbnails.is_thumbable(ext):
            thumbnails.move_thumbnail(rel_source, rel_dest)
    except Exception:
        logger.warning("Thumbnail move failed for %s -> %s", rel_source, rel_dest, exc_info=True)

    invalidate_listing_cache(rel_source)
    invalidate_listing_cache(rel_dest)

    return Response(status_code=201)


@router.api_route("/{file_path:path}", methods=["COPY"])
async def copy_item(
    file_path: str,
    request: Request,
    _auth: str = Depends(verify_basic_auth),
):
    """COPY /dav/{file_path} — copy a file or directory (Destination header)."""
    source = safe_path(file_path)
    if not source.exists():
        raise HTTPException(status_code=404, detail="Source not found")

    # Parse Destination header
    destination_header = request.headers.get("Destination")
    if not destination_header:
        raise HTTPException(status_code=400, detail="Missing Destination header")

    # Strip scheme/host/port from destination URL
    parsed = urlparse(destination_header)
    dest_path = parsed.path
    if not dest_path.startswith("/dav/"):
        raise HTTPException(status_code=400, detail="Invalid Destination path")
    dest_rel_path = dest_path[5:].lstrip("/")

    dest = safe_path(dest_rel_path)

    # Check if destination already exists
    overwrite_header = request.headers.get("Overwrite", "T").upper()
    if dest.exists():
        if overwrite_header != "T":
            raise HTTPException(status_code=412, detail="Destination exists (Overwrite: F)")
        if dest.is_dir():
            shutil.rmtree(dest)
        else:
            dest.unlink()

    # Ensure parent directory exists
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Copy on filesystem
    if source.is_dir():
        shutil.copytree(str(source), str(dest))
    else:
        shutil.copy2(str(source), str(dest))

    # Update database
    storage = get_storage()
    rel_dest = str(dest.relative_to(storage))

    if db.get_pool() is not None:
        try:
            if dest.is_dir():
                # For directory copies, upsert the top-level dir row
                mtime = datetime.fromtimestamp(dest.stat().st_mtime, tz=timezone.utc)
                await db.upsert_file(
                    path=rel_dest,
                    filename=dest.name,
                    extension=None,
                    size=0,
                    checksum="",
                    is_dir=True,
                    modified_at=mtime,
                )
            else:
                # For file copies, upsert with new checksum
                mtime = datetime.fromtimestamp(dest.stat().st_mtime, tz=timezone.utc)
                size = dest.stat().st_size
                checksum = ""
                try:
                    h = hashlib.sha256()
                    with open(dest, "rb") as f:
                        while chunk := f.read(8192):
                            h.update(chunk)
                    checksum = h.hexdigest()
                except Exception:
                    pass
                ext = dest.suffix.lstrip(".").lower() or None
                await db.upsert_file(
                    path=rel_dest,
                    filename=dest.name,
                    extension=ext,
                    size=size,
                    checksum=checksum,
                    is_dir=False,
                    modified_at=mtime,
                )
        except Exception:
            logger.warning("DB upsert failed after WebDAV COPY to %s", rel_dest, exc_info=True)

    invalidate_listing_cache(rel_dest)
    return Response(status_code=201)
