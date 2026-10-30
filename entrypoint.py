from pythowncloud.logging_config import configure_logging

configure_logging()

from pythowncloud.main import app  # noqa: E402,F401
