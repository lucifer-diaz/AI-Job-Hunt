import os
os.environ["LLM_PROVIDER"] = "mock"
os.environ["DATABASE_URL"] = "sqlite:///./test_api.db"

from fastapi.testclient import TestClient
from app.main import app


def test_health_and_dashboard():
    client = TestClient(app)
    assert client.get('/health').status_code == 200
    r = client.get('/')
    assert r.status_code == 200
    assert 'Job Hunt OS' in r.text
