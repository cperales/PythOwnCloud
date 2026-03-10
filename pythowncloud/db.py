"""
Database — aiosqlite connection and all SQL query functions.
The connection is opened on startup and lives in the Python process.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from pythowncloud.config import settings

logger = logging.getLogger(__name__)

_conn: aiosqlite.Connection | None = None

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL UNIQUE,
    filename    TEXT NOT NULL,
    extension   TEXT,
    size        INTEGER NOT NULL,
    checksum    TEXT NOT NULL,
    is_dir      INTEGER NOT NULL DEFAULT 0,
    author      TEXT NOT NULL DEFAULT 'admin',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    modified_at TEXT NOT NULL,
    scanned_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_files_filename  ON files (filename);
CREATE INDEX IF NOT EXISTS idx_files_extension ON files (extension);
CREATE INDEX IF NOT EXISTS idx_files_modified  ON files (modified_at);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    expires_at  TEXT NOT NULL
);
"""


# ─── Connection lifecycle ────────────────────────────────────────────────────

async def create_pool() -> None:
    """Open the SQLite connection and enable WAL mode."""
    global _conn
    db_path = settings.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _conn = await aiosqlite.connect(str(db_path))
    except sqlite3.OperationalError as e:
        logger.error(f"Failed to connect to DB at {db_path}: {e}")
        raise
    # Enable WAL mode for better concurrency
    await _conn.execute("PRAGMA journal_mode=WAL")
    # Set row_factory so rows can be accessed by column name
    _conn.row_factory = aiosqlite.Row
    logger.info(f"DB connection opened at {db_path}")


async def is_empty() -> bool:
    """Return True if the files table has no rows (fresh DB)."""
    if _conn is None:
        return True
    cursor = await _conn.execute("SELECT COUNT(*) FROM files")
    row = await cursor.fetchone()
    return row[0] == 0


async def close_pool() -> None:
    """Close the SQLite connection."""
    global _conn
    if _conn:
        await _conn.close()
        _conn = None
        logger.info("DB connection closed")


def get_pool() -> aiosqlite.Connection | None:
    """Return the SQLite connection (or None if not initialized)."""
    return _conn


async def init_schema() -> None:
    """Create tables and indexes."""
    if _conn is None:
        return
    await _conn.executescript(SCHEMA_SQL)
    await _conn.commit()
    logger.info("DB schema initialised")


# ─── File queries ────────────────────────────────────────────────────────────

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
    """Upsert a file record. INSERT ... ON CONFLICT DO UPDATE."""
    if _conn is None:
        return
    # Convert datetime to ISO 8601 string
    modified_str = modified_at.isoformat()
    await _conn.execute(
        """
        INSERT INTO files (path, filename, extension, size, checksum, is_dir, modified_at, scanned_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        ON CONFLICT (path) DO UPDATE
            SET filename    = excluded.filename,
                extension   = excluded.extension,
                size        = excluded.size,
                checksum    = excluded.checksum,
                is_dir      = excluded.is_dir,
                modified_at = excluded.modified_at,
                scanned_at  = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        """,
        (path, filename, extension, size, checksum, int(is_dir), modified_str),
    )
    await _conn.commit()


async def delete_file_row(path: str) -> None:
    """Delete a file record by path."""
    if _conn is None:
        return
    await _conn.execute("DELETE FROM files WHERE path = ?", (path,))
    await _conn.commit()


async def delete_directory_rows(path: str) -> None:
    """Delete a directory record and all descendants."""
    if _conn is None:
        return
    await _conn.execute(
        "DELETE FROM files WHERE path = ? OR path LIKE ?",
        (path, path + "/%"),
    )
    await _conn.commit()


async def get_file_row(path: str) -> dict[str, Any] | None:
    """Get a single file record by path."""
    if _conn is None:
        return None
    cursor = await _conn.execute("SELECT * FROM files WHERE path = ?", (path,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_directory(parent_path: str) -> list[dict[str, Any]]:
    """Return direct children of parent_path, dirs first then files."""
    if _conn is None:
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
        cursor = await _conn.execute(query)
    else:
        p = parent_path.strip("/")
        query = """
            SELECT * FROM files
            WHERE path LIKE ? || '/%'
              AND path NOT LIKE ? || '/%/%'
            ORDER BY is_dir DESC, filename ASC
        """
        cursor = await _conn.execute(query, (p, p))

    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def search_files(
    q: str | None = None,
    extension: str | None = None,
    modified_after: datetime | None = None,
    modified_before: datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Search files by name, extension, and date range."""
    if _conn is None:
        return []

    conditions: list[str] = []
    params: list[Any] = []

    if q:
        conditions.append("filename LIKE ?")
        params.append(f"%{q}%")
    if extension:
        conditions.append("extension = ?")
        params.append(extension.lstrip(".").lower())
    if modified_after:
        conditions.append("modified_at >= ?")
        params.append(modified_after.isoformat())
    if modified_before:
        conditions.append("modified_at <= ?")
        params.append(modified_before.isoformat())

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    query = f"SELECT * FROM files {where} ORDER BY modified_at DESC LIMIT ?"

    cursor = await _conn.execute(query, params)
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def delete_files_not_in(existing_paths: list[str]) -> int:
    """Delete DB rows whose paths are not in the given list. Returns count deleted."""
    if _conn is None:
        return 0

    if not existing_paths:
        # Delete all rows if list is empty (rare, but safe)
        cursor = await _conn.execute("DELETE FROM files")
    else:
        # Build WHERE clause with ? placeholders
        placeholders = ",".join(["?" for _ in existing_paths])
        cursor = await _conn.execute(
            f"DELETE FROM files WHERE path NOT IN ({placeholders})",
            existing_paths,
        )

    await _conn.commit()
    # SQLite cursor.rowcount gives the number of rows affected
    return cursor.rowcount


async def move_file_row(source_path: str, dest_path: str) -> None:
    """Move a single file record: update path, filename, and extension."""
    if _conn is None:
        return

    dest_filename = Path(dest_path).name
    dest_ext = Path(dest_path).suffix.lstrip(".").lower() if "." in dest_path else ""

    await _conn.execute(
        "UPDATE files SET path = ?, filename = ?, extension = ? WHERE path = ?",
        (dest_path, dest_filename, dest_ext, source_path),
    )
    await _conn.commit()


async def move_directory_rows(source_prefix: str, dest_prefix: str) -> int:
    """Move all rows with source_prefix path to use dest_prefix. Returns count updated."""
    if _conn is None:
        return 0

    # Find all rows that start with source_prefix
    cursor = await _conn.execute(
        "SELECT path FROM files WHERE path LIKE ? || '/%' OR path = ?",
        (source_prefix, source_prefix),
    )
    rows = await cursor.fetchall()

    count = 0
    for (old_path,) in rows:
        # Replace the source prefix with the destination prefix
        if old_path == source_prefix:
            new_path = dest_prefix
        else:
            # old_path is like "source/subdir/file.txt"
            # Replace "source" with "dest"
            suffix = old_path[len(source_prefix) + 1:]  # Skip the slash
            new_path = f"{dest_prefix}/{suffix}"

        new_filename = Path(new_path).name
        new_ext = Path(new_path).suffix.lstrip(".").lower() if "." in new_path else ""

        await _conn.execute(
            "UPDATE files SET path = ?, filename = ?, extension = ? WHERE path = ?",
            (new_path, new_filename, new_ext, old_path),
        )
        count += 1

    await _conn.commit()
    return count


async def list_all_directories() -> list[str]:
    """Return all directory paths in the database, sorted."""
    if _conn is None:
        return []

    cursor = await _conn.execute("SELECT path FROM files WHERE is_dir = 1 ORDER BY path")
    rows = await cursor.fetchall()
    return [row[0] for row in rows]


# ─── Session queries ────────────────────────────────────────────────────────

async def create_session(token: str, expires_at: datetime) -> None:
    """Create a new session."""
    if _conn is None:
        return
    expires_str = expires_at.isoformat()
    await _conn.execute(
        "INSERT INTO sessions (token, expires_at) VALUES (?, ?)",
        (token, expires_str),
    )
    await _conn.commit()


async def get_session(token: str) -> dict[str, Any] | None:
    """Return session row only if it exists and has not expired."""
    if _conn is None:
        return None
    cursor = await _conn.execute(
        "SELECT * FROM sessions WHERE token = ? AND expires_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now')",
        (token,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def delete_session(token: str) -> None:
    """Delete a session."""
    if _conn is None:
        return
    await _conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    await _conn.commit()


async def purge_expired_sessions() -> int:
    """Delete all expired sessions. Returns count deleted."""
    if _conn is None:
        return 0
    cursor = await _conn.execute(
        "DELETE FROM sessions WHERE expires_at <= strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
    )
    await _conn.commit()
    return cursor.rowcount
