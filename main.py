"""
PythOwnCloud Server — Phase 1: Core File API
A lightweight self-hosted cloud storage API for Raspberry Pi.
"""

import os
import hashlib
import mimetypes
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, UploadFile, Request, Depends
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from config import settings
from auth import verify_api_key

# ─── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="PythOwnCloud",
    version="0.1.0",
    description="Lightweight self-hosted cloud storage API",
)

STORAGE = Path(settings.storage_path)


# ─── Models ────────────────────────────────────────────────────────────────────

class FileInfo(BaseModel):
    name: str
    path: str
    size: int
    is_dir: bool
    modified: str
    checksum: str | None = None


class DirectoryListing(BaseModel):
    path: str
    items: list[FileInfo]
    total: int


class UploadResponse(BaseModel):
    path: str
    size: int
    checksum: str
    message: str


# ─── Helpers ───────────────────────────────────────────────────────────────────

def safe_path(user_path: str) -> Path:
    """
    Resolve a user-provided path against STORAGE root.
    Prevents directory traversal attacks (../../etc/passwd).
    """
    cleaned = Path(user_path.lstrip("/"))
    full = (STORAGE / cleaned).resolve()
    if not str(full).startswith(str(STORAGE.resolve())):
        raise HTTPException(status_code=403, detail="Path traversal denied")
    return full


def file_checksum(filepath: Path, algo: str = "sha256") -> str:
    """Compute checksum of a file without loading it all in memory."""
    h = hashlib.new(algo)
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def file_info(filepath: Path, relative_to: Path = STORAGE) -> FileInfo:
    """Build a FileInfo object from a filesystem path."""
    stat = filepath.stat()
    return FileInfo(
        name=filepath.name,
        path=str(filepath.relative_to(relative_to)),
        size=stat.st_size,
        is_dir=filepath.is_dir(),
        modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        checksum=None if filepath.is_dir() else file_checksum(filepath),
    )


# ─── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Quick health check."""
    return {"status": "ok", "storage": str(STORAGE), "writable": os.access(STORAGE, os.W_OK)}


@app.get("/files/{file_path:path}", response_model=None)
async def get_file(file_path: str, _key: str = Depends(verify_api_key)):
    """
    GET /files/<path>
    - If path is a directory → return JSON listing
    - If path is a file     → return the file contents (download)
    """
    target = safe_path(file_path)

    if not target.exists():
        raise HTTPException(status_code=404, detail="Not found")

    # Directory → listing
    if target.is_dir():
        items = []
        for child in sorted(target.iterdir()):
            if child.name.startswith("."):
                continue  # skip hidden files
            try:
                items.append(file_info(child))
            except PermissionError:
                continue
        return DirectoryListing(
            path=str(target.relative_to(STORAGE)),
            items=items,
            total=len(items),
        )

    # File → stream it back
    media_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    return FileResponse(
        path=str(target),
        media_type=media_type,
        filename=target.name,
    )


@app.get("/files/", response_model=DirectoryListing)
async def list_root(_key: str = Depends(verify_api_key)):
    """List the storage root directory."""
    return await get_file("", _key=_key)


@app.put("/files/{file_path:path}")
async def upload_file(
    file_path: str,
    file: UploadFile,
    _key: str = Depends(verify_api_key),
):
    """
    PUT /files/<path>
    Upload (or overwrite) a file at the given path.
    Parent directories are created automatically.
    """
    target = safe_path(file_path)

    if target.is_dir():
        raise HTTPException(status_code=400, detail="Cannot overwrite a directory")

    # Ensure parent dirs exist
    target.parent.mkdir(parents=True, exist_ok=True)

    # Stream to disk in chunks (important for Pi 3 memory)
    size = 0
    h = hashlib.sha256()
    with open(target, "wb") as f:
        while chunk := await file.read(8192):
            f.write(chunk)
            h.update(chunk)
            size += len(chunk)

    return UploadResponse(
        path=str(target.relative_to(STORAGE)),
        size=size,
        checksum=h.hexdigest(),
        message="uploaded",
    )


@app.delete("/files/{file_path:path}")
async def delete_file(file_path: str, _key: str = Depends(verify_api_key)):
    """
    DELETE /files/<path>
    Delete a single file. Refuses to delete directories (safety).
    """
    target = safe_path(file_path)

    if not target.exists():
        raise HTTPException(status_code=404, detail="Not found")

    if target.is_dir():
        raise HTTPException(
            status_code=400,
            detail="Directory deletion not allowed via API (safety)"
        )

    size = target.stat().st_size
    target.unlink()
    return {"path": file_path, "size": size, "message": "deleted"}


@app.post("/mkdir/{dir_path:path}")
async def make_directory(dir_path: str, _key: str = Depends(verify_api_key)):
    """Create a directory (and parents if needed)."""
    target = safe_path(dir_path)
    target.mkdir(parents=True, exist_ok=True)
    return {"path": dir_path, "message": "created"}
