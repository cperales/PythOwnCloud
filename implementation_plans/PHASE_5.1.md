# PythOwnCloud — Phase 5.1: WebDAV & TUS Server Implementation

## Status: COMPLETE

This document covers the **server-side** work for Phase 5 — the WebDAV and TUS endpoints that enable native client access. For platform-specific client setup, see:

- [PHASE_5_ANDROID.md](PHASE_5_ANDROID.md) — Phone photo auto-upload
- [PHASE_5_LINUX.md](PHASE_5_LINUX.md) — Desktop file manager & rclone integration
- [PHASE_5_WINDOWS.md](PHASE_5_WINDOWS.md) — Windows Explorer & rclone integration


## The Protocol Decision: WebDAV

| Client | WebDAV | SFTP | FTP | TUS | Raw HTTP PUT |
|--------|--------|------|-----|-----|-------------|
| FolderSync (Android) | ✅ | ✅ | ✅ | ❌ | ❌ |
| Windows Explorer | ✅ | ❌ | ❌ | ❌ | ❌ |
| Linux file managers | ✅ | ✅ | ✅ | ❌ | ❌ |
| rclone | ✅ | ✅ | ✅ | ❌ | ✅ |

WebDAV is the clear winner — supported by every file manager on every platform, and it's the protocol Nextcloud itself uses. It runs over HTTP, so it works through Tailscale without extra configuration.

SFTP would bypass PythOwnCloud entirely — no metadata tracking, no thumbnails, no auth integration.


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

Both REST API and WebDAV coexist on the same port, sharing storage, database, cache, and thumbnail logic. Authentication uses HTTP Basic Auth (username + password), which is what all WebDAV clients expect.


## WebDAV Endpoints

All endpoints live under the `/dav/` prefix:

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

WebDAV clients send credentials via HTTP Basic Auth (`Authorization: Basic base64(user:pass)`). Single-user: username can be anything, only the password is checked against `POC_LOGIN_PASSWORD_HASH`.

Safe over Tailscale (encrypted WireGuard tunnel). Not safe on public networks without HTTPS.

### PROPFIND Response Format

`Depth` header controls recursion: `0` = target only, `1` = target + direct children (default), `infinity` = rejected to prevent full-drive scan.

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


## TUS Resumable Upload Protocol

For large files (videos, backups) where a standard PUT can fail on interrupted connections.

### How It Works

1. `POST /tus/` with `Upload-Length` header → server returns upload URL
2. `PATCH /tus/{id}` with `Upload-Offset` and chunk body → server appends
3. If interrupted: `HEAD /tus/{id}` → server returns current offset → client resumes
4. When complete: server moves file to storage, upserts to SQLite

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `OPTIONS` | `/tus/` | TUS capabilities (version, extensions, max size) |
| `POST` | `/tus/` | Create a new upload (returns upload URL) |
| `HEAD` | `/tus/{upload_id}` | Get current offset for resumption |
| `PATCH` | `/tus/{upload_id}` | Upload a chunk |
| `DELETE` | `/tus/{upload_id}` | Cancel an in-progress upload |

### Partial Upload Storage

```
/data/.uploads/
├── abc123def456.part      ← partial file data
├── abc123def456.meta      ← JSON: {filename, destination, size, offset, created_at}
└── xyz789ghi012.part
```

Abandoned uploads older than 24h are cleaned up by a background task. Chunk size: 1–5 MB (good balance for Pi 3 over Tailscale).


## File Structure

```
pythowncloud/
├── routers/
│   ├── webdav.py        # WebDAV endpoints (~400 lines)
│   └── tus.py           # TUS resumable upload endpoints (~300 lines)
├── webdav_xml.py        # XML builders for PROPFIND responses (~150 lines)
├── main.py              # + include webdav and tus routers
├── auth.py              # + verify_basic_auth() for WebDAV
├── config.py            # + tus_upload_dir, tus_max_age settings
├── db.py                # + copy_file_row() for COPY
└── ... (rest unchanged)
```


## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| WebDAV client quirks (different XML/header expectations) | Test with each client iteratively. Use `cadaver` CLI for debugging. |
| PROPFIND on large directories | SQLite indexed queries (already fast). Reject `Depth: infinity`. |
| `lxml` build issues on armv7 | Using `xml.etree.ElementTree` (stdlib) instead. |
| TUS partial uploads fill up disk | Background cleanup of `.uploads/` older than 24h. Size limit per upload. |
| HTTP Basic Auth in cleartext | Tailscale encrypts the tunnel. Not safe on public networks. |


## Test Coverage

- 31 WebDAV tests: OPTIONS, PROPFIND, GET/HEAD, PUT, DELETE, MKCOL, MOVE, COPY, auth, MIME types
- 22 TUS tests: OPTIONS, create, upload, offset tracking, chunk PATCH, delete, cleanup, error cases
- All tests use HTTP Basic Auth with bcrypt password verification
