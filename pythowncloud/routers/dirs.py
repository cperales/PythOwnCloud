"""Directory API: POST /mkdir/*, DELETE /dirs/*."""
import logging
import shutil
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from pythowncloud.auth import verify_api_key_or_session
from pythowncloud.helpers import get_storage, safe_path
from pythowncloud.cache import invalidate_listing_cache
import pythowncloud.db as db

logger = logging.getLogger(__name__)

router = APIRouter()


@router.delete("/dirs/{dir_path:path}")
async def delete_directory(dir_path: str, _auth: str = Depends(verify_api_key_or_session)):
    target = safe_path(dir_path)

    if not target.exists():
        raise HTTPException(status_code=404, detail="Not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Not a directory")

    rel = str(target.relative_to(get_storage()))
    shutil.rmtree(target)

    invalidate_listing_cache(rel)

    if db.get_pool() is not None:
        try:
            await db.delete_directory_rows(rel)
        except Exception:
            logger.warning("DB delete failed after removal of dir %s", dir_path, exc_info=True)

    return {"path": dir_path, "message": "deleted"}


@router.post("/mkdir/{dir_path:path}")
async def make_directory(dir_path: str, _key: str = Depends(verify_api_key_or_session)):
    target = safe_path(dir_path)
    target.mkdir(parents=True, exist_ok=True)
    rel = str(target.relative_to(get_storage()))
    if db.get_pool() is not None:
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
    invalidate_listing_cache(rel)
    return {"path": dir_path, "message": "created"}
