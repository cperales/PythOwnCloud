# PythOwnCloud — Phase 2: Metadata Database & Web File Browser

## Goal

Add two capabilities on top of the Phase 1 file API:

1. **A PostgreSQL metadata layer** that tracks every file (path, size, checksum, author, timestamps) so listings are fast and searchable without hitting the filesystem every time.
2. **A simple web UI** served from the same FastAPI instance, with a login page and a file browser — so you can manage your cloud from any browser on the tailnet without needing `curl`.

By the end of Phase 2, opening `http://100.93.58.13:8000/` in a browser should show a login screen, and after authenticating, a clean file browser where you can navigate directories, download files, upload new ones, and delete them.


## Why These Two Together

The web UI needs fast directory listings. Phase 1 computes SHA-256 checksums on every file during every listing request — that's fine for a `curl` call on a small folder, but a browser refreshing a photo directory with hundreds of files would be painfully slow on the Pi 3.

PostgreSQL solves this: checksums and metadata are computed once at upload/scan time and cached in the database. Listings become a simple `SELECT` query — instant, even for large directories. The web UI and the metadata DB are natural companions.


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
CREATE TABLE files (
    id          SERIAL PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,   -- relative path from storage root
    filename    TEXT NOT NULL,          -- just the name (for search)
    extension   TEXT,                   -- lowercase, without dot: "jpg", "pdf"
    size        BIGINT NOT NULL,
    checksum    TEXT NOT NULL,          -- sha256 hex digest
    is_dir      BOOLEAN NOT NULL DEFAULT FALSE,
    author      TEXT NOT NULL DEFAULT 'admin',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    modified_at TIMESTAMPTZ NOT NULL,  -- from filesystem mtime
    scanned_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_files_path_parent ON files (path text_pattern_ops);
CREATE INDEX idx_files_filename ON files (filename);
CREATE INDEX idx_files_extension ON files (extension);
CREATE INDEX idx_files_modified ON files (modified_at);
```

### `sessions` table

```sql
CREATE TABLE sessions (
    token       TEXT PRIMARY KEY,       -- random session token
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);
```

Sessions are deliberately simple — there is one user (you), so no `user_id` column needed. The session token is stored in a secure HTTP-only cookie.


## How Metadata Stays in Sync

There are three moments when the database is updated:

1. **On upload** (`PUT /files/{path}`): After writing the file to disk, an `INSERT ... ON CONFLICT (path) DO UPDATE` upserts the metadata row with the new size, checksum, and `modified_at`.

2. **On delete** (`DELETE /files/{path}`): The corresponding row is removed from the `files` table.

3. **On scan** (`POST /api/scan`): A background walk of the entire storage directory. For each file on disk, the scan compares `mtime` and `size` against the database row. If they differ (or the row doesn't exist), it recomputes the checksum and upserts. Rows in the database with no corresponding file on disk are deleted. This handles files added directly to the drive (via `scp`, `rsync`, mounting on the Mac, etc.) outside of the API.

The scan is the Phase 2 equivalent of Nextcloud's `occ files:scan` — you already know this workflow well.


## Web UI Design

The browser interface is intentionally minimal — server-rendered HTML with no JavaScript framework. FastAPI serves Jinja2 templates. This keeps it fast on the Pi, avoids a build step, and means zero JS dependencies.

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
| `asyncpg` | Async PostgreSQL driver (faster than psycopg2 on the Pi) |
| `jinja2` | HTML template rendering for the web UI |
| `bcrypt` | Password hashing for login |
| `python-multipart` | Already present — needed for form uploads in browser |

No SQLAlchemy — raw queries via `asyncpg` keep memory usage low and give full control over the SQL. The schema is simple enough that an ORM would add overhead without real benefit.


## Deployment Changes

The `docker-compose.yml` grows to include the existing PostgreSQL 16 instance:

```yaml
services:
  pythowncloud:
    build: .
    container_name: pythowncloud
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - /mnt/external-disk/ocm-data:/data
      - ./templates:/app/templates
    environment:
      - OCM_API_KEY=${OCM_API_KEY}
      - OCM_DB_URL=postgresql://ocm:${DB_PASSWORD}@db:5432/pythowncloud
      - OCM_LOGIN_PASSWORD_HASH=${LOGIN_PASSWORD_HASH}
    depends_on:
      - db
    mem_limit: 128m

  db:
    image: postgres:16
    container_name: pythowncloud-db
    restart: unless-stopped
    volumes:
      - pgdata:/var/lib/postgresql/data
    environment:
      - POSTGRES_USER=ocm
      - POSTGRES_PASSWORD=${DB_PASSWORD}
      - POSTGRES_DB=pythowncloud
    mem_limit: 128m

volumes:
  pgdata:
```

Alternatively, PythOwnCloud could share the PostgreSQL instance already running for Nextcloud — just create a separate `pythowncloud` database. This saves ~80MB of RAM by avoiding a second Postgres container.


## File Structure After Phase 2

```
requirements.txt
Dockerfile
docker-compose.yml
pythowncloud/
├── main.py              # FastAPI app, API endpoints
├── config.py            # Settings (env vars)
├── auth.py              # API key auth (Phase 1) + session auth (Phase 2)
├── db.py                # Database connection pool, queries
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
- **Scan can be slow.** A full scan of thousands of files computes SHA-256 for each changed file. On the Pi 3 over USB 2.0, scanning the full 5TB drive could take a long time. The scan should run in the background and report progress.
- **Templates are server-rendered.** No live-updating UI — you refresh the page to see changes. This is a feature, not a bug: no WebSocket overhead, no JS framework, no build step.


## Success Criteria

Phase 2 is complete when:

1. File metadata is stored in PostgreSQL and survives container restarts
2. `GET /files/{path}` directory listings are served from the database (no per-file checksum computation)
3. `POST /api/scan` walks the filesystem and reconciles the database
4. Opening `http://100.93.58.13:8000/` in a browser shows a login page
5. After login, the file browser displays directories and files with correct metadata
6. Files can be uploaded and deleted from the browser
7. Sessions expire after the configured TTL
8. Total memory usage of PythOwnCloud + PostgreSQL stays under 200MB