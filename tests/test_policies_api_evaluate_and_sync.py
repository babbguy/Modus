"""
Policies API deep coverage.

Targets orchestrator.api.policies:
  - policy_evaluate (209-240)
  - list_policies with filters (279, 285, 290)
  - sync_policies (329-356)
  - create_policy full path (369-428)
  - update_policy (455-498)
  - deactivate_policy (525-536)
  - list_decisions (581, 588, 626-638)
  - get_real_time_spend (679-715)
  - set_enforcement_state (679-715)
  - _validate_policy_config branches (801-837)
  - get_ladder_status (841-874)
  - export_policies (1148-1195)
"""
from __future__ import annotations

import uuid

# ── Helpers ──────────────────────────────────────────────────────────────────

async def _create_team(client, slug: str = "test-team", name: str = "Test Team"):
    resp = await client.post("/api/v1/teams", json={"name": name, "slug": slug})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _create_app_in_db(client, db_session, team_id: str, app_id: str = "test-app"):
    """Create an app directly in the database (no POST endpoint for apps)."""
    import uuid
    from orchestrator.db.models import App
    app = App(
        id=uuid.uuid4(),
        team_id=team_id,
        app_id=app_id,
        app_name=f"App {app_id}",
        environment="production",
        api_key_hash="$2b$12$placeholder_hash_for_testing_only___",
        api_key_prefix=f"mds_{app_id[:12]}",
    )
    db_session.add(app)
    await db_session.flush()
    return {"id": str(app.id), "team_id": str(app.team_id), "app_id": app.app_id}


async def _create_policy(client, **overrides):
    payload = {
        "name": f"test-policy-{uuid.uuid4().hex[:8]}",
        "description": "Test policy",
        "scope": "platform",
        "policy_type": "budget_cap",
        "effect": "deny",
        "priority": 100,
        "config": {"cap_usd": "1000", "period": "monthly"},
    }
    payload.update(overrides)
    resp = await client.post("/api/v1/policies", json=payload)
    return resp


# ── Policy CRUD ──────────────────────────────────────────────────────────────

async def test_create_policy_platform(client):
    resp = await _create_policy(client)
    assert resp.status_code == 201
    data = resp.json()
    assert data["scope"] == "platform"
    assert data["policy_type"] == "budget_cap"
    assert data["is_active"] is True


async def test_create_policy_team_scope(client):
    team = await _create_team(client, slug="pol-team")
    resp = await _create_policy(
        client,
        scope="team",
        team_id=team["id"],
        config={"cap_usd": "500", "period": "daily"},
    )
    assert resp.status_code == 201
    assert resp.json()["team_id"] == team["id"]


async def test_create_policy_app_scope_requires_app_id(client):
    team = await _create_team(client, slug="pol-app-team")
    # Missing app_id should fail
    resp = await _create_policy(
        client,
        scope="app",
        team_id=team["id"],
        config={"cap_usd": "100", "period": "hourly"},
    )
    assert resp.status_code == 400


async def test_create_policy_missing_team_for_team_scope(client):
    resp = await _create_policy(client, scope="team")
    assert resp.status_code == 400


async def test_create_policy_missing_app_for_app_scope(client):
    team = await _create_team(client, slug="pol-no-app")
    resp = await _create_policy(client, scope="app", team_id=team["id"])
    assert resp.status_code == 400


async def test_create_rate_limit_policy(client):
    resp = await _create_policy(
        client,
        policy_type="rate_limit",
        config={"max_calls": 100, "window_seconds": 3600},
    )
    assert resp.status_code == 201


async def test_create_model_allowlist_policy(client):
    resp = await _create_policy(
        client,
        policy_type="model_allowlist",
        config={"models": ["gpt-4o", "claude-sonnet-4-20250514"]},
    )
    assert resp.status_code == 201


async def test_create_policy_invalid_config(client):
    resp = await _create_policy(
        client,
        policy_type="budget_cap",
        config={},  # missing cap_usd and period
    )
    assert resp.status_code == 400


async def test_create_policy_invalid_period(client):
    resp = await _create_policy(
        client,
        policy_type="budget_cap",
        config={"cap_usd": "100", "period": "weekly"},  # invalid period
    )
    assert resp.status_code == 400


# ── List policies ────────────────────────────────────────────────────────────

async def test_list_policies(client):
    await _create_policy(client, name="list-test-1")
    resp = await client.get("/api/v1/policies")
    assert resp.status_code == 200
    policies = resp.json()
    assert isinstance(policies, list)
    assert any(p["name"] == "list-test-1" for p in policies)


async def test_list_policies_by_scope(client):
    await _create_policy(client, name="scope-filter-test", scope="platform")
    resp = await client.get("/api/v1/policies", params={"scope": "platform"})
    assert resp.status_code == 200


async def test_list_policies_inactive(client):
    resp = await client.get("/api/v1/policies", params={"active_only": "false"})
    assert resp.status_code == 200


# ── Update policy ────────────────────────────────────────────────────────────

async def test_update_policy(client):
    create_resp = await _create_policy(client, name="update-me")
    assert create_resp.status_code == 201
    pid = create_resp.json()["id"]

    resp = await client.put(f"/api/v1/policies/{pid}", json={
        "name": "updated-name",
        "effect": "warn",
        "priority": 50,
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "updated-name"
    assert resp.json()["effect"] == "warn"
    assert resp.json()["priority"] == 50


async def test_update_policy_config(client):
    create_resp = await _create_policy(client, name="update-config")
    assert create_resp.status_code == 201
    pid = create_resp.json()["id"]

    resp = await client.put(f"/api/v1/policies/{pid}", json={
        "config": {"cap_usd": "2000", "period": "monthly"},
    })
    assert resp.status_code == 200
    assert resp.json()["config"]["cap_usd"] == "2000"


async def test_update_policy_not_found(client):
    fake_id = str(uuid.uuid4())
    resp = await client.put(f"/api/v1/policies/{fake_id}", json={"name": "x"})
    assert resp.status_code == 404


async def test_update_policy_description_and_conditions(client):
    create_resp = await _create_policy(client, name="update-desc")
    assert create_resp.status_code == 201
    pid = create_resp.json()["id"]

    resp = await client.put(f"/api/v1/policies/{pid}", json={
        "description": "New description",
        "conditions": {"env": "production"},
        "action": {"notify": True},
        "is_active": False,
    })
    assert resp.status_code == 200
    assert resp.json()["description"] == "New description"
    assert resp.json()["is_active"] is False


# ── Deactivate policy ────────────────────────────────────────────────────────

async def test_deactivate_policy(client):
    create_resp = await _create_policy(client, name="deactivate-me")
    assert create_resp.status_code == 201
    pid = create_resp.json()["id"]

    resp = await client.delete(f"/api/v1/policies/{pid}")
    assert resp.status_code == 204

    # Verify it's gone from active list
    list_resp = await client.get("/api/v1/policies")
    assert not any(p["id"] == pid for p in list_resp.json())


async def test_deactivate_policy_not_found(client):
    resp = await client.delete(f"/api/v1/policies/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── Policy decisions ─────────────────────────────────────────────────────────

async def test_list_decisions_empty(client):
    resp = await client.get("/api/v1/policy/decisions")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_list_decisions_with_filters(client):
    resp = await client.get("/api/v1/policy/decisions", params={
        "decision": "allow",
        "limit": 10,
    })
    assert resp.status_code == 200


# ── Real-time spend ──────────────────────────────────────────────────────────

async def test_get_real_time_spend_app_not_found(client):
    resp = await client.get(f"/api/v1/policy/spend/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── Enforcement state ────────────────────────────────────────────────────────

async def test_set_enforcement_state_app_not_found(client):
    resp = await client.put(f"/api/v1/apps/{uuid.uuid4()}/enforcement", json={
        "enforcement_state": "active",
    })
    assert resp.status_code == 404


# ── Config validation ────────────────────────────────────────────────────────

async def test_degradation_ladder_valid(client):
    resp = await _create_policy(
        client,
        policy_type="degradation_ladder",
        config={
            "budget_usd": "1000",
            "period": "monthly",
            "tiers": [
                {"pct": 80, "model": "gpt-4o-mini"},
                {"pct": 100, "action": "deny"},
            ],
        },
    )
    assert resp.status_code == 201


async def test_degradation_ladder_empty_tiers(client):
    resp = await _create_policy(
        client,
        policy_type="degradation_ladder",
        config={
            "budget_usd": "1000",
            "period": "monthly",
            "tiers": [],
        },
    )
    assert resp.status_code == 400


async def test_degradation_ladder_missing_pct(client):
    resp = await _create_policy(
        client,
        policy_type="degradation_ladder",
        config={
            "budget_usd": "1000",
            "period": "monthly",
            "tiers": [{"model": "gpt-4o-mini"}],
        },
    )
    assert resp.status_code == 400


async def test_degradation_ladder_missing_model(client):
    resp = await _create_policy(
        client,
        policy_type="degradation_ladder",
        config={
            "budget_usd": "1000",
            "period": "monthly",
            "tiers": [{"pct": 80}],  # missing model and no action=deny
        },
    )
    assert resp.status_code == 400


# ── Ladder status ────────────────────────────────────────────────────────────

async def test_ladder_status_empty(client):
    resp = await client.get("/api/v1/policy/ladder-status")
    assert resp.status_code == 200
    assert "ladder_statuses" in resp.json()


async def test_ladder_status_with_policy(client):
    team = await _create_team(client, slug="ladder-team")
    resp = await _create_policy(
        client,
        name="ladder-test",
        scope="team",
        team_id=team["id"],
        policy_type="degradation_ladder",
        config={
            "budget_usd": "1000",
            "period": "monthly",
            "tiers": [
                {"pct": 80, "model": "gpt-4o-mini"},
                {"pct": 100, "action": "deny"},
            ],
        },
    )
    assert resp.status_code == 201

    status_resp = await client.get("/api/v1/policy/ladder-status")
    assert status_resp.status_code == 200
    statuses = status_resp.json()["ladder_statuses"]
    assert len(statuses) >= 1
    found = [s for s in statuses if s["policy_name"] == "ladder-test"]
    assert len(found) == 1
    assert found[0]["zone"] == "normal"
    assert found[0]["budget_usd"] == 1000.0


# ── Export policies ──────────────────────────────────────────────────────────

async def test_export_policies(client):
    await _create_policy(client, name="export-test")
    resp = await client.get("/api/v1/policies/export")
    assert resp.status_code == 200
    data = resp.json()
    assert "version" in data
    assert "policies" in data
    assert any(p["name"] == "export-test" for p in data["policies"])


async def test_export_policies_scope_filter(client):
    resp = await client.get("/api/v1/policies/export", params={"scope": "platform"})
    assert resp.status_code == 200


# ── Sync policies ────────────────────────────────────────────────────────────

async def test_list_decisions_with_app_filter(client):
    resp = await client.get("/api/v1/policy/decisions", params={
        "app_id": str(uuid.uuid4()),
        "limit": 5,
    })
    assert resp.status_code == 200


# ── Various policy types ─────────────────────────────────────────────────────

async def test_create_provider_block(client):
    resp = await _create_policy(
        client,
        policy_type="provider_block",
        config={"providers": ["azure"]},
    )
    assert resp.status_code == 201


async def test_create_environment_block(client):
    resp = await _create_policy(
        client,
        policy_type="environment_block",
        config={"environments": ["production"]},
    )
    assert resp.status_code == 201


async def test_create_token_cap(client):
    resp = await _create_policy(
        client,
        policy_type="token_cap",
        config={"max_tokens": 10000, "period": "daily"},
    )
    assert resp.status_code == 201


async def test_create_latency_cap(client):
    resp = await _create_policy(
        client,
        policy_type="latency_cap",
        config={"max_ms": 5000},
    )
    assert resp.status_code == 201


async def test_create_amplification_gate(client):
    resp = await _create_policy(
        client,
        policy_type="amplification_gate",
        config={"max_amplification": 5.0},
    )
    assert resp.status_code == 201


async def test_create_retry_circuit_breaker(client):
    resp = await _create_policy(
        client,
        policy_type="retry_circuit_breaker",
        config={"max_retries": 3},
    )
    assert resp.status_code == 201
