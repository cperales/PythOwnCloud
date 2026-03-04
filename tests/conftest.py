"""
Shared pytest configuration and fixtures for tests/
"""

import pytest


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "db: mark test as requiring a PostgreSQL database"
    )
