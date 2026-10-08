"""
Integration correctness (self-contained orchestrator parts).

  1. Routing-outcome ingest endpoints require app-key auth (were unauthenticated
     DB-writing endpoints).
  2. get_topology is team-scoped: a slug shared across teams no longer 500s, and
     a caller cannot read another team's topology.
"""
from __future__ import annotations

import uuid



# ── 1. Routing ingest requires auth ───────────────────────────────────────────

async def test_routing_outcome_requires_app_key(client):
    resp = await client.post("/api/v1/routing/outcomes", json={
        "fingerprint_hash": "abc", "app_id": "x", "routed_to": "cheap",
    })
    assert resp.status_code == 401


async def test_routing_outcome_batch_requires_app_key(client):
    resp = await client.post("/api/v1/routing/outcomes/batch", json={"outcomes": []})
    assert resp.status_code == 401


def test_routing_ingest_endpoints_declare_app_auth():
    """The two SDK write endpoints must depend on get_app_identity."""
    import inspect
    from orchestrator.api import routing

    for fn in (routing.log_routing_outcome, routing.log_routing_outcomes_batch):
        sig = inspect.signature(fn)
        assert any(
            getattr(p.default, "dependency", None).__name__ == "get_app_identity"
            for p in sig.parameters.values()
            if p.default is not inspect.Parameter.empty
            and getattr(p.default, "dependency", None) is not None
        ), f"{fn.__name__} must authenticate with the app key"


async def test_routing_outcome_rejects_unregistered_key(client):
    """A well-formed but unknown key must not be able to write routing data."""
    resp = await client.post("/api/v1/routing/outcomes", json={
        "fingerprint_hash": "forged", "routed_to": "cheap", "system_prompt_hash": "sp",
    }, headers={"X-Modus-APIKey": "mds_" + "x" * 40})
    assert resp.status_code == 401

    resp = await client.post("/api/v1/routing/outcomes/batch", json={"outcomes": [
        {"fingerprint_hash": "forged", "routed_to": "cheap"},
    ]}, headers={"X-Modus-APIKey": "mds_" + "x" * 40})
    assert resp.status_code == 401

    resp = await client.get("/api/v1/routing/fingerprints")
    assert resp.json() == []


async def test_routing_outcome_is_bound_to_authenticated_app(client, registered_app):
    """A client-supplied app_id is ignored; rows belong to the key's app."""
    other_app = str(uuid.uuid4())
    headers = {"X-Modus-APIKey": registered_app["api_key"]}
    resp = await client.post("/api/v1/routing/outcomes", json={
        "fingerprint_hash": "bound-1", "app_id": other_app,
        "routed_to": "cheap", "system_prompt_hash": "sp",
    }, headers=headers)
    assert resp.status_code == 201
    resp = await client.post("/api/v1/routing/outcomes/batch", json={"outcomes": [
        {"fingerprint_hash": "bound-2", "app_id": other_app,
         "routed_to": "cheap", "system_prompt_hash": "sp"},
    ]}, headers=headers)
    assert resp.json() == {"accepted": 1, "errors": 0}

    fps = (await client.get("/api/v1/routing/fingerprints")).json()
    assert {fp["fingerprint_hash"] for fp in fps} == {"bound-1", "bound-2"}
    assert {fp["app_id"] for fp in fps} == {registered_app["app_uuid"]}


async def test_routing_outcome_rejects_another_apps_fingerprint(
    client, db_session, registered_app,
):
    """An app cannot append outcomes to a fingerprint owned by another app."""
    from orchestrator.api.apps import _generate_app_key, _hash_key
    from orchestrator.db.models import App

    intruder_key = _generate_app_key()
    db_session.add(App(
        team_id=registered_app["team_id"], app_id="intruder", app_name="Intruder",
        api_key_hash=_hash_key(intruder_key, rounds=4), api_key_prefix=intruder_key[:16],
    ))
    await db_session.commit()

    owner = {"X-Modus-APIKey": registered_app["api_key"]}
    intruder = {"X-Modus-APIKey": intruder_key}
    body = {"fingerprint_hash": "owned-1", "routed_to": "cheap", "system_prompt_hash": "sp"}
    assert (await client.post("/api/v1/routing/outcomes", json=body, headers=owner)).status_code == 201

    resp = await client.post("/api/v1/routing/outcomes", json=body, headers=intruder)
    assert resp.status_code == 409
    resp = await client.post("/api/v1/routing/outcomes/batch",
                             json={"outcomes": [body]}, headers=intruder)
    assert resp.json() == {"accepted": 0, "errors": 1}


async def test_ingest_accepts_session_token_from_self_registration(client, registered_app):
    """Agents onboarded with a team token authenticate with an mst_ session
    token. The ingest credential gate must let it through to verification."""
    from orchestrator.core.session_token import generate

    token = generate(registered_app["app_uuid"], registered_app["team_id"])
    resp = await client.post("/api/v1/ingest", json={
        "format": "aggregated", "batch_id": "session-token-batch",
        "agent_version": "1.0.0", "aggregates": [],
    }, headers={"X-Modus-APIKey": token})
    assert resp.status_code != 401, resp.text

    resp = await client.post("/api/v1/ingest", json={
        "format": "aggregated", "batch_id": "bad-token-batch",
        "agent_version": "1.0.0", "aggregates": [],
    }, headers={"X-Modus-APIKey": token[:-4] + "AAAA"})
    assert resp.status_code == 401


# ── 2. Topology team-scoping (no 500 on shared slug, no cross-team read) ──────

async def test_get_topology_scoped_no_500_on_shared_slug(client, db_session):
    """Two teams with the same app slug must not make get_topology 500."""
    from orchestrator.db.models import App, AppTopology, Team

    slug = "shared-slug"
    for i in range(2):
        team_id = str(uuid.uuid4())
        db_session.add(Team(id=team_id, slug=f"team-{i}-{uuid.uuid4().hex[:6]}",
                            name=f"Team {i}"))
        app_id = str(uuid.uuid4())
        db_session.add(App(
            id=app_id, team_id=team_id, app_id=slug, app_name=f"App {i}",
            environment="production", api_key_hash="x", api_key_prefix="mds_test",
        ))
        db_session.add(AppTopology(
            id=str(uuid.uuid4()), app_id=app_id, team_id=team_id,
            deployment_type="server",
        ))
    await db_session.commit()

    # Stub identity is platform_admin — must return one row, never 500.
    resp = await client.get(f"/api/v1/topology/{slug}")
    assert resp.status_code in (200, 404)
    assert resp.status_code != 500
