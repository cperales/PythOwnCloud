"""
Thumbnail generation and caching for images, videos, and audio files.
Supports WebP output with configurable dimensions and quality.
"""

import asyncio
import logging
from pathlib import Path

from cachetools import TTLCache

from pythowncloud.config import settings

logger = logging.getLogger(__name__)

# ─── Extension Constants (single source of truth) ───────────────────────────

IMAGE_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "gif", "webp", "heic", "bmp", "tiff"})
VIDEO_EXTENSIONS = frozenset({"mp4", "mov", "avi", "mkv", "webm"})
AUDIO_EXTENSIONS = frozenset({"mp3", "flac", "aac", "ogg"})
THUMBABLE_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | AUDIO_EXTENSIONS

# ─── Module state ──────────────────────────────────────────────────────────

STORAGE = Path(settings.storage_path)
THUMBS_DIR = settings.thumbnails_path

_semaphore: asyncio.Semaphore | None = None
_thumb_exists_cache: TTLCache[str, bool] = TTLCache(
    maxsize=4096, ttl=settings.thumb_cache_ttl
)


def _get_semaphore() -> asyncio.Semaphore:
    """Get or create the semaphore (lazy init for event loop)."""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(settings.thumb_max_concurrent)
    return _semaphore


# ─── Path resolution ───────────────────────────────────────────────────────

def thumb_path_for(rel_path: str) -> Path:
    """Given 'photos/2026/sunset.jpg', return Path('.thumbnails/photos/2026/sunset.jpg.webp')."""
    return THUMBS_DIR / (rel_path + ".webp")


def is_thumbable(extension: str | None) -> bool:
    """Check if the extension supports thumbnail generation."""
    return extension is not None and extension.lower() in THUMBABLE_EXTENSIONS


# ─── ffmpeg execution ──────────────────────────────────────────────────────

async def _run_ffmpeg(args: list[str]) -> bool:
    """Run ffmpeg with semaphore-limited concurrency. Returns True on success."""
    sem = _get_semaphore()
    async with sem:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(
                "ffmpeg failed (rc=%d): %s",
                proc.returncode,
                stderr.decode(errors="replace")[:200],
            )
            return False
        return True


async def generate_thumbnail(
    source: Path, dest: Path, extension: str
) -> bool:
    """Generate a WebP thumbnail. Dispatches to image/video/audio strategy."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    ext = extension.lower()
    w = settings.thumb_width
    q = settings.thumb_quality

    if ext in IMAGE_EXTENSIONS:
        args = [
            "-i",
            str(source),
            "-vf",
            f"scale={w}:-1:flags=lanczos",
            "-quality",
            str(q),
            "-y",
            str(dest),
        ]
    elif ext in VIDEO_EXTENSIONS:
        args = [
            "-i",
            str(source),
            "-ss",
            "00:00:01",
            "-frames:v",
            "1",
            "-vf",
            f"scale={w}:-1:flags=lanczos",
            "-quality",
            str(q),
            "-y",
            str(dest),
        ]
    elif ext in AUDIO_EXTENSIONS:
        # Best-effort: extract embedded cover art
        args = [
            "-i",
            str(source),
            "-an",
            "-vf",
            f"scale={w}:-1:flags=lanczos",
            "-quality",
            str(q),
            "-y",
            str(dest),
        ]
    else:
        return False

    return await _run_ffmpeg(args)


# ─── High-level API ───────────────────────────────────────────────────────

async def ensure_thumbnail(rel_path: str, extension: str) -> Path | None:
    """
    Return the thumbnail Path if it exists or can be generated. None on failure.
    Checks the TTLCache first, then disk, then generates.
    """
    # Check cache
    cached = _thumb_exists_cache.get(rel_path)
    if cached is True:
        tp = thumb_path_for(rel_path)
        if tp.exists():
            return tp
        _thumb_exists_cache.pop(rel_path, None)

    # Check disk
    tp = thumb_path_for(rel_path)
    if tp.exists():
        _thumb_exists_cache[rel_path] = True
        return tp

    # Check source exists
    source = STORAGE / rel_path
    if not source.exists():
        _thumb_exists_cache[rel_path] = False
        return None

    # Skip large files during scan
    try:
        if source.stat().st_size > settings.thumb_max_source_bytes:
            _thumb_exists_cache[rel_path] = False
            return None
    except OSError:
        return None

    # Generate
    ok = await generate_thumbnail(source, tp, extension)
    _thumb_exists_cache[rel_path] = ok
    return tp if ok else None


def thumbnail_exists(rel_path: str) -> bool:
    """Synchronous check with TTLCache + disk fallback (for template annotation)."""
    cached = _thumb_exists_cache.get(rel_path)
    if cached is not None:
        return cached
    exists = thumb_path_for(rel_path).exists()
    _thumb_exists_cache[rel_path] = exists
    return exists


def invalidate_thumbnail(rel_path: str) -> None:
    """Remove thumbnail file and clear its cache entry."""
    _thumb_exists_cache.pop(rel_path, None)
    tp = thumb_path_for(rel_path)
    if tp.exists():
        tp.unlink(missing_ok=True)


def invalidate_cache_for_directory(dir_path: str) -> None:
    """Remove all cache entries whose keys start with dir_path/."""
    prefix = dir_path + "/" if dir_path else ""
    keys_to_remove = [k for k in _thumb_exists_cache if k.startswith(prefix)]
    for k in keys_to_remove:
        _thumb_exists_cache.pop(k, None)
