# Branch: dev
# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PythOwnCloud (POC) is a lightweight self-hosted cloud storage server built with FastAPI, optimized for Raspberry Pi 3 (1GB RAM, USB 2.0 storage). It exposes three protocols over a single uvicorn instance:

- **REST API** (`/files/`, `/api/`, `/mkdir/`, `/dirs/`) — programmatic access with API key auth
- **WebDAV** (`/dav/` and `/`) — native filesystem mounting with HTTP Basic Auth
- **S3-compatible API** (`/s3/` and `/`) — AWS S3 protocol for rclone/S3Drive with Signature V4 auth
- **Web UI** (`/browse/`, `/login`) — browser-based file manager with session cookie auth

## Commands

```bash
# Run tests (uses temp storage, no Docker needed)
pytest

# Run a single test file
pytest tests/test_api.py

# Run a single test
pytest tests/test_api.py::TestUpload::test_put_file

# Run locally (requires .env with POC_API_KEY, POC_LOGIN_PASSWORD_HASH, POC_S3_SECRET_KEY)
uvicorn entrypoint:app --host 0.0.0.0 --port 8000 --workers 1

# Build and run with Docker
docker compose up -d --build

# Generate secrets
python3 -c "import secrets; print(secrets.token_urlsafe(32))"                                    # API key
python3 -c "from pythowncloud.passwords import hash_password; print(hash_password('yourpass'))"   # password hash
python3 -c "import secrets; print(secrets.token_hex(32))"                                         # S3 secret
```

## Architecture

**Entry point**: `entrypoint.py` → `pythowncloud/main.py` (FastAPI app with lifespan, middleware, router mounting).

**Configuration**: `pythowncloud/config.py` uses Pydantic BaseSettings with `env_prefix="POC_"`. All settings come from `.env` or environment variables.

**Database**: aiosqlite (async SQLite) with WAL mode. Schema has `files` table (path as PK, checksums, metadata) and `sessions` table. DB can live on a separate, faster disk (`POC_DB_PATH_DIR`) from file storage (`POC_STORAGE_PATH`).

**Auth is split across three modules**:
- `auth.py` — API key, session cookie, HTTP Basic Auth (REST/WebDAV/Web UI)
- `s3_auth.py` — AWS Signature V4 HMAC-SHA256 verification (S3 API)
- `passwords.py` — scrypt hashing for login passwords

**Routers** (`pythowncloud/routers/`):
- `files.py` — GET/PUT/DELETE files, `/health` endpoint
- `dirs.py` — mkdir, rmdir
- `browse.py` — HTML file browser, thumbnail serving
- `login.py` — session management
- `search.py` — file search, filesystem scan trigger
- `webdav.py` — full WebDAV (PROPFIND, MKCOL, MOVE, COPY, etc.)
- `s3.py` — S3 single-object ops, bucket ops, multipart uploads

**S3 and WebDAV routers are dual-mounted** at both their prefix (`/s3/`, `/dav/`) and the root (`/`) for client compatibility. S3 routing relies on auth header detection to disambiguate.

**Key modules**:
- `thumbnails.py` — ffmpeg-based WebP thumbnail generation with burst detection (defers during bulk uploads to avoid Pi 3 resource contention)
- `scanner.py` — walks filesystem, computes SHA256+MD5 checksums, reconciles with DB
- `uploads.py` — cleanup for abandoned TUS and S3 multipart uploads
- `cache.py` — TTLCache (30s) for directory listings, invalidated on mutations
- `helpers.py` — path safety (prevents directory traversal), checksums, breadcrumbs

**XML builders**: `s3_xml.py` (S3 responses) and `webdav_xml.py` (PROPFIND responses) use ElementTree.

## Key Patterns

- **Streaming uploads**: All upload handlers (REST, WebDAV, S3) stream to temp files then atomic-rename to final path. S3 multipart concatenates parts with incremental SHA256 hashing.
- **Path safety**: `helpers.safe_path()` resolves user paths and blocks directory traversal. Always use it for user-supplied paths.
- **Burst-aware thumbnails**: `thumbnails.record_upload()` must be called after uploads. `thumbnails.should_defer_thumbnail()` gates whether to generate thumbnails inline.
- **Single worker**: The app runs with `--workers 1` (SQLite limitation). Concurrency comes from async/await.
- **Hidden files**: Files/dirs starting with `.` are filtered from listings (`.thumb/`, `.uploads/`, `.pythowncloud.db`).

## Testing

Tests use FastAPI's `TestClient` with session-scoped fixtures that override `config.settings` to point at temp directories. No running server or Docker needed. Test files: `test_api.py` (REST), `test_phase2.py` (sessions/login), `test_webdav.py` (WebDAV).

## Deployment

Docker image based on `python:3.14-slim` with ffmpeg and tini. Runs as non-root user `poc`. Memory target: ~30-50MB RAM, 128MB container limit. CI runs pytest and a Docker healthcheck build on every push.
