import os
os.environ["LLM_PROVIDER"] = "mock"
# Tests never use DATABASE_URL: set TEST_DATABASE_URL to run them against Postgres.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "sqlite:///./test_api.db")

from fastapi.testclient import TestClient
from app.main import app


def test_health_and_dashboard():
    client = TestClient(app)
    assert client.get('/health').status_code == 200
    r = client.get('/')
    assert r.status_code == 200
    assert 'Job Hunt OS' in r.text


def test_the_asset_version_changes_when_any_script_changes():
    """Regression: resume_data.js was missing from a hand-kept list, so its changes never reached browsers."""
    import os
    from pathlib import Path

    import app.main as main

    static = Path(main.__file__).parent / "static"
    before = main._asset_version()
    target = static / "resume_data.js"
    stat = target.stat()
    try:
        os.utime(target, (stat.st_atime, stat.st_mtime + 10_000))
        assert main._asset_version() != before
    finally:
        os.utime(target, (stat.st_atime, stat.st_mtime))
