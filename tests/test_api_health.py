"""
Tests — Health endpoints (/health, /ready, /version)
"""
from __future__ import annotations

async def test_health_returns_ok(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "version" in data
    assert "uptime_seconds" in data


async def test_ready_returns_ok(client):
    resp = await client.get("/ready")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["checks"]["database"] == "ok"


async def test_version_returns_expected_fields(client):
    resp = await client.get("/version")
    assert resp.status_code == 200
    data = resp.json()
    assert "version" in data
    assert "environment" in data
    assert "auth_mode" in data
