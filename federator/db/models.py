"""
Modus Federator — Database Models
=========================================
SQLAlchemy 2.x async ORM models for the blind relay.

CRITICAL: This service NEVER stores decrypted customer data.
encrypted_payload is an opaque blob — no column for decrypted content.
Only metadata signals (fitness, gene_count, industry) are queryable.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime, Float, Index, Integer, LargeBinary,
    String, func, text, JSON,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class EncryptedDelta(Base):
    """
    Incoming encrypted constitution delta with ZK proof.
    The encrypted_payload is NEVER decrypted by the federator.
    """
    __tablename__ = "encrypted_deltas"
    __table_args__ = (
        Index("ix_delta_industry_week", "industry_type", "epoch_week"),
        Index("ix_delta_received", "received_at"),
        Index("ix_delta_nonce_week", "nonce", "epoch_week"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    nonce: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="Weekly-rotating HMAC nonce — anonymous, unlinkable across weeks",
    )
    industry_type: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_payload: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False,
        comment="Opaque encrypted blob — NEVER decrypted by federator",
    )
    # ZK proof data
    zk_proof_commitment: Mapped[str] = mapped_column(String(128), nullable=False)
    zk_proof_challenge: Mapped[str] = mapped_column(String(128), nullable=False)
    zk_proof_response: Mapped[str] = mapped_column(String(128), nullable=False)
    zk_public_inputs: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Unencrypted metadata signals (the ONLY thing we aggregate)
    fitness_improvement: Mapped[float] = mapped_column(Float, nullable=False)
    generation_span: Mapped[int] = mapped_column(Integer, nullable=False)
    gene_count: Mapped[int] = mapped_column(Integer, nullable=False)

    # Audit
    merkle_leaf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    epoch_week: Mapped[str] = mapped_column(String(8), nullable=False)  # e.g. "2026-W11"
    received_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )


class MergedResult(Base):
    """Periodically computed aggregate from metadata signals."""
    __tablename__ = "merged_results"
    __table_args__ = (
        Index("ix_merged_industry_version", "industry_type", "version"),
        Index("ix_merged_computed", "computed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    industry_type: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="'all' for cross-industry aggregate, or specific industry",
    )
    aggregate_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    participating_instances: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    epoch_week: Mapped[str] = mapped_column(String(8), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )


class MerkleAnchor(Base):
    """Periodic Merkle root snapshots for audit trail."""
    __tablename__ = "merkle_anchors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    root_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    leaf_count: Mapped[int] = mapped_column(Integer, nullable=False)
    epoch_week: Mapped[str] = mapped_column(String(8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )


class SubscriberAccess(Base):
    """Tracks who can access results."""
    __tablename__ = "subscriber_access"
    __table_args__ = (
        Index("ix_sub_fingerprint", "identity_fingerprint"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    identity_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="SHA-256 of the token subject - never the raw value",
    )
    tier: Mapped[str] = mapped_column(
        String(16), nullable=False,
        comment="participant | consumer",
    )
    industry_type: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'other'"))
    last_access_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
