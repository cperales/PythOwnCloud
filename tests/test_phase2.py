"""
Tests for PythOwnCloud — Phase 2: Metadata DB & Web File Browser.

DB-dependent tests are skipped when POC_DB_URL is not set.
Run with a real Postgres:
    POC_DB_URL=postgresql://... pytest tests/test_phase2.py
"""

import os
import pytest
from fastapi.testclient import TestClient
from pathlib import Path

import pythowncloud.config as config

API_KEY = "test-secret-key-phase2"


@pytest.fixture
def client_no_db(tmp_path):
    """Fixture that provides a test client with fresh temp storage, no DB."""
    import pythowncloud.main as main
    original_storage = main.STORAGE
    original_path = config.settings.storage_path
    original_key = config.settings.api_key
    original_hash = config.settings.login_password_hash
    original_db_url = config.settings.db_url

    try:
        config.settings.storage_path = str(tmp_path)
        config.settings.api_key = API_KEY
        config.settings.db_url = None
        import bcrypt
        config.settings.login_password_hash = bcrypt.hashpw(b"hunter2", bcrypt.gensalt()).decode()
        main.STORAGE = Path(tmp_path)

        yield TestClient(main.app, raise_server_exceptions=True)
    finally:
        config.settings.storage_path = original_path
        config.settings.api_key = original_key
        config.settings.db_url = original_db_url
        config.settings.login_password_hash = original_hash
        main.STORAGE = original_storage


@pytest.fixture
def auth():
    return {"X-API-Key": API_KEY}


# ─── Config ──────────────────────────────────────────────────────────────────────

class TestConfig:
    def test_db_url_none_by_default(self):
        cfg = config.Settings()
        assert cfg.db_url is None

    def test_session_ttl_default(self):
        cfg = config.Settings()
        assert cfg.session_ttl_days == 7

    def test_login_password_hash_default_empty(self):
        cfg = config.Settings()
        assert cfg.login_password_hash == ""


# ─── Password verification ────────────────────────────────────────────────────────

class TestVerifyPassword:
    def test_correct_password(self):
        from pythowncloud.auth import verify_password
        import bcrypt
        original = config.settings.login_password_hash
        config.settings.login_password_hash = bcrypt.hashpw(b"hunter2", bcrypt.gensalt()).decode()
        assert verify_password("hunter2") is True
        config.settings.login_password_hash = original

    def test_wrong_password(self):
        from pythowncloud.auth import verify_password
        import bcrypt
        original = config.settings.login_password_hash
        config.settings.login_password_hash = bcrypt.hashpw(b"hunter2", bcrypt.gensalt()).decode()
        assert verify_password("wrong") is False
        config.settings.login_password_hash = original

    def test_empty_hash_returns_false(self):
        from pythowncloud.auth import verify_password
        original = config.settings.login_password_hash
        config.settings.login_password_hash = ""
        assert verify_password("anything") is False
        config.settings.login_password_hash = original


# ─── Login page ──────────────────────────────────────────────────────────────────

class TestLoginPage:
    def test_get_login_returns_html(self, client_no_db):
        r = client_no_db.get("/login")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert b"password" in r.content.lower()

    def test_post_wrong_password_returns_401(self, client_no_db):
        r = client_no_db.post("/login", data={"password": "wrongpass"}, follow_redirects=False)
        assert r.status_code == 401

    def test_post_correct_password_no_db_returns_503(self, client_no_db):
        r = client_no_db.post("/login", data={"password": "hunter2"}, follow_redirects=False)
        assert r.status_code == 503

    def test_root_redirects_to_browse(self, client_no_db):
        r = client_no_db.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/browse/"


# ─── Browse without session ───────────────────────────────────────────────────────

class TestBrowseNoSession:
    def test_browse_without_session_redirects(self, client_no_db):
        r = client_no_db.get("/browse/", follow_redirects=False)
        assert r.status_code == 307
        assert "/login" in r.headers["location"]

    def test_browse_path_without_session_redirects(self, client_no_db):
        r = client_no_db.get("/browse/somepath/", follow_redirects=False)
        assert r.status_code == 307


# ─── API endpoints without DB ────────────────────────────────────────────────────

class TestApiNoDB:
    def test_health_shows_db_false(self, client_no_db):
        r = client_no_db.get("/health")
        assert r.status_code == 200
        assert r.json()["db"] is False

    def test_search_without_db_returns_503(self, client_no_db, auth):
        r = client_no_db.get("/api/search", headers=auth)
        assert r.status_code == 503

    def test_scan_without_db_returns_503(self, client_no_db, auth):
        r = client_no_db.post("/api/scan", headers=auth)
        assert r.status_code == 503

    def test_scan_requires_api_key(self, client_no_db):
        r = client_no_db.post("/api/scan")
        assert r.status_code == 401

    def test_search_requires_api_key(self, client_no_db):
        r = client_no_db.get("/api/search")
        assert r.status_code == 401


# ─── Phase 1 still works without DB ──────────────────────────────────────────────

class TestPhase1NoDB:
    def test_upload_works_without_db(self, client_no_db, auth):
        r = client_no_db.put(
            "/files/p2test/hello.txt",
            headers=auth,
            files={"file": ("hello.txt", b"phase2 content", "text/plain")},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["message"] == "uploaded"

    def test_download_works_without_db(self, client_no_db, auth):
        client_no_db.put(
            "/files/p2test/dl.txt",
            headers=auth,
            files={"file": ("dl.txt", b"download me", "text/plain")},
        )
        r = client_no_db.get("/files/p2test/dl.txt", headers=auth)
        assert r.status_code == 200
        assert r.content == b"download me"

    def test_listing_fallback_to_filesystem(self, client_no_db, auth):
        client_no_db.put(
            "/files/p2fs/test.txt",
            headers=auth,
            files={"file": ("test.txt", b"fallback test", "text/plain")},
        )
        r = client_no_db.get("/files/p2fs/", headers=auth)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["items"], list)
        assert any(i["name"] == "test.txt" for i in body["items"])

    def test_delete_works_without_db(self, client_no_db, auth):
        client_no_db.put(
            "/files/p2test/del.txt",
            headers=auth,
            files={"file": ("del.txt", b"bye", "text/plain")},
        )
        r = client_no_db.delete("/files/p2test/del.txt", headers=auth)
        assert r.status_code == 200
        assert r.json()["message"] == "deleted"


# ─── DB integration tests (require POC_DB_URL) ───────────────────────────────────

def _skip_no_db():
    return pytest.mark.skipif(
        not os.getenv("POC_DB_URL"),
        reason="POC_DB_URL not set — skipping DB integration tests",
    )


@_skip_no_db()
class TestDBQueries:
    @pytest.fixture(autouse=True)
    async def setup_pool(self):
        import pythowncloud.db as db
        config.settings.db_url = os.environ["POC_DB_URL"]
        await db.create_pool()
        await db.init_schema()
        pool = db.get_pool()
        async with pool.acquire() as conn:
            await conn.execute("TRUNCATE files, sessions RESTART IDENTITY CASCADE")
        yield
        await db.close_pool()
        config.settings.db_url = None

    async def test_upsert_and_get_file(self):
        import pythowncloud.db as db
        from datetime import datetime, timezone
        mtime = datetime.now(timezone.utc)
        await db.upsert_file(
            path="test/file.txt",
            filename="file.txt",
            extension="txt",
            size=42,
            checksum="abc123",
            is_dir=False,
            modified_at=mtime,
        )
        row = await db.get_file_row("test/file.txt")
        assert row is not None
        assert row["size"] == 42
        assert row["checksum"] == "abc123"

    async def test_upsert_updates_existing(self):
        import pythowncloud.db as db
        from datetime import datetime, timezone
        mtime = datetime.now(timezone.utc)
        await db.upsert_file(path="u.txt", filename="u.txt", extension="txt",
                              size=1, checksum="old", is_dir=False, modified_at=mtime)
        await db.upsert_file(path="u.txt", filename="u.txt", extension="txt",
                              size=2, checksum="new", is_dir=False, modified_at=mtime)
        row = await db.get_file_row("u.txt")
        assert row["size"] == 2
        assert row["checksum"] == "new"

    async def test_delete_file_row(self):
        import pythowncloud.db as db
        from datetime import datetime, timezone
        mtime = datetime.now(timezone.utc)
        await db.upsert_file(path="d.txt", filename="d.txt", extension="txt",
                              size=1, checksum="x", is_dir=False, modified_at=mtime)
        await db.delete_file_row("d.txt")
        assert await db.get_file_row("d.txt") is None

    async def test_list_directory_direct_children(self):
        import pythowncloud.db as db
        from datetime import datetime, timezone
        mtime = datetime.now(timezone.utc)
        for p, name in [("dir/a.txt", "a.txt"), ("dir/b.txt", "b.txt"), ("dir/sub/c.txt", "c.txt")]:
            await db.upsert_file(path=p, filename=name, extension="txt",
                                  size=1, checksum="x", is_dir=False, modified_at=mtime)
        rows = await db.list_directory("dir")
        names = [r["filename"] for r in rows]
        assert "a.txt" in names
        assert "b.txt" in names
        assert "c.txt" not in names

    async def test_search_by_filename(self):
        import pythowncloud.db as db
        from datetime import datetime, timezone
        mtime = datetime.now(timezone.utc)
        await db.upsert_file(path="sunset.jpg", filename="sunset.jpg", extension="jpg",
                              size=100, checksum="x", is_dir=False, modified_at=mtime)
        await db.upsert_file(path="notes.txt", filename="notes.txt", extension="txt",
                              size=10, checksum="y", is_dir=False, modified_at=mtime)
        results = await db.search_files(q="sunset")
        assert len(results) == 1
        assert results[0]["filename"] == "sunset.jpg"

    async def test_search_by_extension(self):
        import pythowncloud.db as db
        from datetime import datetime, timezone
        mtime = datetime.now(timezone.utc)
        await db.upsert_file(path="img.png", filename="img.png", extension="png",
                              size=50, checksum="z", is_dir=False, modified_at=mtime)
        results = await db.search_files(extension="png")
        assert any(r["extension"] == "png" for r in results)

    async def test_delete_files_not_in(self):
        import pythowncloud.db as db
        from datetime import datetime, timezone
        mtime = datetime.now(timezone.utc)
        for p, n in [("keep.txt", "keep.txt"), ("remove.txt", "remove.txt")]:
            await db.upsert_file(path=p, filename=n, extension="txt",
                                  size=1, checksum="x", is_dir=False, modified_at=mtime)
        deleted = await db.delete_files_not_in(["keep.txt"])
        assert deleted == 1
        assert await db.get_file_row("keep.txt") is not None
        assert await db.get_file_row("remove.txt") is None

    async def test_session_lifecycle(self):
        import pythowncloud.db as db
        from datetime import datetime, timedelta, timezone
        token = "test-token-abc"
        expires = datetime.now(timezone.utc) + timedelta(days=1)
        await db.create_session(token, expires)
        row = await db.get_session(token)
        assert row is not None
        assert row["token"] == token
        await db.delete_session(token)
        assert await db.get_session(token) is None

    async def test_expired_session_not_returned(self):
        import pythowncloud.db as db
        from datetime import datetime, timedelta, timezone
        token = "expired-token"
        past = datetime.now(timezone.utc) - timedelta(days=1)
        await db.create_session(token, past)
        assert await db.get_session(token) is None

    async def test_purge_expired_sessions(self):
        import pythowncloud.db as db
        from datetime import datetime, timedelta, timezone
        token = "old-token"
        past = datetime.now(timezone.utc) - timedelta(days=1)
        await db.create_session(token, past)
        purged = await db.purge_expired_sessions()
        assert purged >= 1
