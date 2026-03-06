# Phase 3: Thumbnails & LRU Cache — Implementation Plan

## Context

Phase 3 adds real image/video thumbnail previews to the file browser and a lightweight cache layer. The original plan at `implementation_plans/PHASE_3.md` is solid but has gaps around auth, template data flow, cache invalidation granularity, and extension consistency. This plan addresses all of them.

**Key discovery:** The Dockerfile already installs ffmpeg. `API_KEY_HEADER` already uses `auto_error=False`. No Dockerfile changes needed.

---

## Files to Modify (in order)

| File | Action |
|---|---|
| `requirements.txt` | Add `cachetools` |
| `pythowncloud/config.py` | Add thumbnail settings + `thumbnails_path` property |
| `pythowncloud/thumbnails.py` | **New file** — ffmpeg wrapper, path logic, TTLCache for existence checks |
| `pythowncloud/auth.py` | Add `verify_api_key_or_session` combined dependency |
| `pythowncloud/static/style.css` | Add `.thumb` class |
| `pythowncloud/main.py` | Add `/thumb/` endpoint, browse `has_thumb` annotation, upload/delete hooks, listing cache |
| `pythowncloud/templates/browse.html` | Thumbnail `<img>` with emoji fallback, fix extension lists |
| `pythowncloud/scanner.py` | Thumbnail generation + orphan cleanup during scan |

---

## Step 1: `requirements.txt`

Add `cachetools==5.5.2`.

## Step 2: `pythowncloud/config.py`

Add to `Settings`:
```python
thumb_width: int = 320
thumb_quality: int = 80
thumb_max_source_bytes: int = 500 * 1024 * 1024  # skip huge files in scan
thumb_cache_ttl: int = 60                          # TTLCache seconds
thumb_max_concurrent: int = 2                      # max ffmpeg processes
```

Add property:
```python
@property
def thumbnails_path(self) -> Path:
    return Path(self.storage_path) / ".thumbnails"
```

## Step 3: `pythowncloud/thumbnails.py` (new)

### Constants (single source of truth for all extension checks)
```python
IMAGE_EXTENSIONS = frozenset({"jpg","jpeg","png","gif","webp","heic","bmp","tiff"})
VIDEO_EXTENSIONS = frozenset({"mp4","mov","avi","mkv","webm"})
AUDIO_EXTENSIONS = frozenset({"mp3","flac","aac","ogg"})
THUMBABLE_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
```

### Functions
- `thumb_path_for(rel_path: str) -> Path` — returns `{storage}/.thumbnails/{rel_path}.webp`
- `is_thumbable(extension: str | None) -> bool` — checks against `THUMBABLE_EXTENSIONS`
- `_run_ffmpeg(args: list[str]) -> bool` — runs ffmpeg under `asyncio.Semaphore(settings.thumb_max_concurrent)` via `asyncio.create_subprocess_exec`, returns success bool
- `generate_thumbnail(source: Path, dest: Path, extension: str) -> bool` — dispatches to image/video/audio ffmpeg commands, creates parent dirs
- `ensure_thumbnail(rel_path: str, extension: str) -> Path | None` — checks TTLCache, then disk, then generates; respects `thumb_max_source_bytes`; returns thumb path or None
- `thumbnail_exists(rel_path: str) -> bool` — synchronous check with TTLCache fallback to `Path.exists()` (for template annotation)
- `invalidate_thumbnail(rel_path: str) -> None` — deletes thumb file + evicts cache entry

### ffmpeg commands
- **Images:** `ffmpeg -i {src} -vf "scale={w}:-1:flags=lanczos" -quality {q} -y {dest}`
- **Videos:** `ffmpeg -i {src} -ss 00:00:01 -frames:v 1 -vf "scale={w}:-1:flags=lanczos" -quality {q} -y {dest}`
- **Audio:** `ffmpeg -i {src} -an -vf "scale={w}:-1:flags=lanczos" -quality {q} -y {dest}` (best-effort cover art)

### Cache
- `_thumb_exists_cache = TTLCache(maxsize=4096, ttl=settings.thumb_cache_ttl)` — per-file existence
- Semaphore lazily initialized (needs running event loop)

## Step 4: `pythowncloud/auth.py`

Add `verify_api_key_or_session` — accepts either valid `X-API-Key` header OR valid session cookie. Since `API_KEY_HEADER` already has `auto_error=False`, when the header is absent `api_key` is `None` and we fall through to session check.

```python
async def verify_api_key_or_session(
    api_key: str | None = Security(API_KEY_HEADER),
    session: str | None = Cookie(default=None),
) -> str:
    if api_key is not None and api_key == settings.api_key:
        return api_key
    if session is not None and db.get_pool() is not None:
        row = await db.get_session(session)
        if row is not None:
            return session
    raise HTTPException(status_code=401, detail="Valid API key or session required")
```

**Also apply to existing endpoints** (fixes pre-existing browser upload/delete auth bug):
- `PUT /files/{path}` — change `Depends(verify_api_key)` to `Depends(verify_api_key_or_session)`
- `DELETE /files/{path}` — same change

## Step 5: `pythowncloud/static/style.css`

Add after `.icon` rule:
```css
.thumb {
  width: 48px;
  height: 48px;
  object-fit: cover;
  border-radius: 4px;
  vertical-align: middle;
  margin-right: .35rem;
}
```

## Step 6: `pythowncloud/main.py`

### 6a. Imports
Add `from cachetools import TTLCache`, `import pythowncloud.thumbnails as thumbnails`, `from pythowncloud.auth import verify_api_key_or_session`.

### 6b. Listing cache (module-level)
```python
_listing_cache: TTLCache[str, list[dict]] = TTLCache(maxsize=256, ttl=30)
```

Helper to invalidate parent:
```python
def _invalidate_listing_cache(rel_path: str) -> None:
    parent = str(Path(rel_path).parent)
    _listing_cache.pop(parent if parent != "." else "", None)
```

### 6c. `GET /thumb/{file_path:path}` endpoint
- Auth: `Depends(verify_api_key_or_session)`
- Path safety via existing `safe_path()`
- Check `is_thumbable(ext)`, call `ensure_thumbnail()`, return `FileResponse` with `Cache-Control: public, max-age=86400`
- 404 for unsupported types or generation failure (browser falls back to emoji)

### 6d. `browse()` changes
- Use `_listing_cache` before calling `db.list_directory()`
- Annotate each row with `has_thumb = thumbnails.thumbnail_exists(row["path"])` when `is_thumbable(row.get("extension"))`

### 6e. `upload_file()` changes
- Switch auth to `verify_api_key_or_session`
- After DB upsert: call `thumbnails.invalidate_thumbnail(rel)` then `thumbnails.ensure_thumbnail(rel, ext)` (best-effort)
- Call `_invalidate_listing_cache(rel)`

### 6f. `delete_file()` changes
- Switch auth to `verify_api_key_or_session`
- After `target.unlink()`: call `thumbnails.invalidate_thumbnail(rel)`
- Call `_invalidate_listing_cache(rel)`

### 6g. `_run_and_clear()` (scan wrapper)
- Add `_listing_cache.clear()` after scan completes

## Step 7: `pythowncloud/templates/browse.html`

Replace icon block (lines 47-60) — when `item.has_thumb`, render:
```html
<img class="thumb" src="/thumb/{{ item.path }}" alt="" loading="lazy">
```
Otherwise keep emoji fallback. Update emoji extension lists to include `bmp`, `tiff`, `webm` for consistency with `thumbnails.py` constants.

## Step 8: `pythowncloud/scanner.py`

### Thumbnail generation (inside the `for fspath` loop, after upsert)
- If `is_thumbable(ext)` and `not thumbnail_exists(rel_path)` and `size <= thumb_max_source_bytes`: call `generate_thumbnail()`
- Log progress every 50 thumbnails

### Orphan cleanup (after `delete_files_not_in`)
- Walk `.thumbnails/` with `rglob("*.webp")`
- Derive original `rel_path` by stripping `.webp` suffix
- If not in `seen_paths`, delete the thumbnail file

---

## Cache Invalidation Matrix

| Event | Listing Cache | Thumb Exists Cache |
|---|---|---|
| Upload | Parent dir evicted | File entry evicted + regenerated |
| Delete | Parent dir evicted | File entry evicted + thumb file removed |
| Scan complete | Entire cache cleared | Stale entries expire via TTL (60s) |

---

## Memory Budget (Pi 3, 128MB container limit)

- Thumb existence cache: ~400 KB (4096 entries)
- Listing cache: ~512 KB (256 entries)
- ffmpeg peak: ~40 MB per process, max 2 concurrent = 80 MB
- Python runtime: ~30 MB
- **Total peak: ~110 MB** (within 128m + 192m swap)

---

## Verification

1. **Unit test thumbnails.py**: generate thumbnail from `example_data/sunset.jpg`, verify `.webp` output exists and is valid
2. **Test `/thumb/` endpoint**: `curl -H "X-API-Key: ..." localhost:8000/thumb/sunset.jpg` returns WebP with correct headers
3. **Test browser flow**: login, browse to directory with images, verify `<img>` tags render with `/thumb/` src
4. **Test upload hook**: upload a new image via PUT, verify thumbnail is auto-generated
5. **Test delete hook**: delete a file, verify thumbnail is also removed
6. **Test scan**: run `POST /api/scan`, verify thumbnails generated for existing media, orphan thumbnails cleaned
7. **Test cache**: browse same directory twice quickly, verify second request is faster (or add logging to confirm cache hit)
8. **Test combined auth**: verify `/thumb/` works with API key header AND with session cookie (no API key)
9. **Test fallback**: verify non-media files still show emoji icons, audio files without cover art show music emoji
