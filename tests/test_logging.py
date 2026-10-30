"""
Tests for pythowncloud.logging_config: handler/format/level setup and the
/health access-log filter.
"""

import logging
import re

import pytest

from pythowncloud.config import settings
from pythowncloud.logging_config import HealthAccessFilter, configure_logging


@pytest.fixture
def restore_logging():
    """Undo configure_logging() so caplog keeps working in other tests."""
    poc = logging.getLogger("pythowncloud")
    access = logging.getLogger("uvicorn.access")
    level, handlers, propagate = poc.level, poc.handlers[:], poc.propagate
    filters = access.filters[:]
    yield
    poc.setLevel(level)
    poc.handlers[:] = handlers
    poc.propagate = propagate
    access.filters[:] = filters


def _access_record(path, status):
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", path, "1.1", status), None,
    )


class TestConfigureLogging:
    def test_info_record_is_printed_with_timestamp_level_and_name(
        self, restore_logging, monkeypatch, capsys
    ):
        monkeypatch.setattr(settings, "log_level", "INFO")
        configure_logging()

        logging.getLogger("pythowncloud.s3").info("S3 incoming: %s", "GET /storage")

        err = capsys.readouterr().err
        assert re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", err)
        assert "INFO pythowncloud.s3: S3 incoming: GET /storage" in err
        assert logging.getLogger("pythowncloud").propagate is False

    def test_level_setting_suppresses_lower_levels(
        self, restore_logging, monkeypatch, capsys
    ):
        monkeypatch.setattr(settings, "log_level", "warning")
        configure_logging()

        log = logging.getLogger("pythowncloud.s3")
        log.info("hidden")
        log.warning("shown")

        err = capsys.readouterr().err
        assert "hidden" not in err
        assert "WARNING pythowncloud.s3: shown" in err

    def test_invalid_level_raises(self, restore_logging, monkeypatch):
        monkeypatch.setattr(settings, "log_level", "LOUD")
        with pytest.raises(ValueError, match="POC_LOG_LEVEL"):
            configure_logging()

    def test_repeated_calls_do_not_duplicate_handler(self, restore_logging):
        configure_logging()
        before = len(logging.getLogger("pythowncloud").handlers)
        configure_logging()
        assert len(logging.getLogger("pythowncloud").handlers) == before
        access_filters = logging.getLogger("uvicorn.access").filters
        assert sum(isinstance(f, HealthAccessFilter) for f in access_filters) == 1


class TestHealthAccessFilter:
    def test_drops_successful_health_check(self):
        assert HealthAccessFilter().filter(_access_record("/health", 200)) is False

    def test_keeps_failed_health_check(self):
        assert HealthAccessFilter().filter(_access_record("/health", 500)) is True

    def test_keeps_other_paths(self):
        f = HealthAccessFilter()
        assert f.filter(_access_record("/storage/x", 200)) is True
        assert f.filter(_access_record("/health/extra", 200)) is True

    def test_keeps_records_without_access_args(self):
        record = logging.LogRecord(
            "uvicorn.access", logging.INFO, "", 0, "plain message", (), None
        )
        assert HealthAccessFilter().filter(record) is True
