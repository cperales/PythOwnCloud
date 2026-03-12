"""Login / logout web UI routes."""
import logging
from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from pythowncloud.config import settings
from pythowncloud.auth import verify_password, create_session
import pythowncloud.db as db

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()


@router.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/browse/", status_code=302)


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request):
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": None, "show_logout": False}
    )


@router.post("/login", include_in_schema=False)
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
        secure=settings.session_cookie_secure,
        max_age=settings.session_ttl_days * 86400,
    )
    return redirect


@router.post("/logout", include_in_schema=False)
async def logout(session: str | None = Cookie(default=None)):
    if session:
        await db.delete_session(session)
    redirect = RedirectResponse(url="/login", status_code=303)
    redirect.delete_cookie("session")
    return redirect
