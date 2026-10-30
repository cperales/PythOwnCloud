"""
PythOwnCloud Server — Phase 2: Metadata DB & Web File Browser, Phase 5: WebDAV & TUS
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

import pythowncloud.db as db
from pythowncloud.config import settings
from pythowncloud.routers import login, files, dirs, browse, search, webdav, s3
from pythowncloud.s3_xml import build_error
from pythowncloud.uploads import cleanup_abandoned_uploads
from pythowncloud.scanner import run_scan

logger = logging.getLogger("uvicorn.access")
s3_logger = logging.getLogger("pythowncloud.s3")

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


class S3RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log all requests that carry S3/AWS auth headers, regardless of path."""

    async def dispatch(self, request: Request, call_next):
        auth = request.headers.get("Authorization", "")
        has_s3_header_auth = auth.startswith("AWS4-HMAC-SHA256")
        has_presigned = "X-Amz-Signature" in request.url.query

        if has_s3_header_auth or has_presigned:
            # Log only non-sensitive amz headers (exclude credential/signature values)
            _SENSITIVE = {"x-amz-security-token"}
            amz_headers = {
                k: v for k, v in request.headers.items()
                if k.lower().startswith("x-amz-") and k.lower() not in _SENSITIVE
            }
            s3_logger.info(
                "S3 incoming: %s %s | auth=%s | amz=%s",
                request.method,
                request.url.path,
                "presigned" if has_presigned else "header",
                amz_headers,
            )

        response = await call_next(request)

        if (has_s3_header_auth or has_presigned) and response.status_code >= 400:
            s3_logger.warning(
                "S3 error response: %s %s → %d  (S3 API is at /storage/...)",
                request.method, request.url.path, response.status_code,
            )

        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await db.create_pool()
    await db.init_schema()

    if await db.is_empty():
        logger.info("Fresh database detected — triggering initial scan")
        asyncio.create_task(run_scan())

    # Ensure upload directory exists
    settings.tus_upload_path.mkdir(parents=True, exist_ok=True)

    # Schedule cleanup of abandoned uploads as a background task
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
app.add_middleware(S3RequestLoggingMiddleware)


@app.exception_handler(404)
async def not_found_handler(request: Request, _exc):
    """Return S3 XML errors for S3-authenticated requests that hit unknown paths."""
    auth = request.headers.get("Authorization", "")
    has_s3_auth = auth.startswith("AWS4-HMAC-SHA256") or "X-Amz-Signature" in request.url.query
    if has_s3_auth:
        s3_logger.warning(
            "S3 404: %s %s — endpoint must include /storage/ (configure as http://host:8000/)",
            request.method, request.url.path,
        )
        return Response(
            content=build_error("NoSuchKey", "The specified key does not exist", request.url.path),
            media_type="application/xml",
            status_code=404,
        )
    return JSONResponse(status_code=404, content={"detail": "Not Found"})

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(login.router)
app.include_router(files.router)
app.include_router(dirs.router)
app.include_router(browse.router)
app.include_router(search.router)
app.include_router(webdav.router)
# Removed prefixed S3 router mount
app.include_router(s3.router)  # S3-compatible API at /storage/ (root mount only)
