# PythOwnCloud — Phase 4: Web UI Polish

## Goal

Turn the functional file browser from Phase 2/3 into a comfortable daily-use interface. Three major features:

1. **Media preview and playback** — clicking an image opens a full-size viewer, clicking audio/video plays it inline.
2. **Drag-and-drop upload** — drag files from your Mac's Finder (or any file explorer) into the browser window to upload them.
3. **Move files between folders** — relocate files without downloading and re-uploading.

By the end of Phase 4, the web UI should feel like a lightweight version of Nextcloud's Files app — good enough that you never need `curl` for everyday file management.


## What Phase 4 Is (and Isn't)

**In scope:**

- Image viewer overlay (click thumbnail → full-size preview)
- Audio/video player (inline playback using HTML5 `<audio>` / `<video>`)
- Navigation between media files (next/previous arrows in the viewer)
- Drag-and-drop file upload from the desktop
- Upload progress indicator
- `MOVE /files/{path}` API endpoint (rename/relocate a file)
- "Move to..." UI for relocating files (folder picker dialog)
- Drag-to-move files between visible folders (stretch goal)

**Out of scope:**

- File editing or text preview
- Multi-user sharing or permissions
- Mobile/desktop app sync endpoints (Phase 5)
- Offline access


## Feature 1: Media Preview & Playback

### Image Viewer

When a user clicks an image thumbnail (or filename) in the browse page, instead of downloading the file, a **lightbox overlay** opens showing the full-size image.

```
┌─────────────────────────────────────────────────────────┐
│                                                    [✕]  │
│                                                         │
│   [◀]          ┌─────────────────────┐          [▶]     │
│                │                     │                  │
│                │    sunset.jpg       │                  │
│                │    (full size)      │                  │
│                │                     │                  │
│                └─────────────────────┘                  │
│                                                         │
│           sunset.jpg  ·  3.4 MB  ·  2025-03-04          │
│                      [Download]                         │
└─────────────────────────────────────────────────────────┘
```

Key behaviours:

- **Image source**: `GET /files/photos/2025/sunset.jpg` — the same endpoint that currently triggers a download. The difference is that the browser displays it in an `<img>` tag inside the overlay instead of downloading.
- **Navigation**: Left/right arrows (or keyboard ← →) cycle through images in the current directory. The browse page already has the file list — JavaScript filters it to media files and tracks the current index.
- **Close**: Click the X, press Escape, or click outside the image.
- **Download button**: For when you actually want to save the file locally.
- **Lazy loading**: The full-size image is only fetched when the lightbox opens, not when the browse page loads.

Implementation: this is a self-contained JavaScript component — a `<div>` overlay with an `<img>` tag, event listeners for keyboard navigation, and a close button. No library needed; it's roughly 80–120 lines of vanilla JS.

### Audio Player

Clicking an audio file (`.mp3`, `.flac`, `.aac`, `.ogg`) opens an inline player at the bottom of the page or in a small overlay:

```
┌─────────────────────────────────────────────────────────┐
│  🎵 track01.mp3                                        │
│  ▶ ──────────●────────────────── 1:23 / 4:56           │
│  [Download]                                             │
└─────────────────────────────────────────────────────────┘
```

Implementation: a standard HTML5 `<audio>` tag with `controls`:

```html
<audio controls src="/files/music/swing/track01.mp3"></audio>
```

The browser renders its native audio player. FastAPI's `FileResponse` supports HTTP range requests out of the box, so seeking works automatically. FLAC playback depends on the browser — most modern browsers support it natively, but Safari may need AAC fallback.

The player should persist while navigating directories (so music keeps playing). This means either placing it in a fixed-position footer or opening it in a small floating panel.

### Video Player

Clicking a video file (`.mp4`, `.mov`, `.mkv`, `.webm`) opens a player overlay similar to the image viewer:

```html
<video controls src="/files/photos/2025/vacation.mp4" style="max-width: 100%; max-height: 80vh;"></video>
```

Same principle as audio. HTTP range requests enable seeking. Codec support depends on the browser:

| Format | Chrome | Firefox | Safari |
|--------|--------|---------|--------|
| MP4 (H.264) | ✅ | ✅ | ✅ |
| WebM (VP9) | ✅ | ✅ | ❌ |
| MKV | ❌ | ❌ | ❌ |

For MKV files (common container, usually H.264 inside), the browser can't play them natively. Options: show a "Download to play" message, or if demand justifies it, add a server-side transcode endpoint in a future phase. For now, MKV gets the download button only.


## Feature 2: Drag-and-Drop Upload

### How it works

The browse page listens for HTML5 drag events. When files are dragged from the desktop's file explorer onto the browser window:

1. A visual drop zone appears (border highlight, "Drop files here to upload" message).
2. On drop, the files are uploaded to the current directory via `PUT /files/{path}` using `fetch()`.
3. A progress indicator shows upload status per file.
4. On completion, the page reloads to show the new files.

### Implementation

```javascript
const dropZone = document.querySelector('main');

dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropZone.classList.add('drag-active');
});

dropZone.addEventListener('dragleave', () => {
    dropZone.classList.remove('drag-active');
});

dropZone.addEventListener('drop', async (e) => {
    e.preventDefault();
    dropZone.classList.remove('drag-active');
    const files = Array.from(e.dataTransfer.files);
    for (const file of files) {
        await uploadFile(file);
    }
    location.reload();
});
```

The `uploadFile()` function is the same one the existing upload button uses — `fetch('PUT', '/files/' + path)`. The drag-and-drop just provides a second way to trigger it.

### Progress indicator

For single small files, a simple "Uploading..." message suffices. For multiple files or large files, show a progress bar using `XMLHttpRequest` (which supports progress events) or the newer `fetch()` with a `ReadableStream` wrapper:

```
Uploading 3 files...
sunset.jpg       ████████████████████ 100%
vacation.mp4     ██████████░░░░░░░░░░  52%
notes.txt        waiting...
```

This can be a small floating panel at the bottom-right of the page that appears during uploads and disappears when done.

### CSS for the drop zone

```css
main.drag-active {
    outline: 3px dashed #1a1a2e;
    outline-offset: -8px;
    background: rgba(26, 26, 46, 0.03);
}
```

Subtle enough to not be distracting, visible enough to know the drop will be received.


## Feature 3: Move Files Between Folders

### New API Endpoint

```
POST /files/move
Content-Type: application/json

{
    "source": "photos/2025/sunset.jpg",
    "destination": "photos/2025/march/sunset.jpg"
}
```

Response:
```json
{
    "source": "photos/2025/sunset.jpg",
    "destination": "photos/2025/march/sunset.jpg",
    "message": "moved"
}
```

POST rather than a custom MOVE HTTP method, for simplicity and wider client compatibility.

### Backend implementation

The move operation has four steps:

1. **Validate both paths** with `safe_path()` — prevent traversal.
2. **Move the file on disk**: `shutil.move(source, destination)` or `Path.rename()`. Create destination parent directories if they don't exist.
3. **Update SQLite**: change the `path`, `filename`, and `extension` columns for the moved file.
4. **Move the thumbnail**: if a thumbnail exists at `.thumbnails/{source}.webp`, move it to `.thumbnails/{destination}.webp`.

Edge cases to handle:
- Destination already exists → return 409 Conflict
- Source doesn't exist → return 404
- Source is a directory → allow (move entire subtree), update all child paths in SQLite
- Moving to the same path → no-op, return 200

### Web UI: "Move to..." dialog

Each file in the browse table gets a "Move" action (alongside the existing delete button). Clicking it opens a folder picker dialog:

```
┌─────────────────────────────────────────────────────────┐
│  Move "sunset.jpg" to:                                  │
│                                                         │
│  📁 / (root)                                           │
│  ├── 📁 documents                                      │
│  ├── 📁 photos                                         │
│  │   ├── 📁 2024                                       │
│  │   └── 📁 2025                                       │
│  │       ├── 📁 february                               │
│  │       └── 📁 march           ← selected             │
│  └── 📁 music                                          │
│                                                         │
│              [Cancel]     [Move here]                   │
└─────────────────────────────────────────────────────────┘
```

The folder tree is loaded from `GET /files/` recursively (or from SQLite: `SELECT DISTINCT path FROM files WHERE is_dir = 1`). Since there's one user and the directory tree is probably not enormous, loading it all at once is fine.

### Stretch goal: Drag-to-move

True drag-and-drop between folders in the file listing:

- Drag a file row → visual feedback (row becomes semi-transparent, cursor changes)
- Drop onto a folder row → triggers the `POST /files/move` endpoint
- Drop onto a breadcrumb segment → moves to that parent directory

This is doable but finicky. The main challenges:

- Distinguishing "drag from desktop" (= upload) from "drag within page" (= move). The `dataTransfer.types` array tells you: desktop drags include `"Files"`, internal drags include `"text/plain"` or a custom type.
- Making folder rows valid drop targets with proper highlight-on-hover.
- Handling the case where you drag a file but drop it nowhere (cancel).

Recommendation: implement the "Move to..." dialog first. Add drag-to-move only if you find yourself using move frequently enough that the dialog feels slow. The dialog covers 100% of the functionality; drag-to-move is a UX acceleration.


## Changes to Existing Files

### `pythowncloud/main.py`

- Add `POST /files/move` endpoint
- No other changes (media serving already works via `GET /files/{path}`)

### `pythowncloud/db.py`

- Add `move_file_row(source, destination)` function — updates path, filename, extension
- Add `move_directory_rows(source_prefix, destination_prefix)` — bulk update for directory moves
- Add `list_all_directories()` — returns all directory paths for the folder picker

### `pythowncloud/thumbnails.py`

- Add `move_thumbnail(source, destination)` function

### `pythowncloud/templates/browse.html`

This is where most of the work lives. New JavaScript components:

- Lightbox overlay for images
- Audio/video player overlay or footer
- Drag-and-drop upload handler with progress UI
- "Move to..." dialog with folder tree
- (Stretch) Drag-to-move handlers

### `pythowncloud/static/style.css`

- Lightbox overlay styles
- Media player styles
- Drag-active drop zone highlight
- Move dialog / folder picker styles
- Upload progress panel styles

### `pythowncloud/templates/` (new files, optional)

If the JavaScript grows too large for inline `<script>` in `browse.html`, split into separate files:

```
static/
├── style.css
├── lightbox.js      # Image viewer
├── player.js        # Audio/video player
├── upload.js        # Drag-and-drop upload + progress
└── move.js          # Move dialog + drag-to-move
```

These would be loaded via `<script src="/static/lightbox.js">` in `base.html`.


## Implementation Order

Within Phase 4, the recommended build order is:

1. **Drag-and-drop upload** — smallest change, biggest daily impact. Add the event listeners to `browse.html` and the CSS drop zone highlight. You already have the upload JavaScript; this just wires it to drag events.

2. **Image lightbox** — self-contained JS component, no backend changes. Click thumbnail → overlay with full image → keyboard navigation → close.

3. **Audio/video player** — similar to lightbox but with `<audio>` / `<video>` tags. Backend already supports range requests.

4. **`POST /files/move` endpoint** — backend change, straightforward. Test with curl first.

5. **"Move to..." dialog** — frontend folder picker that calls the new endpoint.

6. **(Stretch) Drag-to-move** — only if the dialog feels too slow in practice.

Each step is independently useful and testable. You can ship after step 1 and iterate.


## NiceGUI Considerations

If you're on the NiceGUI branch instead of Jinja2, the implementations change:

- **Lightbox**: NiceGUI has `ui.image()` and `ui.dialog()`. The viewer would be a dialog containing a dynamically loaded image. Navigation with `ui.button` for arrows.
- **Audio/video**: Use `ui.audio()` and `ui.video()` — thin wrappers around HTML5 elements.
- **Drag-upload**: NiceGUI's `ui.upload` handles drag-and-drop natively (the Quasar upload component supports it). But remember the WebSocket memory concern for large files.
- **Move dialog**: `ui.dialog()` with a `ui.tree()` component showing the folder structure. Quasar's tree component handles expand/collapse natively.
- **Drag-to-move**: Quasar has `q-drag` and `q-drop` directives, accessible via NiceGUI's `.props()`. More structured than raw JS but still complex.

The NiceGUI approach replaces roughly 200 lines of JavaScript with roughly 100 lines of Python. The trade-off is the WebSocket overhead per interaction and slightly less control over the exact UX.


## Success Criteria

Phase 4 is complete when:

1. Clicking an image in the browse page opens a full-size preview overlay
2. Left/right arrows navigate between images in the same directory
3. Clicking an audio file plays it in the browser (at least MP3 and FLAC in Chrome/Firefox)
4. Clicking a video file plays it in the browser (at least MP4)
5. Dragging files from the desktop into the browser uploads them to the current directory
6. Upload progress is visible for large files
7. `POST /files/move` relocates a file on disk, in SQLite, and moves its thumbnail
8. The "Move to..." dialog lets you pick a destination folder and moves the file
9. No new Python dependencies are added (all changes are frontend JS/CSS + one new endpoint)
10. Memory usage remains under 64 MB