"""Shared helpers: path safety, checksum, file metadata, breadcrumbs."""
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

from pythowncloud.config import settings
from pythowncloud.models import FileInfo


def get_storage() -> Path:
    """Return the storage path (allows tests to override via settings)."""
    return Path(settings.storage_path)


def safe_path(user_path: str) -> Path:
    """Resolve a user-provided path against STORAGE root, blocking traversal."""
    storage = get_storage()
    cleaned = Path(user_path.lstrip("/"))
    full = (storage / cleaned).resolve()
    if not str(full).startswith(str(storage.resolve())):
        raise HTTPException(status_code=403, detail="Path traversal denied")
    return full


def file_checksum(filepath: Path, algo: str = "sha256") -> str:
    h = hashlib.new(algo)
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def file_info(filepath: Path, relative_to: Path | None = None) -> FileInfo:
    if relative_to is None:
        relative_to = get_storage()
    stat = filepath.stat()
    return FileInfo(
        name=filepath.name,
        path=str(filepath.relative_to(relative_to)),
        size=stat.st_size,
        is_dir=filepath.is_dir(),
        modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        checksum=None if filepath.is_dir() else file_checksum(filepath),
    )


def _build_breadcrumbs(path: str) -> list[dict]:
    crumbs = [{"label": "root", "url": "/browse/"}]
    if not path:
        return crumbs
    parts = Path(path).parts
    for i, part in enumerate(parts):
        url = "/browse/" + "/".join(parts[: i + 1]) + "/"
        crumbs.append({"label": part, "url": url})
    return crumbs


def _parent_url(path: str) -> str:
    if not path:
        return "/browse/"
    parent = str(Path(path).parent)
    if parent == ".":
        return "/browse/"
    return f"/browse/{parent}/"
