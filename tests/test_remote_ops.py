import pytest
from fastapi.testclient import TestClient
from polymarket.apps.dashboard import app

client = TestClient(app)

def test_remote_update_endpoint():
    response = client.post("/api/ops/update")
    assert response.status_code == 200
    data = response.json()
    assert data.get("status") == "ok"
    assert data.get("action") == "update"
    assert "timestamp" in data

def test_remote_restart_endpoint():
    response = client.post("/api/ops/restart")
    assert response.status_code == 200
    data = response.json()
    assert data.get("status") == "ok"
    assert data.get("action") == "restart"
    assert "timestamp" in data
