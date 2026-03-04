# PythOwnCloud Server (POC)

Lightweight self-hosted cloud storage API, built for Raspberry Pi.

## Quick Start

```bash
# 1. Generate an API key
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# 2. Create .env from template
cp .env.example .env
# Edit .env and set your OCM_API_KEY

# 3. Adjust docker-compose.yml volume path to your external drive

# 4. Build and run
docker compose up -d --build

# 5. Test
curl -s http://localhost:8000/health
```

## API Reference

All endpoints (except `/health`) require the `X-API-Key` header.

```
KEY="your-api-key-here"
```

### List files

```bash
# List root
curl -s -H "X-API-Key: $KEY" http://localhost:8000/files/ | python3 -m json.tool

# List a subdirectory
curl -s -H "X-API-Key: $KEY" http://localhost:8000/files/photos/2025/ | python3 -m json.tool
```

### Download a file

```bash
curl -H "X-API-Key: $KEY" http://localhost:8000/files/photos/sunset.jpg -o sunset.jpg
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

### Create a directory

```bash
curl -X POST -H "X-API-Key: $KEY" http://localhost:8000/mkdir/photos/2025/march
```

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  Client (curl / mobile app / browser)                    │
│                    │                                     │
│              Tailscale VPN                               │
│                    │                                     │
│    ┌───────────────▼─────────────────┐                   │
│    │     PythOwnCloud Server (POC)     │                   │
│    │     FastAPI + uvicorn           │                   │
│    │     ~30-50 MB RAM               │                   │
│    └───────────────┬─────────────────┘                   │
│                    │                                     │
│         ┌─────────▼──────────┐                           │
│         │   /mnt/external    │                           │
│         │   5TB ext4 drive   │                           │
│         └────────────────────┘                           │
└──────────────────────────────────────────────────────────┘
```

## Roadmap

- **Phase 1** ✅ Core file API (GET/PUT/DELETE, auth, directory listing)
- **Phase 2** — PostgreSQL metadata tracking (search, history)
- **Phase 3** — Redis caching, thumbnails, simple web UI
- **Phase 4** — Mobile photo auto-upload endpoint
