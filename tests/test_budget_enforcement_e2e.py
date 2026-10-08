"""
Tests — Budget & Policy Enforcement E2E
==========================================
Tests that budget_cap, token_cap, model_denylist, and provider_block policies
produce correct decisions when evaluated through the real policy engine.

Seeds RealTimeSpend rows (what the policy engine reads) and GovernancePolicy
rows, then calls evaluate_policies() and verifies the PolicyResult.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from orchestrator.db.models import (
    Base, App, Team, GovernancePolicy, RealTimeSpend,
)
# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def team_id():
    return str(uuid.uuid4())


@pytest.fixture
def app_uuid():
    return str(uuid.uuid4())


@pytest_asyncio.fixture
async def policy_engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def policy_factory(policy_engine):
    return async_sessionmaker(policy_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def seeded_env(policy_factory, team_id, app_uuid):
    """Seed team + app."""
    async with policy_factory() as db:
        db.add(Team(id=team_id, slug="policy-team", name="Policy Team"))
        db.add(App(
            id=app_uuid,
            team_id=team_id,
            app_id="policy-app",
            app_name="Policy App",
            environment="production",
            api_key_hash="$2b$12$placeholder_hash_for_testing_only___",
            api_key_prefix="mds_policytest00",
        ))
        await db.commit()
    return app_uuid, team_id


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _daily_window_key(now: datetime | None = None):
    """Build the window_key, window_start, window_end the policy engine expects."""
    now = now or _now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    key = f"daily:{start.strftime('%Y-%m-%d')}"
    return key, start, end


async def _add_policy(factory, team_id, policy_type, config, effect="deny"):
    async with factory() as db:
        db.add(GovernancePolicy(
            id=str(uuid.uuid4()),
            team_id=team_id,
            name=f"Test {policy_type}",
            policy_type=policy_type,
            scope="team",
            effect=effect,
            config=config,
            priority=100,
            is_active=True,
            created_by="test-admin",
        ))
        await db.commit()


async def _seed_spend(factory, app_uuid, team_id, total_cost: Decimal,
                      input_tokens: int = 0, output_tokens: int = 0,
                      call_count: int = 10):
    """Insert a RealTimeSpend row for the current daily window."""
    wkey, wstart, wend = _daily_window_key()
    async with factory() as db:
        db.add(RealTimeSpend(
            id=str(uuid.uuid4()),
            app_id=app_uuid,
            team_id=team_id,
            period="daily",
            window_key=wkey,
            window_start=wstart,
            window_end=wend,
            total_cost=total_cost,
            call_count=call_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_duration_ms=2000,
        ))
        await db.commit()


async def _evaluate(factory, app_uuid, team_id, **kwargs):
    """Evaluate policies using the real engine."""
    from orchestrator.core.policy_engine import evaluate_policies
    async with factory() as db:
        return await evaluate_policies(
            db=db,
            app_id=app_uuid,
            team_id=team_id,
            provider=kwargs.get("provider", "openai"),
            model=kwargs.get("model", "gpt-4o"),
            environment=kwargs.get("environment", "production"),
            estimated_tokens=kwargs.get("estimated_tokens"),
            estimated_cost=kwargs.get("estimated_cost"),
        )


# ── Tests: Budget Cap ────────────────────────────────────────────────────────

async def test_budget_cap_denies_when_exceeded(policy_factory, seeded_env):
    """budget_cap with effect=deny blocks calls once spend exceeds cap_usd."""
    app_uuid, team_id = seeded_env

    await _add_policy(policy_factory, team_id, "budget_cap", {
        "cap_usd": "10.00",
        "period": "daily",
    }, effect="deny")

    # Seed spend above the cap
    await _seed_spend(policy_factory, app_uuid, team_id, Decimal("12.00"))

    result = await _evaluate(policy_factory, app_uuid, team_id,
                             estimated_cost=Decimal("1.00"))

    assert result.decision == "deny"
    assert "budget cap" in result.reason.lower()
    assert result.spend_at_decision == Decimal("12.00")
    assert result.spend_limit == Decimal("10.00")


async def test_budget_cap_allows_when_under_limit(policy_factory, seeded_env):
    """budget_cap allows calls when spend + estimated is under the cap."""
    app_uuid, team_id = seeded_env

    await _add_policy(policy_factory, team_id, "budget_cap", {
        "cap_usd": "100.00",
        "period": "daily",
    }, effect="deny")

    # Seed spend well under the cap
    await _seed_spend(policy_factory, app_uuid, team_id, Decimal("5.00"))

    result = await _evaluate(policy_factory, app_uuid, team_id,
                             estimated_cost=Decimal("1.00"))

    assert result.decision == "allow"


async def test_budget_cap_denies_on_projected_overshoot(policy_factory, seeded_env):
    """budget_cap denies when current + estimated_cost exceeds cap."""
    app_uuid, team_id = seeded_env

    await _add_policy(policy_factory, team_id, "budget_cap", {
        "cap_usd": "10.00",
        "period": "daily",
    }, effect="deny")

    # Current spend is $9, but estimated call would push to $11
    await _seed_spend(policy_factory, app_uuid, team_id, Decimal("9.00"))

    result = await _evaluate(policy_factory, app_uuid, team_id,
                             estimated_cost=Decimal("2.00"))

    assert result.decision == "deny"


async def test_budget_cap_warn_does_not_deny(policy_factory, seeded_env):
    """budget_cap with effect=warn fires warning but allows the call."""
    app_uuid, team_id = seeded_env

    await _add_policy(policy_factory, team_id, "budget_cap", {
        "cap_usd": "5.00",
        "period": "daily",
    }, effect="warn")

    await _seed_spend(policy_factory, app_uuid, team_id, Decimal("10.00"))

    result = await _evaluate(policy_factory, app_uuid, team_id,
                             estimated_cost=Decimal("1.00"))

    # Warn effect: engine returns allow (warn is logged, not blocking)
    assert result.decision == "allow"


# ── Tests: Token Cap ─────────────────────────────────────────────────────────

async def test_token_cap_denies_when_exceeded(policy_factory, seeded_env):
    """token_cap blocks when projected tokens exceed max_tokens."""
    app_uuid, team_id = seeded_env

    await _add_policy(policy_factory, team_id, "token_cap", {
        "max_tokens": 10000,
        "period": "daily",
    }, effect="deny")

    # Seed 9000 tokens used
    await _seed_spend(policy_factory, app_uuid, team_id,
                      Decimal("1.00"), input_tokens=7000, output_tokens=2000)

    result = await _evaluate(policy_factory, app_uuid, team_id,
                             estimated_tokens=2000)  # would push to 11k

    assert result.decision == "deny"
    assert "token cap" in result.reason.lower()


async def test_token_cap_allows_when_under(policy_factory, seeded_env):
    """token_cap allows when projected tokens are under the cap."""
    app_uuid, team_id = seeded_env

    await _add_policy(policy_factory, team_id, "token_cap", {
        "max_tokens": 100000,
        "period": "daily",
    }, effect="deny")

    await _seed_spend(policy_factory, app_uuid, team_id,
                      Decimal("1.00"), input_tokens=5000, output_tokens=1000)

    result = await _evaluate(policy_factory, app_uuid, team_id,
                             estimated_tokens=500)

    assert result.decision == "allow"


async def test_token_cap_skips_without_estimate(policy_factory, seeded_env):
    """token_cap is skipped when estimated_tokens is None (can't evaluate)."""
    app_uuid, team_id = seeded_env

    await _add_policy(policy_factory, team_id, "token_cap", {
        "max_tokens": 100,
        "period": "daily",
    }, effect="deny")

    await _seed_spend(policy_factory, app_uuid, team_id,
                      Decimal("1.00"), input_tokens=50000, output_tokens=50000)

    # No estimated_tokens → policy can't fire → allow
    result = await _evaluate(policy_factory, app_uuid, team_id)

    assert result.decision == "allow"


# ── Tests: Model Denylist ────────────────────────────────────────────────────

async def test_model_denylist_blocks(policy_factory, seeded_env):
    """model_denylist denies calls to forbidden models."""
    app_uuid, team_id = seeded_env

    await _add_policy(policy_factory, team_id, "model_denylist", {
        "models": ["gpt-4o", "gpt-4-turbo"],
    })

    result = await _evaluate(policy_factory, app_uuid, team_id, model="gpt-4o")
    assert result.decision == "deny"
    assert "gpt-4o" in result.reason


async def test_model_denylist_allows_non_blocked(policy_factory, seeded_env):
    """model_denylist allows calls to models not in the list."""
    app_uuid, team_id = seeded_env

    await _add_policy(policy_factory, team_id, "model_denylist", {
        "models": ["gpt-4o", "gpt-4-turbo"],
    })

    result = await _evaluate(policy_factory, app_uuid, team_id, model="gpt-4o-mini")
    assert result.decision == "allow"


# ── Tests: Provider Block ────────────────────────────────────────────────────

async def test_provider_block_denies(policy_factory, seeded_env):
    """provider_block denies calls to blocked providers."""
    app_uuid, team_id = seeded_env

    await _add_policy(policy_factory, team_id, "provider_block", {
        "providers": ["deepseek"],
    })

    result = await _evaluate(policy_factory, app_uuid, team_id,
                             provider="deepseek", model="deepseek-chat")
    assert result.decision == "deny"
    assert "deepseek" in result.reason


async def test_provider_block_allows_others(policy_factory, seeded_env):
    """provider_block allows calls to non-blocked providers."""
    app_uuid, team_id = seeded_env

    await _add_policy(policy_factory, team_id, "provider_block", {
        "providers": ["deepseek"],
    })

    result = await _evaluate(policy_factory, app_uuid, team_id,
                             provider="openai", model="gpt-4o")
    assert result.decision == "allow"


# ── Tests: App Enforcement State ─────────────────────────────────────────────

async def test_admin_suspended_app_is_denied(policy_factory, seeded_env):
    """An app with enforcement_state=admin_suspended is always denied."""
    app_uuid, team_id = seeded_env

    # Update app to suspended state
    async with policy_factory() as db:
        from sqlalchemy import update
        await db.execute(
            update(App).where(App.id == app_uuid).values(
                enforcement_state="admin_suspended",
                enforcement_suspended_reason="Policy violation detected",
            )
        )
        await db.commit()

    result = await _evaluate(policy_factory, app_uuid, team_id)
    assert result.decision == "deny"
    assert "suspended" in result.reason.lower()


async def test_budget_suspended_app_is_denied(policy_factory, seeded_env):
    """An app with enforcement_state=budget_suspended is denied."""
    app_uuid, team_id = seeded_env

    async with policy_factory() as db:
        from sqlalchemy import update
        await db.execute(
            update(App).where(App.id == app_uuid).values(
                enforcement_state="budget_suspended",
                enforcement_suspended_reason="Daily budget exceeded",
            )
        )
        await db.commit()

    result = await _evaluate(policy_factory, app_uuid, team_id)
    assert result.decision == "deny"
    assert "budget" in result.reason.lower()
