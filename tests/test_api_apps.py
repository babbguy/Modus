"""
Tests — Apps API (register, list, pagination)
"""
from __future__ import annotations

async def test_list_apps_empty(client):
    resp = await client.get("/api/v1/apps")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_apps_pagination_params(client):
    resp = await client.get("/api/v1/apps", params={"limit": 1, "offset": 0})
    assert resp.status_code == 200


async def test_list_apps_invalid_limit(client):
    resp = await client.get("/api/v1/apps", params={"limit": 0})
    assert resp.status_code == 422


async def test_list_apps_limit_too_high(client):
    resp = await client.get("/api/v1/apps", params={"limit": 999})
    assert resp.status_code == 422
