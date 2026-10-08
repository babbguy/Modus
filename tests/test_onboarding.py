"""
Tests — Onboarding Wizard API
Covers: status, apply (managed + manual), upload, complete, YAML parser.
"""
from __future__ import annotations

# ── Status endpoint ──────────────────────────────────────────────────────────

async def test_onboarding_status_fresh(client):
    resp = await client.get("/api/v1/onboarding/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["needs_onboarding"] is True
    assert body["has_teams"] is False
    assert body["has_apps"] is False
    assert body["has_policies"] is False
    assert body["has_thresholds"] is False
    assert body["completed_at"] is None


async def test_onboarding_status_requires_auth_once_completed(client, monkeypatch):
    from orchestrator.core.config import get_settings

    settings = get_settings()
    # Fresh install: status is reachable without auth.
    fresh = await client.get("/api/v1/onboarding/status")
    assert fresh.status_code == 200
    done = await client.post("/api/v1/onboarding/complete")
    assert done.status_code == 200

    # Signed in (stub mode): the dashboard learns onboarding is done instead
    # of getting a 401 on every load.
    resp = await client.get("/api/v1/onboarding/status")
    assert resp.status_code == 200
    assert resp.json()["needs_onboarding"] is False

    # jwt mode: anonymous callers are rejected, the master key is accepted.
    monkeypatch.setattr(settings, "auth_mode", "jwt")
    monkeypatch.setattr(settings, "jwt_secret", "unit-test-jwt-secret-0123456789abcdef")
    anon = await client.get("/api/v1/onboarding/status")
    assert anon.status_code == 401
    admin = await client.get(
        "/api/v1/onboarding/status", headers={"X-Modus-APIKey": settings.master_api_key},
    )
    assert admin.status_code == 200


# ── Apply endpoint (managed mode) ───────────────────────────────────────────

async def test_onboarding_apply_managed(client):
    resp = await client.post("/api/v1/onboarding/apply", json={
        "org_name": "Test Corp",
        "first_team": {"name": "Platform", "slug": "platform"},
        "budget_daily": 100.0,
        "budget_monthly": 3000.0,
        "policy_mode": "managed",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["team_id"]
    assert body["policies_created"] == 3  # managed mode creates 3 default policies
    assert body["thresholds_created"] == 1
    assert "Platform" in body["message"]


async def test_onboarding_apply_manual(client):
    resp = await client.post("/api/v1/onboarding/apply", json={
        "org_name": "Manual Corp",
        "first_team": {"name": "Engineering", "slug": "engineering"},
        "budget_daily": 50.0,
        "budget_monthly": 1500.0,
        "policy_mode": "manual",
        "policies": [
            {"name": "Token Cap", "type": "token_cap", "scope": "team", "effect": "deny", "config": {"max_tokens": 10000}},
        ],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["policies_created"] == 1
    assert body["thresholds_created"] == 1


async def test_onboarding_apply_no_policies(client):
    resp = await client.post("/api/v1/onboarding/apply", json={
        "org_name": "Empty Corp",
        "first_team": {"name": "Empty Team", "slug": "empty-team"},
        "budget_daily": 200.0,
        "budget_monthly": 5000.0,
        "policy_mode": "manual",
        "policies": [],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["policies_created"] == 0


async def test_onboarding_apply_duplicate_slug(client):
    # First apply creates a team — subsequent call may fail with 409 or 401
    # depending on whether _onboarding_auth blocks the second attempt
    await client.post("/api/v1/onboarding/apply", json={
        "org_name": "Dup Corp",
        "first_team": {"name": "Dup Team", "slug": "dup-team"},
        "budget_daily": 200.0,
        "budget_monthly": 5000.0,
        "policy_mode": "managed",
    })
    resp = await client.post("/api/v1/onboarding/apply", json={
        "org_name": "Dup Corp 2",
        "first_team": {"name": "Dup Team 2", "slug": "dup-team"},
        "budget_daily": 200.0,
        "budget_monthly": 5000.0,
        "policy_mode": "managed",
    })
    # After first apply, teams exist so _onboarding_auth returns 401,
    # or if auth passes, 409 for duplicate slug
    assert resp.status_code in (401, 409)


# ── Complete endpoint ────────────────────────────────────────────────────────

async def test_onboarding_complete(client):
    resp = await client.post("/api/v1/onboarding/complete")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "completed_at" in body


async def test_onboarding_complete_requires_auth_once_completed(client, monkeypatch):
    from orchestrator.core.config import get_settings

    settings = get_settings()
    # First completion succeeds on a fresh install (no teams, not yet completed).
    first = await client.post("/api/v1/onboarding/complete")
    assert first.status_code == 200
    # Once onboarding is marked complete, the endpoint is auth-gated: an
    # unauthenticated caller (jwt mode; stub mode is open by design) must be
    # rejected rather than silently re-running.
    monkeypatch.setattr(settings, "auth_mode", "jwt")
    monkeypatch.setattr(settings, "jwt_secret", "unit-test-jwt-secret-0123456789abcdef")
    resp = await client.post("/api/v1/onboarding/complete")
    assert resp.status_code == 401


# ── YAML parser unit tests ──────────────────────────────────────────────────

def test_yaml_scalar_true():
    from orchestrator.api.onboarding import _yaml_scalar
    assert _yaml_scalar("true") is True


def test_yaml_scalar_false():
    from orchestrator.api.onboarding import _yaml_scalar
    assert _yaml_scalar("false") is False


def test_yaml_scalar_null():
    from orchestrator.api.onboarding import _yaml_scalar
    assert _yaml_scalar("null") is None
    assert _yaml_scalar("~") is None


def test_yaml_scalar_int():
    from orchestrator.api.onboarding import _yaml_scalar
    assert _yaml_scalar("42") == 42


def test_yaml_scalar_float():
    from orchestrator.api.onboarding import _yaml_scalar
    assert _yaml_scalar("3.14") == 3.14


def test_yaml_scalar_quoted():
    from orchestrator.api.onboarding import _yaml_scalar
    assert _yaml_scalar('"hello"') == "hello"
    assert _yaml_scalar("'world'") == "world"


def test_yaml_scalar_empty():
    from orchestrator.api.onboarding import _yaml_scalar
    assert _yaml_scalar("") is None


def test_yaml_scalar_plain_string():
    from orchestrator.api.onboarding import _yaml_scalar
    assert _yaml_scalar("budget_cap") == "budget_cap"


def test_parse_yaml_simple_basic():
    from orchestrator.api.onboarding import _parse_yaml_simple
    raw = """policies:
  - name: Daily Budget
    type: budget_cap
    scope: team
    effect: deny
"""
    result = _parse_yaml_simple(raw)
    assert "policies" in result
    assert len(result["policies"]) == 1
    assert result["policies"][0]["name"] == "Daily Budget"
    assert result["policies"][0]["type"] == "budget_cap"


def test_parse_yaml_simple_with_nested():
    from orchestrator.api.onboarding import _parse_yaml_simple
    raw = """policies:
  - name: Rate Limit
    type: rate_limit
    config:
      max_calls: 1000
      window_seconds: 3600
"""
    result = _parse_yaml_simple(raw)
    policies = result["policies"]
    assert len(policies) == 1
    assert policies[0]["config"]["max_calls"] == 1000


def test_parse_yaml_simple_comments_blank_lines():
    from orchestrator.api.onboarding import _parse_yaml_simple
    raw = """# Top-level comment
policies:
  # Policy comment

  - name: Test
    type: budget_cap
"""
    result = _parse_yaml_simple(raw)
    assert len(result["policies"]) == 1


def test_parse_yaml_simple_multiple_items():
    from orchestrator.api.onboarding import _parse_yaml_simple
    raw = """policies:
  - name: P1
    type: budget_cap
  - name: P2
    type: rate_limit
"""
    result = _parse_yaml_simple(raw)
    assert len(result["policies"]) == 2
    assert result["policies"][0]["name"] == "P1"
    assert result["policies"][1]["name"] == "P2"


# ── Upload endpoint ──────────────────────────────────────────────────────────

async def test_onboarding_upload_valid(client):
    yaml_content = b"""policies:
  - name: Test Upload
    type: budget_cap
    scope: team
    effect: deny
"""
    import io
    resp = await client.post(
        "/api/v1/onboarding/upload",
        files={"file": ("policy.yaml", io.BytesIO(yaml_content), "application/yaml")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["policies"][0]["name"] == "Test Upload"


async def test_onboarding_upload_invalid_type(client):
    yaml_content = b"""policies:
  - name: Bad Type
    type: nonexistent_type
    scope: team
    effect: deny
"""
    import io
    resp = await client.post(
        "/api/v1/onboarding/upload",
        files={"file": ("policy.yaml", io.BytesIO(yaml_content), "application/yaml")},
    )
    assert resp.status_code == 422


async def test_onboarding_upload_missing_policies_key(client):
    yaml_content = b"""rules:
  - name: No policies key
"""
    import io
    resp = await client.post(
        "/api/v1/onboarding/upload",
        files={"file": ("policy.yaml", io.BytesIO(yaml_content), "application/yaml")},
    )
    assert resp.status_code == 422


async def test_onboarding_upload_missing_name(client):
    yaml_content = b"""policies:
  - type: budget_cap
    scope: team
"""
    import io
    resp = await client.post(
        "/api/v1/onboarding/upload",
        files={"file": ("policy.yaml", io.BytesIO(yaml_content), "application/yaml")},
    )
    assert resp.status_code == 422


async def test_onboarding_upload_invalid_effect(client):
    yaml_content = b"""policies:
  - name: Bad Effect
    type: budget_cap
    effect: punish
"""
    import io
    resp = await client.post(
        "/api/v1/onboarding/upload",
        files={"file": ("policy.yaml", io.BytesIO(yaml_content), "application/yaml")},
    )
    assert resp.status_code == 422
