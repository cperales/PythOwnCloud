"""
Configuration — loaded from environment variables or .env file.
"""

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Path to the root storage directory (your external drive)
    storage_path: str = "/data"

    # API key for authentication — change this!
    api_key: str = "a1b2c3d4"

    # Server settings
    host: str = "0.0.0.0"
    port: int = 8000

    # Max upload size in bytes (default 2 GB)
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024

    # Phase 2: bcrypt hash of the web UI login password
    # Generate: python3 -c "import bcrypt; print(bcrypt.hashpw(b'pw', bcrypt.gensalt()).decode())"
    login_password_hash: str = ""

    # Phase 2: session TTL in days
    session_ttl_days: int = 7

    # Phase 3: Thumbnails
    thumb_width: int = 320
    thumb_quality: int = 80
    thumb_max_source_bytes: int = 500 * 1024 * 1024  # skip huge files in scan
    thumb_cache_ttl: int = 60                          # TTLCache seconds
    thumb_max_concurrent: int = 2                      # max ffmpeg processes

    # Phase 5: TUS resumable uploads
    tus_max_age_hours: int = 24                        # cleanup abandoned uploads after N hours

    model_config = {"env_prefix": "POC_", "env_file": ".env"}

    @property
    def db_path(self) -> Path:
        """Path to the SQLite database file (hidden in storage)."""
        return Path(self.storage_path) / ".pythowncloud.db"

    @property
    def thumbnails_path(self) -> Path:
        """Path to the thumbnails directory (hidden in storage)."""
        return Path(self.storage_path) / ".thumbnails"

    @property
    def tus_upload_path(self) -> Path:
        """Path to the TUS partial uploads directory (hidden in storage)."""
        return Path(self.storage_path) / ".uploads"


settings = Settings()
