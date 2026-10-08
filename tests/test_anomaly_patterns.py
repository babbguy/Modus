"""
Tests for orchestrator.core.anomaly_patterns — Pattern anomaly detectors.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from orchestrator.core.anomaly_patterns import (
    _signal,
    detect_anomaly_patterns,
    _detect_threshold_ceiling,
    _detect_team_spend_anomaly,
)
from orchestrator.db.models import (
    Alert,
    Base,
    Team,
    Threshold,
    UsageAggregate,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def anom_engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def anom_session(anom_engine):
    factory = async_sessionmaker(anom_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _create_team(db, team_id: str) -> Team:
    t = Team(id=team_id, slug=f"team-{team_id[:8]}", name=f"Team {team_id[:8]}")
    db.add(t)
    await db.flush()
    return t


async def _create_threshold(db, threshold_id: str, team_id: str) -> Threshold:
    t = Threshold(
        id=threshold_id,
        team_id=team_id,
        name="Daily Budget",
        metric="total_cost",
        period="daily",
        critical_value=Decimal("100"),
    )
    db.add(t)
    await db.flush()
    return t


# ── _signal ──────────────────────────────────────────────────────────────────


class TestSignal:
    def test_builds_signal(self):
        s = _signal("type", "title", "reason", "warning", {"k": "v"}, ["tag1"])
        assert s["signal_type"] == "type"
        assert s["title"] == "title"
        assert s["severity"] == "warning"
        assert s["evidence"]["k"] == "v"
        assert s["regulatory_tags"] == ["tag1"]

    def test_default_empty_tags(self):
        s = _signal("type", "title", "reason", "warning", {})
        assert s["regulatory_tags"] == []


# ── _detect_threshold_ceiling ────────────────────────────────────────────────


class TestDetectThresholdCeiling:
    @pytest.mark.asyncio
    async def test_detects_repeated_breaches(self, anom_session):
        team_id = str(uuid.uuid4())
        threshold_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        await _create_team(anom_session, team_id)
        await _create_threshold(anom_session, threshold_id, team_id)

        # Create 6 alerts (> 5 threshold)
        for i in range(6):
            anom_session.add(Alert(
                threshold_id=threshold_id,
                team_id=team_id,
                severity="critical",
                metric="total_cost",
                threshold_value=Decimal("100"),
                actual_value=Decimal("110"),
                period_start=now - timedelta(days=i),
                period_end=now - timedelta(days=i) + timedelta(days=1),
            ))
        await anom_session.flush()

        signals = await _detect_threshold_ceiling(anom_session, team_id, now - timedelta(days=7))
        assert len(signals) == 1
        assert signals[0]["signal_type"] == "threshold_ceiling"
        assert signals[0]["evidence"]["breach_count"] == 6

    @pytest.mark.asyncio
    async def test_no_signal_for_few_breaches(self, anom_session):
        team_id = str(uuid.uuid4())
        threshold_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        await _create_team(anom_session, team_id)
        await _create_threshold(anom_session, threshold_id, team_id)

        # Only 2 alerts (below threshold of 5)
        for i in range(2):
            anom_session.add(Alert(
                threshold_id=threshold_id,
                team_id=team_id,
                severity="warning",
                metric="total_cost",
                threshold_value=Decimal("100"),
                actual_value=Decimal("80"),
                period_start=now - timedelta(days=i),
                period_end=now,
            ))
        await anom_session.flush()

        signals = await _detect_threshold_ceiling(anom_session, team_id, now - timedelta(days=7))
        assert len(signals) == 0


# ── _detect_team_spend_anomaly ───────────────────────────────────────────────


class TestDetectTeamSpendAnomaly:
    @pytest.mark.asyncio
    async def test_detects_spend_spike(self, anom_session):
        team_id = str(uuid.uuid4())
        app_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        await _create_team(anom_session, team_id)

        # Baseline: 7 days at $10/day = $70
        for day_offset in range(1, 8):
            anom_session.add(UsageAggregate(
                app_id=app_id,
                team_id=team_id,
                provider="openai",
                model="gpt-4o",
                resource_type="llm_call",
                granularity="daily",
                period_start=now - timedelta(days=day_offset + 1),
                period_end=now - timedelta(days=day_offset),
                call_count=100,
                input_tokens=10000,
                output_tokens=5000,
                total_tokens=15000,
                total_cost=Decimal("10.00"),
            ))

        # Today: $30 (3x baseline)
        anom_session.add(UsageAggregate(
            app_id=app_id,
            team_id=team_id,
            provider="openai",
            model="gpt-4o",
            resource_type="llm_call",
            granularity="daily",
            period_start=now - timedelta(hours=12),
            period_end=now,
            call_count=300,
            input_tokens=30000,
            output_tokens=15000,
            total_tokens=45000,
            total_cost=Decimal("30.00"),
        ))
        await anom_session.flush()

        signals = await _detect_team_spend_anomaly(anom_session, team_id, now - timedelta(days=14))
        assert len(signals) == 1
        assert signals[0]["signal_type"] == "team_spend_anomaly"
        assert signals[0]["evidence"]["ratio"] >= 2.0

    @pytest.mark.asyncio
    async def test_no_signal_for_normal_spend(self, anom_session):
        team_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        signals = await _detect_team_spend_anomaly(anom_session, team_id, now - timedelta(days=7))
        assert len(signals) == 0


# ── detect_anomaly_patterns (main entry) ─────────────────────────────────────


class TestDetectAnomalyPatterns:
    @pytest.mark.asyncio
    async def test_runs_all_detectors(self, anom_session):
        team_id = str(uuid.uuid4())
        signals = await detect_anomaly_patterns(anom_session, team_id)
        # With empty DB, no signals expected
        assert isinstance(signals, list)
        assert len(signals) == 0
