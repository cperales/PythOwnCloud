"""Directory listing TTLCache — shared across routers."""
from cachetools import TTLCache
from pathlib import Path

_listing_cache: TTLCache[str, list[dict]] = TTLCache(maxsize=256, ttl=30)


def invalidate_listing_cache(rel_path: str) -> None:
    """Invalidate the listing cache entry for the parent directory of rel_path."""
    parent = str(Path(rel_path).parent)
    cache_key = parent if parent != "." else ""
    _listing_cache.pop(cache_key, None)
