"""File API: GET, PUT, DELETE /files/*, POST /files/move, GET /health."""
import hashlib
import logging
import mimetypes
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse

from pythowncloud.auth import verify_api_key_or_session
from pythowncloud.models import DirectoryListing, FileInfo, MoveRequest, UploadResponse
from pythowncloud.helpers import get_storage, safe_path, file_info
from pythowncloud.cache import _listing_cache, invalidate_listing_cache
import pythowncloud.db as db
import pythowncloud.thumbnails as thumbnails

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health():
    storage = get_storage()
    return {
        "status": "ok",
        "storage": str(storage),
        "writable": os.access(storage, os.W_OK),
        "db": db.get_pool() is not None,
    }


@router.get("/files/", response_model=DirectoryListing)
async def list_root(_key: str = Depends(verify_api_key_or_session)):
    return await get_file("", _key=_key)


@router.get("/files/{file_path:path}", response_model=None)
async def get_file(file_path: str, _key: str = Depends(verify_api_key_or_session)):
    target = safe_path(file_path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Not found")
    if target.is_dir():
        rel = str(target.relative_to(get_storage()))
        db_rows = await db.list_directory(rel)
        if db_rows:
            items = [
                FileInfo(
                    name=r["filename"],
                    path=r["path"],
                    size=r["size"],
                    is_dir=r["is_dir"],
                    modified=r["modified_at"],
                    checksum=r["checksum"] or None,
                )
                for r in db_rows
            ]
            return DirectoryListing(path=rel, items=items, total=len(items))
        items = []
        for child in sorted(target.iterdir()):
            if child.name.startswith("."):
                continue
            try:
                items.append(file_info(child))
            except PermissionError:
                continue
        return DirectoryListing(
            path=str(target.relative_to(get_storage())),
            items=items,
            total=len(items),
        )
    media_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    return FileResponse(path=str(target), media_type=media_type, filename=target.name)


@router.put("/files/{file_path:path}")
async def upload_file(
    file_path: str,
    file: UploadFile,
    _auth: str = Depends(verify_api_key_or_session),
):
    target = safe_path(file_path)
    if target.is_dir():
        raise HTTPException(status_code=400, detail="Cannot overwrite a directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    h = hashlib.sha256()
    with open(target, "wb") as f:
        while chunk := await file.read(8192):
            f.write(chunk)
            h.update(chunk)
            size += len(chunk)
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
            logger.warning("DB upsert failed after upload of %s", file_path, exc_info=True)
    rel = str(target.relative_to(get_storage()))
    ext_lower = target.suffix.lstrip(".").lower()
    if thumbnails.is_thumbable(ext_lower):
        try:
            thumbnails.invalidate_thumbnail(rel)
            await thumbnails.ensure_thumbnail(rel, ext_lower)
        except Exception:
            logger.warning("Thumbnail generation failed for %s", file_path, exc_info=True)
    invalidate_listing_cache(rel)
    return UploadResponse(
        path=str(target.relative_to(get_storage())),
        size=size,
        checksum=h.hexdigest(),
        message="uploaded",
    )


@router.delete("/files/{file_path:path}")
async def delete_file(file_path: str, _auth: str = Depends(verify_api_key_or_session)):
    target = safe_path(file_path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Not found")
    if target.is_dir():
        raise HTTPException(
            status_code=400,
            detail="Directory deletion not allowed via API (safety)",
        )
    size = target.stat().st_size
    rel = str(target.relative_to(get_storage()))
    target.unlink()
    try:
        thumbnails.invalidate_thumbnail(rel)
    except Exception:
        logger.warning("Thumbnail cleanup failed for %s", file_path, exc_info=True)
    invalidate_listing_cache(rel)
    if db.get_pool() is not None:
        try:
            await db.delete_file_row(str(target.relative_to(get_storage())))
        except Exception:
            logger.warning("DB delete failed after removal of %s", file_path, exc_info=True)
    return {"path": file_path, "size": size, "message": "deleted"}


@router.post("/files/move")
async def move_file(req: MoveRequest, _auth: str = Depends(verify_api_key_or_session)):
    source_path = req.source.strip("/")
    dest_path = req.destination.strip("/")
    source = safe_path(source_path)
    dest = safe_path(dest_path)
    if not source.exists():
        raise HTTPException(status_code=404, detail="Source not found")
    if source.resolve() == dest.resolve():
        return {"source": source_path, "destination": dest_path, "message": "same path"}
    if dest.exists():
        raise HTTPException(status_code=409, detail="Destination already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(dest))
    storage = get_storage()
    rel_source = str(source.relative_to(storage))
    rel_dest = str(dest.relative_to(storage))
    invalidate_listing_cache(rel_source)
    invalidate_listing_cache(rel_dest)
    if db.get_pool() is not None:
        try:
            if source.is_dir():
                await db.move_directory_rows(rel_source, rel_dest)
            else:
                await db.move_file_row(rel_source, rel_dest)
        except Exception:
            logger.warning("DB move failed for %s -> %s", rel_source, rel_dest, exc_info=True)
    try:
        ext = Path(rel_dest).suffix.lstrip(".").lower()
        if thumbnails.is_thumbable(ext):
            thumbnails.move_thumbnail(rel_source, rel_dest)
    except Exception:
        logger.warning("Thumbnail move failed for %s -> %s", rel_source, rel_dest, exc_info=True)
    return {"source": source_path, "destination": dest_path, "message": "moved"}
