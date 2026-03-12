"""
Cleanup for S3 multipart uploads.

Removes abandoned uploads older than configured age:
- .meta file: metadata JSON with upload_id, created_at
- .part.* files: numbered part data files
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from pythowncloud.config import settings

logger = logging.getLogger(__name__)


async def cleanup_abandoned_uploads() -> None:
    """
    Clean up abandoned S3 multipart uploads older than tus_max_age_hours.
    Called periodically or on startup.
    """
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
