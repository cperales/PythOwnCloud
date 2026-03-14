import pytest
from fastapi.testclient import TestClient

# Import the FastAPI app and related modules for monkey‑patching during tests.
from pythowncloud.main import app as _app
from pythowncloud.config import settings as cfg_settings
from pythowncloud.auth import verify_api_key_or_session
import pythowncloud.routers.nicegui_ui as gui

@pytest.fixture(autouse=True)
def set_dummy_api_key(monkeypatch):
    """
    Ensure a deterministic API key is present during the test run.
    """
    monkeypatch.setattr(cfg_settings, "api_key", "testkey")

# ---------------------------------------------------------------------------
# Helper to stub out external calls made by the NiceGUI page.
async def _fake_api_get(path: str, auth_token=None):  # pragma: no cover
    return {"items": []}

def test_index_route_success(monkeypatch):
    """
    Verify that a request with a valid API key renders the UI label and returns HTTP 200.
    """
    # Override dependency to provide an authenticated token without performing real auth logic.
    _app.dependency_overrides[verify_api_key_or_session] = lambda: "testkey"

    # Replace api_get with a stub that avoids external network traffic.
    monkeypatch.setattr(gui, "api_get", _fake_api_get)

    client = TestClient(_app)
    response = client.get("/", headers={"X-API-Key": "testkey"})
    assert response.status_code == 200
    assert "PythOwnCloud — NiceGUI UI" in response.text

def test_index_route_unauth(monkeypatch):
    """
    Verify that accessing the page without authentication results in a 401 error.
    """
    # Ensure no dependency override is present so FastAPI will enforce auth.
    _app.dependency_overrides.pop(verify_api_key_or_session, None)

    client = TestClient(_app)
    response = client.get("/")
    assert response.status_code == 401 or response.status_code == 307
