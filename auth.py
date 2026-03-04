"""
Authentication — simple API key via header or query param.
Behind Tailscale this is a second layer of defense, not the only one.
"""

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from config import settings

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: str = Security(API_KEY_HEADER)):
    if api_key is None:
        raise HTTPException(
            status_code=401,
            detail="Missing X-API-Key header",
        )
    if api_key != settings.api_key:
        raise HTTPException(
            status_code=403,
            detail="Invalid API key",
        )
    return api_key
