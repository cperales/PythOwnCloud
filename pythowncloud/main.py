"""
PythOwnCloud Server — Phase 2: Metadata DB & Web File Browser, Phase 5: WebDAV & TUS
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

import pythowncloud.db as db
from pythowncloud.config import settings
from pythowncloud.routers import login, files, dirs, browse, search, webdav, tus
from pythowncloud.routers.tus import cleanup_abandoned_uploads

logger = logging.getLogger("uvicorn.access")

STATIC_DIR = Path(__file__).parent / "static"


class HealthCheckFilter(BaseHTTPMiddleware):
    """Log /health checks at DEBUG level instead of INFO to reduce noise."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        # Set log level to DEBUG for /health checks
        if request.url.path == "/health":
            logger.debug(
                f'{request.client.host}:{request.client.port} - "{request.method} {request.url.path} HTTP/1.1" {response.status_code}'
            )
        return response


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

app.add_middleware(HealthCheckFilter)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(login.router)
app.include_router(files.router)
app.include_router(dirs.router)
app.include_router(browse.router)
app.include_router(search.router)
app.include_router(webdav.router)
app.include_router(tus.router)
