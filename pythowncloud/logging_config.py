"""
Logging setup for the pythowncloud.* loggers.

uvicorn only configures its own loggers, so without this the application's
records fall through to Python's last-resort handler (WARNING and above, no
timestamp, no level, no logger name).
"""
import logging

from pythowncloud.config import settings

_HANDLER_NAME = "pythowncloud-stderr"
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class HealthAccessFilter(logging.Filter):
    """Drop successful GET /health records from the uvicorn access log.

    uvicorn logs access records as (client_addr, method, full_path,
    http_version, status_code). Failed health checks are kept on purpose.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5:
            return not (args[2] == "/health" and args[4] == 200)
        return True


_health_filter = HealthAccessFilter()


def configure_logging() -> None:
    level = settings.log_level.upper()
    if level not in logging.getLevelNamesMapping():
        raise ValueError(f"Invalid POC_LOG_LEVEL: {settings.log_level!r}")

    poc_logger = logging.getLogger("pythowncloud")
    poc_logger.setLevel(level)
    poc_logger.propagate = False
    if not any(h.get_name() == _HANDLER_NAME for h in poc_logger.handlers):
        handler = logging.StreamHandler()
        handler.set_name(_HANDLER_NAME)
        handler.setFormatter(logging.Formatter(_FORMAT))
        poc_logger.addHandler(handler)

    logging.getLogger("uvicorn.access").addFilter(_health_filter)
