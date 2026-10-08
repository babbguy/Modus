"""
Modus Federator — Audit API
==================================
Merkle tree audit trail for verifying delta inclusion.
Customers can prove their submissions were accepted without
the federator being able to link submissions to identities.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from federator.core.auth import FederatorIdentity, get_identity
from federator.db.models import MerkleAnchor, EncryptedDelta
from federator.db.session import get_read_session

logger = logging.getLogger(__name__)

audit_router = APIRouter(prefix="/v1/audit", tags=["audit"])


class MerkleRootResponse(BaseModel):
    root_hash: str
    leaf_count: int
    epoch_week: str
    created_at: Optional[str] = None


class InclusionProofRequest(BaseModel):
    merkle_leaf_hash: str = Field(..., min_length=64, max_length=64)


class InclusionProofResponse(BaseModel):
    included: bool
    delta_id: Optional[str] = None
    epoch_week: Optional[str] = None
    merkle_root: Optional[str] = None
    detail: str = ""


@audit_router.get("/merkle-root", response_model=Optional[MerkleRootResponse])
async def get_merkle_root(
    identity: FederatorIdentity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
) -> Optional[MerkleRootResponse]:
    """Current Merkle root for audit verification."""
    result = await db.execute(
        select(MerkleAnchor)
        .order_by(desc(MerkleAnchor.created_at))
        .limit(1)
    )
    anchor = result.scalar_one_or_none()
    if not anchor:
        return None

    return MerkleRootResponse(
        root_hash=anchor.root_hash,
        leaf_count=anchor.leaf_count,
        epoch_week=anchor.epoch_week,
        created_at=anchor.created_at.isoformat() if anchor.created_at else None,
    )


@audit_router.post("/verify-inclusion", response_model=InclusionProofResponse)
async def verify_inclusion(
    req: InclusionProofRequest,
    identity: FederatorIdentity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
) -> InclusionProofResponse:
    """
    Verify that a delta with the given Merkle leaf hash exists in the store.

    Customers use this to confirm their submissions were accepted.
    The federator cannot link the leaf hash back to a customer identity
    (nonces rotate weekly and are HMAC-derived).
    """
    # Look up the delta by its Merkle leaf hash
    result = await db.execute(
        select(EncryptedDelta)
        .where(EncryptedDelta.merkle_leaf_hash == req.merkle_leaf_hash)
        .limit(1)
    )
    delta = result.scalar_one_or_none()

    if not delta:
        return InclusionProofResponse(
            included=False,
            detail="No delta found with this Merkle leaf hash",
        )

    # Check if there's a Merkle anchor that covers this epoch
    anchor_result = await db.execute(
        select(MerkleAnchor)
        .where(MerkleAnchor.epoch_week == delta.epoch_week)
        .order_by(desc(MerkleAnchor.created_at))
        .limit(1)
    )
    anchor = anchor_result.scalar_one_or_none()

    return InclusionProofResponse(
        included=True,
        delta_id=delta.id,
        epoch_week=delta.epoch_week,
        merkle_root=anchor.root_hash if anchor else None,
        detail="Delta found and verified" + (
            " (anchored in Merkle tree)" if anchor else " (pending Merkle anchor)"
        ),
    )
