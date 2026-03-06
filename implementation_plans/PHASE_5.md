# PythOwnCloud — Phase 5: Client Apps & Sync

## Goal

Make PythOwnCloud usable from mobile and desktop clients without opening a browser. The three concrete objectives are:

1. **Android photo auto-upload** — your phone automatically sends new photos to PythOwnCloud, replacing Nextcloud's instant upload to `SubidaInstantánea/Camera/`.
2. **Resumable uploads** — large files (videos, backups) can survive interrupted connections without restarting from zero.
3. **Desktop integration** — access PythOwnCloud files from Finder (Mac) or a file manager without manually running `curl`.

By the end of Phase 5, taking a photo on your phone should result in it appearing on the Pi's external drive within minutes, and your MacBook should be able to browse the storage as a mounted drive.


## The Protocol Decision: WebDAV

Before choosing what to build, it's worth looking at what the client apps already speak.

| Client | WebDAV | SFTP | FTP | TUS | Raw HTTP PUT |
|--------|--------|------|-----|-----|-------------|
| FolderSync (Android) | ✅ | ✅ | ✅ | ❌ | ❌ |
| macOS Finder | ✅ | ✅ | ❌ | ❌ | ❌ |
| Windows Explorer | ✅ | ❌ | ❌ | ❌ | ❌ |
| Linux file managers | ✅ | ✅ | ✅ | ❌ | ❌ |
| Nextcloud Android app | ✅ (WebDAV) | ❌ | ❌ | ❌ | ❌ |
| rclone | ✅ | ✅ | ✅ | ❌ | ✅ |
| curl | ❌ | ❌ | ❌ | ❌ | ✅ |

WebDAV is the clear winner — it's supported by every file manager on every platform, and it's the protocol Nextcloud itself uses for all client sync. It's also built on HTTP, which means it runs through Tailscale without extra configuration.

SFTP would work too (the Pi already runs SSH), but it bypasses PythOwnCloud entirely — no metadata tracking, no thumbnails, no auth integration. Files uploaded via SFTP would only appear after a scan.

**The plan: implement a minimal WebDAV server inside PythOwnCloud, alongside the existing REST API.** Both coexist on the same port. The REST API serves the web UI and scripts; WebDAV serves native file manager clients and mobile sync apps.


## What Is WebDAV?

WebDAV (Web Distributed Authoring and Versioning) is an extension of HTTP that adds file management operations. If you know HTTP, you already know most of WebDAV:

| HTTP Method | WebDAV Purpose | PythOwnCloud Equivalent |
|-------------|---------------|------------------------|
| `GET` | Download a file | `GET /files/{path}` (already exists) |
| `PUT` | Upload a file | `PUT /files/{path}` (already exists) |
| `DELETE` | Delete a file | `DELETE /files/{path}` (already exists) |
| `MKCOL` | Create a directory | `POST /mkdir/{path}` (already exists) |
| `MOVE` | Move/rename | `POST /files/move` (already exists) |
| `COPY` | Copy a file | New (not hard) |
| `PROPFIND` | List directory / get metadata | Similar to `GET /files/{path}` on a directory |
| `OPTIONS` | Discover capabilities | New (trivial) |

The key insight: **you've already built 80% of a WebDAV server.** The existing REST endpoints implement the same operations — WebDAV just uses different HTTP methods and expects XML responses instead of JSON.

`PROPFIND` is the most complex piece. It's how clients request directory listings. The client sends an XML body specifying which properties it wants, and the server responds with a `207 Multi-Status` XML document listing each file with its metadata (size, modification date, content type, ETag). This is the same data you already have in SQLite.


## Architecture

```
┌──────────────────────────────────────────────┐
│              PythOwnCloud Server             │
│                                              │
│  ┌────────────┐  ┌────────────┐              │
│  │  REST API   │  │   WebDAV   │              │
│  │  /files/*   │  │   /dav/*   │              │
│  │  /api/*     │  │            │              │
│  │  /browse/*  │  │            │              │
│  └─────┬──────┘  └─────┬──────┘              │
│        │               │                     │
│        └───────┬───────┘                     │
│                │                             │
│         ┌──────▼──────┐                      │
│         │  Shared     │                      │
│         │  helpers,   │                      │
│         │  db, cache, │                      │
│         │  thumbnails │                      │
│         └──────┬──────┘                      │
│                │                             │
│         ┌──────▼──────┐                      │
│         │   SQLite    │                      │
│         │   + ext4    │                      │
│         └─────────────┘                      │
└──────────────────────────────────────────────┘
```

The WebDAV endpoints live under `/dav/` (or `/webdav/`) and share the same storage, database, cache, and thumbnail logic as the REST API. Authentication uses HTTP Basic Auth (username + password), which is what all WebDAV clients expect.


## WebDAV Endpoints

All WebDAV endpoints live under the `/dav/` prefix:

| Method | Path | Description |
|--------|------|-------------|
| `OPTIONS` | `/dav/*` | Return supported methods and DAV compliance class |
| `PROPFIND` | `/dav/{path}` | List directory contents or file properties (XML) |
| `GET` | `/dav/{path}` | Download a file |
| `PUT` | `/dav/{path}` | Upload a file |
| `DELETE` | `/dav/{path}` | Delete a file or empty directory |
| `MKCOL` | `/dav/{path}` | Create a directory |
| `MOVE` | `/dav/{path}` | Move/rename (destination in `Destination` header) |
| `COPY` | `/dav/{path}` | Copy a file (destination in `Destination` header) |
| `HEAD` | `/dav/{path}` | File metadata without body |

### Authentication

WebDAV clients send credentials via HTTP Basic Auth (`Authorization: Basic base64(user:pass)`). Since PythOwnCloud is single-user, the username can be anything (or fixed to `admin`) — only the password is checked against the same bcrypt hash used for browser login.

```
Authorization: Basic YWRtaW46eW91cnBhc3N3b3Jk
→ decoded: admin:yourpassword
→ verify password against POC_LOGIN_PASSWORD_HASH
```

This is safe over Tailscale (encrypted WireGuard tunnel). On a public network, HTTPS would be mandatory — but that's not the deployment scenario.


### PROPFIND Response Format

This is the core of WebDAV. A `PROPFIND` request on a directory returns XML like:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/dav/photos/2025/</D:href>
    <D:propstat>
      <D:prop>
        <D:displayname>2025</D:displayname>
        <D:resourcetype><D:collection/></D:resourcetype>
        <D:getlastmodified>Sat, 01 Mar 2025 12:00:00 GMT</D:getlastmodified>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>/dav/photos/2025/sunset.jpg</D:href>
    <D:propstat>
      <D:prop>
        <D:displayname>sunset.jpg</D:displayname>
        <D:resourcetype/>
        <D:getcontentlength>3452918</D:getcontentlength>
        <D:getcontenttype>image/jpeg</D:getcontenttype>
        <D:getlastmodified>Tue, 04 Mar 2025 18:30:00 GMT</D:getlastmodified>
        <D:getetag>"sha256:a1b2c3..."</D:getetag>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>
```

The `Depth` header controls recursion: `Depth: 0` returns only the target, `Depth: 1` returns the target and its direct children (default for directory listings), `Depth: infinity` returns the entire subtree (should be limited or rejected to prevent scanning the full 5TB drive).

The data comes straight from SQLite — the same `db.list_directory()` function used by the browse page, just formatted as XML instead of HTML or JSON.


## Resumable Uploads (TUS Protocol)

For large files (videos from the phone, backups), a standard PUT can fail if the connection drops. TUS is an open protocol built on HTTP that solves this with chunked, resumable uploads.

### How TUS Works

1. **Client creates an upload**: `POST /tus/` with `Upload-Length` header → server returns a URL (`/tus/{upload-id}`)
2. **Client uploads in chunks**: `PATCH /tus/{upload-id}` with `Upload-Offset` and chunk body → server appends to partial file
3. **If interrupted**: `HEAD /tus/{upload-id}` → server returns current `Upload-Offset` → client resumes from there
4. **When complete**: server assembles the final file, moves it to storage, upserts to SQLite

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `OPTIONS` | `/tus/` | TUS capabilities (version, extensions, max size) |
| `POST` | `/tus/` | Create a new upload (returns upload URL) |
| `HEAD` | `/tus/{upload_id}` | Get current offset for resumption |
| `PATCH` | `/tus/{upload_id}` | Upload a chunk |
| `DELETE` | `/tus/{upload_id}` | Cancel an in-progress upload |

### Storage for Partial Uploads

Incomplete uploads are stored in `/data/.uploads/` as temporary files:

```
/data/.uploads/
├── abc123def456.part      ← partial file data
├── abc123def456.meta      ← JSON: {filename, destination, size, offset, created_at}
└── xyz789ghi012.part
```

When the last chunk completes (offset == total size), the `.part` file is moved to its final destination, metadata is upserted to SQLite, and the temporary files are cleaned up.

A background task (or scanner enhancement) should clean up abandoned partial uploads older than 24 hours.

### Chunk Size

On the Pi 3 over Tailscale, a reasonable chunk size is 1–5 MB. Small enough that a single chunk completes in seconds on a mobile connection, large enough to avoid excessive overhead from per-chunk HTTP round-trips.

### Client Support

TUS is natively supported by `tus-js-client` (for the web UI) and several Android upload libraries. FolderSync does **not** support TUS natively, but a simple wrapper script on Android (via Tasker) or a dedicated upload app could use it.

For the web UI, the existing drag-and-drop upload could be enhanced: files under 50 MB use the current simple PUT, files over 50 MB use TUS with a progress bar showing chunk-by-chunk progress and the ability to retry.


## Mobile Auto-Upload (Android)

### Option A: FolderSync + WebDAV (Recommended)

FolderSync is a well-maintained Android app that syncs local folders to remote servers. It supports WebDAV natively. Configuration:

1. Install FolderSync from Google Play
2. Add a WebDAV account:
   - URL: `http://100.93.58.13:8000/dav/`
   - Username: `admin`
   - Password: your PythOwnCloud password
3. Create a folder pair:
   - Local: `/DCIM/Camera/` (your phone's camera folder)
   - Remote: `SubidaInstantánea/Camera/` (matching your current Nextcloud path)
   - Sync type: "To remote folder" (one-way upload)
   - Schedule: every 15 minutes, or instant sync on change
4. Optional: add more folder pairs for Screenshots, WhatsApp images, etc.

This replaces Nextcloud's auto-upload with zero custom development on the Android side. FolderSync handles retry, conflict detection, and battery optimization. The only requirement is that PythOwnCloud implements the WebDAV endpoints.

### Option B: Custom Upload Script (Tasker/Automate)

If you don't want to install FolderSync, a Tasker profile can watch for new files in `/DCIM/Camera/` and use `curl` to PUT them:

```bash
# Tasker shell action
FILE="%evtpath"
FILENAME=$(basename "$FILE")
curl -X PUT \
  -H "X-API-Key: your-key" \
  -T "$FILE" \
  "http://100.93.58.13:8000/files/SubidaInstantánea/Camera/$FILENAME"
```

This works today with no server changes, but has no retry logic — if the upload fails, the photo is missed. Good for testing, not for daily use.

### Option C: Dedicated PythOwnCloud Android App (Future)

A minimal Android app that watches the camera folder and uploads via TUS. This would give the best experience (background upload, retry, progress notification) but requires writing and maintaining an Android app. Defer until the simpler options prove insufficient.


## Desktop Integration

### macOS Finder via WebDAV

macOS has built-in WebDAV support. Once the WebDAV endpoint exists:

1. In Finder: Go → Connect to Server (⌘K)
2. Enter: `http://100.93.58.13:8000/dav/`
3. Authenticate with username/password
4. The PythOwnCloud storage appears as a mounted drive in Finder

You can then drag files, create folders, rename, and delete — all through the native file manager. Files are transferred over Tailscale, so this works from anywhere on your tailnet.

### Linux File Managers

GNOME Files (Nautilus), Dolphin (KDE), and Thunar (XFCE) all support WebDAV via the `davs://` or `dav://` URL scheme. Same URL, same credentials.

### rclone

rclone supports WebDAV as a remote type. Configure it once:

```bash
rclone config
# name: pythowncloud
# type: webdav
# url: http://100.93.58.13:8000/dav/
# vendor: other
# user: admin
# pass: yourpassword
```

Then use it for scripted sync, backup, or mount:

```bash
# List files
rclone ls pythowncloud:

# Sync a local folder to the server
rclone sync ~/Documents pythowncloud:documents/

# Mount as a FUSE filesystem
rclone mount pythowncloud: ~/mnt/pythowncloud --vfs-cache-mode full
```

rclone mount with `--vfs-cache-mode full` gives you a local filesystem that reads and writes to PythOwnCloud transparently. This is the closest thing to a "desktop sync client" without writing one.

### rsync over SSH (Already Works)

For bulk transfers, rsync remains the fastest option:

```bash
rsync -avz ~/Photos/ pi@100.93.58.13:/mnt/external-disk/poc-data/photos/
```

This bypasses PythOwnCloud entirely — files need a `POST /api/scan` afterwards to appear in the database. But for initial migration of large datasets, it's unbeatable.


## New Dependencies

| Package | Purpose |
|---------|---------|
| `lxml` | XML generation for PROPFIND responses (faster than stdlib `xml.etree`) |

`lxml` is optional — `xml.etree.ElementTree` from the standard library works too, just slightly more verbose. If `lxml` has build issues on armv7, fall back to stdlib.

No new dependencies for TUS — it's pure HTTP, implemented as regular FastAPI endpoints.


## Implementation Order

### Step 1: WebDAV Core (enables FolderSync + Finder)

This is the highest-impact step — it unlocks both mobile and desktop clients at once.

1. Create `pythowncloud/routers/webdav.py`
2. Implement `OPTIONS /dav/*` — return `DAV: 1, 2` header and allowed methods
3. Implement `PROPFIND /dav/{path}` — XML directory listing from SQLite
4. Wire `GET /dav/{path}` → reuse existing file download logic
5. Wire `PUT /dav/{path}` → reuse existing upload logic
6. Wire `DELETE /dav/{path}` → reuse existing delete logic
7. Implement `MKCOL /dav/{path}` → reuse existing mkdir logic
8. Implement `MOVE /dav/{path}` → reuse existing move logic, read `Destination` header
9. Add HTTP Basic Auth that verifies against `POC_LOGIN_PASSWORD_HASH`
10. Test with: macOS Finder, FolderSync, `cadaver` (CLI WebDAV client), rclone

### Step 2: FolderSync Configuration & Testing

1. Install FolderSync on your Android
2. Configure WebDAV account pointing at PythOwnCloud
3. Set up camera folder sync to `SubidaInstantánea/Camera/`
4. Test: take a photo → verify it appears in PythOwnCloud within the sync interval
5. Test: upload a large video → verify it completes without corruption
6. Test: upload when Tailscale reconnects after being offline → verify retry works

### Step 3: TUS Resumable Upload (enables reliable large-file uploads)

1. Create `pythowncloud/routers/tus.py`
2. Implement `POST /tus/` — create upload, return URL
3. Implement `HEAD /tus/{id}` — return current offset
4. Implement `PATCH /tus/{id}` — append chunk, move to storage on completion
5. Implement `DELETE /tus/{id}` — cancel upload
6. Add cleanup logic for abandoned partial uploads
7. Optionally: enhance the web UI drag-and-drop to use TUS for large files

### Step 4: COPY Endpoint (nice-to-have)

1. Implement `COPY /dav/{path}` with `Destination` header
2. Add `db.copy_file_row()` and `thumbnails.copy_thumbnail()`
3. Some WebDAV clients expect this; others never use it


## New File Structure

```
pythowncloud/
├── routers/
│   ├── __init__.py
│   ├── browse.py        # unchanged
│   ├── dirs.py          # unchanged
│   ├── files.py         # unchanged
│   ├── login.py         # unchanged
│   ├── search.py        # unchanged
│   ├── webdav.py        # NEW: WebDAV endpoints (PROPFIND, MKCOL, MOVE, etc.)
│   └── tus.py           # NEW: TUS resumable upload endpoints
├── webdav_xml.py        # NEW: XML builders for PROPFIND responses
├── main.py              # + include webdav and tus routers
├── auth.py              # + verify_basic_auth() for WebDAV
├── config.py            # + tus_upload_dir, tus_max_age settings
├── db.py                # + copy_file_row() if COPY is implemented
├── ... (rest unchanged)
```


## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| WebDAV client quirks — Finder, FolderSync, and rclone may expect slightly different XML or headers | Test with each client iteratively. Use `cadaver` (CLI) for debugging raw requests. Log all WebDAV requests during development. |
| PROPFIND on large directories is slow | Use SQLite indexed queries (already fast). Set `Depth: infinity` limit. |
| `lxml` doesn't build on armv7 | Fall back to `xml.etree.ElementTree` (stdlib). Slightly more verbose code, same result. |
| TUS partial uploads fill up disk | Background cleanup of `.uploads/` entries older than 24h. Size limit per upload. |
| FolderSync drains phone battery | Configure sync schedule (every 30 min or on WiFi only). Not a server-side issue. |
| HTTP Basic Auth sends password in cleartext | Tailscale encrypts the tunnel. Document that this is not safe on public networks. |


## What This Replaces

After Phase 5, the Nextcloud Android app is no longer needed. The migration path:

1. **Before**: Phone → Nextcloud Android app → Nextcloud (PHP) → PostgreSQL → external drive
2. **After**: Phone → FolderSync → PythOwnCloud (Python) → SQLite → external drive

The file destination path stays the same (`SubidaInstantánea/Camera/`), so no reorganization is needed. You can run both in parallel during testing — Nextcloud on port 443, PythOwnCloud on port 8000 — and cut over when confident.

For desktop access:

1. **Before**: MacBook → Nextcloud WebDAV → Nextcloud (PHP) → external drive
2. **After**: MacBook → PythOwnCloud WebDAV → external drive

Same protocol, same Finder workflow, just pointing at a different URL.


## Success Criteria

Phase 5 is complete when:

1. `PROPFIND /dav/` returns a valid XML directory listing
2. macOS Finder can connect to `http://pi:8000/dav/` and browse files
3. FolderSync on Android can sync the camera folder to PythOwnCloud via WebDAV
4. A photo taken on the phone appears on the Pi within the configured sync interval
5. rclone can list, upload, download, and sync via the WebDAV endpoint
6. `POST /tus/` + `PATCH /tus/{id}` successfully uploads a 500 MB file in chunks
7. Interrupting a TUS upload mid-transfer and resuming completes successfully
8. Abandoned TUS uploads are cleaned up after 24 hours
9. Memory usage remains under 64 MB during normal operation
10. Total PythOwnCloud deployment is still a single Docker container