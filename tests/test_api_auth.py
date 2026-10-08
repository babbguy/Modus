"""
Tests — Auth modes (stub passthrough, JWT rejection without token)
"""
from __future__ import annotations


async def test_stub_mode_allows_requests(client):
    """In stub auth mode, requests should pass without a token."""
    resp = await client.get("/api/v1/apps")
    assert resp.status_code == 200


async def test_stub_mode_health_no_auth(client):
    """Health endpoints never require auth."""
    resp = await client.get("/health")
    assert resp.status_code == 200


async def test_jwt_mode_rejects_missing_token(client):
    """
    When auth_mode is jwt, endpoints should reject requests without a Bearer token.
    We test this by temporarily overriding the auth mode at the settings level.
    """
    from orchestrator.core.config import settings

    original = settings.auth_mode
    try:
        # Force JWT mode
        object.__setattr__(settings, "auth_mode", "jwt")
        resp = await client.get("/api/v1/apps")
        assert resp.status_code == 401
    finally:
        object.__setattr__(settings, "auth_mode", original)
