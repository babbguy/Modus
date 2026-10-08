# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0
"""
Tests — team detail, edit, delete (soft) and restore.

Runs in stub auth mode, where the caller is a platform admin. Team-scoped
callers are simulated with a get_identity dependency override.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from orchestrator.api.apps import _generate_app_key, _hash_key
from orchestrator.core.auth import Identity, get_identity
from orchestrator.db.models import App, AuditLog, CostCenter, GovernancePolicy, Team

T = "/api/v1/teams"


@pytest.fixture(autouse=True)
def queued(monkeypatch):
    """Replace the DB write queue; collects the items ingest would persist."""
    from orchestrator.core import write_queue

    items: list = []

    async def _enqueue(item):
        items.append(item)

    monkeypatch.setattr(write_queue, "enqueue", _enqueue)
    monkeypatch.setattr(write_queue, "queue_over_pressure", lambda: False)
    return items


async def _team(client, slug, **extra):
    resp = await client.post(T, json={"slug": slug, "name": slug.title(), **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _app(db_session, team_id, app_id="svc"):
    raw = _generate_app_key()
    app = App(team_id=team_id, app_id=app_id, app_name=app_id,
              api_key_hash=_hash_key(raw, rounds=4), api_key_prefix=raw[:16])
    db_session.add(app)
    await db_session.commit()
    return str(app.id), raw


async def _ingest(client, key):
    return await client.post(
        "/api/v1/ingest",
        json={"batch_id": str(uuid.uuid4()), "records": [{
            "provider": "openai", "resource_type": "llm", "model": "gpt-4o",
            "input_tokens": 10, "output_tokens": 5, "total_cost": "0.0025",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]},
        headers={"X-Modus-APIKey": key},
    )


async def _audit(db_session, team_id, action, resource_type="team"):
    db_session.expire_all()
    rows = (await db_session.execute(
        select(AuditLog).where(AuditLog.resource_type == resource_type, AuditLog.action == action)
    )).scalars().all()
    return [r for r in rows if str(r.team_id) == team_id or str(r.resource_id) == team_id]


# ── Detail ───────────────────────────────────────────────────────────────────

async def test_detail_returns_budgets_parent_children_and_counts(client, db_session):
    parent = await _team(client, "parent")
    child = await _team(client, "child", parent_id=parent["id"])
    await _app(db_session, child["id"])
    cc = CostCenter(name="Eng", code="CC-1")
    db_session.add(cc)
    await db_session.commit()
    r = await client.patch(f"{T}/{child['id']}", json={
        "max_budget_usd": "12.50000000", "budget_duration": "monthly",
        "budget_monthly_usd": "300", "budget_quarterly_usd": "900.25",
        "cost_center_id": str(cc.id)})
    assert r.status_code == 200, r.text

    d = (await client.get(f"{T}/{child['id']}")).json()
    assert d["parent"]["slug"] == "parent"
    assert d["max_budget_usd"] == "12.50000000"
    assert d["budget_duration"] == "monthly"
    assert d["budget_monthly_usd"] == "300.00000000" or d["budget_monthly_usd"] == "300"
    assert d["budget_quarterly_usd"].startswith("900.25")
    assert d["cost_center"]["code"] == "CC-1"
    assert d["counts"] == {"active_apps": 1, "members": 0, "policies": 0, "child_teams": 0}
    assert d["has_registration_token"] is False
    pd = (await client.get(f"{T}/{parent['id']}")).json()
    assert [c["slug"] for c in pd["child_teams"]] == ["child"]
    assert pd["counts"]["child_teams"] == 1


async def test_detail_unknown_and_malformed_ids_are_404(client):
    assert (await client.get(f"{T}/{uuid.uuid4()}")).status_code == 404
    assert (await client.get(f"{T}/not-a-uuid")).status_code == 404


# ── Patch ────────────────────────────────────────────────────────────────────

async def test_patch_text_fields_and_clear_nullable(client):
    t = await _team(client, "alpha")
    r = await client.patch(f"{T}/{t['id']}", json={
        "name": "Alpha Prime", "slug": "alpha-prime",
        "description": "desc", "department": "Engineering"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert (d["name"], d["slug"], d["description"], d["department"]) == (
        "Alpha Prime", "alpha-prime", "desc", "Engineering")
    r = await client.patch(f"{T}/{t['id']}", json={"description": None, "department": None})
    assert r.json()["description"] is None and r.json()["department"] is None


async def test_patch_empty_body_and_unknown_field_are_422(client):
    t = await _team(client, "alpha")
    assert (await client.patch(f"{T}/{t['id']}", json={})).status_code == 422
    assert (await client.patch(f"{T}/{t['id']}", json={"bogus": 1})).status_code == 422
    assert (await client.patch(f"{T}/{t['id']}", json={"name": None})).status_code == 422
    assert (await client.patch(f"{T}/{t['id']}", json={"slug": "Bad Slug"})).status_code == 422


async def test_patch_unknown_team_404(client):
    assert (await client.patch(f"{T}/{uuid.uuid4()}", json={"name": "x"})).status_code == 404


async def test_patch_slug_conflict_409(client):
    await _team(client, "taken")
    t = await _team(client, "other")
    r = await client.patch(f"{T}/{t['id']}", json={"slug": "taken"})
    assert r.status_code == 409
    assert (await client.patch(f"{T}/{t['id']}", json={"slug": "other"})).status_code == 200


async def test_slug_of_deleted_team_is_reserved(client):
    t = await _team(client, "gone")
    assert (await client.delete(f"{T}/{t['id']}")).status_code == 200
    r = await client.post(T, json={"slug": "gone", "name": "Again"})
    assert r.status_code == 409 and "deleted team" in r.json()["detail"]
    other = await _team(client, "other")
    assert (await client.patch(f"{T}/{other['id']}", json={"slug": "gone"})).status_code == 409


async def test_patch_parent_set_clear_and_validation(client):
    a = await _team(client, "a")
    b = await _team(client, "b")
    r = await client.patch(f"{T}/{b['id']}", json={"parent_id": a["id"]})
    assert r.status_code == 200 and r.json()["parent"]["slug"] == "a"
    assert (await client.patch(f"{T}/{b['id']}", json={"parent_id": None})).json()["parent"] is None
    assert (await client.patch(f"{T}/{b['id']}", json={"parent_id": str(uuid.uuid4())})).status_code == 422
    assert (await client.patch(f"{T}/{b['id']}", json={"parent_id": "nope"})).status_code == 422


async def test_patch_parent_self_and_cycle_are_422(client):
    a = await _team(client, "a")
    b = await _team(client, "b", parent_id=a["id"])
    c = await _team(client, "c", parent_id=b["id"])
    assert (await client.patch(f"{T}/{a['id']}", json={"parent_id": a["id"]})).status_code == 422
    r = await client.patch(f"{T}/{a['id']}", json={"parent_id": c["id"]})
    assert r.status_code == 422 and "cycle" in r.json()["detail"]
    assert (await client.patch(f"{T}/{a['id']}", json={"parent_id": b["id"]})).status_code == 422


async def test_patch_deleted_parent_is_422(client):
    a = await _team(client, "a")
    b = await _team(client, "b")
    await client.delete(f"{T}/{a['id']}")
    assert (await client.patch(f"{T}/{b['id']}", json={"parent_id": a["id"]})).status_code == 422


@pytest.mark.parametrize("field", ["max_budget_usd", "budget_monthly_usd", "budget_quarterly_usd"])
async def test_patch_budget_validation(client, field):
    t = await _team(client, "money")
    assert (await client.patch(f"{T}/{t['id']}", json={field: "-1"})).status_code == 422
    assert (await client.patch(f"{T}/{t['id']}", json={field: "abc"})).status_code == 422
    assert (await client.patch(f"{T}/{t['id']}", json={field: 10.5})).status_code == 422  # float refused
    assert (await client.patch(f"{T}/{t['id']}", json={field: "1.123456789"})).status_code == 422
    assert (await client.patch(f"{T}/{t['id']}", json={field: "NaN"})).status_code == 422
    r = await client.patch(f"{T}/{t['id']}", json={field: "0.1"})
    assert r.status_code == 200 and float(r.json()[field]) == 0.1
    assert r.json()[field].startswith("0.1")
    r = await client.patch(f"{T}/{t['id']}", json={field: None})
    assert r.json()[field] is None


async def test_patch_budget_duration_validation(client):
    t = await _team(client, "dur")
    assert (await client.patch(f"{T}/{t['id']}", json={"budget_duration": "yearly"})).status_code == 422
    for ok in ("daily", "monthly", "30d", "1h"):
        r = await client.patch(f"{T}/{t['id']}", json={"budget_duration": ok})
        assert r.status_code == 200 and r.json()["budget_duration"] == ok


async def test_patch_cost_center(client, db_session):
    t = await _team(client, "cc")
    cc = CostCenter(name="Ops", code="CC-9")
    inactive = CostCenter(name="Old", code="CC-0", is_active=False)
    db_session.add_all([cc, inactive])
    await db_session.commit()
    assert (await client.patch(f"{T}/{t['id']}", json={"cost_center_id": str(uuid.uuid4())})).status_code == 422
    assert (await client.patch(f"{T}/{t['id']}", json={"cost_center_id": str(inactive.id)})).status_code == 422
    r = await client.patch(f"{T}/{t['id']}", json={"cost_center_id": str(cc.id)})
    assert r.json()["cost_center"]["code"] == "CC-9"
    assert (await client.patch(f"{T}/{t['id']}", json={"cost_center_id": None})).json()["cost_center"] is None


async def test_patch_writes_audit_before_after(client, db_session):
    t = await _team(client, "aud")
    await client.patch(f"{T}/{t['id']}", json={"name": "Renamed", "max_budget_usd": "5"})
    rows = await _audit(db_session, t["id"], "updated")
    assert len(rows) == 1
    assert rows[0].before["name"] == "Aud" and rows[0].after["name"] == "Renamed"
    assert rows[0].before["max_budget_usd"] is None and rows[0].after["max_budget_usd"].startswith("5")
    assert "slug" not in rows[0].after  # only changed fields recorded


# ── Delete ───────────────────────────────────────────────────────────────────

async def test_delete_without_apps_soft_deletes_and_keeps_row(client, db_session):
    t = await _team(client, "solo")
    r = await client.delete(f"{T}/{t['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["apps_reassigned"] == 0 and r.json()["apps_deactivated"] == 0
    assert (await client.get(f"{T}/{t['id']}")).status_code == 404
    assert (await client.delete(f"{T}/{t['id']}")).status_code == 404
    db_session.expire_all()
    row = await db_session.get(Team, t["id"])
    assert row is not None and row.deleted_at is not None
    assert len(await _audit(db_session, t["id"], "deleted")) == 1


async def test_delete_with_active_apps_is_409(client, db_session):
    t = await _team(client, "busy")
    await _app(db_session, t["id"], "one")
    await _app(db_session, t["id"], "two")
    r = await client.delete(f"{T}/{t['id']}")
    assert r.status_code == 409
    assert "2 active app" in r.json()["detail"] and "reassign_to" in r.json()["detail"]
    assert (await client.get(f"{T}/{t['id']}")).status_code == 200


async def test_delete_reassign_to_moves_apps_and_ingest_follows(client, db_session, queued):
    src = await _team(client, "src")
    dst = await _team(client, "dst")
    app_uuid, key = await _app(db_session, src["id"])
    assert (await _ingest(client, key)).status_code == 202  # warms the app/key caches
    db_session.add(GovernancePolicy(
        team_id=src["id"], name="cap", scope="team", policy_type="budget_cap",
        effect="deny", created_by="tester",
        config={"budget_usd": "1", "period": "daily"}))
    await db_session.commit()

    r = await client.delete(f"{T}/{src['id']}", params={"reassign_to": dst["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["apps_reassigned"] == 1

    db_session.expire_all()
    assert (await db_session.get(App, app_uuid)).team_id == dst["id"]
    pol = (await db_session.execute(select(GovernancePolicy))).scalars().one()
    assert pol.team_id == dst["id"]
    # the moved app still authenticates and new usage is attributed to the new team
    assert (await _ingest(client, key)).status_code == 202
    assert [i.team_id for i in queued] == [src["id"], dst["id"]]
    assert (await client.get(f"{T}/{dst['id']}")).json()["counts"]["active_apps"] == 1
    moved = await _audit(db_session, dst["id"], "reassigned", resource_type="app")
    assert len(moved) == 1 and moved[0].before["team_id"] == src["id"]


async def test_delete_reassign_validation(client, db_session):
    src = await _team(client, "src")
    other = await _team(client, "other")
    gone = await _team(client, "gone")
    await client.delete(f"{T}/{gone['id']}")
    app_uuid, _ = await _app(db_session, src["id"], "svc")
    assert (await client.delete(f"{T}/{src['id']}", params={"reassign_to": src["id"]})).status_code == 422
    assert (await client.delete(f"{T}/{src['id']}", params={"reassign_to": gone["id"]})).status_code == 422
    assert (await client.delete(f"{T}/{src['id']}", params={"reassign_to": str(uuid.uuid4())})).status_code == 422
    assert (await client.delete(
        f"{T}/{src['id']}", params={"reassign_to": other["id"], "cascade": "true"})).status_code == 422
    # same app_id already present on the target
    await _app(db_session, other["id"], "svc")
    r = await client.delete(f"{T}/{src['id']}", params={"reassign_to": other["id"]})
    assert r.status_code == 409 and "svc" in r.json()["detail"]
    assert (await client.get(f"{T}/{src['id']}")).status_code == 200  # nothing changed
    db_session.expire_all()
    assert (await db_session.get(App, app_uuid)).team_id == src["id"]


async def test_delete_cascade_deactivates_apps_and_keys_stop_working(client, db_session):
    t = await _team(client, "casc")
    app_uuid, key = await _app(db_session, t["id"])
    assert (await _ingest(client, key)).status_code == 202
    r = await client.delete(f"{T}/{t['id']}", params={"cascade": "true"})
    assert r.status_code == 200 and r.json()["apps_deactivated"] == 1
    db_session.expire_all()
    app = await db_session.get(App, app_uuid)
    assert app.is_active is False and app.deleted_at is not None
    assert (await _ingest(client, key)).status_code == 401
    assert len(await _audit(db_session, app_uuid, "deleted", resource_type="app")) == 1


async def test_delete_with_children_needs_reassign_children_to(client):
    parent = await _team(client, "parent")
    kid = await _team(client, "kid", parent_id=parent["id"])
    new_home = await _team(client, "home")
    r = await client.delete(f"{T}/{parent['id']}")
    assert r.status_code == 409 and "child team" in r.json()["detail"]
    # target inside the subtree is refused
    r = await client.delete(f"{T}/{parent['id']}", params={"reassign_children_to": kid["id"]})
    assert r.status_code == 422
    r = await client.delete(f"{T}/{parent['id']}", params={"reassign_children_to": new_home["id"]})
    assert r.status_code == 200 and r.json()["child_teams_reassigned"] == 1
    assert (await client.get(f"{T}/{kid['id']}")).json()["parent"]["slug"] == "home"


async def test_delete_revokes_registration_token(client, db_session):
    t = await _team(client, "tok")
    from orchestrator.core.config import get_settings
    hdr = {"X-Modus-APIKey": get_settings().master_api_key}
    gen = await client.post(f"{T}/{t['id']}/registration-token", headers=hdr)
    assert gen.status_code == 201, gen.text
    token = gen.json()["registration_token"]
    body = {"app_id": "newapp", "app_name": "New"}
    assert (await client.post("/api/v1/self-register", json=body,
                              headers={"X-Modus-TeamToken": token})).status_code == 200
    assert (await client.get(f"{T}/{t['id']}")).json()["has_registration_token"] is True

    r = await client.delete(f"{T}/{t['id']}", params={"cascade": "true"})
    assert r.status_code == 200 and r.json()["registration_token_revoked"] is True
    db_session.expire_all()
    row = await db_session.get(Team, t["id"])
    assert row.registration_token_hash is None and row.registration_token_prefix is None
    assert (await client.post("/api/v1/self-register", json={"app_id": "late", "app_name": "Late"},
                              headers={"X-Modus-TeamToken": token})).status_code == 401
    # restoring does not bring the token back
    await client.post(f"{T}/{t['id']}/restore")
    assert (await client.post("/api/v1/self-register", json={"app_id": "late", "app_name": "Late"},
                              headers={"X-Modus-TeamToken": token})).status_code == 401


# ── Lists / restore ──────────────────────────────────────────────────────────

async def test_deleted_excluded_from_lists_and_included_on_request(client):
    keep = await _team(client, "keep", department="Ops")
    gone = await _team(client, "gone", department="Ops")
    await client.delete(f"{T}/{gone['id']}")
    slugs = [t["slug"] for t in (await client.get(T)).json()]
    assert slugs == ["keep"]
    allr = (await client.get(T, params={"include_deleted": "true"})).json()
    assert {t["slug"]: t["deleted_at"] is not None for t in allr} == {"keep": False, "gone": True}
    assert (await client.get(f"{T}/departments")).json() == [{"department": "Ops", "team_count": 1}]
    assert keep["id"]


async def test_restore_and_errors(client, db_session):
    t = await _team(client, "back")
    assert (await client.post(f"{T}/{t['id']}/restore")).status_code == 409  # not deleted
    assert (await client.post(f"{T}/{uuid.uuid4()}/restore")).status_code == 404
    await client.delete(f"{T}/{t['id']}")
    r = await client.post(f"{T}/{t['id']}/restore")
    assert r.status_code == 200, r.text
    assert r.json()["deleted_at"] is None and r.json()["slug"] == "back"
    assert [x["slug"] for x in (await client.get(T)).json()] == ["back"]
    assert len(await _audit(db_session, t["id"], "restored")) == 1


async def test_restore_slug_conflict_409(client, monkeypatch):
    """Defence in depth: if another live team ever holds the slug, restore refuses."""
    t = await _team(client, "back")
    await client.delete(f"{T}/{t['id']}")
    from orchestrator.api import teams as teams_api

    async def fake_owner(db, slug, *, exclude_id=None):
        return Team(id=str(uuid.uuid4()), slug=slug, name="Squatter")

    monkeypatch.setattr(teams_api, "_slug_owner", fake_owner)
    r = await client.post(f"{T}/{t['id']}/restore")
    assert r.status_code == 409 and "slug" in r.json()["detail"]
    monkeypatch.undo()
    assert (await client.post(f"{T}/{t['id']}/restore")).status_code == 200


async def test_restore_with_deleted_parent_clears_parent(client):
    parent = await _team(client, "parent")
    kid = await _team(client, "kid", parent_id=parent["id"])
    await client.delete(f"{T}/{kid['id']}")
    await client.delete(f"{T}/{parent['id']}")
    r = await client.post(f"{T}/{kid['id']}/restore")
    assert r.status_code == 200
    assert r.json()["parent_id"] is None and r.json()["parent_cleared"] is True


# ── Permissions ──────────────────────────────────────────────────────────────

@pytest.fixture
def scoped(client):
    """Install a team-scoped identity; yields a setter taking the allowed team ids."""
    app = client._transport.app

    def _set(team_ids, perms=("teams:read", "teams:write")):
        app.dependency_overrides[get_identity] = lambda: Identity(
            actor_id="scoped-user", role="team_admin", team_ids=list(team_ids),
            permissions=frozenset(perms))

    yield _set
    app.dependency_overrides.pop(get_identity, None)


async def test_team_scoped_identity_cannot_touch_other_teams(client, scoped):
    mine = await _team(client, "mine")
    theirs = await _team(client, "theirs")
    scoped([mine["id"]])
    assert (await client.get(f"{T}/{theirs['id']}")).status_code == 403
    assert (await client.patch(f"{T}/{theirs['id']}", json={"name": "x"})).status_code == 403
    assert (await client.delete(f"{T}/{theirs['id']}")).status_code == 403
    assert (await client.post(f"{T}/{theirs['id']}/restore")).status_code == 403
    assert (await client.patch(f"{T}/{mine['id']}", json={"name": "Mine2"})).status_code == 200
    # cannot adopt, re-parent under, or hand apps to a team outside their scope
    assert (await client.patch(f"{T}/{mine['id']}", json={"parent_id": theirs["id"]})).status_code == 403
    assert (await client.get(T, params={"include_deleted": "true"})).status_code == 403
    assert [t["slug"] for t in (await client.get(T)).json()] == ["mine"]


async def test_scoped_delete_cannot_reassign_outside_scope(client, db_session, scoped):
    mine = await _team(client, "mine")
    theirs = await _team(client, "theirs")
    await _app(db_session, mine["id"])
    scoped([mine["id"]])
    r = await client.delete(f"{T}/{mine['id']}", params={"reassign_to": theirs["id"]})
    assert r.status_code == 403


async def test_read_only_identity_cannot_write(client, scoped):
    t = await _team(client, "ro")
    scoped([t["id"]], perms=("teams:read",))
    assert (await client.get(f"{T}/{t['id']}")).status_code == 200
    assert (await client.patch(f"{T}/{t['id']}", json={"name": "x"})).status_code == 403
    assert (await client.delete(f"{T}/{t['id']}")).status_code == 403
    assert (await client.post(f"{T}/{t['id']}/restore")).status_code == 403
