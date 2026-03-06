"""
PythOwnCloud Server — Phase 2: Metadata DB & Web File Browser
"""

import logging
import mimetypes
import os
import hashlib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from cachetools import TTLCache
from fastapi import BackgroundTasks, Cookie, Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from pythowncloud.config import settings
from pythowncloud.auth import verify_api_key, verify_password, create_session, verify_session, verify_api_key_or_session
import pythowncloud.db as db
import pythowncloud.scanner as scanner
import pythowncloud.thumbnails as thumbnails

logger = logging.getLogger(__name__)

STORAGE = Path(settings.storage_path)
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


# ─── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.create_pool()
    await db.init_schema()
    yield
    await db.close_pool()


# ─── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="PythOwnCloud",
    version="0.2.0",
    description="Lightweight self-hosted cloud storage API",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# ─── Caching (Phase 3) ──────────────────────────────────────────────────────

_listing_cache: TTLCache[str, list[dict]] = TTLCache(maxsize=256, ttl=30)


def _invalidate_listing_cache(rel_path: str) -> None:
    """Invalidate the listing cache entry for the parent directory of rel_path."""
    parent = str(Path(rel_path).parent)
    cache_key = parent if parent != "." else ""
    _listing_cache.pop(cache_key, None)


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
    """Resolve a user-provided path against STORAGE root, blocking traversal."""
    cleaned = Path(user_path.lstrip("/"))
    full = (STORAGE / cleaned).resolve()
    if not str(full).startswith(str(STORAGE.resolve())):
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
        relative_to = STORAGE
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


# ─── Web UI endpoints ───────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/browse/", status_code=302)


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request):
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": None, "show_logout": False}
    )


@app.post("/login", include_in_schema=False)
async def login(request: Request, password: str = Form(...)):
    if not verify_password(password):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid password", "show_logout": False},
            status_code=401,
        )
    if db.get_pool() is None:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Database not configured", "show_logout": False},
            status_code=503,
        )
    token = await create_session()
    redirect = RedirectResponse(url="/browse/", status_code=303)
    redirect.set_cookie(
        key="session",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=settings.session_ttl_days * 86400,
    )
    return redirect


@app.post("/logout", include_in_schema=False)
async def logout(session: str | None = Cookie(default=None)):
    if session:
        await db.delete_session(session)
    redirect = RedirectResponse(url="/login", status_code=303)
    redirect.delete_cookie("session")
    return redirect


@app.get("/thumb/{file_path:path}", include_in_schema=False)
async def get_thumbnail(
    file_path: str,
    _auth: str = Depends(verify_api_key_or_session),
):
    """Serve a WebP thumbnail for the given file path."""
    rel_path = file_path.strip("/")

    # Path traversal protection
    safe_path(rel_path)

    # Check extension
    ext = Path(rel_path).suffix.lstrip(".").lower()
    if not thumbnails.is_thumbable(ext):
        raise HTTPException(status_code=404, detail="No thumbnail for this file type")

    thumb = await thumbnails.ensure_thumbnail(rel_path, ext)
    if thumb is None or not thumb.exists():
        raise HTTPException(status_code=404, detail="Thumbnail unavailable")

    return FileResponse(
        path=str(thumb),
        media_type="image/webp",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/browse/", response_class=HTMLResponse, include_in_schema=False)
@app.get("/browse/{browse_path:path}", response_class=HTMLResponse, include_in_schema=False)
async def browse(
    request: Request,
    browse_path: str = "",
    _session: str = Depends(verify_session),
):
    if db.get_pool() is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    browse_path = browse_path.strip("/")
    target = safe_path(browse_path)

    if not target.exists():
        raise HTTPException(status_code=404, detail="Directory not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Not a directory")

    # Use cached listing if available
    cache_key = browse_path or "/"
    rows = _listing_cache.get(cache_key)
    if rows is None:
        rows = await db.list_directory(browse_path)
        _listing_cache[cache_key] = rows

    # Annotate rows with thumbnail availability
    for row in rows:
        if not row["is_dir"] and thumbnails.is_thumbable(row.get("extension")):
            row["has_thumb"] = thumbnails.thumbnail_exists(row["path"])
        else:
            row["has_thumb"] = False

    total_size = sum(r["size"] for r in rows if not r["is_dir"])

    return templates.TemplateResponse(
        "browse.html",
        {
            "request": request,
            "show_logout": True,
            "current_path": browse_path,
            "breadcrumbs": _build_breadcrumbs(browse_path),
            "parent_url": _parent_url(browse_path),
            "items": rows,
            "total_size": total_size,
        },
    )


# ─── API search & scan ──────────────────────────────────────────────────────────

@app.get("/api/search")
async def search_files(
    q: str | None = None,
    extension: str | None = None,
    modified_after: datetime | None = None,
    modified_before: datetime | None = None,
    limit: int = 100,
    _key: str = Depends(verify_api_key),
):
    if db.get_pool() is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    results = await db.search_files(q, extension, modified_after, modified_before, limit)
    return {"results": results, "total": len(results)}


_scan_running = False


@app.post("/api/scan")
async def trigger_scan(
    background_tasks: BackgroundTasks,
    _key: str = Depends(verify_api_key),
):
    global _scan_running
    if _scan_running:
        raise HTTPException(status_code=409, detail="Scan already in progress")
    if db.get_pool() is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    _scan_running = True

    async def _run_and_clear():
        global _scan_running
        try:
            await scanner.run_scan()
        finally:
            _listing_cache.clear()
            _scan_running = False

    background_tasks.add_task(_run_and_clear)
    return {"message": "Scan started", "status": "running"}


# ─── File API (Phase 1, extended with DB hooks) ─────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "storage": str(STORAGE),
        "writable": os.access(STORAGE, os.W_OK),
        "db": db.get_pool() is not None,
    }


@app.get("/files/{file_path:path}", response_model=None)
async def get_file(file_path: str, _key: str = Depends(verify_api_key)):
    """
    GET /files/<path>
    - Directory → JSON listing (DB-first, filesystem fallback)
    - File       → stream download
    """
    target = safe_path(file_path)

    if not target.exists():
        raise HTTPException(status_code=404, detail="Not found")

    if target.is_dir():
        rel = str(target.relative_to(STORAGE))
        db_rows = await db.list_directory(rel)
        if db_rows:
            items = [
                FileInfo(
                    name=r["filename"],
                    path=r["path"],
                    size=r["size"],
                    is_dir=r["is_dir"],
                    modified=r["modified_at"],  # Already an ISO 8601 string from DB
                    checksum=r["checksum"] or None,
                )
                for r in db_rows
            ]
            return DirectoryListing(path=rel, items=items, total=len(items))

        # Fallback: compute from filesystem (Phase 1 behaviour)
        items = []
        for child in sorted(target.iterdir()):
            if child.name.startswith("."):
                continue
            try:
                items.append(file_info(child))
            except PermissionError:
                continue
        return DirectoryListing(
            path=str(target.relative_to(STORAGE)),
            items=items,
            total=len(items),
        )

    media_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    return FileResponse(path=str(target), media_type=media_type, filename=target.name)


@app.get("/files/", response_model=DirectoryListing)
async def list_root(_key: str = Depends(verify_api_key)):
    return await get_file("", _key=_key)


@app.put("/files/{file_path:path}")
async def upload_file(
    file_path: str,
    file: UploadFile,
    _auth: str = Depends(verify_api_key_or_session),
):
    target = safe_path(file_path)

    if target.is_dir():
        raise HTTPException(status_code=400, detail="Cannot overwrite a directory")

    target.parent.mkdir(parents=True, exist_ok=True)

    size = 0
    h = hashlib.sha256()
    with open(target, "wb") as f:
        while chunk := await file.read(8192):
            f.write(chunk)
            h.update(chunk)
            size += len(chunk)

    # Upsert metadata (best-effort — DB failure must not break upload)
    if db.get_pool() is not None:
        try:
            mtime = datetime.fromtimestamp(target.stat().st_mtime, tz=timezone.utc)
            ext = target.suffix.lstrip(".").lower() or None
            await db.upsert_file(
                path=str(target.relative_to(STORAGE)),
                filename=target.name,
                extension=ext,
                size=size,
                checksum=h.hexdigest(),
                is_dir=False,
                modified_at=mtime,
            )
        except Exception:
            logger.warning("DB upsert failed after upload of %s", file_path, exc_info=True)

    # Generate thumbnail for supported types (best-effort)
    rel = str(target.relative_to(STORAGE))
    ext_lower = target.suffix.lstrip(".").lower()
    if thumbnails.is_thumbable(ext_lower):
        try:
            thumbnails.invalidate_thumbnail(rel)
            await thumbnails.ensure_thumbnail(rel, ext_lower)
        except Exception:
            logger.warning("Thumbnail generation failed for %s", file_path, exc_info=True)

    # Invalidate listing cache for parent directory
    _invalidate_listing_cache(rel)

    return UploadResponse(
        path=str(target.relative_to(STORAGE)),
        size=size,
        checksum=h.hexdigest(),
        message="uploaded",
    )


@app.delete("/files/{file_path:path}")
async def delete_file(file_path: str, _auth: str = Depends(verify_api_key_or_session)):
    target = safe_path(file_path)

    if not target.exists():
        raise HTTPException(status_code=404, detail="Not found")

    if target.is_dir():
        raise HTTPException(
            status_code=400,
            detail="Directory deletion not allowed via API (safety)",
        )

    size = target.stat().st_size
    rel = str(target.relative_to(STORAGE))
    target.unlink()

    # Remove thumbnail (best-effort)
    try:
        thumbnails.invalidate_thumbnail(rel)
    except Exception:
        logger.warning("Thumbnail cleanup failed for %s", file_path, exc_info=True)

    # Invalidate listing cache for parent directory
    _invalidate_listing_cache(rel)

    # Remove from DB (best-effort)
    if db.get_pool() is not None:
        try:
            await db.delete_file_row(str(target.relative_to(STORAGE)))
        except Exception:
            logger.warning("DB delete failed after removal of %s", file_path, exc_info=True)

    return {"path": file_path, "size": size, "message": "deleted"}


@app.post("/mkdir/{dir_path:path}")
async def make_directory(dir_path: str, _key: str = Depends(verify_api_key)):
    target = safe_path(dir_path)
    target.mkdir(parents=True, exist_ok=True)
    return {"path": dir_path, "message": "created"}
