"""Browse UI routes: GET /browse/*, GET /thumb/*."""
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from pythowncloud.auth import verify_session, verify_api_key_or_session
from pythowncloud.helpers import get_storage, safe_path, _build_breadcrumbs, _parent_url
from pythowncloud.cache import _listing_cache
import pythowncloud.db as db
import pythowncloud.thumbnails as thumbnails

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()


@router.get("/thumb/{file_path:path}", include_in_schema=False)
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


@router.get("/browse/", response_class=HTMLResponse, include_in_schema=False)
@router.get("/browse/{browse_path:path}", response_class=HTMLResponse, include_in_schema=False)
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

    # Exclude ignored folders such as ".thumb"
    IGNORED_FOLDERS = {".thumb"}
    rows = [row for row in rows if not (row["is_dir"] and row["filename"] in IGNORED_FOLDERS)]

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
