"""
Filesystem scanner — walks STORAGE and reconciles it with the database.
Designed to run as a FastAPI BackgroundTask (async, non-blocking I/O).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path

from pythowncloud.config import settings
import pythowncloud.db as db

logger = logging.getLogger(__name__)


def _checksum_sync(filepath: Path) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


async def _compute_checksum(filepath: Path) -> str:
    """Compute SHA-256 in a thread pool to avoid blocking the event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _checksum_sync, filepath)


async def run_scan() -> dict:
    """
    Walk the storage directory, compare mtime+size against DB rows,
    upsert changed/new entries, delete stale DB rows.
    Returns a summary dict.
    """
    storage = Path(settings.storage_path)
    if db.get_pool() is None:
        logger.warning("Scan requested but DB pool is not available")
        return {"error": "Database unavailable"}

    scanned = 0
    updated = 0
    errors = 0
    seen_paths: list[str] = []

    for fspath in storage.rglob("*"):
        # Skip hidden files/directories
        if any(part.startswith(".") for part in fspath.relative_to(storage).parts):
            continue
        try:
            stat = fspath.stat()
            rel_path = str(fspath.relative_to(storage))
            seen_paths.append(rel_path)
            scanned += 1

            existing = await db.get_file_row(rel_path)
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

            needs_update = (
                existing is None
                or existing["size"] != stat.st_size
                or existing["modified_at"].replace(tzinfo=timezone.utc) != mtime
            )

            if needs_update:
                if fspath.is_dir():
                    checksum = ""
                else:
                    checksum = await _compute_checksum(fspath)
                ext = fspath.suffix.lstrip(".").lower() or None
                await db.upsert_file(
                    path=rel_path,
                    filename=fspath.name,
                    extension=ext,
                    size=stat.st_size,
                    checksum=checksum,
                    is_dir=fspath.is_dir(),
                    modified_at=mtime,
                )
                updated += 1
        except (PermissionError, OSError) as e:
            logger.warning("Scan error on %s: %s", fspath, e)
            errors += 1

    deleted = await db.delete_files_not_in(seen_paths)
    purged = await db.purge_expired_sessions()

    logger.info(
        "Scan complete: scanned=%d updated=%d deleted=%d errors=%d sessions_purged=%d",
        scanned, updated, deleted, errors, purged,
    )
    return {
        "scanned": scanned,
        "updated": updated,
        "deleted_from_db": deleted,
        "errors": errors,
        "sessions_purged": purged,
    }
