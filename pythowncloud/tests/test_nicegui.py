import os
from fastapi.testclient import TestClient
import pytest

# Ensure the test environment has required secrets set.
os.environ.setdefault("POC_API_KEY", "test-api-key")
os.environ.setdefault("POC_LOGIN_PASSWORD_HASH", "hash")
os.environ.setdefault("POC_S3_SECRET_KEY", "secret")

from pythowncloud.main import app  # Import after setting env vars so Settings loads correctly.

# The NiceGUI page is mounted at the root path '/'. It requires an API key for authenticated requests to /files.
@pytest.fixture(scope="module")
def client():
    return TestClient(app)

def test_index_route_status(client):
    """The '/' route should be reachable and return a 200 status code."""
    response = client.get("/", headers={"X-API-Key": os.getenv("POC_API_KEY")})
    assert response.status_code == 200
    # The rendered page contains the label we added in nicegui_ui.
    assert "PythOwnCloud — NiceGUI UI" in response.text

# Additional sanity checks can be performed by inspecting the presence of expected HTML elements.
def test_index_page_contains_upload_and_table(client):
    """Verify that the rendered page includes an upload button and a table element."""
    html = client.get("/", headers={"X-API-Key": os.getenv("POC_API_KEY")}).text
    # NiceGUI renders its components inside <div class="nicegui"> tags.
    assert "<ui-upload" in html or "Upload file" in html  # upload component label
    assert "<table" in html or "name" in html and "size" in html
