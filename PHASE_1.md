# PythOwnCloud Server — Phase 1: Core File API

## Goal

Replace the basic file storage functionality of Nextcloud with a minimal Python API that runs comfortably on a Raspberry Pi 3 (1GB RAM, armv7l). Phase 1 covers only the essentials: storing, retrieving, listing, and deleting files over HTTP, protected by API key authentication.

By the end of Phase 1, any device on the Tailscale network should be able to browse, upload, download, and delete files on the 5TB external drive — all via `curl` or any HTTP client.


## What Phase 1 Is (and Isn't)

**In scope:**

- File upload (PUT), download (GET), delete (DELETE)
- Directory listing with file metadata (name, size, modification date, checksum)
- Directory creation
- API key authentication via header
- Docker deployment matching the existing Compose workflow
- Streaming I/O (never load a full file into memory)

**Out of scope (future phases):**

- Database-backed metadata, search, or history (Phase 2)
- Mobile auto-upload / photo backup (Phase 3)
- Redis caching, thumbnails, web UI (Phase 4)
- Sync clients, conflict resolution, versioning
- Multi-user support


## Endpoints

All endpoints except `/health` require the `X-API-Key` header.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Returns server status and storage info |
| GET | `/files/` | List root directory |
| GET | `/files/{path}` | If directory → JSON listing. If file → download |
| PUT | `/files/{path}` | Upload or overwrite a file (multipart) |
| DELETE | `/files/{path}` | Delete a single file (directories refused) |
| POST | `/mkdir/{path}` | Create a directory (parents created automatically) |


## Data Model

Phase 1 has no database — the filesystem *is* the source of truth. Metadata is computed on the fly from `stat()` calls and checksums.

A file listing entry looks like:

```json
{
  "name": "sunset.jpg",
  "path": "photos/2025/sunset.jpg",
  "size": 3452918,
  "is_dir": false,
  "modified": "2025-03-04T18:30:00+00:00",
  "checksum": "sha256:a1b2c3..."
}
```

The checksum is computed at read time. This is intentionally simple — Phase 2 will move checksums into PostgreSQL so they don't need to be recalculated on every listing.


## Storage Layout

```
/mnt/external-disk/poc-data/       ← mounted as /data inside the container
├── documents/
│   ├── notes.md
│   └── taxes/
│       └── 2024.pdf
├── photos/
│   └── 2025/
│       ├── sunset.jpg
│       └── cat.png
└── music/
    └── swing/
        └── track01.mp3
```

The API preserves whatever directory structure you create. There are no special folders, no metadata sidecar files, no `.poc/` hidden directories. Files are stored exactly as-is — you can always `ls` the drive directly and see everything.


## Security

There are two layers:

1. **Network layer**: Tailscale isolates the service to your personal tailnet. The API is never exposed to the public internet.
2. **Application layer**: Every mutating and reading endpoint requires a valid `X-API-Key` header. The key is stored as an environment variable.

Additionally, every user-supplied path is resolved and validated against the storage root to prevent directory traversal attacks (`../../etc/shadow`).

Phase 1 does **not** include HTTPS at the application level — encryption is handled by Tailscale's WireGuard tunnel, same as the current Nextcloud setup.


## Deployment

The service runs as a single Docker container via Compose, matching the existing infrastructure pattern on the Pi:

- **Image**: `python:3.11-slim` (armv7 compatible)
- **Process manager**: `tini` as PID 1 for proper signal handling
- **Workers**: 1 uvicorn worker (more would waste RAM on a single-user system)
- **Memory limit**: 128MB hard limit in Compose to prevent OOM
- **Volume**: The external drive is mounted read-write into the container at `/data`

Expected RAM usage: **30–50 MB** (compared to 300+ MB for Nextcloud + PHP-FPM + Apache).


## How to Test

Once deployed, the full workflow can be verified with `curl`:

```bash
KEY="your-api-key"
BASE="http://100.93.58.13:8000"

# Health check
curl -s $BASE/health | python3 -m json.tool

# Create a directory
curl -X POST -H "X-API-Key: $KEY" $BASE/mkdir/test

# Upload a file
echo "hello from POC" > /tmp/hello.txt
curl -X PUT -H "X-API-Key: $KEY" -F "file=@/tmp/hello.txt" $BASE/files/test/hello.txt

# List directory
curl -s -H "X-API-Key: $KEY" $BASE/files/test/ | python3 -m json.tool

# Download file
curl -H "X-API-Key: $KEY" $BASE/files/test/hello.txt

# Delete file
curl -X DELETE -H "X-API-Key: $KEY" $BASE/files/test/hello.txt
```


## Known Limitations

These are intentional trade-offs for Phase 1 simplicity:

- **Checksums are expensive on large directories.** Listing a folder with many big files will be slow because SHA-256 is computed per file on every request. Phase 2 solves this by caching checksums in PostgreSQL.
- **No resumable uploads.** If a large upload fails mid-transfer, you start over. Phase 3 can add TUS protocol support for resumable uploads.
- **No versioning.** Overwriting a file destroys the previous version. A simple versioning scheme (copy-on-write to a `.versions/` directory) could be added before Phase 2 if needed.
- **Single worker.** Two simultaneous large uploads will queue, not parallelize. This is fine for single-user; the Pi 3's USB 2.0 bandwidth is the real bottleneck anyway.
- **No web UI.** Everything is done via API calls. A browse-and-upload web page comes in Phase 4.


## Success Criteria

Phase 1 is complete when:

1. The container builds and starts on the Raspberry Pi 3 without errors
2. Files can be uploaded, listed, downloaded, and deleted via curl over Tailscale
3. Memory usage stays under 64MB during normal operation
4. A 1GB file can be uploaded without the container being OOM-killed
5. Directory traversal attempts return 403
6. Requests without a valid API key return 401/403