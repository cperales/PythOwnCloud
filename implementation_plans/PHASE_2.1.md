# PythOwnCloud — Phase 2: Metadata Database & Web File Browser

## Goal

Add two capabilities on top of the Phase 1 file API:

1. **An SQLite metadata layer** that tracks every file (path, size, checksum, author, timestamps) so listings are fast and searchable without hitting the filesystem every time.
2. **A simple web UI** served from the same FastAPI instance, with a login page and a file browser — so you can manage your cloud from any browser on the tailnet without needing `curl`.

By the end of Phase 2, opening `http://100.93.58.13:8000/` in a browser should show a login screen, and after authenticating, a clean file browser where you can navigate directories, download files, upload new ones, and delete them.


## Why SQLite Instead of PostgreSQL

The original design called for PostgreSQL. After thinking more carefully about what the database actually does here, SQLite is a better fit for PythOwnCloud:

- **No extra process.** SQLite is a library, not a server. It lives inside the Python process as `import sqlite3` (or `aiosqlite` for async). No second Docker container, no connection pool, no TCP socket. One container, one process.
- **Zero extra RAM.** PostgreSQL costs 80–100 MB as a separate container. SQLite adds effectively nothing — the database is a single file on disk, and only the pages being queried are loaded into memory.
- **Single-user is the sweet spot.** SQLite's only real limitation is concurrent writes from multiple processes. PythOwnCloud has one user and one uvicorn worker — there is never write contention.
- **Simpler backups.** The database is one file. Copy it, and you have a backup. No `pg_dump`, no credentials, no connecting to a running server.
- **Already built in.** Python ships with `sqlite3`. The only extra dependency is `aiosqlite` for async access from FastAPI.

The filesystem remains the source of truth. SQLite is an **index** — if you delete the database file, a scan rebuilds it entirely.


## Why These Two Together (metadata + web UI)

The web UI needs fast directory listings. Phase 1 computes SHA-256 checksums on every file during every listing request — that's fine for a `curl` call on a small folder, but a browser refreshing a photo directory with hundreds of files would be painfully slow on the Pi 3.

SQLite solves this: checksums and metadata are computed once at upload/scan time and cached in the database. Listings become a simple `SELECT` query — instant, even for large directories. The web UI and the metadata DB are natural companions.


## Endpoints

New endpoints in addition to Phase 1:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Redirect to `/browse/` (web UI) |
| GET | `/login` | Login page (HTML form) |
| POST | `/login` | Authenticate, set session cookie |
| POST | `/logout` | Clear session, redirect to `/login` |
| GET | `/browse/{path}` | Web file browser (HTML, requires session) |
| GET | `/api/search` | Search files by name, extension, or date range |
| POST | `/api/scan` | Trigger a full filesystem scan to rebuild metadata |

The existing `/files/` and `/files/{path}` API endpoints remain unchanged and continue to use `X-API-Key` auth. The web UI uses session cookies instead — two auth mechanisms for two use cases (scripts vs. browser).


## Data Model

### `files` table

```sql
CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL UNIQUE,
    filename    TEXT NOT NULL,
    extension   TEXT,
    size        INTEGER NOT NULL,
    checksum    TEXT NOT NULL,
    is_dir      INTEGER NOT NULL DEFAULT 0,   -- SQLite has no boolean
    author      TEXT NOT NULL DEFAULT 'admin',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    modified_at TEXT NOT NULL,
    scanned_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_files_filename  ON files (filename);
CREATE INDEX IF NOT EXISTS idx_files_extension ON files (extension);
CREATE INDEX IF NOT EXISTS idx_files_modified  ON files (modified_at);
```

Note the differences from the PostgreSQL version: `BOOLEAN` becomes `INTEGER` (0/1), `TIMESTAMPTZ` becomes `TEXT` storing ISO 8601 strings, and `SERIAL` becomes `INTEGER PRIMARY KEY AUTOINCREMENT`. SQLite is dynamically typed, so these are conventions rather than constraints.

### `sessions` table

```sql
CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    expires_at  TEXT NOT NULL
);
```

Sessions are deliberately simple — there is one user (you), so no `user_id` column needed. The session token is stored in a secure HTTP-only cookie.

### Database file location

The SQLite database is stored at `/data/.pythowncloud.db` — inside the storage volume, but as a hidden file so it doesn't appear in file listings. This means:

- It persists across container restarts (it's on the external drive, not in the container filesystem).
- It's included in any backup of the storage volume.
- It can be deleted without losing any actual files — a scan regenerates it.


## How Metadata Stays in Sync

There are three moments when the database is updated:

1. **On upload** (`PUT /files/{path}`): After writing the file to disk, an `INSERT ... ON CONFLICT (path) DO UPDATE` upserts the metadata row with the new size, checksum, and `modified_at`.

2. **On delete** (`DELETE /files/{path}`): The corresponding row is removed from the `files` table.

3. **On scan** (`POST /api/scan`): A background walk of the entire storage directory. For each file on disk, the scan compares `mtime` and `size` against the database row. If they differ (or the row doesn't exist), it recomputes the checksum and upserts. Rows in the database with no corresponding file on disk are deleted. This handles files added directly to the drive (via `scp`, `rsync`, mounting on the Mac, etc.) outside of the API.

The scan is the Phase 2 equivalent of Nextcloud's `occ files:scan` — you already know this workflow well.


## Web UI Design

The browser interface is intentionally minimal — server-rendered HTML with no JavaScript framework. FastAPI serves Jinja2 templates. This keeps it fast on the Pi, avoids a build step, and means zero JS dependencies.

(Alternatively, a NiceGUI branch is being explored — see `NICEGUI_PLAN.md`.)

### Login page (`/login`)

A single form with a password field. No username — there is one user. The password is stored hashed (bcrypt) in the environment or config file. On success, a session cookie is set and you are redirected to `/browse/`.

### File browser (`/browse/{path}`)

```
┌─────────────────────────────────────────────────────────┐
│  PythOwnCloud                              [Logout]     │
├─────────────────────────────────────────────────────────┤
│  / > photos > 2025                                      │
│                                                         │
│  ┌──────────────────────────────────────────────────┐   │
│  │ [Upload files]                                   │   │
│  └──────────────────────────────────────────────────┘   │
│                                                         │
│  📁 march/                          —        2025-03-01 │
│  📁 february/                       —        2025-02-14 │
│  🖼️ sunset.jpg                  3.4 MB       2025-03-04 │
│  🖼️ cat.png                     1.2 MB       2025-02-28 │
│  📄 notes.txt                   412 B        2025-01-15 │
│                                                         │
│  5 items · 4.6 MB total                                 │
└─────────────────────────────────────────────────────────┘
```

Key interactions:

- **Click a folder** → navigates to `/browse/photos/2025/march/`
- **Click a file** → downloads it (triggers the existing `GET /files/{path}` endpoint)
- **Breadcrumb trail** → click any segment to jump back up
- **Upload button** → expands a simple `<input type="file" multiple>` form that POSTs to the existing `PUT /files/` endpoint
- **Delete** → a small button or icon per file, with a confirm dialog, calls `DELETE /files/{path}`
- **Search bar** (optional) → `GET /api/search?q=sunset` queries the `files` table


## Authentication Flow

```
Browser                     PythOwnCloud
   │                             │
   │  GET /browse/photos/        │
   │────────────────────────────>│
   │                             │  (no session cookie)
   │  302 Redirect → /login      │
   │<────────────────────────────│
   │                             │
   │  GET /login                 │
   │────────────────────────────>│
   │  HTML login form            │
   │<────────────────────────────│
   │                             │
   │  POST /login (password)     │
   │────────────────────────────>│
   │                             │  (verify bcrypt hash)
   │  Set-Cookie + 302 → /browse/│
   │<────────────────────────────│
   │                             │
   │  GET /browse/               │
   │────────────────────────────>│
   │  HTML file listing          │
   │<────────────────────────────│
```

Sessions expire after a configurable TTL (default: 7 days). Expired sessions are cleaned up lazily — checked on each request, bulk-purged during scan.


## New Dependencies

Added on top of Phase 1:

| Package | Purpose |
|---------|---------|
| `aiosqlite` | Async SQLite driver (wraps `sqlite3` in a thread for async/await) |
| `jinja2` | HTML template rendering for the web UI |
| `bcrypt` | Password hashing for login |
| `python-multipart` | Already present — needed for form uploads in browser |

No `asyncpg`. No SQLAlchemy. The `sqlite3` module is part of Python's standard library; `aiosqlite` is a thin async wrapper around it. Total added footprint: negligible.


## Deployment Changes

The `docker-compose.yml` **shrinks** — the `db` service and `pgdata` volume are removed entirely:

```yaml
services:
  pythowncloud:
    build: .
    container_name: pythowncloud-server
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - /mnt/external-disk/poc-data:/data
    environment:
      - POC_API_KEY=${POC_API_KEY}
      - POC_STORAGE_PATH=/data
      - POC_LOGIN_PASSWORD_HASH=${POC_LOGIN_PASSWORD_HASH}
      - POC_SESSION_TTL_DAYS=7
    mem_limit: 128m
    memswap_limit: 192m
```

That's it. One service. No database container, no `depends_on`, no `DB_PASSWORD`, no `healthcheck` waiting for Postgres to be ready. The SQLite database lives at `/data/.pythowncloud.db` inside the same volume that holds your files.

The `.env.example` also simplifies — remove `POC_DB_URL` and `DB_PASSWORD`:

```bash
# PythOwnCloud — copy to .env and fill in real values

# Secret API key for curl/script access
POC_API_KEY=change-me

# bcrypt hash of the web UI login password
POC_LOGIN_PASSWORD_HASH=$2b$12$changeme

# Session TTL in days (default: 7)
POC_SESSION_TTL_DAYS=7
```


## Changes to `db.py`

The database module is the biggest code change. It replaces `asyncpg` (PostgreSQL) with `aiosqlite` (SQLite). The function signatures stay identical — the rest of the codebase doesn't need to know which database is underneath.

Key differences in implementation:

- **No connection pool.** SQLite uses a single persistent connection (opened on startup, closed on shutdown). One connection is all you need with one worker.
- **`? placeholders` instead of `$1, $2`.** SQLite uses `?` for parameters, not the numbered `$N` style.
- **`ON CONFLICT DO UPDATE` works the same.** SQLite has supported `UPSERT` since version 3.24 (2018). Python 3.11+ ships with SQLite 3.39+, so this is safe.
- **WAL mode enabled on startup.** `PRAGMA journal_mode=WAL` allows reads while a write is in progress — important so a background scan doesn't block browser listings.
- **Booleans are integers.** All `is_dir` values are stored as 0/1 and converted in Python.
- **Timestamps are ISO 8601 strings.** Compared as text (which works because ISO 8601 sorts lexicographically).


## File Structure After Phase 2

```
pythowncloud/
├── main.py              # FastAPI app, API endpoints
├── config.py            # Settings (env vars) — POC_DB_URL removed
├── auth.py              # API key auth (Phase 1) + session auth (Phase 2)
├── db.py                # SQLite connection, all query functions
├── scanner.py           # Filesystem scan logic
├── templates/
│   ├── base.html        # Shared layout (header, nav, footer)
│   ├── login.html       # Login form
│   └── browse.html      # File browser
└── static/
    └── style.css        # Minimal stylesheet
```


## Known Limitations

- **Single-user only.** The `author` field exists in the schema for future use, but login and sessions assume one person.
- **No thumbnails.** Photos show as file entries, not previews. Thumbnail generation is deferred — it is CPU-heavy on the Pi 3 and deserves its own phase.
- **Scan can be slow.** A full scan of thousands of files computes SHA-256 for each changed file. On the Pi 3 over USB 2.0, scanning the full 5TB drive could take a long time. The scan runs in the background and doesn't block the API.
- **Templates are server-rendered.** No live-updating UI — you refresh the page to see changes. This is a feature, not a bug: no WebSocket overhead, no JS framework, no build step.
- **SQLite file on the storage volume.** If the external drive unmounts unexpectedly, both the files and the database are unavailable. This is actually fine — you can't serve files without the drive anyway, and the database is rebuilt by a scan.
- **No concurrent writers.** If you ever move to multiple workers or multiple instances, SQLite would need to be replaced. With one worker and one user, this is a non-issue.


## Success Criteria

Phase 2 is complete when:

1. File metadata is stored in SQLite at `/data/.pythowncloud.db` and survives container restarts
2. `GET /files/{path}` directory listings are served from the database (no per-file checksum computation)
3. `POST /api/scan` walks the filesystem and reconciles the database
4. Opening `http://100.93.58.13:8000/` in a browser shows a login page
5. After login, the file browser displays directories and files with correct metadata
6. Files can be uploaded and deleted from the browser
7. Sessions expire after the configured TTL
8. `docker compose` has a single service — no database container
9. Total memory usage of PythOwnCloud stays under **64 MB**