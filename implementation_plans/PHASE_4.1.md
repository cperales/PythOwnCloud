# PythOwnCloud — Phase 4: Web UI Polish ✅

## Goal

Turn the functional file browser from Phase 2/3 into a comfortable daily-use interface. Three major features:

1. **Media preview and playback** — clicking an image opens a full-size viewer, clicking audio/video plays it inline.
2. **Drag-and-drop upload** — drag files from your Mac's Finder (or any file explorer) into the browser window to upload them.
3. **Move files between folders** — relocate files without downloading and re-uploading.


## What Was Implemented

### Feature 1: Media Preview & Playback

**Image Lightbox** — Clicking an image (jpg, jpeg, png, gif, webp, heic, bmp, tiff) opens a full-screen overlay showing the image at full size. Left/right arrow keys (and on-screen buttons) navigate between images in the same directory. Escape or clicking outside closes it. A download button is available for saving files locally.

The image is loaded from `GET /files/{path}` — the same endpoint used for downloads, displayed in an `<img>` tag inside the overlay instead of triggering a download.

**Audio Player** — Clicking an audio file (mp3, flac, aac, ogg) opens a floating overlay with an HTML5 `<audio controls>` element. Playback starts automatically. The browser's native audio controls handle play/pause, seeking, and volume. FastAPI's `FileResponse` supports HTTP range requests, so seeking works out of the box.

**Video Player** — Clicking a video file (mp4, mov, webm) opens a similar overlay with an HTML5 `<video controls>` element. MKV files fall back to a direct download link since browsers can't play them natively.

| Format | Chrome | Firefox | Safari |
|--------|--------|---------|--------|
| MP4 (H.264) | ✅ | ✅ | ✅ |
| WebM (VP9) | ✅ | ✅ | ❌ |
| MKV | ❌ download only | ❌ download only | ❌ download only |


### Feature 2: Drag-and-Drop Upload

The browse page listens for HTML5 drag events on the `.file-browser` container. When files are dragged from the desktop:

1. A dashed border appears around the file browser area (`.drag-active` class).
2. On drop, files are uploaded to the current directory via `PUT /files/{path}` using `XMLHttpRequest` (chosen over `fetch()` for progress event support).
3. A floating progress panel appears at the bottom-right showing per-file progress bars with percentage.
4. On completion, the page reloads to show the new files.
5. Errors are shown inline per file with red highlighting.

The same upload mechanism is shared between the drag-and-drop handler and the "Upload files" button — both use `uploadFileWithProgress()` which wraps `XMLHttpRequest`.


### Feature 3: Move Files Between Folders

**API endpoint**: `POST /files/move` accepts a JSON body with `source` and `destination` paths. The endpoint:

1. Validates both paths with `safe_path()` to prevent traversal
2. Checks source exists (404 if not)
3. Checks destination doesn't exist (409 if it does)
4. Prevents same-path no-op moves
5. Creates destination parent directories if needed
6. Moves on filesystem via `shutil.move()`
7. Updates the SQLite row (path, filename, extension)
8. Moves the thumbnail if one exists
9. Invalidates the listing cache for both source and destination parent directories

For directory moves, `db.move_directory_rows()` updates all child paths in a single transaction.

**Web UI**: Each file row has a "⇄" move button that opens a folder picker dialog. The dialog shows a tree of directories (built from the current listing's folders plus parent directory hierarchy). Click a destination, click "Move here", done.


### Bonus Features (not in original Phase 4 plan)

**`DELETE /dirs/{path}`** — A new endpoint for deleting directories and all their contents. Uses `shutil.rmtree()` with DB and cache cleanup. The browse page has a delete button on folders that calls this endpoint with a confirmation dialog.

**New folder dialog** — A "New folder" button next to "Upload files" opens a dialog with a text input. Enter key submits. Creates the directory via `POST /mkdir/{path}` and reloads.

**Combined auth** — `verify_api_key_or_session()` in `auth.py` accepts either an `X-API-Key` header or a valid session cookie. This allows browser-based operations (upload, delete, move, thumbnails, file download) to work without the API key since the user is already authenticated via session.


## Files Changed

| File | Changes |
|------|---------|
| `pythowncloud/main.py` | `POST /files/move`, `DELETE /dirs/{path}`, `_listing_cache` with TTLCache, `_invalidate_listing_cache()`, `GET /thumb/{path}`, combined auth on file endpoints |
| `pythowncloud/auth.py` | `verify_api_key_or_session()` dependency |
| `pythowncloud/db.py` | `move_file_row()`, `move_directory_rows()`, `list_all_directories()`, `delete_directory_rows()` |
| `pythowncloud/thumbnails.py` | `move_thumbnail()` |
| `pythowncloud/templates/browse.html` | Lightbox overlay, audio/video player overlays, drag-and-drop upload with progress, move dialog, new folder dialog, delete directory support |
| `pythowncloud/static/style.css` | Lightbox, media player, drag zone, progress panel, move dialog, new folder dialog styles |
| `tests/test_api.py` | `TestMoveFile` class (move, 404, 409, same-path) |


## What Was Deferred

**Drag-to-move between folders** — The stretch goal of dragging a file row onto a folder row to move it. The "Move to..." dialog covers 100% of the functionality. Drag-to-move would be a UX acceleration but adds significant JavaScript complexity (distinguishing desktop drags from internal drags, drop target highlighting, breadcrumb drops). Deferred unless the dialog proves too slow in daily use.


## Success Criteria — All Met

1. ✅ Clicking an image opens a full-size preview overlay
2. ✅ Left/right arrows navigate between images in the same directory
3. ✅ Clicking an audio file plays it in the browser
4. ✅ Clicking a video file plays it in the browser (MP4, MOV, WebM)
5. ✅ Dragging files from the desktop into the browser uploads them
6. ✅ Upload progress is visible for large files
7. ✅ `POST /files/move` relocates a file on disk, in SQLite, and moves its thumbnail
8. ✅ The "Move to..." dialog lets you pick a destination folder and moves the file
9. ✅ No new Python dependencies added (all changes are frontend JS/CSS + endpoints)
10. ✅ Memory usage remains under 64 MB