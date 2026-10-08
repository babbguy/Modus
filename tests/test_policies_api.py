"""
Tests for orchestrator.api.policies — Schema validation for policy
request/response models, valid type/effect/scope constants.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from orchestrator.api.policies import (
    VALID_EFFECTS,
    VALID_POLICY_TYPES,
    VALID_SCOPES,
    EvaluateRequestBody,
    EvaluateResponse,
    PolicyCreate,
    PolicyUpdate,
)


# ── Constants ───────────────────────────────────────────────────────────────


class TestPolicyConstants:
    def test_valid_policy_types_not_empty(self):
        assert len(VALID_POLICY_TYPES) > 0

    def test_known_types_present(self):
        for t in ("budget_cap", "rate_limit", "model_allowlist",
                   "model_denylist", "provider_block", "environment_block",
                   "token_cap", "degradation_ladder"):
            assert t in VALID_POLICY_TYPES

    def test_valid_effects(self):
        assert VALID_EFFECTS == {"deny", "throttle", "warn"}

    def test_valid_scopes(self):
        assert VALID_SCOPES == {"platform", "team", "app"}


# ── EvaluateRequestBody ────────────────────────────────────────────────────


class TestEvaluateRequestBody:
    def test_valid_body(self):
        body = EvaluateRequestBody(
            provider="openai",
            model="gpt-4o",
            environment="production",
            resource_type="llm_call",
        )
        assert body.provider == "openai"
        assert body.model == "gpt-4o"

    def test_provider_lowercased(self):
        body = EvaluateRequestBody(provider="OpenAI")
        assert body.provider == "openai"

    def test_provider_stripped(self):
        body = EvaluateRequestBody(provider="  openai  ")
        assert body.provider == "openai"

    def test_provider_required(self):
        with pytest.raises(ValidationError):
            EvaluateRequestBody()

    def test_optional_fields_default_none(self):
        body = EvaluateRequestBody(provider="openai")
        assert body.model is None
        assert body.environment is None
        assert body.estimated_tokens is None
        assert body.estimated_cost is None

    def test_resource_type_defaults(self):
        body = EvaluateRequestBody(provider="openai")
        assert body.resource_type == "llm_call"

    def test_estimated_tokens_non_negative(self):
        body = EvaluateRequestBody(
            provider="openai",
            estimated_tokens=0,
        )
        assert body.estimated_tokens == 0

        with pytest.raises(ValidationError):
            EvaluateRequestBody(
                provider="openai",
                estimated_tokens=-1,
            )

    def test_estimated_cost_non_negative(self):
        body = EvaluateRequestBody(
            provider="openai",
            estimated_cost=Decimal("0.05"),
        )
        assert body.estimated_cost == Decimal("0.05")

        with pytest.raises(ValidationError):
            EvaluateRequestBody(
                provider="openai",
                estimated_cost=Decimal("-0.01"),
            )


# ── EvaluateResponse ────────────────────────────────────────────────────────


class TestEvaluateResponse:
    def test_allow_response(self):
        resp = EvaluateResponse(decision="allow", reason="ok")
        assert resp.decision == "allow"
        assert resp.policy_id is None
        assert resp.suggested_model is None

    def test_deny_with_details(self):
        resp = EvaluateResponse(
            decision="deny",
            reason="Budget exceeded",
            policy_id="p-123",
            policy_name="daily-cap",
            suggested_model="gpt-4o-mini",
            message="Switch to cheaper model",
        )
        assert resp.decision == "deny"
        assert resp.policy_name == "daily-cap"

    def test_throttle_with_retry(self):
        resp = EvaluateResponse(
            decision="throttle",
            reason="Rate limit",
            retry_after_seconds=30,
        )
        assert resp.retry_after_seconds == 30


# ── PolicyCreate ────────────────────────────────────────────────────────────


class TestPolicyCreate:
    def test_valid_create(self):
        pc = PolicyCreate(
            name="test-policy",
            scope="team",
            policy_type="budget_cap",
            config={"cap_usd": "100", "period": "daily"},
        )
        assert pc.name == "test-policy"
        assert pc.effect == "deny"  # default
        assert pc.priority == 100  # default

    def test_invalid_policy_type(self):
        with pytest.raises(ValidationError):
            PolicyCreate(
                name="bad",
                scope="team",
                policy_type="nonexistent_type",
                config={},
            )

    def test_invalid_scope(self):
        with pytest.raises(ValidationError):
            PolicyCreate(
                name="bad",
                scope="invalid",
                policy_type="budget_cap",
                config={},
            )

    def test_invalid_effect(self):
        with pytest.raises(ValidationError):
            PolicyCreate(
                name="bad",
                scope="team",
                policy_type="budget_cap",
                effect="invalid",
                config={},
            )

    def test_name_required(self):
        with pytest.raises(ValidationError):
            PolicyCreate(
                scope="team",
                policy_type="budget_cap",
                config={},
            )

    def test_config_required(self):
        with pytest.raises(ValidationError):
            PolicyCreate(
                name="no-config",
                scope="team",
                policy_type="budget_cap",
            )

    def test_priority_range(self):
        pc = PolicyCreate(
            name="p", scope="team", policy_type="budget_cap",
            config={}, priority=1,
        )
        assert pc.priority == 1

        pc2 = PolicyCreate(
            name="p", scope="team", policy_type="budget_cap",
            config={}, priority=999,
        )
        assert pc2.priority == 999

        with pytest.raises(ValidationError):
            PolicyCreate(
                name="p", scope="team", policy_type="budget_cap",
                config={}, priority=0,
            )

        with pytest.raises(ValidationError):
            PolicyCreate(
                name="p", scope="team", policy_type="budget_cap",
                config={}, priority=1000,
            )

    def test_all_valid_types_accepted(self):
        for ptype in VALID_POLICY_TYPES:
            pc = PolicyCreate(
                name=f"test-{ptype}",
                scope="team",
                policy_type=ptype,
                config={},
            )
            assert pc.policy_type == ptype


# ── PolicyUpdate ────────────────────────────────────────────────────────────


class TestPolicyUpdate:
    def test_empty_update(self):
        pu = PolicyUpdate()
        assert pu.name is None
        assert pu.config is None
        assert pu.is_active is None

    def test_partial_update(self):
        pu = PolicyUpdate(name="updated", priority=50)
        assert pu.name == "updated"
        assert pu.priority == 50

    def test_invalid_effect(self):
        with pytest.raises(ValidationError):
            PolicyUpdate(effect="invalid")

    def test_deactivate(self):
        pu = PolicyUpdate(is_active=False)
        assert pu.is_active is False
