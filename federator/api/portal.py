"""
Modus Federator — Portal API
===================================
Portal metadata and browsable information for subscribers.
Available to both participants and consumers.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, func, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from federator.config import settings
from federator.core.auth import FederatorIdentity, get_identity
from federator.db.models import EncryptedDelta, MergedResult, MerkleAnchor
from federator.db.session import get_read_session

logger = logging.getLogger(__name__)

portal_router = APIRouter(prefix="/v1", tags=["portal"])


class IndustryStats(BaseModel):
    industry: str
    total_deltas: int
    unique_contributors: int
    latest_epoch_week: Optional[str] = None


class PortalResponse(BaseModel):
    service: str = "modus-federator"
    version: str
    available_industries: list[str]
    industry_stats: list[IndustryStats]
    total_deltas: int
    total_contributors: int
    total_merged_results: int
    latest_merkle_root: Optional[str] = None
    latest_epoch_week: Optional[str] = None


@portal_router.get("/portal", response_model=PortalResponse)
async def get_portal(
    identity: FederatorIdentity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
) -> PortalResponse:
    """Portal overview — available industries, stats, and status."""

    # Industry stats
    stats_query = (
        select(
            EncryptedDelta.industry_type,
            func.count(EncryptedDelta.id).label("total"),
            func.count(distinct(EncryptedDelta.nonce)).label("contributors"),
            func.max(EncryptedDelta.epoch_week).label("latest_week"),
        )
        .group_by(EncryptedDelta.industry_type)
        .order_by(EncryptedDelta.industry_type)
    )
    stats_result = await db.execute(stats_query)
    industry_rows = stats_result.all()

    industry_stats = []
    total_deltas = 0
    total_contributors = 0
    latest_week = None

    for industry, count, contributors, week in industry_rows:
        industry_stats.append(IndustryStats(
            industry=industry,
            total_deltas=count,
            unique_contributors=contributors,
            latest_epoch_week=week,
        ))
        total_deltas += count
        total_contributors += contributors
        if week and (latest_week is None or week > latest_week):
            latest_week = week

    # Total merged results
    merged_count_result = await db.execute(select(func.count(MergedResult.id)))
    merged_count = merged_count_result.scalar() or 0

    # Latest Merkle root
    anchor_result = await db.execute(
        select(MerkleAnchor)
        .order_by(MerkleAnchor.created_at.desc())
        .limit(1)
    )
    anchor = anchor_result.scalar_one_or_none()

    return PortalResponse(
        version=settings.version,
        available_industries=settings.allowed_industries,
        industry_stats=industry_stats,
        total_deltas=total_deltas,
        total_contributors=total_contributors,
        total_merged_results=merged_count,
        latest_merkle_root=anchor.root_hash if anchor else None,
        latest_epoch_week=latest_week,
    )
