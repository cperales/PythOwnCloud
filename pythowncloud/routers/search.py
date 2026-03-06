"""API search and scan endpoints."""
import logging
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from pythowncloud.auth import verify_api_key
from pythowncloud.cache import _listing_cache
import pythowncloud.db as db
import pythowncloud.scanner as scanner

logger = logging.getLogger(__name__)

router = APIRouter()

_scan_running = False


@router.get("/api/search")
async def search_files(
    q: str | None = None,
    extension: str | None = None,
    modified_after: datetime | None = None,
    modified_before: datetime | None = None,
    limit: int = 100,
    _key: str = Depends(verify_api_key),
):
    if db.get_pool() is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    results = await db.search_files(q, extension, modified_after, modified_before, limit)
    return {"results": results, "total": len(results)}


@router.post("/api/scan")
async def trigger_scan(
    background_tasks: BackgroundTasks,
    _key: str = Depends(verify_api_key),
):
    global _scan_running
    if _scan_running:
        raise HTTPException(status_code=409, detail="Scan already in progress")
    if db.get_pool() is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    _scan_running = True

    async def _run_and_clear():
        global _scan_running
        try:
            await scanner.run_scan()
        finally:
            _listing_cache.clear()
            _scan_running = False

    background_tasks.add_task(_run_and_clear)
    return {"message": "Scan started", "status": "running"}
