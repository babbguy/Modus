"""
Tests — master key as a platform-admin service identity (X-Modus-APIKey).

The policy CLI, GitHub Action, seed script and dashboard authenticate admin
endpoints with the master key in both stub and jwt auth modes.
"""
from __future__ import annotations

import time

import jwt as pyjwt
import pytest

from orchestrator.core.config import get_settings

# NB: ``orchestrator.core.config.settings`` is a lazy proxy; mutate the real
# cached instance, not the proxy.
settings = get_settings()

ADMIN_URL = "/api/v1/apps"
SECRET = "unit-test-jwt-secret-0123456789abcdef"


@pytest.fixture
def jwt_mode():
    originals = {k: getattr(settings, k) for k in ("auth_mode", "jwt_secret", "jwt_issuer", "jwt_audience")}
    object.__setattr__(settings, "auth_mode", "jwt")
    object.__setattr__(settings, "jwt_secret", SECRET)
    object.__setattr__(settings, "jwt_issuer", "")
    object.__setattr__(settings, "jwt_audience", "")
    yield
    for k, v in originals.items():
        object.__setattr__(settings, k, v)


def _master_headers(key: str | None = None) -> dict[str, str]:
    return {"X-Modus-APIKey": key or settings.master_api_key}


async def test_jwt_mode_master_key_reaches_admin_endpoint(client, jwt_mode):
    resp = await client.get(ADMIN_URL, headers=_master_headers())
    assert resp.status_code == 200


async def test_jwt_mode_master_key_reaches_policies_admin(client, jwt_mode):
    resp = await client.get("/api/v1/policies", headers=_master_headers())
    assert resp.status_code == 200


async def test_jwt_mode_wrong_master_key_is_401(client, jwt_mode):
    resp = await client.get(ADMIN_URL, headers=_master_headers("mds_master_definitely_not_the_key_000"))
    assert resp.status_code == 401


async def test_jwt_mode_app_key_is_not_accepted_as_admin(client, jwt_mode):
    resp = await client.get(ADMIN_URL, headers=_master_headers("mds_abcdefghijklmnopqrstuvwxyz012345"))
    assert resp.status_code == 401


async def test_jwt_mode_no_credentials_is_401(client, jwt_mode):
    resp = await client.get(ADMIN_URL)
    assert resp.status_code == 401


async def test_jwt_mode_bearer_jwt_still_works(client, jwt_mode):
    token = pyjwt.encode(
        {"sub": "alice", "exp": int(time.time()) + 600, "cs_role": "platform_admin"},
        SECRET,
        algorithm="HS256",
    )
    resp = await client.get(ADMIN_URL, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


async def test_jwt_mode_master_key_in_bearer_is_not_a_jwt(client, jwt_mode):
    """The master key is only honoured in X-Modus-APIKey, never as a Bearer JWT."""
    resp = await client.get(ADMIN_URL, headers={"Authorization": f"Bearer {settings.master_api_key}"})
    assert resp.status_code == 401


async def test_stub_mode_master_key_accepted(client):
    resp = await client.get(ADMIN_URL, headers=_master_headers())
    assert resp.status_code == 200


async def test_stub_mode_no_credentials_still_open(client):
    resp = await client.get(ADMIN_URL)
    assert resp.status_code == 200


async def test_master_identity_is_platform_admin_service_actor():
    from types import SimpleNamespace

    from orchestrator.core.auth import get_identity

    req = SimpleNamespace(headers={"X-Modus-APIKey": settings.master_api_key})
    ident = await get_identity(req, None)
    assert ident.actor_id == "master-key"
    assert ident.is_platform_admin


def test_verify_master_key_rejects_empty_and_non_ascii():
    from orchestrator.core.auth import verify_master_key

    assert verify_master_key(settings.master_api_key)
    assert not verify_master_key("")
    assert not verify_master_key("mds_master_éééééééééé")


async def test_dashboard_login_probe_endpoint_accepts_master_key_and_rejects_garbage(client, jwt_mode):
    """dashboard/js/login-gate.js verifies a credential with GET /api/v1/users/me."""
    ok = await client.get("/api/v1/users/me", headers=_master_headers())
    assert ok.status_code == 200 and ok.json()["role"] == "platform_admin"
    bad = await client.get("/api/v1/users/me", headers=_master_headers("mds_master_definitely_not_the_key_000"))
    assert bad.status_code == 401
    anon = await client.get("/api/v1/users/me")
    assert anon.status_code == 401


async def test_rotate_key_works_with_master_key_header_in_jwt_mode(client, jwt_mode, registered_app):
    resp = await client.post(
        f"/api/v1/apps/{registered_app['app_uuid']}/rotate-key", headers=_master_headers()
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["api_key"] != registered_app["api_key"]
    denied = await client.post(f"/api/v1/apps/{registered_app['app_uuid']}/rotate-key")
    assert denied.status_code == 401
