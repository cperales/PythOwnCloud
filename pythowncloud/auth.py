"""
Authentication — API key (scripts) and session cookie (browser), HTTP Basic Auth (WebDAV/TUS).
Behind Tailscale this is a second layer of defense, not the only one.
"""

import base64
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Cookie, HTTPException, Security, Header
from fastapi.security import APIKeyHeader

from pythowncloud.config import settings
import pythowncloud.db as db

# ─── API key auth (Phase 1, unchanged) ─────────────────────────────────────────

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: str = Security(API_KEY_HEADER)):
    if api_key is None:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    if api_key != settings.api_key:
        raise HTTPException(status_code=403, detail="Invalid API key")
    return api_key


# ─── Session / browser auth (Phase 2) ──────────────────────────────────────────

def verify_password(plain: str) -> bool:
    """Return True if plain matches the stored bcrypt hash."""
    if not settings.login_password_hash:
        return False
    return bcrypt.checkpw(plain.encode(), settings.login_password_hash.encode())


async def create_session() -> str:
    """Generate a session token, persist it, return the token."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.session_ttl_days)
    await db.create_session(token, expires_at)
    return token


async def verify_session(session: str | None = Cookie(default=None)) -> str:
    """
    FastAPI dependency for session-protected endpoints.
    Redirects to /login (307) when no valid session is present.
    """
    if session is None:
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    if db.get_pool() is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    row = await db.get_session(session)
    if row is None:
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    return session


# ─── Combined auth (Phase 3) ───────────────────────────────────────────────

async def verify_api_key_or_session(
    api_key: str | None = Security(API_KEY_HEADER),
    session: str | None = Cookie(default=None),
) -> str:
    """
    Accept either a valid X-API-Key header OR a valid session cookie.
    Used for endpoints that serve both scripts and browsers (e.g., /thumb/, /files/).
    """
    # Try API key first
    if api_key is not None and api_key == settings.api_key:
        return api_key

    # Fall back to session cookie
    if session is not None and db.get_pool() is not None:
        row = await db.get_session(session)
        if row is not None:
            return session

    raise HTTPException(status_code=401, detail="Valid API key or session required")


# ─── HTTP Basic Auth (Phase 5: WebDAV, TUS) ──────────────────────────────

async def verify_basic_auth(authorization: str | None = Header(default=None)) -> str:
    """
    Verify HTTP Basic Auth (Authorization: Basic base64(user:pass)).
    The username is accepted as anything (single-user app).
    The password is verified against the login password hash.
    Returns the username (always "admin" for single-user).
    Used by WebDAV and TUS endpoints.
    """
    if not authorization or not authorization.lower().startswith("basic "):
        raise HTTPException(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="PythOwnCloud"'},
            detail="Authentication required",
        )

    try:
        encoded = authorization[6:]  # skip "Basic "
        decoded = base64.b64decode(encoded).decode()
        username, password = decoded.split(":", 1)
    except Exception:
        raise HTTPException(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="PythOwnCloud"'},
            detail="Invalid Basic Auth format",
        )

    if not verify_password(password):
        raise HTTPException(
            status_code=403,
            headers={"WWW-Authenticate": 'Basic realm="PythOwnCloud"'},
            detail="Invalid password",
        )

    return username
