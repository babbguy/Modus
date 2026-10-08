"""
Tests for the four broken features fixed in fix/kill-dead-code:

1. pricing_overrides — estimate_cost now respects DB overrides
2. policy_proofs — verify endpoint persists proof certificates
3. trism_threat_events — scan endpoint persists detected threats
4. neuromorphic_metrics — background task collects real metrics
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import (
    PolicyProof,
    PricingOverride,
)


# ── Fix 1: pricing_overrides ────────────────────────────────────────────────


class TestEstimateCostWithOverrides:
    """estimate_cost uses override_rates when provided."""

    def test_bundled_pricing_fallback(self):
        """Without override_rates, estimate_cost uses the bundled _PRICING dict."""
        from orchestrator.core.pricing import estimate_cost

        inp, out, total = estimate_cost("anthropic", "claude-haiku-3", 1000, 1000)
        assert inp == Decimal("0.000250")
        assert out == Decimal("0.001250")
        assert total == Decimal("0.001500")

    def test_override_rates_take_precedence(self):
        """When override_rates is supplied, those rates are used instead of bundled."""
        from orchestrator.core.pricing import estimate_cost

        override = (Decimal("0.100000"), Decimal("0.200000"))
        inp, out, total = estimate_cost(
            "anthropic", "claude-haiku-3", 1000, 1000,
            override_rates=override,
        )
        assert inp == Decimal("0.100000")
        assert out == Decimal("0.200000")
        assert total == Decimal("0.300000")

    def test_override_rates_none_falls_through(self):
        """Passing override_rates=None behaves identically to the old signature."""
        from orchestrator.core.pricing import estimate_cost

        result_default = estimate_cost("openai", "gpt-4o", 1000, 500)
        result_explicit = estimate_cost("openai", "gpt-4o", 1000, 500, override_rates=None)
        assert result_default == result_explicit


@pytest.mark.asyncio
class TestLookupOverride:
    """_lookup_override queries the pricing_overrides table."""

    async def test_returns_none_when_no_override(self, db_session: AsyncSession):
        """When no override exists, _lookup_override returns None."""
        from orchestrator.core.pricing import _lookup_override

        result = await _lookup_override(db_session, "anthropic", "claude-haiku-3")
        assert result is None

    async def test_returns_rates_when_override_exists(self, db_session: AsyncSession):
        """When an active override exists, _lookup_override returns its rates."""
        from orchestrator.core.pricing import _lookup_override

        override = PricingOverride(
            provider="anthropic",
            model="claude-haiku-3",
            resource_type="llm_call",
            input_cost_per_1k=Decimal("0.050000"),
            output_cost_per_1k=Decimal("0.100000"),
            is_active=True,
        )
        db_session.add(override)
        await db_session.flush()

        result = await _lookup_override(db_session, "anthropic", "claude-haiku-3")
        assert result is not None
        assert result[0] == Decimal("0.050000")
        assert result[1] == Decimal("0.100000")

    async def test_inactive_override_ignored(self, db_session: AsyncSession):
        """Inactive overrides are not returned by _lookup_override."""
        from orchestrator.core.pricing import _lookup_override

        override = PricingOverride(
            provider="openai",
            model="gpt-4o",
            resource_type="llm_call",
            input_cost_per_1k=Decimal("0.999999"),
            output_cost_per_1k=Decimal("0.999999"),
            is_active=False,
        )
        db_session.add(override)
        await db_session.flush()

        result = await _lookup_override(db_session, "openai", "gpt-4o")
        assert result is None


# ── Fix 2: policy_proofs persisted ───────────────────────────────────────────


@pytest.mark.asyncio
class TestPolicyProofPersistence:
    """The verify endpoint persists proof certificates to policy_proofs."""

    async def test_verify_persists_proof(self, client):
        """POST /api/v1/policies/verify must persist a PolicyProof row."""
        from orchestrator.db.models import GovernancePolicy
        from orchestrator.db.session import _session_factory

        # Create a governance policy to satisfy the FK
        async with _session_factory() as db:
            policy = GovernancePolicy(
                app_id=None,
                team_id=None,
                name="Test budget cap",
                scope="platform",
                policy_type="budget_cap",
                effect="deny",
                config={"cap_usd": 100},
                is_active=True,
                created_by="test",
            )
            db.add(policy)
            await db.commit()
            policy_id = str(policy.id)

        policy_yaml = yaml.dump({
            "type": "budget_cap",
            "config": {"cap_usd": 100},
        })

        resp = await client.post("/api/v1/policies/verify", json={
            "policy_yaml": policy_yaml,
            "method": "bounded",
            "policy_id": policy_id,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("proven", "sampled", "disproven", "timeout", "unknown")

        # Verify the proof was persisted
        async with _session_factory() as db:
            result = await db.execute(
                select(PolicyProof).where(PolicyProof.policy_id == policy_id)
            )
            row = result.scalars().first()
            assert row is not None
            assert row.proof_type == "bounded"
            assert row.proof_status == data["status"]

    async def test_get_proof_after_verify(self, client):
        """GET /api/v1/policies/{id}/proof returns persisted proof."""
        from orchestrator.db.models import GovernancePolicy
        from orchestrator.db.session import _session_factory

        async with _session_factory() as db:
            policy = GovernancePolicy(
                app_id=None,
                team_id=None,
                name="Test rate limit",
                scope="platform",
                policy_type="rate_limit",
                effect="deny",
                config={"max_calls": 50},
                is_active=True,
                created_by="test",
            )
            db.add(policy)
            await db.commit()
            policy_id = str(policy.id)

        policy_yaml = yaml.dump({
            "type": "rate_limit",
            "config": {"max_calls": 50},
        })

        # Verify first
        resp = await client.post("/api/v1/policies/verify", json={
            "policy_yaml": policy_yaml,
            "method": "bounded",
            "policy_id": policy_id,
        })
        assert resp.status_code == 200

        # Now GET should find it
        resp2 = await client.get(f"/api/v1/policies/{policy_id}/proof")
        assert resp2.status_code == 200
        assert resp2.json()["policy_id"] == policy_id


# ── Fix 3: trism_threat_events persisted ─────────────────────────────────────


@pytest.mark.asyncio
class TestThreatEventPersistence:
    """The scan endpoint persists detected threats to trism_threat_events."""

    async def test_scan_with_threats_persists(self, client):
        """POST /api/v1/sentinel/scan persists threats, then GET retrieves them."""
        from sqlalchemy import text
        from orchestrator.db.session import _session_factory

        # Build a trace with context poisoning (high entropy spike)
        trace = []
        for i in range(10):
            trace.append({
                "call_idx": i,
                "model": "gpt-4o",
                "tokens_in": 200,
                "tokens_out": 100,
                "cost": 0.01,
                "tool_calls": [{"name": "read_file", "args": "x.py", "result": "ok"}],
                "context_hash": f"{'a' * 64}" if i < 8 else f"{'z' * 64}",
                "app_id": "app-main",
                "error": "InternalError" if i >= 8 else None,
            })

        resp = await client.post("/api/v1/sentinel/scan", json={
            "session_id": "test-session-001",
            "session_trace": trace,
        })
        assert resp.status_code == 200
        data = resp.json()

        # Threats were detected — verify via raw SQL to avoid ORM UUID issues on SQLite
        if data["threats"]:
            async with _session_factory() as db:
                result = await db.execute(
                    text("SELECT session_id, threat_type, severity "
                         "FROM trism_threat_events "
                         "WHERE session_id = :sid"),
                    {"sid": "test-session-001"},
                )
                rows = result.fetchall()
                assert len(rows) == len(data["threats"])
                for row in rows:
                    assert row[0] == "test-session-001"
                    assert row[1] in (
                        "context_poisoning", "goal_hijack",
                        "cascade_failure", "communication_anomaly",
                        "prompt_injection", "tool_misuse",
                    )

    async def test_clean_scan_no_persist(self, client):
        """A clean scan with no threats should not persist anything."""
        trace = [
            {
                "call_idx": i,
                "model": "gpt-4o-mini",
                "tokens_in": 200,
                "tokens_out": 100,
                "cost": 0.01,
                "tool_calls": [{"name": "read_file", "args": "x.py", "result": "ok"}],
                "context_hash": f"aabbccdd{'0' * 56}",
                "app_id": "app-main",
                "error": None,
            }
            for i in range(5)
        ]

        resp = await client.post("/api/v1/sentinel/scan", json={
            "session_id": "test-clean-session",
            "session_trace": trace,
        })
        assert resp.status_code == 200
        assert resp.json()["threats"] == []


# ── Fix 4: neuromorphic_metrics collection ──────────────────────────────────


@pytest.mark.asyncio
class TestNeuromorphicMetricsCollection:
    """collect_neuromorphic_metrics writes rows when enforcer is active."""

    async def test_noop_when_no_enforcer(self):
        """When no enforcer is registered, collection returns silently."""
        from orchestrator.core.neuromorphic_engine import (
            collect_neuromorphic_metrics,
            set_active_enforcer,
        )
        set_active_enforcer(None)
        # Should not raise
        await collect_neuromorphic_metrics()

    async def test_noop_when_no_topologies(self):
        """When enforcer has no cached topologies, collection returns silently."""
        from orchestrator.core.neuromorphic_engine import (
            NeuromorphicEnforcer,
            collect_neuromorphic_metrics,
            set_active_enforcer,
        )
        enforcer = NeuromorphicEnforcer()
        set_active_enforcer(enforcer)
        assert enforcer.cached_topology_count == 0
        await collect_neuromorphic_metrics()
        set_active_enforcer(None)

    async def test_collects_when_active(self, client):
        """When enforcer has cached topologies, metrics are written to DB."""
        from sqlalchemy import text
        from orchestrator.core.neuromorphic_engine import (
            NeuromorphicEnforcer,
            collect_neuromorphic_metrics,
            set_active_enforcer,
        )
        from orchestrator.db.session import _session_factory

        enforcer = NeuromorphicEnforcer()
        policies_yaml = yaml.dump([
            {"type": "budget_cap", "config": {"max_usd": 100.0}},
        ])
        enforcer.compile_policies(policies_yaml)
        assert enforcer.cached_topology_count == 1

        set_active_enforcer(enforcer)
        await collect_neuromorphic_metrics()

        # Read from DB via raw SQL to verify persistence
        async with _session_factory() as db:
            result = await db.execute(
                text("SELECT topology_hash, hardware_backend, spike_efficiency "
                     "FROM neuromorphic_metrics")
            )
            rows = result.fetchall()
            assert len(rows) >= 1
            row = rows[0]
            assert row[1] == "software"
            assert row[0] is not None  # topology_hash
            assert row[2] >= 0.0  # spike_efficiency

        set_active_enforcer(None)
