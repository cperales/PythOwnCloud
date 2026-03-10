# PythOwnCloud — Phase 3.2: Deferred Thumbnail Generation

## Problem

When rclone (or any WebDAV client) bulk-uploads hundreds of files, every `PUT` triggers immediate thumbnail generation via ffmpeg. On the Pi 3 this causes two problems:

1. **Race conditions.** The file is written to disk and the `201 Created` response is sent, but the ext4 journal may not have fully flushed the data. ffmpeg reads a truncated or incomplete file and fails — this is the `rc=69` you see in the logs.

2. **Resource contention.** Even with the semaphore limiting to 2 concurrent ffmpeg processes, a sustained burst of uploads (rclone sends many PUTs in parallel) means ffmpeg is constantly competing with the upload I/O for the Pi's single USB 2.0 bus. The disk can't serve write requests and read-back-for-thumbnailing at the same time efficiently.

3. **Invisible errors.** The `_run_ffmpeg` function logs `stderr[:200]`, but ffmpeg's version banner alone consumes those 200 characters. The actual error message is never visible in the logs.

The result: a wall of `ffmpeg failed (rc=69)` warnings during any bulk upload, thumbnails that never get generated until the next scan, and no way to diagnose the root cause from the logs.


## Solution: Upload Burst Detection + Deferred Thumbnailing

Instead of removing thumbnail-on-upload entirely (which would hurt the single-file-upload experience), detect when the server is under bulk upload load and defer thumbnail generation to the scanner.

The approach has three parts:

1. **Fix the ffmpeg error logging** so you can actually see what went wrong.
2. **Add a burst detector** — a simple sliding window counter that tracks recent uploads.
3. **Skip inline thumbnail generation when a burst is active**, relying on the scanner (or on-demand `GET /thumb/`) to catch up later.


## What This Is (and Isn't)

**In scope:**

- Fix ffmpeg stderr logging (show tail, not head)
- Upload burst detection with configurable threshold and window
- Skip thumbnail generation during bursts (both REST and WebDAV PUT)
- Optional: auto-trigger a scan after a burst subsides
- Log a single summary line instead of per-file ffmpeg failures

**Out of scope:**

- Changes to the scanner's thumbnail generation (already works correctly)
- Changes to the `GET /thumb/` on-demand path (already works correctly)
- Changes to the browse page or thumbnail serving


## Files to Modify (in order)

| File | Action |
|---|---|
| `pythowncloud/config.py` | Add burst detection settings |
| `pythowncloud/thumbnails.py` | Fix stderr logging, add burst detector, add `should_defer()` |
| `pythowncloud/routers/files.py` | Wrap thumbnail block in `should_defer()` check |
| `pythowncloud/routers/webdav.py` | Same change as files.py |
| `pythowncloud/routers/tus.py` | Same change (TUS completion path) |


## Step 1: Fix ffmpeg Error Logging

**File:** `pythowncloud/thumbnails.py`

The current code:

```python
logger.warning(
    "ffmpeg failed (rc=%d): %s",
    proc.returncode,
    stderr.decode(errors="replace")[:200],
)
```

The ffmpeg version banner is emitted to stderr on every invocation and looks like this:

```
ffmpeg version 7.1.3-0+deb13u1 Copyright (c) 2000-2025 the FFmpeg developers
  built with gcc 14 (Debian 14.2.0-19)
  configuration: --prefix=/usr --extra-version=0+deb13u1 --toolchain=hardened --libd...
```

That's already ~200 characters before the actual error line. Fix by logging the **tail** of stderr:

```python
stderr_text = stderr.decode(errors="replace")
# Skip the version banner — the actual error is at the end
logger.warning(
    "ffmpeg failed (rc=%d): %s",
    proc.returncode,
    stderr_text.strip().splitlines()[-1] if stderr_text.strip() else "(no output)",
)
```

Taking just the last line is better than a fixed tail slice because ffmpeg's actual error is always on the final line (e.g. `input.jpg: No such file or directory`, `Invalid data found when processing input`, `Output file is empty`).

For debug-level logging, keep the full stderr available:

```python
logger.debug("ffmpeg full stderr for %s:\n%s", args[-1], stderr_text)
```


## Step 2: Add Burst Detection Settings

**File:** `pythowncloud/config.py`

Add to `Settings`:

```python
# Phase 3.2: Deferred thumbnails during bulk uploads
thumb_burst_window_seconds: int = 30       # sliding window size
thumb_burst_threshold: int = 5             # uploads within window to trigger deferral
thumb_burst_cooldown_seconds: int = 60     # how long after last upload to consider burst over
thumb_auto_scan_after_burst: bool = True   # trigger a scan when burst subsides
```

The logic: if 5 or more uploads land within any 30-second window, thumbnail generation is deferred for all subsequent uploads until 60 seconds pass with no new uploads. This means:

- Single file upload via browser → immediate thumbnail (burst not triggered)
- rclone syncing 200 photos → first 4 get thumbnails, then deferral kicks in
- After rclone finishes and 60s of quiet passes → optional auto-scan generates the rest


## Step 3: Burst Detector in thumbnails.py

**File:** `pythowncloud/thumbnails.py`

Add a lightweight burst tracker using a `deque` of timestamps:

```python
import time
from collections import deque

# ─── Burst detection ───────────────────────────────────────────────────────

_upload_timestamps: deque[float] = deque()
_burst_active: bool = False
_last_upload_time: float = 0.0


def record_upload() -> None:
    """Record that an upload just happened. Call from PUT handlers."""
    global _burst_active, _last_upload_time
    now = time.monotonic()
    _last_upload_time = now
    _upload_timestamps.append(now)

    # Trim timestamps outside the window
    cutoff = now - settings.thumb_burst_window_seconds
    while _upload_timestamps and _upload_timestamps[0] < cutoff:
        _upload_timestamps.popleft()

    # Check threshold
    if len(_upload_timestamps) >= settings.thumb_burst_threshold:
        if not _burst_active:
            logger.info(
                "Bulk upload detected (%d uploads in %ds) — deferring thumbnail generation",
                len(_upload_timestamps),
                settings.thumb_burst_window_seconds,
            )
        _burst_active = True


def should_defer_thumbnail() -> bool:
    """Return True if thumbnail generation should be skipped for this upload."""
    global _burst_active
    if not _burst_active:
        return False

    # Check if cooldown has elapsed since last upload
    now = time.monotonic()
    if now - _last_upload_time > settings.thumb_burst_cooldown_seconds:
        logger.info(
            "Bulk upload burst ended (%.0fs idle) — resuming inline thumbnails",
            now - _last_upload_time,
        )
        _burst_active = False
        return False

    return True


def is_burst_active() -> bool:
    """Check if a burst is currently active (for auto-scan trigger)."""
    return _burst_active
```

This is zero-dependency (just a `deque` and `time.monotonic()`), adds negligible memory, and is thread-safe enough for a single-worker uvicorn process.


## Step 4: Update Upload Handlers

The same pattern applies to all three upload paths.

### `pythowncloud/routers/files.py` — REST PUT

Replace the current thumbnail block:

```python
# Current code:
if thumbnails.is_thumbable(ext_lower):
    try:
        thumbnails.invalidate_thumbnail(rel)
        await thumbnails.ensure_thumbnail(rel, ext_lower)
    except Exception:
        logger.warning("Thumbnail generation failed for %s", file_path, exc_info=True)
```

With:

```python
thumbnails.record_upload()
if thumbnails.is_thumbable(ext_lower) and not thumbnails.should_defer_thumbnail():
    try:
        thumbnails.invalidate_thumbnail(rel)
        await thumbnails.ensure_thumbnail(rel, ext_lower)
    except Exception:
        logger.warning("Thumbnail generation failed for %s", file_path, exc_info=True)
elif thumbnails.is_thumbable(ext_lower):
    # Burst active — just invalidate stale thumb, scanner will regenerate
    thumbnails.invalidate_thumbnail(rel)
```

### `pythowncloud/routers/webdav.py` — WebDAV PUT

Identical change in the `upload_file` handler.

### `pythowncloud/routers/tus.py` — TUS completion

The TUS PATCH handler has a completion block that moves the file to storage. Apply the same pattern there after the file is finalized.


## Step 5 (Optional): Auto-Scan After Burst

**File:** `pythowncloud/thumbnails.py` + `pythowncloud/main.py`

When `thumb_auto_scan_after_burst` is enabled, a background task periodically checks whether a burst has ended and triggers a scan to generate the deferred thumbnails.

In `main.py`, add a background coroutine that runs on startup:

```python
async def _burst_watcher():
    """Watch for bulk upload bursts ending, then trigger a thumbnail scan."""
    if not settings.thumb_auto_scan_after_burst:
        return

    was_active = False
    while True:
        await asyncio.sleep(15)  # check every 15 seconds

        if thumbnails.is_burst_active():
            was_active = True
            # Poke should_defer to check cooldown expiry
            thumbnails.should_defer_thumbnail()
        elif was_active and not thumbnails.is_burst_active():
            was_active = False
            logger.info("Post-burst scan: generating deferred thumbnails...")
            try:
                await run_scan()
            except Exception:
                logger.warning("Post-burst scan failed", exc_info=True)
```

Register it in the lifespan:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... existing startup ...
    burst_task = asyncio.create_task(_burst_watcher())
    yield
    burst_task.cancel()
    # ... existing shutdown ...
```

This is optional because you can also just wait for the next scheduled scan (if you have one via cron) or trigger `POST /api/scan` manually after a big upload session.


## Configuration Examples

### Default (good for most use)

```env
POC_THUMB_BURST_WINDOW_SECONDS=30
POC_THUMB_BURST_THRESHOLD=5
POC_THUMB_BURST_COOLDOWN_SECONDS=60
POC_THUMB_AUTO_SCAN_AFTER_BURST=true
```

Five uploads in 30 seconds triggers deferral. After 60 seconds of quiet, a scan runs automatically.

### Aggressive deferral (slow Pi, big uploads)

```env
POC_THUMB_BURST_THRESHOLD=3
POC_THUMB_BURST_COOLDOWN_SECONDS=120
```

Triggers faster (3 uploads), waits longer before scanning (2 minutes of quiet).

### Disable deferral entirely

```env
POC_THUMB_BURST_THRESHOLD=999999
```

Effectively never triggers — every upload gets immediate thumbnailing (current behavior).


## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| Burst detector fires during normal browsing + occasional uploads | Threshold of 5 in 30s is high enough that manual uploads won't trigger it. Browsing doesn't call `record_upload()`. |
| Auto-scan after burst is expensive on the Pi | The scanner already skips files that have thumbnails. A post-burst scan only generates thumbnails for the new files — proportional to what was uploaded, not the full library. |
| Race between burst watcher and a manual `POST /api/scan` | The scanner is idempotent — running it twice just means the second run finds nothing to do. No harm. |
| `deque` grows unbounded during a very long burst | The window trimming in `record_upload()` keeps the deque at most `burst_window_seconds` worth of entries. Even at 100 uploads/sec for 30s, that's only 3000 floats (~24 KB). |
| Thumbnails missing in browse UI during/after burst | `GET /thumb/` still generates on-demand. The browse page's `loading="lazy"` means thumbnails are requested as the user scrolls, and on-demand generation fills the gaps. The auto-scan is just a belt-and-suspenders approach. |


## New Dependencies

None.


## Success Criteria

1. Single file upload via browser or `curl PUT` → thumbnail generated immediately (no deferral)
2. rclone bulk-syncing 50+ files → first few get thumbnails, then deferral activates and logs a single info line
3. After rclone finishes and cooldown elapses → auto-scan generates remaining thumbnails
4. `docker logs` shows the actual ffmpeg error (not the version banner) when a thumbnail fails
5. No `ffmpeg failed` warnings during a bulk upload session (because ffmpeg isn't being called)
6. Memory usage during bulk upload stays well under 64 MB (no concurrent ffmpeg processes competing with I/O)
7. Browse page still shows thumbnails for files uploaded before the burst, and fills in the rest lazily via `GET /thumb/`
