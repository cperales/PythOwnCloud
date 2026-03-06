# PythOwnCloud Server (POC)

Lightweight self-hosted cloud storage API, built for Raspberry Pi.

## Quick Start

```bash
# 1. Generate an API key
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# 2. Create .env from template
cp .env.example .env
# Edit .env and set your POC_API_KEY and POC_LOGIN_PASSWORD_HASH

# 3. Adjust docker-compose.yml volume path to your external drive

# 4. Build and run
docker compose up -d --build

# 5. Test
curl -s http://localhost:8000/health
```

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

## API Reference

All API endpoints require the `X-API-Key` header. Browser endpoints use session cookies instead.

```
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
curl -X PUT \
  -H "X-API-Key: $KEY" \
  -F "file=@./myfile.txt" \
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

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  Client (curl / mobile app / browser)                    │
│                    │                                     │
│              Tailscale VPN                               │
│                    │                                     │
│    ┌───────────────▼─────────────────┐                   │
│    │     PythOwnCloud Server (POC)   │                   │
│    │     FastAPI + uvicorn           │                   │
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
│         │  └── .pythowncloud │                           │
│         │       .db          │                           │
│         └────────────────────┘                           │
└──────────────────────────────────────────────────────────┘
```

## Roadmap

- **Phase 1** ✅ Core file API (GET/PUT/DELETE, auth, directory listing)
- **Phase 2** ✅ SQLite metadata tracking, web file browser with login
- **Phase 3** ✅ Thumbnail generation (ffmpeg), LRU cache for listings
- **Phase 4** ✅ UI polish: image lightbox, media playback, drag-upload, file move
- **Phase 5** — Mobile photo auto-upload, desktop sync, TUS protocol