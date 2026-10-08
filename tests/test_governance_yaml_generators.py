"""
Governance Loop coverage.

Targets orchestrator.core.governance_loop: YAML generators, detection
functions, settings defaults, expensive model lookups.
"""
from __future__ import annotations

import uuid

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from orchestrator.db.models import Base
@pytest_asyncio.fixture
async def gov_engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def gov_session(gov_engine):
    factory = async_sessionmaker(gov_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


# ═══════════════════════════════════════════════════════════════════════════════
# 1. YAML GENERATORS
# ═══════════════════════════════════════════════════════════════════════════════

def test_generate_degradation_ladder_yaml():
    from orchestrator.core.governance_loop import _generate_degradation_ladder_yaml
    result = _generate_degradation_ladder_yaml(
        name="test-ladder",
        budget_usd="500",
        period="daily",
        tiers=[
            {"pct": 50, "model": "gpt-4o-mini"},
            {"pct": 100, "action": "deny"},
        ],
    )
    assert "test-ladder" in result
    assert "degradation_ladder" in result
    assert "gpt-4o-mini" in result
    assert "deny" in result


def test_generate_budget_cap_yaml():
    from orchestrator.core.governance_loop import _generate_budget_cap_yaml
    result = _generate_budget_cap_yaml(
        name="test-cap",
        cap_usd="1000",
        period="monthly",
    )
    assert "test-cap" in result
    assert "budget_cap" in result
    assert "1000" in result
    assert "monthly" in result


def test_generate_rate_limit_yaml():
    from orchestrator.core.governance_loop import _generate_rate_limit_yaml
    result = _generate_rate_limit_yaml(
        name="test-rate",
        max_calls=100,
        window_seconds=3600,
    )
    assert "test-rate" in result
    assert "rate_limit" in result
    assert "100" in result
    assert "3600" in result


def test_generate_amplification_gate_yaml():
    from orchestrator.core.governance_loop import _generate_amplification_gate_yaml
    result = _generate_amplification_gate_yaml(
        name="test-amp",
        max_amplification=3.0,
    )
    assert "test-amp" in result
    assert "amplification_gate" in result
    assert "3.0" in result


def test_generate_model_denylist_yaml():
    from orchestrator.core.governance_loop import _generate_model_denylist_yaml
    result = _generate_model_denylist_yaml(
        name="test-deny",
        models=["gpt-4", "o1"],
    )
    assert "test-deny" in result
    assert "model_denylist" in result
    assert "gpt-4" in result
    assert "o1" in result


# ═══════════════════════════════════════════════════════════════════════════════
# 2. EXPENSIVE MODEL LOOKUPS
# ═══════════════════════════════════════════════════════════════════════════════

def test_expensive_models():
    from orchestrator.core.governance_loop import _EXPENSIVE_MODELS, _CHEAP_ALTERNATIVES
    assert "gpt-4o" in _EXPENSIVE_MODELS
    assert "gpt-4o" in _CHEAP_ALTERNATIVES
    assert _CHEAP_ALTERNATIVES["gpt-4o"] == "gpt-4o-mini"
    assert "claude-opus-4-5-20251022" in _EXPENSIVE_MODELS
    assert "claude-opus-4-5-20251022" in _CHEAP_ALTERNATIVES


# ═══════════════════════════════════════════════════════════════════════════════
# 3. DETECT MODEL OVERPROVISION (empty DB)
# ═══════════════════════════════════════════════════════════════════════════════

async def test_detect_model_overprovision_empty(gov_session):
    from orchestrator.core.governance_loop import _detect_model_overprovision
    proposals = await _detect_model_overprovision(
        gov_session,
        team_id=str(uuid.uuid4()),
        lookback_hours=24,
        min_calls=50,
        output_threshold=200,
        cost_threshold=0.01,
    )
    assert proposals == []


# ═══════════════════════════════════════════════════════════════════════════════
# 4. DETECT BUDGET OVERRUNS (empty DB)
# ═══════════════════════════════════════════════════════════════════════════════

async def test_detect_budget_overruns_empty(gov_session):
    from orchestrator.core.governance_loop import _detect_budget_overruns
    proposals = await _detect_budget_overruns(
        gov_session,
        team_id=str(uuid.uuid4()),
        lookback_days=7,
    )
    assert proposals == []


# ═══════════════════════════════════════════════════════════════════════════════
# 5. DETECT BUDGET OVERRUNS WITH POLICY
# ═══════════════════════════════════════════════════════════════════════════════

async def test_detect_budget_overruns_with_policy_no_breaches(gov_session):
    """Create a budget_cap policy but no usage data — should return empty."""
    from orchestrator.db.models import GovernancePolicy, Team
    team_id = str(uuid.uuid4())
    # Create a team
    team = Team(id=team_id, slug="gov-team", name="Gov Team")
    gov_session.add(team)
    # Create a budget_cap policy
    policy = GovernancePolicy(
        name="Test Cap",
        scope="team",
        policy_type="budget_cap",
        effect="deny",
        priority=100,
        team_id=team_id,
        config={"cap_usd": "100.00", "period": "daily"},
        is_active=True,
        created_by="test",
    )
    gov_session.add(policy)
    await gov_session.flush()

    from orchestrator.core.governance_loop import _detect_budget_overruns
    proposals = await _detect_budget_overruns(
        gov_session, team_id=team_id, lookback_days=7,
    )
    # No usage data so no breaches detected
    assert proposals == []


# ═══════════════════════════════════════════════════════════════════════════════
# 6. DETECT AMPLIFICATION PATTERNS (empty DB)
# ═══════════════════════════════════════════════════════════════════════════════

async def test_detect_amplification_patterns_empty(gov_session):
    from orchestrator.core.governance_loop import _detect_amplification_patterns
    proposals = await _detect_amplification_patterns(
        gov_session,
        team_id=str(uuid.uuid4()),
        lookback_hours=24,
        threshold=3.0,
    )
    assert proposals == []


# ═══════════════════════════════════════════════════════════════════════════════
# 7. DETECT REWIND PATTERNS (empty DB)
# ═══════════════════════════════════════════════════════════════════════════════

async def test_detect_rewind_patterns_empty(gov_session):
    from orchestrator.core.governance_loop import _detect_rewind_patterns
    proposals = await _detect_rewind_patterns(
        gov_session,
        team_id=str(uuid.uuid4()),
        lookback_hours=24,
        pattern_threshold=3,
    )
    assert proposals == []


# ═══════════════════════════════════════════════════════════════════════════════
# 8. GOVERNANCE DEFAULTS
# ═══════════════════════════════════════════════════════════════════════════════

def test_governance_defaults():
    from orchestrator.core.governance_loop import GOVERNANCE_DEFAULTS
    assert "task.governance_loop.enabled" in GOVERNANCE_DEFAULTS
    assert GOVERNANCE_DEFAULTS["task.governance_loop.enabled"] == "true"
    assert "governance.lookback_hours" in GOVERNANCE_DEFAULTS
    assert "governance.amplification_threshold" in GOVERNANCE_DEFAULTS


# ═══════════════════════════════════════════════════════════════════════════════
# 9. COT IMPORTS
# ═══════════════════════════════════════════════════════════════════════════════

def test_cot_imports():
    from orchestrator.core.governance_loop import _cot_imports
    append_entry, link_entry, infer_tags, build_step = _cot_imports()
    assert callable(append_entry)
    assert callable(link_entry)


def test_proposal_type_to_rule_mapping():
    from orchestrator.core.governance_loop import _PROPOSAL_TYPE_TO_RULE, _RULE_DETAIL
    # Verify mapping consistency
    for ptype, rule in _PROPOSAL_TYPE_TO_RULE.items():
        assert rule in _RULE_DETAIL, f"Rule '{rule}' for proposal '{ptype}' not in _RULE_DETAIL"
