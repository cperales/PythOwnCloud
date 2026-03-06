# PythOwnCloud — Phase 3: Thumbnails & LRU Cache

## Goal

Make the file browser visually useful for photos and videos. Instead of showing every image as a generic 🖼️ icon, generate real thumbnail previews and display them in the browse page. Add a lightweight in-memory cache to avoid redundant work on repeated directory listings.

By the end of Phase 3, opening a photos directory in the browser should show a grid or list of actual image previews, and video files should show a frame grab as their thumbnail.


## What Phase 3 Is (and Isn't)

**In scope:**

- Thumbnail generation for images and video using ffmpeg
- Background thumbnail generation during filesystem scan
- On-demand thumbnail generation when a file is first browsed
- Thumbnail storage in a hidden directory on the storage volume
- A `GET /thumb/{path}` endpoint to serve thumbnails
- LRU cache for directory listings and thumbnail lookups
- Browse page updated to show thumbnails

**Out of scope:**

- Media playback in the browser (Phase 4)
- Drag-and-drop interactions (Phase 4)
- Mobile/desktop app endpoints (Phase 5)


## Thumbnail Generation with ffmpeg

You already have a working ffmpeg build for armv7 from the Nextcloud custom image. The same binary handles both image and video thumbnails.

### Images

Supported extensions: `jpg`, `jpeg`, `png`, `gif`, `webp`, `heic`, `bmp`, `tiff`

```bash
ffmpeg -i input.jpg -vf "scale=320:-1:flags=lanczos" -quality 80 -y thumb.webp
```

- `scale=320:-1` — 320px wide, height proportional (no distortion)
- `flags=lanczos` — good quality downscaling
- Output as WebP — smaller file size than JPEG, supported by all modern browsers
- `-quality 80` — good balance between size and clarity

### Videos

Supported extensions: `mp4`, `mov`, `avi`, `mkv`, `webm`

```bash
ffmpeg -i input.mp4 -ss 00:00:01 -frames:v 1 -vf "scale=320:-1:flags=lanczos" -y thumb.webp
```

- `-ss 00:00:01` — grab a frame 1 second in (skips black intro frames)
- `-frames:v 1` — extract exactly one frame
- Same scaling and format as images

### What about audio?

Audio files (`mp3`, `flac`, `aac`, `ogg`) sometimes have embedded cover art. Extracting it is possible:

```bash
ffmpeg -i track.mp3 -an -vcodec copy cover.jpg
```

This is best-effort — not all audio files have artwork. If extraction fails, fall back to a generic music icon. This is a nice-to-have within Phase 3, not a blocker.


## Thumbnail Storage Layout

Thumbnails live in `/data/.thumbnails/`, mirroring the directory structure of the original files (that can be found in `example_data/`):

```
/data/
├── .pythowncloud.db
├── .thumbnails/
│   ├── photos/
│   │   └── 2026/
│   │       ├── sunset.jpg.webp
│   │       ├── cat.png.webp
│   │       └── video.mp4.webp
│   └── music/
│       └── swing/
│           └── Team Up - Wingy Carpenter.webp    ← extracted cover art
├── photos/
│   └── 2026/
│       ├── sunset.jpg
│       └── video.mp4
└── music/
    └── swing/
        └── Team Up - Wingy Carpenter.mp3
```

The thumbnail filename is the original filename plus `.webp`. This makes the mapping trivial: given a file path, the thumbnail path is always `.thumbnails/{path}.webp`.

The `.thumbnails/` directory is hidden (leading dot), so it won't appear in file listings — same convention as `.pythowncloud.db`.


## Thumbnail Lifecycle

### When are thumbnails created?

1. **During scan** (`POST /api/scan`): After upserting file metadata, the scanner checks if a thumbnail exists. If not (or if the file's `mtime` is newer than the thumbnail), it generates one. This runs in the background, so it doesn't block the API.

2. **On upload** (`PUT /files/{path}`): After saving the file and upserting metadata, if the file is a supported media type, generate its thumbnail immediately. This keeps thumbnails fresh for newly uploaded files.

3. **On demand** (`GET /thumb/{path}`): If a thumbnail is requested but doesn't exist yet (edge case — file was added outside the API and no scan has run), generate it on the fly and cache the result. Return a placeholder icon while generating if it takes too long.

### When are thumbnails deleted?

- **On file delete** (`DELETE /files/{path}`): Delete the corresponding thumbnail file.
- **During scan**: If a thumbnail exists but the original file doesn't, delete the orphan.

### Regeneration

Thumbnails are cheap to regenerate. If the `.thumbnails/` directory is deleted entirely, the next scan recreates everything. No data is lost.


## New Endpoint

### `GET /thumb/{file_path:path}`

Returns the thumbnail for a given file. Requires API key or valid session (same auth as the rest of the app).

```
GET /thumb/photos/2025/sunset.jpg
→ 200 OK, Content-Type: image/webp, body: thumbnail bytes

GET /thumb/documents/report.pdf
→ 404 Not Found (no thumbnail for PDFs)

GET /thumb/photos/2025/new_photo.jpg  (thumbnail not yet generated)
→ 202 Accepted, triggers background generation
   (or returns a placeholder image)
```

Response headers should include `Cache-Control: public, max-age=86400` — thumbnails don't change unless the source file changes, so browsers can cache them aggressively.


## LRU Cache

### What gets cached?

Two things benefit from caching:

1. **Directory listings from SQLite.** Even though SQLite queries are fast, the same directory might be requested multiple times in quick succession (browser refresh, navigating back, multiple tabs). Cache the query result for a short TTL.

2. **Thumbnail existence checks.** Before serving a browse page, we check if each file has a thumbnail. This involves a `Path.exists()` call per file. Cache the result.

### Implementation

Python's `functools.lru_cache` doesn't support TTL (time-based expiry). Two simple options:

**Option A: `cachetools.TTLCache`** — a third-party library (pure Python, tiny) that provides a dictionary with max size and time-based expiry.

```python
from cachetools import TTLCache

# Cache up to 256 directory listings for 30 seconds each
_listing_cache = TTLCache(maxsize=256, ttl=30)

async def list_directory_cached(path: str) -> list[dict]:
    if path in _listing_cache:
        return _listing_cache[path]
    rows = await db.list_directory(path)
    _listing_cache[path] = rows
    return rows
```

**Option B: Hand-rolled with a dict and timestamps.** No extra dependency, slightly more code, same effect.

Either way, the cache is invalidated on:
- File upload (invalidate the parent directory's cache entry)
- File delete (invalidate the parent directory's cache entry)
- Scan completion (clear the entire cache)

### Memory budget

Each cached directory listing is a list of dicts — maybe 1–2 KB for a typical directory with 20 files. With 256 entries max, the cache uses roughly 256–512 KB. Negligible.


## Changes to the Browse Page

The file table gains a thumbnail column (or replaces the emoji icons):

```
Before (Phase 2):
📁 march/                          —        2025-03-01
🖼️ sunset.jpg                  3.4 MB       2025-03-04
🎬 vacation.mp4                 1.2 GB       2025-03-02

After (Phase 3):
📁 march/                          —        2025-03-01
[thumb] sunset.jpg              3.4 MB       2025-03-04
[thumb] vacation.mp4            1.2 GB       2025-03-02
```

Where `[thumb]` is an `<img>` tag loading from `/thumb/photos/2025/sunset.jpg`.

For files without thumbnails (documents, archives, etc.), the original emoji icons remain as fallback.

The thumbnail images should be small in the listing (48×48 or 64×64 CSS) and load lazily (`loading="lazy"` attribute) so opening a directory with hundreds of photos doesn't trigger hundreds of simultaneous requests.


## ffmpeg in Docker

The Dockerfile needs ffmpeg available inside the container. Two options:

**Option A: Install from apt** (simplest):
```dockerfile
RUN apt-get update && \
    apt-get install -y --no-install-recommends tini curl ffmpeg && \
    rm -rf /var/lib/apt/lists/*
```

This pulls the Debian-packaged ffmpeg. On Trixie (which your Dockerfile uses) this is ffmpeg 7.x, which is recent enough for all the features needed.

**Option B: Copy your custom ffmpeg build** from the Nextcloud image. Only worth it if the apt version is missing codecs you need (unlikely for thumbnail generation).

Option A is recommended unless testing reveals a problem.


## New Dependencies

| Package | Purpose |
|---------|---------|
| `cachetools` | TTLCache for directory listing and thumbnail caching (pure Python, ~20 KB) |

`ffmpeg` is a system dependency installed via apt in the Dockerfile, not a Python package.


## Performance Considerations on the Pi 3

Thumbnail generation is CPU-intensive. A single ffmpeg thumbnail from a 12MP JPEG takes roughly 1–3 seconds on the Pi 3. For video frame extraction, it's about 2–5 seconds depending on the container format.

This means:

- **Initial scan with thumbnail generation will be slow.** Thousands of photos could take hours. The scanner should log progress (`Thumbnails: 142/3847 generated...`) so you can monitor via `docker logs`.
- **Rate-limit generation.** Don't spawn 50 ffmpeg processes at once — the Pi has 4 cores and 1 GB of RAM. Process files sequentially, or at most 2 concurrently.
- **Skip files above a size threshold during scan.** A 4K video file that's 5 GB doesn't need its thumbnail generated during a bulk scan — defer it to on-demand. A reasonable cutoff might be 500 MB.
- **Generation should be interruptible.** If the container restarts mid-scan, the next scan picks up where it left off (files without thumbnails get processed).


## Scanner Changes

The scanner (`scanner.py`) adds a thumbnail generation step after metadata upsert:

```python
# Pseudocode addition to run_scan()
for fspath in storage.rglob("*"):
    # ... existing metadata upsert logic ...

    # Thumbnail generation
    if _is_supported_media(fspath) and not _thumbnail_exists(fspath):
        if fspath.stat().st_size < MAX_THUMB_SIZE:
            await _generate_thumbnail(fspath)
            thumbnails_generated += 1
```

The `_generate_thumbnail()` function calls ffmpeg via `asyncio.create_subprocess_exec()`, which runs it in a subprocess without blocking the event loop.


## File Structure After Phase 3

```
pythowncloud/
├── main.py              # + GET /thumb/{path} endpoint
├── config.py            # + thumbnail settings (size, max source size)
├── auth.py              # unchanged
├── db.py                # unchanged
├── scanner.py           # + thumbnail generation during scan
├── thumbnails.py        # NEW: ffmpeg wrapper, thumbnail path logic, cache
├── templates/
│   ├── base.html        # unchanged
│   ├── login.html       # unchanged
│   └── browse.html      # + <img> tags for thumbnails, lazy loading
└── static/
    └── style.css        # + thumbnail sizing in listings
```


## Success Criteria

Phase 3 is complete when:

1. `GET /thumb/photos/2025/sunset.jpg` returns a WebP thumbnail
2. The browse page shows real image previews instead of emoji icons for supported media
3. Video files show a frame grab as their thumbnail
4. Thumbnails are generated in the background during `POST /api/scan`
5. Uploading a new photo via `PUT` generates its thumbnail immediately
6. Deleting a file also deletes its thumbnail
7. Repeated directory listings are served from the LRU cache (measurably faster)
8. Thumbnail generation doesn't OOM the container (stays under 64 MB)
9. A directory with 100 photos loads in the browser within 2–3 seconds (thumbnails load lazily)