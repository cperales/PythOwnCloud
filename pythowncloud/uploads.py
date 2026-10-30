"""
Upload temp files and cleanup for S3 multipart uploads.

Removes abandoned uploads older than configured age:
- .meta file: metadata JSON with upload_id, created_at
- .part.* files: numbered part data files
- .part.* files whose .meta is gone, and tmp-* files left by a crash
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pythowncloud.config import settings

logger = logging.getLogger(__name__)

_CLEANUP_INTERVAL_SECONDS = 3600


def new_temp_path() -> Path:
    """Return a unique temp path in .uploads, on the storage volume so os.replace() is atomic."""
    uploads_dir = settings.tus_upload_path
    uploads_dir.mkdir(parents=True, exist_ok=True)
    return uploads_dir / f"tmp-{uuid.uuid4().hex}"


def _cleanup_sync() -> None:
    """Blocking cleanup work: call through asyncio.to_thread."""
    uploads_dir = settings.tus_upload_path
    if not uploads_dir.exists():
        return

    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=settings.tus_max_age_hours)

    for meta_file in uploads_dir.glob("s3-*.meta"):
        try:
            with open(meta_file) as f:
                meta = json.load(f)
            created_at = datetime.fromisoformat(meta["created_at"].replace("Z", "+00:00"))
            if created_at < cutoff:
                # Delete all .part.N files for this upload
                for part_file in uploads_dir.glob(meta_file.stem + ".part.*"):
                    part_file.unlink(missing_ok=True)
                # Clean up the metadata file itself
                meta_file.unlink(missing_ok=True)
                logger.info("Cleaned up abandoned S3 upload %s", meta.get("upload_id", "unknown"))
        except Exception:
            logger.warning("Failed to process %s during cleanup", meta_file, exc_info=True)

    # Nothing can complete these: the upload has no .meta, or the writer is gone
    live_uploads = {meta_file.stem for meta_file in uploads_dir.glob("s3-*.meta")}
    leftovers = [
        part_file for part_file in uploads_dir.glob("s3-*.part.*")
        if part_file.name.split(".part.")[0] not in live_uploads
    ]
    leftovers.extend(uploads_dir.glob("tmp-*"))

    for leftover in leftovers:
        try:
            if leftover.stat().st_mtime < cutoff.timestamp():
                leftover.unlink(missing_ok=True)
                logger.info("Cleaned up leftover upload file %s", leftover.name)
        except OSError:
            logger.warning("Failed to remove %s during cleanup", leftover, exc_info=True)


async def cleanup_abandoned_uploads() -> None:
    """Clean up abandoned uploads older than tus_max_age_hours."""
    await asyncio.to_thread(_cleanup_sync)


async def periodic_upload_cleanup() -> None:
    """Run cleanup at startup and then hourly, for as long as the app is up."""
    while True:
        try:
            await cleanup_abandoned_uploads()
        except Exception:
            logger.warning("Upload cleanup failed", exc_info=True)
        await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)
