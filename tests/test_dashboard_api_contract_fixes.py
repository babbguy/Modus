# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0
"""
Regression tests for dashboard <-> API contract mismatches.

Every test here runs against a SEEDED database. The older contract tests loop
over whatever the endpoint returns, which is nothing on an empty database, so a
tile that could never be populated still passed. These assert on real rows.

Covers:
  * admin identities (stub / master key / platform admin, ``team_id`` is None)
    see every team's anomalies, recommendations, enforcement summary and ROI;
    team-scoped identities see only their own teams
  * timestamps leave the API timezone-aware (``Z`` / ``+00:00``)
  * the field names the dashboard views read exist on the responses
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

from orchestrator.core.auth import Identity, get_identity
from orchestrator.db.models import (
    AnomalyEvent,
    App,
    CostCenter,
    EvolutionGeneration,
    FederationPeer,
    MerkleRoot,
    OptimizationRecommendation,
    PolicyDecision,
    Team,
    TeamCostCenter,
    TRiSMThreatEvent,
    TrajectoryProof,
    UsageAggregate,
)

NOW = datetime.now(timezone.utc).replace(microsecond=0)
# A moment earlier today (UTC). "NOW - 30 min" falls on the previous day in the
# first half hour after midnight, which the "today" endpoints rightly exclude.
EARLIER_TODAY = max(NOW - timedelta(minutes=30), NOW.replace(hour=0, minute=0, second=0))
TZ_SUFFIX = re.compile(r"(Z|[+-]\d{2}:\d{2})$")


def _aware(value: str) -> bool:
    return bool(TZ_SUFFIX.search(value))


@pytest_asyncio.fixture
async def world(db_session):
    """Two teams with one app each plus rows for every tile under test."""
    teams, apps = {}, {}
    for slug in ("alpha", "beta"):
        t = Team(slug=slug, name=slug.title())
        db_session.add(t)
        await db_session.flush()
        a = App(
            team_id=t.id, app_id=f"{slug}-app", app_name=f"{slug.title()} App",
            api_key_hash="x", api_key_prefix="mds_xxxxxxxxxxxx",
            enforcement_state="active" if slug == "alpha" else "budget_suspended",
            enforcement_suspended_reason=None if slug == "alpha" else "Monthly budget exceeded",
            enforcement_suspended_at=None if slug == "alpha" else NOW - timedelta(hours=2),
        )
        db_session.add(a)
        await db_session.flush()
        teams[slug], apps[slug] = t, a

        db_session.add(AnomalyEvent(
            team_id=t.id, app_id=a.id, metric="cost", severity="high", z_score=Decimal("4.2"),
            baseline_value=Decimal("10"), actual_value=Decimal("42"),
            ai_explanation=f"{slug} spike", detected_at=NOW - timedelta(hours=1),
        ))
        db_session.add(OptimizationRecommendation(
            team_id=t.id, app_id=a.id, provider="anthropic", current_model="big", suggested_model="small",
            call_volume_basis=1000, estimated_monthly_savings=Decimal("120.50"), confidence="high",
            recommendation_text=f"{slug} switch",
        ))
        for decision, cost in (("deny", "2.50"), ("deny", "1.50"), ("throttle", "0")):
            db_session.add(PolicyDecision(
                app_id=a.id, team_id=t.id, decision=decision, reason="test",
                request_estimated_cost=Decimal(cost), decided_at=EARLIER_TODAY,
            ))
        hour = NOW.replace(minute=0, second=0)
        db_session.add(UsageAggregate(
            app_id=a.id, team_id=t.id, provider="anthropic", model="big", resource_type="llm",
            granularity="hourly", period_start=hour, period_end=hour + timedelta(hours=1),
            call_count=100, input_tokens=4000, output_tokens=1000, total_tokens=5000,
            total_cost=Decimal("12.00"), duration_ms_sum=50_000,
        ))
        db_session.add(EvolutionGeneration(
            team_id=t.id, generation_number=3, population_size=64, best_fitness=0.81, avg_fitness=0.6,
            best_genome_yaml="x: 1", mutations_applied=json.dumps(["Mutated a", "Mutated b", "Added c"]),
        ))
        db_session.add(TRiSMThreatEvent(
            session_id=f"s-{slug}", team_id=t.id, threat_type="prompt_injection", severity="critical",
            confidence_score=0.9, detection_method="pattern", action_taken="block",
        ))
        db_session.add(TrajectoryProof(
            session_id=f"s-{slug}", team_id=t.id, proof_type="trajectory", proof_status="valid",
            proof_data="00", prover_time_ms=5, circuit_size=10,
        ))
    cc = CostCenter(name="Engineering", code="ENG-1", budget_owner_name="Pat Owner", budget_monthly_usd=Decimal("5000"))
    db_session.add(cc)
    await db_session.flush()
    db_session.add(TeamCostCenter(team_id=teams["alpha"].id, cost_center_id=cc.id))
    db_session.add(MerkleRoot(batch_id="b1", root_hash="a" * 64, leaf_count=1,
                              period_start=NOW - timedelta(hours=1), period_end=NOW))
    await db_session.commit()
    return {"teams": teams, "apps": apps}


def _as(client, identity: Identity):
    """Run requests as ``identity`` (the app under test is client._transport.app)."""
    client._transport.app.dependency_overrides[get_identity] = lambda: identity


ADMIN_NO_TEAM = Identity(actor_id="master-key", role="platform_admin", team_ids=[])


# ── Admin identities see every team ──────────────────────────────────────────

async def test_admin_without_team_sees_all_teams_in_insights(client, world):
    _as(client, ADMIN_NO_TEAM)
    assert ADMIN_NO_TEAM.team_id is None  # the condition that used to empty every tile

    anomalies = (await client.get("/api/v1/insights/anomalies")).json()
    assert {a["app_name"] for a in anomalies} == {"Alpha App", "Beta App"}

    recs = (await client.get("/api/v1/insights/recommendations")).json()
    assert {r["app_name"] for r in recs} == {"Alpha App", "Beta App"}

    enf = (await client.get("/api/v1/insights/enforcement-summary")).json()
    assert enf["blocked"] == 4 and enf["throttle"] == 2
    assert enf["total_savings"] == pytest.approx(8.0)

    roi = (await client.get("/api/v1/reports/roi")).json()
    assert roi["blocked_calls"] == 4
    assert roi["estimated_savings"] == pytest.approx(8.0)
    assert roi["net_savings"] == pytest.approx(8.0)
    assert roi["cost_per_blocked"] == pytest.approx(2.0)
    assert roi["roi_multiple"] is None  # no platform cost metered -> no multiple


async def test_admin_can_filter_insights_to_one_team(client, world):
    _as(client, ADMIN_NO_TEAM)
    beta = str(world["teams"]["beta"].id)
    rows = (await client.get(f"/api/v1/insights/anomalies?team_id={beta}")).json()
    assert [r["app_name"] for r in rows] == ["Beta App"]
    assert (await client.get(f"/api/v1/insights/enforcement-summary?team_id={beta}")).json()["blocked"] == 2


async def test_team_scoped_identity_sees_only_its_team(client, world):
    alpha = str(world["teams"]["alpha"].id)
    beta = str(world["teams"]["beta"].id)
    _as(client, Identity(actor_id="u", role="team_member", team_ids=[alpha],
                         permissions=frozenset({"apps:read"})))
    rows = (await client.get("/api/v1/insights/anomalies")).json()
    assert [r["app_name"] for r in rows] == ["Alpha App"]
    assert (await client.get("/api/v1/insights/enforcement-summary")).json()["blocked"] == 2
    assert (await client.get("/api/v1/reports/roi")).json()["blocked_calls"] == 2
    # Asking for someone else's team is refused, not silently emptied.
    assert (await client.get(f"/api/v1/insights/anomalies?team_id={beta}")).status_code == 403


async def test_identity_with_no_teams_sees_nothing(client, world):
    _as(client, Identity(actor_id="u", role="team_member", team_ids=[]))
    assert (await client.get("/api/v1/insights/anomalies")).json() == []
    assert (await client.get("/api/v1/insights/recommendations")).json() == []
    assert (await client.get("/api/v1/reports/roi")).json()["blocked_calls"] == 0
    assert (await client.get("/api/v1/dashboard/top-models")).json() == []


async def test_dismiss_recommendation_works_for_admin(client, world):
    _as(client, ADMIN_NO_TEAM)
    rec = (await client.get("/api/v1/insights/recommendations")).json()[0]
    assert (await client.post(f"/api/v1/insights/recommendations/{rec['id']}/dismiss")).status_code == 200
    left = (await client.get("/api/v1/insights/recommendations")).json()
    assert rec["id"] not in {r["id"] for r in left}


async def test_enforcement_summary_derives_allowed_from_metered_usage(client, world):
    """'allow' decisions are never stored; allowed = metered calls - blocked - throttled."""
    _as(client, ADMIN_NO_TEAM)
    enf = (await client.get("/api/v1/insights/enforcement-summary")).json()
    # 2 teams x 100 calls this hour, minus 4 denied and 2 throttled
    assert enf["allowed"] == 200 - 4 - 2


async def test_ops_kpis_are_computed_from_stored_data(client, world):
    _as(client, ADMIN_NO_TEAM)
    k = (await client.get("/api/v1/insights/ops-kpis")).json()
    assert k["blocked_today"] == 4 and k["throttled_today"] == 2
    assert k["calls_today"] == 200
    assert k["tokens_per_call"] == pytest.approx(50.0)
    assert k["avg_latency_ms"] == pytest.approx(500.0)  # 50_000 ms / 100 calls
    assert _aware(k["window_start"])


# ── Timestamps are timezone-aware ────────────────────────────────────────────

async def test_orm_datetimes_round_trip_as_aware_utc(db_session, world):
    from sqlalchemy import select
    row = (await db_session.execute(select(AnomalyEvent))).scalars().first()
    await db_session.refresh(row)
    assert row.detected_at.tzinfo is not None
    assert row.detected_at.utcoffset() == timedelta(0)


async def test_aware_input_is_normalised_to_utc_on_write(db_session):
    from datetime import timezone as tz
    from sqlalchemy import select
    offset = tz(timedelta(hours=-7))
    moment = datetime(2026, 3, 1, 12, 0, tzinfo=offset)  # == 19:00 UTC
    t = Team(slug="tzteam", name="TZ")
    db_session.add(t)
    await db_session.flush()
    db_session.add(MerkleRoot(batch_id="tz", root_hash="b" * 64, leaf_count=1, period_start=moment, period_end=moment))
    await db_session.commit()
    row = (await db_session.execute(select(MerkleRoot).where(MerkleRoot.batch_id == "tz"))).scalar_one()
    assert row.period_start == datetime(2026, 3, 1, 19, 0, tzinfo=timezone.utc)


async def test_api_timestamps_carry_a_timezone(client, world):
    _as(client, ADMIN_NO_TEAM)
    anomalies = (await client.get("/api/v1/insights/anomalies")).json()
    assert anomalies and all(_aware(a["detected_at"]) for a in anomalies)
    recs = (await client.get("/api/v1/insights/recommendations")).json()
    assert recs and all(_aware(r["generated_at"]) for r in recs)
    apps = (await client.get("/api/v1/apps")).json()
    assert apps and all(_aware(a["created_at"]) for a in apps)
    threats = (await client.get("/api/v1/sentinel/threats")).json()
    assert threats and all(_aware(t["created_at"]) for t in threats)
    proofs_stats = (await client.get("/api/v1/compliance/zk-proofs/stats")).json()
    assert proofs_stats["total_proofs"] == 2


# ── Governance ───────────────────────────────────────────────────────────────

async def test_evolution_status_reports_what_the_dashboard_reads(client, world):
    _as(client, ADMIN_NO_TEAM)
    s = (await client.get("/api/v1/governance/evolution/status")).json()
    assert s["latest_generation"] == 3
    assert s["latest_best_fitness"] == pytest.approx(0.81)
    assert s["latest_population_size"] == 64
    assert s["total_mutations"] == 6  # 3 mutations x 2 teams
    assert s["total_generations"] == 2


async def test_cot_ledger_fields_and_admin_verify(client, db_session, world):
    from orchestrator.core.cot_ledger import append_entry
    from orchestrator.core.config import settings
    if not settings.cot_ledger_enabled:
        pytest.skip("CoT ledger disabled")
    for slug in ("alpha", "beta"):
        for i in range(2):
            await append_entry(db_session, str(world["teams"][slug].id), "policy_applied", "manual",
                               f"{slug} decision {i}", linked_proposal_id=None)
    await db_session.commit()
    _as(client, ADMIN_NO_TEAM)

    entries = (await client.get("/api/v1/governance/cot-ledger/entries")).json()
    assert len(entries) == 4
    # The names the Governance view renders (not summary/title/timestamp).
    for key in ("decision_summary", "created_at", "linked_proposal_id", "linked_policy_id",
                "linked_evolution_gen_id", "entry_hash"):
        assert key in entries[0]
    assert _aware(entries[0]["created_at"])

    verify = (await client.get("/api/v1/governance/cot-ledger/verify")).json()
    assert verify["valid"] is True
    assert verify["entries_checked"] == 4  # admin verifies EVERY team's chain
    assert verify["teams_checked"] == 2


# ── Sentinel / compliance ────────────────────────────────────────────────────

async def test_sentinel_stats_have_blocked_and_severity_counts(client, world):
    _as(client, ADMIN_NO_TEAM)
    stats = (await client.get("/api/v1/sentinel/stats")).json()
    assert stats["total_threats"] == 2
    assert stats["by_severity"]["critical"] == 2
    assert stats["blocked_threats"] == 2
    assert stats["by_action"] == {"block": 2}
    threats = (await client.get("/api/v1/sentinel/threats")).json()
    for key in ("session_id", "threat_type", "severity", "confidence_score", "action_taken", "created_at"):
        assert key in threats[0]


async def test_zk_stats_include_coverage(client, world):
    _as(client, ADMIN_NO_TEAM)
    s = (await client.get("/api/v1/compliance/zk-proofs/stats")).json()
    assert s["valid_proofs"] == 2 and s["coverage"] == pytest.approx(1.0)


async def test_attestation_stats_count_merkle_roots_and_pqc_score_lists_algorithms(client, db_session, world):
    _as(client, ADMIN_NO_TEAM)
    assert (await client.get("/api/v1/compliance/attestation-stats")).json()["merkle_roots"] == 1
    pqc = (await client.get("/api/v1/compliance/pqc/score")).json()
    assert pqc["pqc_algorithms"] == []
    assert pqc["pqc_signed"] == 0


# ── Apps / overview / finance / pricing / nomus ──────────────────────────────

async def test_apps_expose_enforcement_state(client, world):
    _as(client, ADMIN_NO_TEAM)
    apps = {a["app_id"]: a for a in (await client.get("/api/v1/apps")).json()}
    assert apps["alpha-app"]["enforcement_state"] == "active"
    assert apps["beta-app"]["enforcement_state"] == "budget_suspended"
    assert apps["beta-app"]["enforcement_suspended_reason"] == "Monthly budget exceeded"
    assert _aware(apps["beta-app"]["enforcement_suspended_at"])
    detail = (await client.get(f"/api/v1/apps/{apps['beta-app']['id']}")).json()
    assert detail["enforcement_state"] == "budget_suspended"


async def test_top_models_include_share_of_total_spend(client, db_session, world):
    # A second, cheaper model so the share is not trivially 100%.
    a = world["apps"]["alpha"]
    day = NOW.replace(hour=0, minute=0, second=0)
    for model, cost in (("big", "30.00"), ("small", "10.00")):
        db_session.add(UsageAggregate(
            app_id=a.id, team_id=a.team_id, provider="anthropic", model=model, resource_type="llm",
            granularity="daily", period_start=day, period_end=day + timedelta(days=1),
            call_count=10, input_tokens=1, output_tokens=1, total_tokens=2, total_cost=Decimal(cost),
        ))
    await db_session.commit()
    _as(client, ADMIN_NO_TEAM)
    models = (await client.get("/api/v1/dashboard/top-models?limit=1")).json()
    assert models[0]["model"] == "big"
    assert models[0]["pct"] == 75.0  # share of ALL spend (30 of 40), not of the single row returned
    assert sum(m["pct"] for m in (await client.get("/api/v1/dashboard/top-models")).json()) == pytest.approx(100.0, abs=0.2)


async def test_finance_chargeback_and_cost_centers_fields(client, db_session, world):
    a = world["apps"]["alpha"]
    day = NOW.replace(day=1, hour=0, minute=0, second=0)
    db_session.add(UsageAggregate(
        app_id=a.id, team_id=a.team_id, provider="anthropic", model="big", resource_type="llm",
        granularity="daily", period_start=day, period_end=day + timedelta(days=1),
        call_count=10, input_tokens=1, output_tokens=1, total_tokens=2, total_cost=Decimal("25.00"),
    ))
    await db_session.commit()
    _as(client, ADMIN_NO_TEAM)

    rows = (await client.get("/api/v1/finance/chargeback")).json()
    assert rows and all(r["period"] == NOW.strftime("%Y-%m") for r in rows)
    assert {"cost_center_code", "team_name", "cost"} <= rows[0].keys()
    assert rows[0]["cost_center_code"] == "ENG-1"

    cc = (await client.get("/api/v1/finance/cost-centers")).json()
    assert cc[0]["budget_owner_name"] == "Pat Owner"
    assert cc[0]["team_count"] == 1
    assert cc[0]["budget_monthly_usd"] == "5000.00"

    summary = (await client.get("/api/v1/finance/summary")).json()
    assert "cost_trend_pct" in summary  # None when the prior month has no spend


async def test_pricing_override_audit_entries_carry_full_before_and_after(client, world):
    _as(client, ADMIN_NO_TEAM)
    created = await client.post("/api/v1/pricing/overrides", json={
        "provider": "anthropic", "model": "big", "input_cost_per_1k": "0.003", "output_cost_per_1k": "0.015",
    })
    assert created.status_code in (200, 201), created.text
    oid = created.json()["id"]
    upd = await client.put(f"/api/v1/pricing/overrides/{oid}", json={"output_cost_per_1k": "0.02"})
    assert upd.status_code == 200, upd.text

    log = (await client.get("/api/v1/audit-log?resource_type=pricing_override")).json()
    update = next(e for e in log if e["action"] == "updated")
    for side in ("before", "after"):
        assert update[side]["provider"] == "anthropic" and update[side]["model"] == "big"
        assert "input_cost_per_1k" in update[side] and "output_cost_per_1k" in update[side]
    assert update["before"]["output_cost_per_1k"] != update["after"]["output_cost_per_1k"]
    assert _aware(update["occurred_at"])


async def test_nomus_status_reports_regulation_count(client, monkeypatch):
    from orchestrator.core import nomus_client
    monkeypatch.setattr(nomus_client, "_cached_policies", [
        {"jurisdiction": "EU", "category": "transparency", "severity": "high", "ruleKey": "a"},
        {"jurisdiction": "EU", "category": "risk", "severity": "low", "ruleKey": "b"},
        {"jurisdiction": "US-CO", "category": "risk", "severity": "medium", "ruleKey": "c"},
    ])
    _as(client, ADMIN_NO_TEAM)
    s = (await client.get("/api/v1/admin/nomus/status")).json()
    assert s["policy_count"] == 3
    assert s["regulation_count"] == 2  # distinct jurisdictions, same grouping as /regulations
    regs = (await client.get("/api/v1/admin/nomus/regulations")).json()
    assert len(regs["regulations"]) == s["regulation_count"]
    integ = (await client.get("/api/v1/integrations/nomus/status")).json()
    assert integ["regulation_count"] == s["regulation_count"]


async def test_federation_peer_list_includes_team_slug(client, db_session, world):
    db_session.add(FederationPeer(
        name="eu", peer_url="https://peer.example", api_key_hash="h", api_key_prefix="mds_fed_",
        team_id=str(world["teams"]["alpha"].id),
    ))
    await db_session.commit()
    _as(client, ADMIN_NO_TEAM)
    peers = (await client.get("/api/v1/admin/federation/peers")).json()
    assert peers[0]["team_slug"] == "alpha"
