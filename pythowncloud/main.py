"""
PythOwnCloud Server — Phase 2: Metadata DB & Web File Browser, Phase 5: WebDAV & TUS
"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import pythowncloud.db as db
from pythowncloud.config import settings
from pythowncloud.routers import login, files, dirs, browse, search, webdav, tus
from pythowncloud.routers.tus import cleanup_abandoned_uploads

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await db.create_pool()
    await db.init_schema()

    # Ensure TUS upload directory exists
    settings.tus_upload_path.mkdir(parents=True, exist_ok=True)

    # Schedule TUS cleanup as a background task
    asyncio.create_task(cleanup_abandoned_uploads())

    yield

    # Shutdown
    await db.close_pool()


app = FastAPI(
    title="PythOwnCloud",
    version="0.2.0",
    description="Lightweight self-hosted cloud storage API",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(login.router)
app.include_router(files.router)
app.include_router(dirs.router)
app.include_router(browse.router)
app.include_router(search.router)
app.include_router(webdav.router)
app.include_router(tus.router)
