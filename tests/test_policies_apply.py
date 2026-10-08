"""
Tests — POST /api/v1/policies/apply (Budget-as-Code batch apply).

Covers create / update / unchanged / remove on SQLite, team resolution by slug
then name, 4xx on bad references, the audit entry, and dry-run parity.
"""
from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from orchestrator.db.models import AuditLog, GovernancePolicy, Team

APPLY = "/api/v1/policies/apply"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _budget(name="daily-cap", cap="100.00", **extra):
    item = {"name": name, "type": "budget_cap", "config": {"cap_usd": cap, "period": "daily"}}
    item.update(extra)
    return item


@pytest.fixture
async def team(db_session):
    t = Team(slug="eng-platform", name="Engineering Platform")
    db_session.add(t)
    await db_session.commit()
    return t


async def _rows(db_session):
    result = await db_session.execute(
        select(GovernancePolicy).execution_options(populate_existing=True)
    )
    return result.scalars().all()


async def test_apply_creates_policy_by_team_slug(client, db_session, team):
    resp = await client.post(APPLY, json={"policies": [_budget(team="eng-platform")]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["summary"] == {"created": 1, "updated": 0, "removed": 0, "unchanged": 0}
    assert body["errors"] == []
    assert UUID_RE.match(body["policies"][0]["id"])

    rows = await _rows(db_session)
    assert len(rows) == 1
    p = rows[0]
    assert p.team_id == str(team.id) and p.scope == "team" and p.is_active
    assert p.config == {"cap_usd": "100.00", "period": "daily"}
    assert p.created_by == "platform-admin"


async def test_apply_writes_audit_log_with_real_columns(client, db_session, team):
    resp = await client.post(APPLY, json={"policies": [_budget(team="eng-platform")]})
    assert resp.status_code == 200
    db_session.expire_all()
    logs = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "batch_applied")
    )).scalars().all()
    assert len(logs) == 1
    assert logs[0].actor_id == "platform-admin"
    assert logs[0].resource_type == "governance_policy"
    assert logs[0].after["created"] == 1


async def test_apply_updates_unchanged_and_removes(client, db_session, team):
    first = {"policies": [_budget("a", team="eng-platform"), _budget("b", team="eng-platform")]}
    assert (await client.post(APPLY, json=first)).status_code == 200

    second = {"policies": [_budget("a", team="eng-platform"),               # unchanged
                           _budget("b", cap="250.00", team="eng-platform"),  # updated
                           _budget("c", team="eng-platform")]}              # created
    r = await client.post(APPLY, json=second)
    assert r.json()["summary"] == {"created": 1, "updated": 1, "removed": 0, "unchanged": 1}

    third = {"policies": [_budget("a", team="eng-platform")]}
    r = await client.post(APPLY, json=third)
    assert r.json()["summary"] == {"created": 0, "updated": 0, "removed": 2, "unchanged": 1}

    rows = {p.name: p for p in await _rows(db_session)}
    assert rows["a"].is_active
    assert not rows["b"].is_active and not rows["c"].is_active
    assert rows["b"].config["cap_usd"] == "250.00"


async def test_apply_is_idempotent_including_disabled_policies(client, db_session, team):
    payload = {"policies": [_budget("on", team="eng-platform"),
                            _budget("off", team="eng-platform", enabled=False)]}
    for _ in range(3):
        r = await client.post(APPLY, json=payload)
        assert r.status_code == 200, r.text
    assert len(await _rows(db_session)) == 2
    assert r.json()["summary"] == {"created": 0, "updated": 0, "removed": 0, "unchanged": 2}


async def test_apply_dry_run_reports_plan_and_writes_nothing(client, db_session, team):
    payload = {"dry_run": True, "policies": [_budget(team="eng-platform")]}
    r = await client.post(APPLY, json=payload)
    assert r.status_code == 200
    assert r.json()["summary"]["created"] == 1
    assert r.json()["policies"][0]["action"] == "create"
    assert await _rows(db_session) == []
    db_session.expire_all()
    assert (await db_session.execute(select(AuditLog))).scalars().all() == []


async def test_apply_team_resolution_slug_then_name_then_uuid(client, db_session, team):
    for ref in ("eng-platform", "Engineering Platform", str(team.id)):
        r = await client.post(APPLY, json={"policies": [_budget(f"p-{len(ref)}", team=ref)]})
        assert r.status_code == 200, (ref, r.text)
    assert {p.team_id for p in await _rows(db_session)} == {str(team.id)}


async def test_apply_slug_wins_over_name(client, db_session):
    db_session.add_all([
        Team(slug="alpha", name="beta"),
        Team(slug="beta", name="Something Else"),
    ])
    await db_session.commit()
    r = await client.post(APPLY, json={"policies": [_budget(team="beta")]})
    assert r.status_code == 200
    beta = (await db_session.execute(select(Team).where(Team.slug == "beta"))).scalar_one()
    assert (await _rows(db_session))[0].team_id == str(beta.id)


async def test_apply_unknown_team_is_422_and_writes_nothing(client, db_session, team):
    payload = {"policies": [_budget("ok", team="eng-platform"), _budget("bad", team="nope")]}
    r = await client.post(APPLY, json=payload)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert any("team 'nope' not found" in e for e in detail["errors"])
    assert await _rows(db_session) == []


async def test_apply_ambiguous_team_name_is_422(client, db_session):
    db_session.add_all([Team(slug="t1", name="Same Name"), Team(slug="t2", name="Same Name")])
    await db_session.commit()
    r = await client.post(APPLY, json={"policies": [_budget(team="Same Name")]})
    assert r.status_code == 422
    assert "ambiguous" in r.text and "t1" in r.text


async def test_apply_unknown_team_uuid_is_422_not_500(client, team):
    ref = "11111111-1111-4111-8111-111111111111"
    r = await client.post(APPLY, json={"policies": [_budget(team=ref)]})
    assert r.status_code == 422


async def test_apply_bad_config_is_422(client, team):
    bad = {"name": "x", "type": "budget_cap", "team": "eng-platform", "config": {"period": "daily"}}
    r = await client.post(APPLY, json={"policies": [bad]})
    assert r.status_code == 422
    assert "cap_usd" in r.text


async def test_apply_invalid_scope_effect_and_type_are_422(client, team):
    for patch in ({"scope": "galaxy"}, {"effect": "explode"}, {"type": "webhook"}):
        item = {**_budget(team="eng-platform"), **patch}
        r = await client.post(APPLY, json={"policies": [item]})
        assert r.status_code == 422, patch


async def test_apply_defaults_block_is_honoured(client, db_session, team):
    payload = {
        "defaults": {"scope": "team", "effect": "warn", "team": "eng-platform"},
        "policies": [_budget()],
    }
    r = await client.post(APPLY, json=payload)
    assert r.status_code == 200, r.text
    p = (await _rows(db_session))[0]
    assert p.effect == "warn" and p.team_id == str(team.id)


async def test_apply_platform_scope_has_no_team(client, db_session, team):
    item = _budget("plat", scope="platform", team="eng-platform")
    r = await client.post(APPLY, json={"policies": [item]})
    assert r.status_code == 200
    p = (await _rows(db_session))[0]
    assert p.scope == "platform" and p.team_id is None


async def test_apply_app_scope_resolves_app_by_app_id(client, db_session, registered_app):
    item = {"name": "app-rate", "type": "rate_limit", "scope": "app", "team": "routing-team",
            "app": "routing-app", "config": {"max_calls": 5, "window_seconds": 60}}
    r = await client.post(APPLY, json={"policies": [item]})
    assert r.status_code == 200, r.text
    p = (await _rows(db_session))[0]
    assert p.app_id == registered_app["app_uuid"]

    item["app"] = "missing-app"
    r = await client.post(APPLY, json={"policies": [item]})
    assert r.status_code == 422 and "app 'missing-app' not found" in r.text


async def test_apply_duplicate_names_in_file_is_422(client, team):
    r = await client.post(APPLY, json={"policies": [_budget(team="eng-platform"),
                                                    _budget(team="eng-platform")]})
    assert r.status_code == 422


async def test_applied_policy_is_listed_with_hyphenated_uuid(client, team):
    await client.post(APPLY, json={"policies": [_budget(team="eng-platform")]})
    listed = (await client.get("/api/v1/policies")).json()
    assert len(listed) == 1 and UUID_RE.match(listed[0]["id"])
    assert listed[0]["team_id"] == str(team.id)


async def test_export_emits_team_slug_and_roundtrips_through_apply(client, team):
    await client.post(APPLY, json={"policies": [_budget(team="Engineering Platform")]})
    exported = (await client.get("/api/v1/policies/export")).json()
    assert exported["policies"][0]["team"] == "eng-platform"
    again = await client.post(APPLY, json=exported)
    assert again.status_code == 200, again.text
    assert again.json()["summary"] == {"created": 0, "updated": 0, "removed": 0, "unchanged": 1}
