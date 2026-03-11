"""
Unified cleanup for TUS and S3 multipart uploads.

Handles cleanup of abandoned uploads from both protocols, which store
metadata and parts differently:
- TUS: .meta + single .part file
- S3: .meta + multiple .part.1/.part.2/.part.N files
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from pythowncloud.config import settings

logger = logging.getLogger(__name__)


async def cleanup_abandoned_uploads() -> None:
    """
    Clean up abandoned TUS and S3 uploads older than tus_max_age_hours.
    Called periodically or on startup.
    """
    uploads_dir = settings.tus_upload_path
    if not uploads_dir.exists():
        return

    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=settings.tus_max_age_hours)

    for meta_file in uploads_dir.glob("*.meta"):
        try:
            with open(meta_file) as f:
                meta = json.load(f)
            created_at = datetime.fromisoformat(meta["created_at"].replace("Z", "+00:00"))
            if created_at < cutoff:
                # TUS cleanup: delete .part file
                if meta_file.name.startswith("tus-"):
                    meta_file.with_suffix(".part").unlink(missing_ok=True)
                # S3 cleanup: delete all .part.N files
                elif meta_file.name.startswith("s3-"):
                    for part_file in uploads_dir.glob(meta_file.stem + ".part.*"):
                        part_file.unlink(missing_ok=True)
                # Clean up the metadata file itself
                meta_file.unlink(missing_ok=True)
                logger.info(f"Cleaned up abandoned upload {meta.get('upload_id', 'unknown')}")
        except Exception:
            logger.warning(f"Failed to process {meta_file} during cleanup", exc_info=True)
