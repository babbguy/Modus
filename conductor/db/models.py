"""
Modus Conductor — Database Models
=========================================
SQLAlchemy 2.x async ORM models for the Conductor's own database.

Design principles (same as Orchestrator — bank-grade):
  - UUID primary keys (safe for horizontal scale)
  - All money as NUMERIC(18,8) — never float
  - UTC timestamps throughout
  - Indexes designed for dashboard query patterns
  - Append-only audit approach for data lineage

The Conductor stores:
  - Orchestrator registry (who is reporting, when, health)
  - Aggregated data pushed from Orchestrators (the source of truth for dashboard)
  - Reconciliation state (completeness, discrepancies)
  - Push receipts (for exactly-once delivery guarantee)

Federation-ready: all models include region_id for future multi-region support.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    JSON,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uuid() -> str:
    return str(uuid.uuid4())


class UUID(TypeDecorator):
    """UUID column stored as the canonical hyphenated string.

    PostgreSQL uses its native ``uuid`` type. SQLite stores VARCHAR(36) text in
    the same hyphenated form the ORM returns, so hand-written SQL, bound
    parameters and ORM reads all agree on one representation. (The stock
    PostgreSQL UUID type stores 32 hex characters on SQLite, which raw SQL
    then compares and returns in a different form, and its NUMERIC affinity
    turns all-digit values into REALs.)
    """

    impl = String(36)
    cache_ok = True

    def __init__(self, as_uuid: bool = False) -> None:  # signature kept for existing columns
        super().__init__()

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=False))
        return dialect.type_descriptor(String(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        try:
            return str(uuid.UUID(str(value)))
        except ValueError:
            # Not a UUID (e.g. a lookup by an unknown id): pass it through so
            # the comparison matches no rows instead of failing the request.
            # Request validation rejects malformed ids before they get here.
            return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        try:
            return str(uuid.UUID(str(value)))
        except ValueError:
            return str(value)


class Base(DeclarativeBase):
    pass


# ── Orchestrator Registry ──────────────────────────────────────────────────────


class OrchestratorNode(Base):
    """
    A registered Orchestrator instance. Each application environment has one
    Orchestrator; this table tracks all known Orchestrators in the org.

    The Conductor uses this registry to:
      - Know how many Orchestrators should be reporting
      - Detect when an Orchestrator goes silent
      - Calculate data completeness percentage
    """
    __tablename__ = "orchestrator_nodes"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)

    # Human-readable name (e.g., "payments-api-prod")
    name: Mapped[str] = mapped_column(String(256), nullable=False)

    # Unique identifier — typically the Orchestrator's instance_id
    instance_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)

    # URL where this Orchestrator can be reached (for pull-based queries if needed)
    endpoint_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    # Region for future federation
    region_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    # Orchestrator metadata
    version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    environment: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # App count and agent count reported by this Orchestrator
    app_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    agent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Health tracking
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_push_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consecutive_missed_pushes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Registration
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Secret hash for authenticating pushes from this Orchestrator
    push_secret_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        Index("ix_orch_node_instance_id", "instance_id"),
        Index("ix_orch_node_region", "region_id"),
    )


# ── Aggregated Data (pushed from Orchestrators) ───────────────────────────────


class ConductorAggregate(Base):
    """
    Pre-aggregated usage data pushed from Orchestrators.

    This is the Conductor's materialized view of all org-wide spend.
    Each row represents a (orchestrator, app, team, provider, model, period, granularity)
    aggregation — the same shape as UsageAggregate in the Orchestrator, but
    with the orchestrator_node_id added for provenance.

    The Conductor's dashboard endpoints query this table, never the Orchestrator's DB.
    """
    __tablename__ = "conductor_aggregates"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)

    # Provenance — which Orchestrator sent this data
    orchestrator_node_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("orchestrator_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Identifiers (same as UsageAggregate in Orchestrator)
    app_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    app_name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    team_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    team_slug: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    team_name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False, default="llm_call")
    environment: Mapped[str] = mapped_column(String(32), nullable=False, default="production")

    # Granularity: "hourly" or "daily"
    granularity: Mapped[str] = mapped_column(String(16), nullable=False)

    # Time window
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Metrics — all money as NUMERIC(18,8)
    call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    input_cost: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False, default=0)
    output_cost: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False, default=0)
    total_cost: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False, default=0)

    # Duration stats
    avg_duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    p95_duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Region for federation
    region_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    __table_args__ = (
        # Upsert key: one aggregate per (orchestrator, app, team, provider, model, period, granularity)
        UniqueConstraint(
            "orchestrator_node_id", "app_id", "team_id", "provider", "model",
            "period_start", "granularity",
            name="uq_conductor_agg",
        ),
        Index("ix_cond_agg_team_period", "team_id", "period_start", "granularity"),
        Index("ix_cond_agg_app_period", "app_id", "period_start", "granularity"),
        Index("ix_cond_agg_provider", "provider", "period_start"),
        Index("ix_cond_agg_orch_period", "orchestrator_node_id", "period_start"),
    )


# ── Push Receipts (exactly-once delivery) ──────────────────────────────────────


class PushReceipt(Base):
    """
    Records each data push from an Orchestrator.

    Used for:
      - Exactly-once delivery (batch_id deduplication)
      - Push frequency monitoring (detect silent Orchestrators)
      - Data lineage (which push delivered which aggregates)
    """
    __tablename__ = "push_receipts"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)

    orchestrator_node_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("orchestrator_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Unique batch identifier from the Orchestrator
    batch_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # What was delivered
    aggregate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cost_in_batch: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False, default=0)

    # Timing
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Ack status
    acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("orchestrator_node_id", "batch_id", name="uq_push_receipt"),
        Index("ix_push_receipt_received", "received_at"),
    )


# ── Reconciliation State ──────────────────────────────────────────────────────


class ReconciliationSnapshot(Base):
    """
    Periodic snapshot of data completeness and reconciliation state.

    The Conductor runs a reconciliation loop that:
      - Counts active Orchestrators vs reporting Orchestrators
      - Calculates completeness percentage
      - Flags discrepancies (e.g., Orchestrator A reports $500 but Conductor has $480)
      - Stores the result for audit and dashboard display
    """
    __tablename__ = "reconciliation_snapshots"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)

    # How many Orchestrators are registered and active
    total_orchestrators: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reporting_orchestrators: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completeness_pct: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=0)

    # Org-wide totals at this snapshot
    total_cost_mtd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False, default=0)
    total_calls_mtd: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    # Discrepancies found (JSON list of {orchestrator_id, expected, actual, delta})
    discrepancies: Mapped[Optional[str]] = mapped_column(JSON, nullable=True)

    # Status
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="complete"
    )  # complete | partial | degraded

    snapshot_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_recon_snapshot_at", "snapshot_at"),
    )


# ── Team & App Metadata Cache ─────────────────────────────────────────────────


class TeamCache(Base):
    """
    Cached team metadata from Orchestrators.

    Orchestrators push team metadata alongside aggregates so the Conductor
    can serve team breakdowns without querying individual Orchestrators.
    """
    __tablename__ = "team_cache"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    department: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    max_budget_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 8), nullable=True)
    budget_monthly_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 8), nullable=True)
    budget_quarterly_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 8), nullable=True)
    cost_center_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    cost_center_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)

    # Which Orchestrator last updated this team
    source_orchestrator_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_team_cache_slug", "slug"),
    )


class AppCache(Base):
    """
    Cached app metadata from Orchestrators.
    """
    __tablename__ = "app_cache"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    app_id: Mapped[str] = mapped_column(String(256), nullable=False)
    app_name: Mapped[str] = mapped_column(String(256), nullable=False)
    team_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False, default="production")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    agent_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    instrumented_providers: Mapped[Optional[str]] = mapped_column(JSON, nullable=True)

    # Provenance
    source_orchestrator_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_app_cache_team", "team_id"),
        Index("ix_app_cache_app_id", "app_id"),
    )


# ── Alert Cache ────────────────────────────────────────────────────────────────


class AlertCache(Base):
    """
    Cached alerts pushed from Orchestrators.
    """
    __tablename__ = "alert_cache"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    threshold_value: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    actual_value: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    app_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), nullable=True)
    team_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    fired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    source_orchestrator_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), nullable=True
    )

    __table_args__ = (
        Index("ix_alert_cache_fired", "fired_at"),
        Index("ix_alert_cache_team", "team_id"),
    )


# ── Policy Decision Cache ─────────────────────────────────────────────────────


class PolicyDecisionCache(Base):
    """
    Cached policy decisions for executive charts (savings, enforcement mix).
    """
    __tablename__ = "policy_decision_cache"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)  # allow, deny, throttle
    estimated_cost: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False, default=0)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    team_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), nullable=True)
    app_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), nullable=True)

    source_orchestrator_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), nullable=True
    )

    __table_args__ = (
        Index("ix_policy_cache_decided", "decided_at"),
        Index("ix_policy_cache_decision", "decision", "decided_at"),
    )
