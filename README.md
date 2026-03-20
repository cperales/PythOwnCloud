# Branch: main
# PythOwnCloud Server (POC)

Lightweight self-hosted cloud storage API, built for Raspberry Pi.

After bringing my old Pi3 back to life, I decided to clear out the files I have on Google Drive and save them to local external drives. I've been working quite a bit with [Nextcloud](https://nextcloud.com/), trying to optimize it for a Raspberry Pi 3 (1GB of RAM, a 32-bit OS). I did it, but sometimes it crashes.

So I decided to build my own server in Python. This project is about building a file server that allows me to:

- Save any type of file and browse from any device (including a web browser).
- Periodically upload photos and videos from my mobile phone.
- View photos and videos from my computers (Windows and Linux).

## Quick Start

```bash
# 1. Generate an API key
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# 2. Generate a password hash for the web UI
python3 -c "import hashlib, getpass; p=getpass.getpass(); print(hashlib.sha256(p.encode()).hexdigest())"

# 3. Create .env from template
cp .env.example .env
# Edit .env and set your POC_API_KEY and POC_LOGIN_PASSWORD_HASH

# 4. Adjust docker-compose.yml volume path to your external drive

# 5. Build and run
docker compose up -d --build

# 6. Test
curl -s http://localhost:8000/health
```

## Authentication

Three authentication methods are supported:

| Method | Used by | Header / Mechanism |
| ------ | ------- | --- |
| API Key | REST API (curl, scripts) | `X-API-Key: <key>` |
| Session Cookie | Web UI | Login form, 7-day cookie |
| HTTP Basic Auth | WebDAV | Username: anything, Password: your login password |

## Web UI

Open `http://<your-tailscale-ip>:8000/` in a browser to access the file manager. Features include:

- Browse directories with thumbnail previews for images and videos
- Click images to open a full-size lightbox viewer with keyboard navigation
- Click audio/video files to play them inline
- Drag files from your desktop into the browser to upload
- Upload progress tracking with per-file progress bars
- Move files between folders via the "Move to..." dialog
- Create and delete folders
- Search files by name, extension, or date range via the API

## WebDAV

Mount as a native drive on any operating system via `/dav/` (also available at `/` for clients that don't support path prefixes).

```bash
# macOS — Finder > Go > Connect to Server
# Windows — Map Network Drive
# Linux — davfs2 or file manager

http://<your-tailscale-ip>:8000/dav/
```

Authentication uses HTTP Basic Auth — any username, your login password.

### rclone example

```ini
[poc]
type = webdav
url = http://<your-tailscale-ip>:8000/dav/
vendor = other
user = user
pass = <your-password>
```

```bash
rclone copy ./photos poc:/photos/2025/
```

## API Reference

All API endpoints require the `X-API-Key` header.

```bash
KEY="your-api-key-here"
```

### List files

```bash
# List root
curl -s -H "X-API-Key: $KEY" http://localhost:8000/files/ | python3 -m json.tool

# List a subdirectory
curl -s -H "X-API-Key: $KEY" http://localhost:8000/files/documents/ | python3 -m json.tool
```

### Download a file

```bash
curl -H "X-API-Key: $KEY" http://localhost:8000/files/documents/myfile.txt -o myfile.txt
```

### Upload a file

```bash
# Multipart form
curl -X PUT \
  -H "X-API-Key: $KEY" \
  -F "file=@./myfile.txt" \
  http://localhost:8000/files/documents/myfile.txt

# Raw stream (alternative)
curl -X PUT \
  -H "X-API-Key: $KEY" \
  --data-binary @./myfile.txt \
  http://localhost:8000/files/documents/myfile.txt
```

### Delete a file

```bash
curl -X DELETE -H "X-API-Key: $KEY" http://localhost:8000/files/documents/myfile.txt
```

### Delete a directory

```bash
curl -X DELETE -H "X-API-Key: $KEY" http://localhost:8000/dirs/documents/old-folder
```

### Create a directory

```bash
curl -X POST -H "X-API-Key: $KEY" http://localhost:8000/mkdir/documents/2026/
```

### Move a file

```bash
curl -X POST \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"source": "photos/sunset.jpg", "destination": "photos/2025/sunset.jpg"}' \
  http://localhost:8000/files/move
```

### Search files

```bash
curl -s -H "X-API-Key: $KEY" "http://localhost:8000/api/search?q=sunset&extension=jpg" | python3 -m json.tool
```

### Trigger filesystem scan

```bash
curl -X POST -H "X-API-Key: $KEY" http://localhost:8000/api/scan
```

## S3-Compatible API

AWS S3-compatible API at `/s3/` for programmatic access and rclone integration. Supports:

- Single-object operations: GET, PUT, HEAD, DELETE
- Bucket operations: ListBuckets, HeadBucket, ListObjectsV2
- Multipart uploads: InitiateMultipartUpload, UploadPart, CompleteMultipartUpload, AbortMultipartUpload, ListParts
- Streaming uploads with MD5 validation
- Atomic directory creation (trailing-slash PUT)

Authentication uses AWS Signature V4 (UNSIGNED-PAYLOAD mode for HTTP requests).

### rclone S3 example

```ini
[poc-s3]
type = s3
provider = Other
access_key_id = pythowncloud
secret_access_key = <your-s3-secret-key>
endpoint = http://<your-tailscale-ip>:8000/s3/
region = us-east-1
```

```bash
rclone copy ./photos poc-s3:photos/2025/
```

Configuration via environment variables:

| Variable | Description | Default |
| --- | --- | --- |
| `POC_S3_ACCESS_KEY` | AWS access key ID | `pythowncloud` |
| `POC_S3_SECRET_KEY` | AWS secret access key | required |
| `POC_S3_REGION` | AWS region (arbitrary but must match client) | `us-east-1` |

## Configuration

Copy `.env.example` to `.env` and adjust as needed.

| Variable | Description | Default |
| --- | --- | --- |
| `POC_API_KEY` | API key for REST access | required |
| `POC_LOGIN_PASSWORD_HASH` | SHA-256 hash of web UI / WebDAV password | required |
| `POC_DATA_FOLDER` | Host directory to mount as storage | required |
| `POC_STORAGE_PATH` | Storage path inside container | `/data` |
| `POC_DB_PATH_DIR` | Directory for the SQLite database (separate from storage) | same as storage |
| `POC_SESSION_TTL_DAYS` | Web UI session cookie lifetime | `7` |
| `POC_THUMB_BURST_WINDOW_SECONDS` | Window for burst upload detection | `30` |
| `POC_THUMB_BURST_THRESHOLD` | Uploads in window to trigger deferral | `5` |
| `POC_THUMB_BURST_COOLDOWN_SECONDS` | Idle time before resuming thumbnail generation | `60` |
| `POC_THUMB_AUTO_SCAN_AFTER_BURST` | Trigger scan after bulk upload completes | `true` |
| `POC_S3_ACCESS_KEY` | S3 API access key ID | `pythowncloud` |
| `POC_S3_SECRET_KEY` | S3 API secret access key | required |
| `POC_S3_REGION` | S3 region (arbitrary but must match client) | `us-east-1` |

### Separate database storage

On a Raspberry Pi you may want to keep the database on the SD card (faster random I/O) while files live on a large USB drive:

```env
POC_DATA_FOLDER=/mnt/external
POC_DB_PATH_DIR=/home/pi/poc-db
```

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  Client (curl / WebDAV / browser / rclone / S3 client)   │
│                    │                                     │
│              Tailscale VPN                               │
│                    │                                     │
│    ┌───────────────▼─────────────────┐                   │
│    │     PythOwnCloud Server (POC)   │                   │
│    │     FastAPI + uvicorn           │                   │
│    │     REST API  /files/           │                   │
│    │     WebDAV    /dav/ (and /)     │                   │
│    │     S3        /s3/              │                   │
│    │     SQLite + LRU cache          │                   │
│    │     ffmpeg (thumbnails)         │                   │
│    │     ~30-50 MB RAM               │                   │
│    └───────────────┬─────────────────┘                   │
│                    │                                     │
│         ┌─────────▼──────────┐                           │
│         │  /mnt/external     │                           │
│         │  5TB ext4 drive    │                           │
│         │  ├── files...      │                           │
│         │  ├── .thumbnails/  │                           │
│         │  ├── .uploads/     │                           │
│         │  └── .pythowncloud │                           │
│         │       .db          │                           │
│         └────────────────────┘                           │
└──────────────────────────────────────────────────────────┘
```

## Bulk Upload Behavior

When many files are uploaded at once (e.g., rclone sync, WebDAV copy), thumbnail generation is automatically deferred to avoid ffmpeg resource contention. Once uploads settle, thumbnails are generated in the background.

Tunable via `POC_THUMB_BURST_*` environment variables.

## Roadmap

- **Phase 1** ✅ Core file API (GET/PUT/DELETE, auth, directory listing)
- **Phase 2** ✅ SQLite metadata tracking, web file browser with login
- **Phase 3** ✅ Thumbnail generation (ffmpeg), LRU cache for listings
- **Phase 4** ✅ UI polish: image lightbox, media playback, drag-upload, file move
- **Phase 5** ✅ WebDAV server, S3-compatible API with multipart uploads, deferred thumbnail generation during bulk uploads
- **Phase 6** — Mobile photo auto-upload client, desktop sync agent
