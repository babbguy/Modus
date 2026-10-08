"""
Tests for orchestrator.core.governance_loop — YAML generators,
deduplication, apply/dismiss helpers, and detection rule helpers.
"""

from __future__ import annotations

import pytest

from orchestrator.core.governance_loop import (
    _generate_amplification_gate_yaml,
    _generate_budget_cap_yaml,
    _generate_degradation_ladder_yaml,
    _generate_model_denylist_yaml,
    _generate_rate_limit_yaml,
    _deduplicate_proposals,
    apply_proposal,
    dismiss_proposal,
)


# ── YAML generation helpers ──────────────────────────────────────────────────


class TestGenerateDegradationLadderYaml:
    def test_basic_output(self):
        result = _generate_degradation_ladder_yaml(
            name="auto-downshift-myapp",
            budget_usd="10.00",
            period="daily",
            tiers=[
                {"pct": 50, "model": "gpt-4o-mini"},
                {"pct": 90, "model": "gpt-4o-mini"},
                {"pct": 100, "action": "deny"},
            ],
        )
        assert "- name: auto-downshift-myapp" in result
        assert "  type: degradation_ladder" in result
        assert '    budget_usd: "10.00"' in result
        assert "    period: daily" in result
        assert "      - pct: 50" in result
        assert "        model: gpt-4o-mini" in result
        assert "      - pct: 100" in result
        assert "        action: deny" in result

    def test_single_tier_model_only(self):
        result = _generate_degradation_ladder_yaml(
            name="simple", budget_usd="5", period="hourly",
            tiers=[{"pct": 80, "model": "claude-sonnet-4-20250514"}],
        )
        assert "      - pct: 80" in result
        assert "        model: claude-sonnet-4-20250514" in result
        assert "action" not in result

    def test_empty_tiers(self):
        result = _generate_degradation_ladder_yaml(
            name="empty", budget_usd="1", period="daily", tiers=[],
        )
        assert "    tiers:" in result
        # No tier entries
        assert "      - pct:" not in result


class TestGenerateBudgetCapYaml:
    def test_basic_output(self):
        result = _generate_budget_cap_yaml(
            name="cap-team-alpha", cap_usd="100.00", period="daily",
        )
        assert "- name: cap-team-alpha" in result
        assert "  type: budget_cap" in result
        assert "  effect: deny" in result
        assert '    cap_usd: "100.00"' in result
        assert "    period: daily" in result
        assert '    message: "Budget cap of $100.00/daily reached."' in result

    def test_monthly_period(self):
        result = _generate_budget_cap_yaml(
            name="monthly-cap", cap_usd="500", period="monthly",
        )
        assert "    period: monthly" in result
        assert "$500/monthly" in result


class TestGenerateRateLimitYaml:
    def test_basic_output(self):
        result = _generate_rate_limit_yaml(
            name="auto-ratelimit-app1", max_calls=100, window_seconds=3600,
        )
        assert "- name: auto-ratelimit-app1" in result
        assert "  type: rate_limit" in result
        assert "  effect: throttle" in result
        assert "    max_calls: 100" in result
        assert "    window_seconds: 3600" in result
        assert "    retry_after_seconds: 30" in result

    def test_small_window(self):
        result = _generate_rate_limit_yaml(
            name="burst", max_calls=10, window_seconds=60,
        )
        assert "    max_calls: 10" in result
        assert "    window_seconds: 60" in result


class TestGenerateAmplificationGateYaml:
    def test_basic_output(self):
        result = _generate_amplification_gate_yaml(
            name="auto-amp-gate-myapp", max_amplification=3.0,
        )
        assert "- name: auto-amp-gate-myapp" in result
        assert "  type: amplification_gate" in result
        assert "  effect: deny" in result
        assert "    max_amplification: 3.0" in result
        assert "Amplification factor exceeds 3.0x limit." in result

    def test_high_threshold(self):
        result = _generate_amplification_gate_yaml(
            name="lenient-gate", max_amplification=10.5,
        )
        assert "    max_amplification: 10.5" in result


class TestGenerateModelDenylistYaml:
    def test_basic_output(self):
        result = _generate_model_denylist_yaml(
            name="deny-expensive", models=["gpt-4", "o1-preview"],
        )
        assert "- name: deny-expensive" in result
        assert "  type: model_denylist" in result
        assert "  effect: deny" in result
        assert "      - gpt-4" in result
        assert "      - o1-preview" in result

    def test_single_model(self):
        result = _generate_model_denylist_yaml(
            name="deny-one", models=["gpt-4"],
        )
        assert "      - gpt-4" in result

    def test_empty_models(self):
        result = _generate_model_denylist_yaml(
            name="deny-none", models=[],
        )
        assert "    models:" in result


# ── Deduplication ─────────────────────────────────────────────────────────────


class TestDeduplicateProposals:
    # Deterministic UUIDs for test data
    _TEAM1 = "00000000-0000-4000-a000-000000000001"
    _APP1 = "00000000-0000-4000-a000-0000000000a1"
    _APP2 = "00000000-0000-4000-a000-0000000000a2"

    @pytest.mark.asyncio
    async def test_empty_proposals_returns_empty(self, db_session):
        result = await _deduplicate_proposals(db_session, [])
        assert result == []

    @pytest.mark.asyncio
    async def test_no_existing_keeps_all(self, db_session):
        proposals = [
            {"team_id": self._TEAM1, "proposal_type": "model_downshift", "app_id": self._APP1},
            {"team_id": self._TEAM1, "proposal_type": "budget_tighten", "app_id": self._APP2},
        ]
        result = await _deduplicate_proposals(db_session, proposals)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_removes_duplicates_of_pending(self, db_session):
        from orchestrator.db.models import GovernanceProposal

        # Insert a pending proposal
        existing = GovernanceProposal(
            team_id=self._TEAM1,
            proposal_type="model_downshift",
            app_id=self._APP1,
            title="existing",
            rationale="existing",
            proposed_yaml="---",
            status="pending",
        )
        db_session.add(existing)
        await db_session.commit()

        proposals = [
            {"team_id": self._TEAM1, "proposal_type": "model_downshift", "app_id": self._APP1},
            {"team_id": self._TEAM1, "proposal_type": "budget_tighten", "app_id": self._APP2},
        ]
        result = await _deduplicate_proposals(db_session, proposals)
        assert len(result) == 1
        assert result[0]["proposal_type"] == "budget_tighten"

    @pytest.mark.asyncio
    async def test_applied_proposals_do_not_block(self, db_session):
        from orchestrator.db.models import GovernanceProposal

        # Insert an applied proposal (should NOT block new ones)
        existing = GovernanceProposal(
            team_id=self._TEAM1,
            proposal_type="model_downshift",
            app_id=self._APP1,
            title="old",
            rationale="old",
            proposed_yaml="---",
            status="applied",
        )
        db_session.add(existing)
        await db_session.commit()

        proposals = [
            {"team_id": self._TEAM1, "proposal_type": "model_downshift", "app_id": self._APP1},
        ]
        result = await _deduplicate_proposals(db_session, proposals)
        assert len(result) == 1


# ── Apply / dismiss helpers ──────────────────────────────────────────────────


class TestApplyProposal:
    _TEAM1 = "00000000-0000-4000-a000-000000000001"
    _APP1 = "00000000-0000-4000-a000-0000000000a1"

    @pytest.mark.asyncio
    async def test_apply_nonexistent_returns_none(self, db_session):
        result = await apply_proposal(db_session, "nonexistent-id", "user1")
        assert result is None

    @pytest.mark.asyncio
    async def test_apply_non_pending_returns_none(self, db_session):
        from orchestrator.db.models import GovernanceProposal

        proposal = GovernanceProposal(
            team_id=self._TEAM1,
            proposal_type="budget_tighten",
            title="dismissed one",
            rationale="some reason",
            proposed_yaml="- name: test\n  type: budget_cap",
            status="dismissed",
        )
        db_session.add(proposal)
        await db_session.commit()

        result = await apply_proposal(db_session, str(proposal.id), "user1")
        assert result is None

    @pytest.mark.asyncio
    async def test_apply_pending_creates_policy(self, db_session):
        from orchestrator.db.models import GovernanceProposal

        yaml_str = (
            "- name: auto-budget-app1\n"
            "  type: budget_cap\n"
            "  effect: deny\n"
            "  config:\n"
            '    cap_usd: \"50.00\"\n'
            "    period: daily\n"
        )
        proposal = GovernanceProposal(
            team_id=self._TEAM1,
            app_id=self._APP1,
            proposal_type="budget_tighten",
            title="Tighten budget",
            rationale="Repeated overruns",
            proposed_yaml=yaml_str,
            status="pending",
        )
        db_session.add(proposal)
        await db_session.commit()

        policy = await apply_proposal(db_session, str(proposal.id), "admin1")
        assert policy is not None
        assert policy.team_id == self._TEAM1
        assert policy.policy_type == "budget_cap"
        assert policy.effect == "deny"
        assert policy.is_active is True
        assert policy.created_by == "governance:admin1"

        # Proposal should be marked applied
        await db_session.refresh(proposal)
        assert proposal.status == "applied"
        assert proposal.applied_by == "admin1"
        assert proposal.applied_at is not None


class TestDismissProposal:
    _TEAM1 = "00000000-0000-4000-a000-000000000001"

    @pytest.mark.asyncio
    async def test_dismiss_nonexistent_returns_false(self, db_session):
        result = await dismiss_proposal(db_session, "nonexistent-id", "not needed")
        assert result is False

    @pytest.mark.asyncio
    async def test_dismiss_non_pending_returns_false(self, db_session):
        from orchestrator.db.models import GovernanceProposal

        proposal = GovernanceProposal(
            team_id=self._TEAM1,
            proposal_type="model_downshift",
            title="already applied",
            rationale="reason",
            proposed_yaml="---",
            status="applied",
        )
        db_session.add(proposal)
        await db_session.commit()

        result = await dismiss_proposal(db_session, str(proposal.id), "don't want it")
        assert result is False

    @pytest.mark.asyncio
    async def test_dismiss_pending_succeeds(self, db_session):
        from orchestrator.db.models import GovernanceProposal

        proposal = GovernanceProposal(
            team_id=self._TEAM1,
            proposal_type="model_downshift",
            title="dismiss me",
            rationale="reason",
            proposed_yaml="---",
            status="pending",
        )
        db_session.add(proposal)
        await db_session.commit()

        result = await dismiss_proposal(db_session, str(proposal.id), "Not relevant")
        assert result is True

        await db_session.refresh(proposal)
        assert proposal.status == "dismissed"
        assert proposal.dismissed_reason == "Not relevant"
        assert proposal.dismissed_at is not None
