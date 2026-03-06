"""
PythOwnCloud Server — Phase 2: Metadata DB & Web File Browser
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import pythowncloud.db as db
from pythowncloud.routers import login, files, dirs, browse, search

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.create_pool()
    await db.init_schema()
    yield
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
