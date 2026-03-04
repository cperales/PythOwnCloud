"""
Database — asyncpg connection pool and all SQL query functions.
The pool is only created when POC_DB_URL is set; all functions
are no-ops (return empty results) when the pool is None.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import asyncpg

from pythowncloud.config import settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS files (
    id          SERIAL PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,
    filename    TEXT NOT NULL,
    extension   TEXT,
    size        BIGINT NOT NULL,
    checksum    TEXT NOT NULL,
    is_dir      BOOLEAN NOT NULL DEFAULT FALSE,
    author      TEXT NOT NULL DEFAULT 'admin',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    modified_at TIMESTAMPTZ NOT NULL,
    scanned_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_files_path_parent ON files (path text_pattern_ops);
CREATE INDEX IF NOT EXISTS idx_files_filename    ON files (filename);
CREATE INDEX IF NOT EXISTS idx_files_extension   ON files (extension);
CREATE INDEX IF NOT EXISTS idx_files_modified    ON files (modified_at);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);
"""


# ─── Pool lifecycle ─────────────────────────────────────────────────────────────

async def create_pool() -> None:
    global _pool
    if settings.db_url is None:
        return
    _pool = await asyncpg.create_pool(settings.db_url, min_size=1, max_size=5)
    logger.info("DB pool created")


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("DB pool closed")


def get_pool() -> asyncpg.Pool | None:
    return _pool


async def init_schema() -> None:
    if _pool is None:
        return
    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
    logger.info("DB schema initialised")


# ─── File queries ───────────────────────────────────────────────────────────────

async def upsert_file(
    *,
    path: str,
    filename: str,
    extension: str | None,
    size: int,
    checksum: str,
    is_dir: bool,
    modified_at: datetime,
) -> None:
    if _pool is None:
        return
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO files (path, filename, extension, size, checksum, is_dir, modified_at, scanned_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, now())
            ON CONFLICT (path) DO UPDATE
                SET filename    = EXCLUDED.filename,
                    extension   = EXCLUDED.extension,
                    size        = EXCLUDED.size,
                    checksum    = EXCLUDED.checksum,
                    is_dir      = EXCLUDED.is_dir,
                    modified_at = EXCLUDED.modified_at,
                    scanned_at  = now()
            """,
            path, filename, extension, size, checksum, is_dir, modified_at,
        )


async def delete_file_row(path: str) -> None:
    if _pool is None:
        return
    async with _pool.acquire() as conn:
        await conn.execute("DELETE FROM files WHERE path = $1", path)


async def get_file_row(path: str) -> dict[str, Any] | None:
    if _pool is None:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM files WHERE path = $1", path)
    return dict(row) if row else None


async def list_directory(parent_path: str) -> list[dict[str, Any]]:
    """Return direct children of parent_path, dirs first then files."""
    if _pool is None:
        return []
    # Root is stored with empty string; sub-paths with no leading slash
    if parent_path in ("", "/"):
        # Top-level: path has no slash
        query = """
            SELECT * FROM files
            WHERE path NOT LIKE '%/%'
              AND path != ''
            ORDER BY is_dir DESC, filename ASC
        """
        args: tuple = ()
    else:
        p = parent_path.strip("/")
        query = """
            SELECT * FROM files
            WHERE path LIKE $1 || '/%'
              AND path NOT LIKE $1 || '/%/%'
            ORDER BY is_dir DESC, filename ASC
        """
        args = (p,)
    async with _pool.acquire() as conn:
        rows = await conn.fetch(query, *args)
    return [dict(r) for r in rows]


async def search_files(
    q: str | None = None,
    extension: str | None = None,
    modified_after: datetime | None = None,
    modified_before: datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if _pool is None:
        return []
    conditions: list[str] = []
    params: list[Any] = []
    idx = 1

    if q:
        conditions.append(f"filename ILIKE ${idx}")
        params.append(f"%{q}%")
        idx += 1
    if extension:
        conditions.append(f"extension = ${idx}")
        params.append(extension.lstrip(".").lower())
        idx += 1
    if modified_after:
        conditions.append(f"modified_at >= ${idx}")
        params.append(modified_after)
        idx += 1
    if modified_before:
        conditions.append(f"modified_at <= ${idx}")
        params.append(modified_before)
        idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    query = f"SELECT * FROM files {where} ORDER BY modified_at DESC LIMIT ${idx}"

    async with _pool.acquire() as conn:
        rows = await conn.fetch(query, *params)
    return [dict(r) for r in rows]


async def delete_files_not_in(existing_paths: list[str]) -> int:
    """Delete DB rows whose paths are not in the given list. Returns count deleted."""
    if _pool is None:
        return 0
    async with _pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM files WHERE NOT (path = ANY($1::text[]))",
            existing_paths,
        )
    # result is like "DELETE 3"
    return int(result.split()[-1])


# ─── Session queries ─────────────────────────────────────────────────────────────

async def create_session(token: str, expires_at: datetime) -> None:
    if _pool is None:
        return
    async with _pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO sessions (token, expires_at) VALUES ($1, $2)",
            token, expires_at,
        )


async def get_session(token: str) -> dict[str, Any] | None:
    """Return session row only if it exists and has not expired."""
    if _pool is None:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM sessions WHERE token = $1 AND expires_at > now()",
            token,
        )
    return dict(row) if row else None


async def delete_session(token: str) -> None:
    if _pool is None:
        return
    async with _pool.acquire() as conn:
        await conn.execute("DELETE FROM sessions WHERE token = $1", token)


async def purge_expired_sessions() -> int:
    if _pool is None:
        return 0
    async with _pool.acquire() as conn:
        result = await conn.execute("DELETE FROM sessions WHERE expires_at <= now()")
    return int(result.split()[-1])
