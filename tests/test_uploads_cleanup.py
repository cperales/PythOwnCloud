"""
Tests for upload housekeeping: abandoned multipart uploads, orphaned part
files, leftover temp files, and the periodic cleanup loop.
"""

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

import pythowncloud.uploads as uploads
from pythowncloud.config import settings


@pytest.fixture
def uploads_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_path", str(tmp_path))
    monkeypatch.setattr(settings, "tus_max_age_hours", 24)
    path = settings.tus_upload_path
    path.mkdir()
    return path


def _file(path, age_hours=0):
    """Create a file whose mtime is age_hours in the past."""
    path.write_bytes(b"data")
    mtime = time.time() - age_hours * 3600
    os.utime(path, (mtime, mtime))
    return path


def _meta(uploads_dir, upload_id, age_hours=0):
    created_at = datetime.now(tz=timezone.utc) - timedelta(hours=age_hours)
    path = uploads_dir / f"{upload_id}.meta"
    path.write_text(json.dumps({"upload_id": upload_id, "created_at": created_at.isoformat()}))
    return path


def _cleanup():
    asyncio.run(uploads.cleanup_abandoned_uploads())


class TestCleanup:
    def test_removes_abandoned_upload(self, uploads_dir):
        meta = _meta(uploads_dir, "s3-old", age_hours=48)
        part = _file(uploads_dir / "s3-old.part.1", age_hours=48)

        _cleanup()

        assert not meta.exists()
        assert not part.exists()

    def test_removes_old_orphan_parts(self, uploads_dir):
        orphan = _file(uploads_dir / "s3-orphan.part.1", age_hours=48)

        _cleanup()

        assert not orphan.exists()

    def test_keeps_recent_orphan_parts(self, uploads_dir):
        orphan = _file(uploads_dir / "s3-orphan.part.1", age_hours=1)

        _cleanup()

        assert orphan.exists()

    def test_keeps_old_parts_of_a_live_upload(self, uploads_dir):
        meta = _meta(uploads_dir, "s3-live", age_hours=1)
        part = _file(uploads_dir / "s3-live.part.1", age_hours=48)

        _cleanup()

        assert meta.exists()
        assert part.exists()

    def test_removes_only_old_temp_files(self, uploads_dir):
        old = _file(uploads_dir / "tmp-old", age_hours=48)
        recent = _file(uploads_dir / "tmp-recent", age_hours=1)

        _cleanup()

        assert not old.exists()
        assert recent.exists()

    def test_missing_directory_is_a_no_op(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "storage_path", str(tmp_path / "absent"))

        _cleanup()


class TestPeriodicCleanup:
    def test_keeps_running_after_a_failure(self, monkeypatch):
        calls = []

        async def failing_cleanup():
            calls.append("cleanup")
            raise OSError("disk went away")

        async def stop_after_second_sleep(_seconds):
            calls.append("sleep")
            if calls.count("sleep") == 2:
                raise asyncio.CancelledError

        monkeypatch.setattr(uploads, "cleanup_abandoned_uploads", failing_cleanup)
        monkeypatch.setattr(uploads.asyncio, "sleep", stop_after_second_sleep)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(uploads.periodic_upload_cleanup())

        assert calls == ["cleanup", "sleep", "cleanup", "sleep"]
