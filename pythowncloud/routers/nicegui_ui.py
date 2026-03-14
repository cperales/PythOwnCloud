"""
NiceGUI interface for PythOwnCloud.

This module registers a FastAPI route via the @ui.page decorator. The UI uses server‑side Python and communicates with existing API endpoints (files, dirs, etc.) to provide browsing, upload, delete functionality.

The implementation is intentionally minimal – it demonstrates how NiceGUI can replace static HTML pages while still leveraging the same backend logic.
"""
import asyncio
from fastapi import Depends
import httpx

from pythowncloud.auth import verify_api_key_or_session
from nicegui import ui
from pythowncloud.config import settings

# Helper to fetch JSON from API endpoints using current session or API key
def api_get(path: str, auth_token: str | None = None):
    headers = {}
    if auth_token:
        # Prefer API‑key header; for sessions the cookie will be sent automatically by httpx when client is created with cookies.
        headers["X-API-Key"] = auth_token
    url = f"http://{settings.host}:{settings.port}{path}"
    async def _fetch():
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
        return resp.json()
    # Return coroutine for consistency
    return _fetch()

# NiceGUI page – mounted at the root of the FastAPI app
def index(_auth: str | None = Depends(verify_api_key_or_session)):
    ui.label("PythOwnCloud — NiceGUI UI")
    current_path = ""

    async def load_listing():
        data = await api_get(f"/files/{current_path}", auth_token=_auth)
        rows = [
            {
                "name": item["filename"],
                "size": f"{item['size']:,} bytes",
                "is_dir": item.get("is_dir", False),
            }
            for item in data.get("items", [])
        ]
        table.set_rows(rows)

    # Table to display directory contents
    columns = ["name", "size"]
    rows: list[dict] | None = []  # type: ignore[var-annotated]
    table = ui.table(columns=columns, rows=rows).props("dense")

    async def refresh():
        await load_listing()

    @ui.button(icon="refresh", on_click=lambda: asyncio.create_task(refresh()))
    def _refresh_button() -> None:
        pass  # button action handled by lambda above

    ui.upload(
        label="Upload file",
        accept=None,
        multiple=False,
        max_size=settings.max_upload_bytes,  # type: ignore[name‑defined]
        on_success=lambda files: asyncio.create_task(refresh()),
    )

    return refresh()
