"""
Configuration — loaded from environment variables or .env file.
"""

from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Path to the root storage directory (your external drive)
    storage_path: str = "/data"

    # Path to the database directory (can be on faster storage like SD card)
    db_path_dir: str = "/data"

    # API key for authentication — required, set via POC_API_KEY
    api_key: str = ""

    # Server settings
    host: str = "0.0.0.0"
    port: int = 8000

    # Max upload size in bytes (default 2 GB)
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024

    # Phase 2: scrypt hash of the web UI login password
    # Generate: python3 -c "from pythowncloud.passwords import hash_password; print(hash_password('pw'))"
    login_password_hash: str = ""

    # Phase 2: session TTL in days
    session_ttl_days: int = 7

    # Set to true when the server is behind HTTPS (sets Secure flag on session cookie)
    session_cookie_secure: bool = False

    # Phase 3: Thumbnails
    thumb_width: int = 320
    thumb_quality: int = 80
    thumb_max_source_bytes: int = 500 * 1024 * 1024  # skip huge files in scan
    thumb_cache_ttl: int = 60                          # TTLCache seconds
    thumb_max_concurrent: int = 2                      # max ffmpeg processes

    # Phase 3.2: Deferred thumbnails during bulk uploads
    thumb_burst_window_seconds: int = 30       # sliding window size
    thumb_burst_threshold: int = 5             # uploads within window to trigger deferral
    thumb_burst_cooldown_seconds: int = 60     # how long after last upload to consider burst over
    thumb_auto_scan_after_burst: bool = True   # trigger a scan when burst subsides (unused, skipped)

    # Phase 5: TUS resumable uploads
    tus_max_age_hours: int = 24                        # cleanup abandoned uploads after N hours

    # Phase 5.2: S3-compatible API
    s3_access_key: str = "pythowncloud"               # AWS access key ID
    s3_secret_key: str = ""                           # AWS secret access key (required)
    s3_region: str = "us-east-1"                      # AWS region (arbitrary, must match client)

    model_config = {"env_prefix": "POC_", "env_file": ".env", "extra": "ignore"}

    @model_validator(mode="after")
    def _require_secrets(self) -> "Settings":
        missing = []
        if not self.api_key:
            missing.append("POC_API_KEY")
        if not self.login_password_hash:
            missing.append("POC_LOGIN_PASSWORD_HASH")
        if not self.s3_secret_key:
            missing.append("POC_S3_SECRET_KEY")
        if missing:
            raise ValueError(
                f"Required environment variables not set: {', '.join(missing)}. "
                "Copy .env.example to .env and fill in real values before starting the server."
            )
        return self

    @property
    def db_path(self) -> Path:
        """Path to the SQLite database file (can be on separate faster storage)."""
        return Path(self.db_path_dir) / ".pythowncloud.db"

    @property
    def thumbnails_path(self) -> Path:
        """Path to the thumbnails directory (hidden in storage)."""
        return Path(self.storage_path) / ".thumbnails"

    @property
    def tus_upload_path(self) -> Path:
        """Path to the TUS partial uploads directory (hidden in storage)."""
        return Path(self.storage_path) / ".uploads"


settings = Settings()
