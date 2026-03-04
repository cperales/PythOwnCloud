"""
Configuration — loaded from environment variables or .env file.
"""

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

    model_config = {"env_prefix": "POC_", "env_file": ".env"}


settings = Settings()
