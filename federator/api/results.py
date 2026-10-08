"""
Modus Federator — Results API
====================================
GET /v1/results — serve merged collective intelligence.
GET /v1/results/{industry} — industry-filtered results.
GET /v1/benchmarks — cross-industry benchmark summary.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from federator.config import settings
from federator.core.auth import FederatorIdentity, get_identity
from federator.db.models import MergedResult
from federator.db.session import get_read_session

logger = logging.getLogger(__name__)

results_router = APIRouter(prefix="/v1", tags=["results"])

# Rate limiter (injected from main.py)
_read_limiter = None


def set_read_limiter(limiter) -> None:
    global _read_limiter
    _read_limiter = limiter


# ── Response schemas ──────────────────────────────────────────────────────────

class FitnessTrend(BaseModel):
    avg_improvement: float = 0.0
    median_improvement: float = 0.0
    p25_improvement: float = 0.0
    p75_improvement: float = 0.0
    p90_improvement: float = 0.0
    stddev: float = 0.0
    trend_direction: str = "stable"
    sample_size: int = 0


class EvolutionActivity(BaseModel):
    avg_gene_count: float = 0.0
    avg_generation_span: float = 0.0
    median_gene_count: float = 0.0
    median_generation_span: float = 0.0
    active_contributors: int = 0


class MergedResultResponse(BaseModel):
    version: int
    industry_type: str
    fitness_trend: FitnessTrend
    evolution_activity: EvolutionActivity
    participating_instances: int
    confidence_score: float
    epoch_week: str
    computed_at: Optional[str] = None


class BenchmarkEntry(BaseModel):
    industry: str
    avg_fitness_improvement: float
    active_contributors: int
    confidence: float
    epoch_week: str


class BenchmarkResponse(BaseModel):
    benchmarks: list[BenchmarkEntry]
    total_industries: int
    total_contributors: int
    last_updated: Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@results_router.get("/results", response_model=Optional[MergedResultResponse])
async def get_latest_results(
    identity: FederatorIdentity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
) -> Optional[MergedResultResponse]:
    """Latest cross-industry merged results."""
    if _read_limiter and not _read_limiter.allow(identity.identity_fingerprint):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    result = await db.execute(
        select(MergedResult)
        .where(MergedResult.industry_type == "all")
        .order_by(desc(MergedResult.computed_at))
        .limit(1)
    )
    merged = result.scalar_one_or_none()
    if not merged:
        return None

    agg = merged.aggregate_data or {}
    return MergedResultResponse(
        version=merged.version,
        industry_type=merged.industry_type,
        fitness_trend=FitnessTrend(**(agg.get("fitness_trend", {}))),
        evolution_activity=EvolutionActivity(**(agg.get("evolution_activity", {}))),
        participating_instances=merged.participating_instances,
        confidence_score=merged.confidence_score,
        epoch_week=merged.epoch_week,
        computed_at=merged.computed_at.isoformat() if merged.computed_at else None,
    )


@results_router.get("/results/{industry}", response_model=Optional[MergedResultResponse])
async def get_industry_results(
    industry: str,
    identity: FederatorIdentity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
) -> Optional[MergedResultResponse]:
    """Latest results for a specific industry."""
    if _read_limiter and not _read_limiter.allow(identity.identity_fingerprint):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    if industry not in settings.allowed_industries:
        raise HTTPException(status_code=404, detail=f"Unknown industry: {industry}")

    result = await db.execute(
        select(MergedResult)
        .where(MergedResult.industry_type == industry)
        .order_by(desc(MergedResult.computed_at))
        .limit(1)
    )
    merged = result.scalar_one_or_none()
    if not merged:
        return None

    agg = merged.aggregate_data or {}
    return MergedResultResponse(
        version=merged.version,
        industry_type=merged.industry_type,
        fitness_trend=FitnessTrend(**(agg.get("fitness_trend", {}))),
        evolution_activity=EvolutionActivity(**(agg.get("evolution_activity", {}))),
        participating_instances=merged.participating_instances,
        confidence_score=merged.confidence_score,
        epoch_week=merged.epoch_week,
        computed_at=merged.computed_at.isoformat() if merged.computed_at else None,
    )


@results_router.get("/benchmarks", response_model=BenchmarkResponse)
async def get_benchmarks(
    identity: FederatorIdentity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
) -> BenchmarkResponse:
    """Cross-industry benchmark summary — latest results per industry."""
    if _read_limiter and not _read_limiter.allow(identity.identity_fingerprint):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    # Get latest result per industry (excluding "all")
    # Subquery: max version per industry
    subq = (
        select(
            MergedResult.industry_type,
            func.max(MergedResult.version).label("max_version"),
        )
        .where(MergedResult.industry_type != "all")
        .group_by(MergedResult.industry_type)
        .subquery()
    )

    query = (
        select(MergedResult)
        .join(subq, (MergedResult.industry_type == subq.c.industry_type) &
              (MergedResult.version == subq.c.max_version))
        .order_by(MergedResult.industry_type)
    )
    result = await db.execute(query)
    rows = result.scalars().all()

    benchmarks = []
    total_contributors = 0
    last_updated = None

    for row in rows:
        agg = row.aggregate_data or {}
        fitness = agg.get("fitness_trend", {})
        avg_fi = fitness.get("avg_improvement", 0.0)
        contributors = row.participating_instances

        benchmarks.append(BenchmarkEntry(
            industry=row.industry_type,
            avg_fitness_improvement=avg_fi,
            active_contributors=contributors,
            confidence=row.confidence_score,
            epoch_week=row.epoch_week,
        ))
        total_contributors += contributors
        if row.computed_at:
            ts = row.computed_at.isoformat()
            if last_updated is None or ts > last_updated:
                last_updated = ts

    return BenchmarkResponse(
        benchmarks=benchmarks,
        total_industries=len(benchmarks),
        total_contributors=total_contributors,
        last_updated=last_updated,
    )
