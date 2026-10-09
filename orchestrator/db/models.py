"""
Modus — Database Models
================================
SQLAlchemy 2.x async ORM models.

Design principles:
  - Every table has UUID primary key (not serial int) — safe for horizontal scale
    and external system references without collision risk.
  - team_id is on every tenant-scoped table — enforced at the query layer via
    the auth middleware context. This is the Option A → B bridge: the column
    exists, the queries are scoped, the middleware currently returns a platform-
    admin identity that sees all teams. Switching to real auth is a middleware
    swap, zero schema changes.
  - Timestamps are UTC, stored as TIMESTAMP WITH TIME ZONE.
  - Soft deletes on teams and apps (deleted_at) — hard deletes lose audit trails.
  - audit_log is append-only. No application code may UPDATE or DELETE from it.
  - All money values stored as NUMERIC(18,8) — never float. Float rounding errors
    in financial data are unacceptable.
  - indexes are designed for the dashboard's actual query patterns:
      time-range scans on records, per-app aggregation, per-team rollups.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
    true,
    JSON,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC ``datetime`` column.

    PostgreSQL stores ``TIMESTAMP WITH TIME ZONE`` and already returns aware
    values. SQLite has no timestamp type: SQLAlchemy stores naive text and hands
    back *naive* datetimes, which then serialise to JSON without an offset
    (``2026-04-01T10:00:00``). Browsers parse such strings as LOCAL time, so
    every relative time in the dashboard was off by the viewer's UTC offset.

    This type makes both dialects behave the same way: values read from the
    database are always aware UTC, and aware values written are normalised to
    UTC first (a naive value is taken to already be UTC). API responses built
    from ORM rows therefore serialise as ``...Z`` / ``+00:00`` everywhere.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or not isinstance(value, datetime):
            return value
        if value.tzinfo is None:
            # Naive input is taken to be UTC already.
            return value.replace(tzinfo=timezone.utc) if dialect.name == "postgresql" else value
        value = value.astimezone(timezone.utc)
        return value if dialect.name == "postgresql" else value.replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


# ── Teams ──────────────────────────────────────────────────────────────────────

class Team(Base):
    """
    Organisational unit. Every app belongs to a team.
    Teams are the primary namespace for cost isolation.

    In Option A (current): all teams are visible to all authenticated sessions.
    In Option B (future): users belong to teams, middleware enforces visibility.

    registration_token — a mds_team_ prefixed token that developers use to
    self-register their apps without needing the master key. The admin creates
    a team and shares the registration_token with their developers.
    Developers set MODUS_TEAM_TOKEN=<token> and the agent handles
    everything else automatically.
    """
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    department: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True,
        comment="Department or division this team belongs to (e.g., 'Engineering', 'Marketing').",
    )

    # Developer self-registration token — scoped to this team only.
    # mds_team_ prefix distinguishes it from per-app mds_ keys.
    # Nullable: teams created before this feature have no token until generated.
    registration_token_hash: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    registration_token_prefix: Mapped[Optional[str]] = mapped_column(
        String(24), nullable=True
    )

    # ── Hierarchical budgets (Phase 2) ────────────────────────────────────────
    # Team-level budget caps. Cascading: team cap → app caps → session caps.
    max_budget_usd: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 8), nullable=True,
        comment="Max daily budget for the entire team. None = unlimited.",
    )
    budget_duration: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True,
        comment="Budget period: 'daily', 'monthly', '30d', '1h'. Default: daily.",
    )
    current_spend_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), server_default="0", nullable=False,
        comment="Rolling spend counter for the current budget_duration window.",
    )

    # ── Finance budgets (Phase 4) ────────────────────────────────────────────
    # Fixed calendar-period budgets for Finance Intelligence burn-rate tracking.
    budget_monthly_usd: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 8), nullable=True,
        comment="Monthly budget allocation for burn-rate tracking.",
    )
    budget_quarterly_usd: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 8), nullable=True,
        comment="Quarterly budget allocation for EOQ projections.",
    )
    cost_center_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("cost_centers.id", ondelete="SET NULL"),
        nullable=True,
        comment="Direct cost-center assignment (alternative to team_cost_centers mapping).",
    )

    # Soft delete — preserves audit trail
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # ── Hierarchy ───────────────────────────────────────────────────────────
    parent_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id", ondelete="SET NULL"),
        nullable=True,
        comment="Parent team ID for hierarchical team structure. NULL = root.",
    )

    # Relationships
    apps: Mapped[list[App]] = relationship(back_populates="team")
    parent: Mapped[Optional["Team"]] = relationship(
        remote_side="Team.id",
        foreign_keys="Team.parent_id",
        back_populates="children",
    )
    children: Mapped[list["Team"]] = relationship(
        back_populates="parent",
        foreign_keys="Team.parent_id",
    )
    memberships: Mapped[list["TeamMembership"]] = relationship(
        back_populates="team",
    )

    def __repr__(self) -> str:
        return f"<Team {self.slug!r}>"


# ── Apps ───────────────────────────────────────────────────────────────────────

class App(Base):
    """
    A registered application. Each app gets one API key (mds_...).
    Apps are the primary unit of cost attribution.

    api_key_hash — bcrypt hash of the mds_ key. The plaintext key is shown
    once at registration and never stored. Verification is hash comparison.
    """
    __tablename__ = "apps"
    __table_args__ = (
        UniqueConstraint("team_id", "app_id", name="uq_apps_team_app_id"),
        Index("ix_apps_team_id", "team_id"),
        Index("ix_apps_api_key_hash", "api_key_hash"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id"), nullable=False
    )
    app_id: Mapped[str] = mapped_column(String(128), nullable=False)
    app_name: Mapped[str] = mapped_column(String(256), nullable=False)
    environment: Mapped[str] = mapped_column(
        String(32), nullable=False, default="production"
    )  # production | staging | dev

    api_key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    api_key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    # e.g. "mds_abc123" — shown in UI so users can identify which key

    # Agent metadata — populated on first heartbeat
    agent_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    sdk_versions: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )
    # e.g. {"openai": "1.12.0", "anthropic": "0.20.0", "boto3": "1.34.0"}

    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    first_seen_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )

    # ── Enforcement state ──────────────────────────────────────────────────────
    # Controls whether the policy engine allows calls from this app.
    #
    #   active            — normal operation, policies evaluated per-call
    #   budget_suspended  — hard budget cap breached; all calls denied until
    #                       the period resets or an admin overrides
    #   rate_limited      — per-minute/hour rate cap hit; deny with retry_after
    #   admin_suspended   — manually suspended by platform/team admin
    #
    # is_active=False remains the hard kill switch (deregistered app).
    # enforcement_state operates within is_active=True apps.
    # ── Auto-pause callback (Phase 4e) ────────────────────────────────────────
    # Optional URL on the app's control plane. On critical threshold breach,
    # orchestrator POSTs to this endpoint to pause AI calls in the app itself.
    # Set via app registration or PATCH /api/v1/apps/{id}. Opt-in per app.
    pause_endpoint_url: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True
    )

    enforcement_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active"
    )
    enforcement_suspended_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    enforcement_suspended_reason: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # ── Hierarchical budget nesting (Phase 2) ────────────────────────────────
    parent_hierarchy: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True,
        comment="Hierarchy path e.g. 'team:proj:user123' for nested budgets.",
    )

    # Relationships
    team: Mapped[Team] = relationship(back_populates="apps")
    records: Mapped[list[UsageRecord]] = relationship(back_populates="app")
    aggregates: Mapped[list[UsageAggregate]] = relationship(back_populates="app")
    thresholds: Mapped[list[Threshold]] = relationship(back_populates="app")
    alerts: Mapped[list[Alert]] = relationship(back_populates="app")
    heartbeats: Mapped[list[AgentHeartbeat]] = relationship(back_populates="app")
    pricing_overrides: Mapped[list[PricingOverride]] = relationship(
        back_populates="app"
    )
    policy_decisions: Mapped[list["PolicyDecision"]] = relationship(
        back_populates="app"
    )

    def get_active_policies(self):
        """Helper for plugin system — returns active policies for this app's team."""
        if self.team:
            return [
                p for p in getattr(self.team, 'policies', []) if p.is_active
            ]
        return []

    def __repr__(self) -> str:
        return f"<App {self.app_id!r} team={self.team_id!r}>"


# ── Usage Records ──────────────────────────────────────────────────────────────

class UsageRecord(Base):
    """
    Raw ingest record from an agent flush.

    One record per API call or infrastructure event. High write volume.
    Partitioned by month in production (see migration notes).

    provider: anthropic | openai | bedrock | azure | gcp | databricks | mlflow | custom
    resource_type: llm_call | embedding | image | container | function | storage | custom
    """
    __tablename__ = "usage_records"
    __table_args__ = (
        Index("ix_records_app_id_ts", "app_id", "timestamp"),
        Index("ix_records_team_id_ts", "team_id", "timestamp"),
        Index("ix_records_provider_ts", "provider", "timestamp"),
        Index("ix_records_timestamp", "timestamp"),
        # Composite for dashboard's most common query: team + time range
        Index("ix_records_team_time", "team_id", "timestamp", "provider"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    app_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id"), nullable=False
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id"), nullable=False
    )
    # Denormalised for query performance — avoids joins on hot read path

    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    operation: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # e.g. "messages.create", "chat.completions", "invoke_model"

    # Token usage — nullable because not all resource types have tokens
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Cost — NUMERIC not float
    input_cost: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 8), nullable=True
    )
    output_cost: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 8), nullable=True
    )
    total_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=0
    )

    # Timing
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False
    )
    # Timestamp of the actual API call, not when it was ingested

    ingested_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )

    # Extended metadata — provider-specific fields, custom tags
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )
    # e.g. {"region": "us-east-1", "function_name": "my-lambda", "tags": {...}}

    # For deduplication — agent sends a batch_id, we reject duplicate batch_ids
    batch_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # ── Agent session tracing (Phase 2) ───────────────────────────────────────
    # Links usage records to multi-step agent sessions (LangGraph, CrewAI, ReAct)
    session_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True,
        comment="Agent session ID for multi-step tracing.",
    )

    # Relationships
    app: Mapped[App] = relationship(back_populates="records")

    def __repr__(self) -> str:
        return f"<UsageRecord {self.provider}/{self.model} ${self.total_cost}>"


# ── Usage Aggregates ───────────────────────────────────────────────────────────

class UsageAggregate(Base):
    """
    Pre-computed hourly and daily rollups.

    The dashboard reads from this table for charts and summaries,
    not from usage_records directly. A background task (or DB trigger)
    populates this from usage_records.

    This keeps dashboard queries fast regardless of record volume.
    Granularity: hourly for the last 7 days, daily for everything older.
    """
    __tablename__ = "usage_aggregates"
    __table_args__ = (
        UniqueConstraint(
            "app_id", "team_id", "provider", "model", "period_start", "granularity",
            name="uq_aggregates_key",
        ),
        Index("ix_agg_team_period", "team_id", "period_start", "granularity"),
        Index("ix_agg_app_period", "app_id", "period_start", "granularity"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    app_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id"), nullable=False
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id"), nullable=False
    )

    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)

    granularity: Mapped[str] = mapped_column(String(8), nullable=False)
    # "hourly" | "daily" | "monthly"

    period_start: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False
    )
    period_end: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False
    )

    call_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    input_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=0
    )
    output_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=0
    )
    total_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=0
    )
    avg_duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    min_duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    duration_ms_sum: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    p95_duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Source: "compaction" (rolled up from raw records) or "sdk" (direct from SDK aggregation)
    source: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    computed_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )

    app: Mapped[App] = relationship(back_populates="aggregates")


# ── Thresholds ─────────────────────────────────────────────────────────────────

class Threshold(Base):
    """
    Cost or token alert thresholds. Evaluated by the orchestrator
    on each aggregate computation cycle.

    scope: "app" — threshold applies to one app
           "team" — threshold applies to total team spend
           "provider" — threshold applies to one provider across the app/team
           "user" — threshold applies to a specific user
           "cost_center" — threshold applies to a cost center
    """
    __tablename__ = "thresholds"
    __table_args__ = (
        Index("ix_thresholds_app_id", "app_id"),
        Index("ix_thresholds_team_id", "team_id"),
        Index("ix_thresholds_user_id", "user_id"),
        Index("ix_thresholds_cost_center_id", "cost_center_id"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    app_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id"), nullable=True
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id"), nullable=False
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        comment="Per-user threshold scoping.",
    )
    cost_center_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("cost_centers.id", ondelete="SET NULL"), nullable=True,
        comment="Per-cost-center threshold scoping.",
    )

    name: Mapped[str] = mapped_column(String(256), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="app")
    provider: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    # "total_cost" | "input_tokens" | "output_tokens" | "call_count"

    period: Mapped[str] = mapped_column(String(16), nullable=False)
    # "hourly" | "daily" | "weekly" | "monthly"

    warning_value: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 8), nullable=True
    )
    critical_value: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ── Degradation (auto-downgrade model on breach) ──────────────────────────
    degradation_model: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True,
        comment="Model to degrade to when threshold breached (e.g., 'gpt-4o-mini').",
    )
    degradation_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="Whether to auto-degrade instead of deny on breach.",
    )

    # Notification channels — JSONB for flexibility
    notify: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )
    # e.g. {"slack": "#cost-alerts", "email": ["team@org.com"], "webhook": "https://..."}

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    app: Mapped[Optional[App]] = relationship(back_populates="thresholds")
    alerts: Mapped[list[Alert]] = relationship(back_populates="threshold")


# ── Alerts ─────────────────────────────────────────────────────────────────────

class Alert(Base):
    """
    Fired alert record. One row per threshold breach event.
    Immutable after creation — alerts are never updated, only acknowledged.
    """
    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alerts_app_id_fired", "app_id", "fired_at"),
        Index("ix_alerts_team_id_fired", "team_id", "fired_at"),
        Index("ix_alerts_acknowledged", "acknowledged_at"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    threshold_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("thresholds.id"), nullable=False
    )
    app_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id"), nullable=True
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id"), nullable=False
    )

    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    # "warning" | "critical"

    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    threshold_value: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    actual_value: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    period_start: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False
    )
    period_end: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False
    )

    fired_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    acknowledged_by: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)

    notification_sent: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    notification_result: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )

    app: Mapped[Optional[App]] = relationship(back_populates="alerts")
    threshold: Mapped[Threshold] = relationship(back_populates="alerts")


# ── Notification Delivery (durability + dead-letter) ────────────────────────────

class NotificationDelivery(Base):
    """
    Durable record of a single alert → channel delivery attempt.

    Every notification dispatch writes one row per channel with its terminal
    status. Rows with ``status='dead_letter'`` ARE the dead-letter queue: when a
    notification's bounded retries are exhausted it is persisted here rather than
    silently dropped into a log line, so an operator can audit — and later replay —
    undelivered alerts. This is the durability backbone behind "prove the critical
    budget alert actually went out." The ``payload`` column carries the original
    alert_data so a dead-lettered notification can be re-dispatched.

    Telemetry only — never contains PII (Modus never touches PII).
    """
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        Index("ix_notif_deliveries_alert", "alert_id"),
        Index("ix_notif_deliveries_status", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    alert_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("alerts.id", ondelete="CASCADE"), nullable=True
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    # slack | teams | email | pagerduty | webhook
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # "delivered" | "dead_letter"
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payload: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(), onupdate=func.now(), nullable=False,
    )


# ── Agent Heartbeats ───────────────────────────────────────────────────────────

class AgentHeartbeat(Base):
    """
    Periodic liveness signal from each agent instance.
    Used to detect stale/offline agents in the dashboard.
    Retained for 30 days, pruned by a scheduled task.
    """
    __tablename__ = "agent_heartbeats"
    __table_args__ = (
        Index("ix_heartbeats_app_id_ts", "app_id", "received_at"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    app_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id"), nullable=False
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id"), nullable=False
    )

    agent_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    instrumented_providers: Mapped[Optional[list]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )
    # e.g. ["anthropic", "openai", "bedrock"]

    host_info: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )
    # e.g. {"runtime": "lambda", "region": "us-east-1", "python": "3.11.4"}

    received_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )

    app: Mapped[App] = relationship(back_populates="heartbeats")


# ── Pricing Overrides ──────────────────────────────────────────────────────────

class PricingOverride(Base):
    """
    Per-app or per-team pricing overrides.
    Takes precedence over the global pricing table.

    Use case: enterprise discount agreements, reserved capacity pricing,
    internal chargeback rates that differ from list price.

    Resolution order (highest wins):
        1. app_id + team_id set  → app-level override
        2. team_id set, app_id null → team-level override
        3. both null             → platform-wide override (platform admin only)

    is_active: soft-delete. Deactivated overrides are retained for audit.
    """
    __tablename__ = "pricing_overrides"
    __table_args__ = (
        UniqueConstraint(
            "app_id", "team_id", "provider", "model", "resource_type",
            name="uq_pricing_override",
        ),
        Index("ix_pricing_app_provider", "app_id", "provider"),
        Index("ix_pricing_team_provider", "team_id", "provider"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    app_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id"), nullable=True
    )
    # null app_id = team-wide or platform-wide override

    team_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id"), nullable=True
    )
    # null team_id = platform-wide override (platform admin only)

    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(256), nullable=False)
    resource_type: Mapped[str] = mapped_column(
        String(64), nullable=False, default="llm_call"
    )

    # At least one of these must be non-null — enforced at the API layer
    input_cost_per_1k: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 8), nullable=True
    )
    output_cost_per_1k: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 8), nullable=True
    )
    per_unit_cost: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 8), nullable=True,
        comment="For non-token resources: queries, API calls, etc."
    )
    unit_label: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
        comment="Human label for the unit, e.g. 'query', 'request'."
    )

    override_reason: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True,
        comment="Why this override exists. Shown in audit log."
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    app: Mapped[Optional[App]] = relationship(back_populates="pricing_overrides")


# ── Global Pricing Table ───────────────────────────────────────────────────────

class PricingModel(Base):
    """
    Global pricing table. Populated from the pricing.py sync module.
    All costs in USD per 1,000 tokens (or per call for flat-rate resources).

    This replaces the in-memory dict from the v1 pricing module.
    Updated by a scheduled task that compares against known provider
    pricing endpoints or a manually maintained YAML.
    """
    __tablename__ = "pricing_models"
    __table_args__ = (
        UniqueConstraint("provider", "model", "effective_from", name="uq_pricing_model"),
        Index("ix_pricing_model_lookup", "provider", "model"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # "llm_call" | "embedding" | "image" | "container" | "function" | "storage"

    input_cost_per_1k: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 8), nullable=True
    )
    output_cost_per_1k: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 8), nullable=True
    )
    flat_cost_per_call: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 8), nullable=True
    )

    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")

    effective_from: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False
    )
    effective_until: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )

    source: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    # e.g. "anthropic_pricing_page_2024_01" — for audit trail

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )


# ── Audit Log ─────────────────────────────────────────────────────────────────

class AuditLog(Base):
    """
    Immutable audit trail. Append-only.

    Records every mutating operation against the orchestrator API:
    app registration, key rotation, threshold changes, alert acknowledgement,
    pricing override changes.

    actor_id: identity from the auth middleware (stub returns "platform-admin"
    in Option A; real user ID in Option B).

    No foreign keys intentionally — audit records must survive app/team deletion.
    """
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_actor", "actor_id", "occurred_at"),
        Index("ix_audit_resource", "resource_type", "resource_id", "occurred_at"),
        Index("ix_audit_team", "team_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )

    actor_id: Mapped[str] = mapped_column(String(256), nullable=False)
    actor_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    team_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    # stored as string, not FK — survives team deletion

    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # "team" | "app" | "threshold" | "alert" | "pricing_override" | "api_key"

    resource_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    action: Mapped[str] = mapped_column(String(64), nullable=False)
    # "created" | "updated" | "deleted" | "key_rotated" | "acknowledged" | "registered"

    before: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )
    after: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )

    occurred_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )

    # ── Tamper-evidence: global hash chain ────────────────────────────────────
    # Assigned automatically at flush time by orchestrator.core.audit_chain.
    # chain_seq is a gap-free monotonic sequence; entry_hash commits the row's
    # content plus prev_hash, so any edit/deletion breaks the chain from that
    # point forward. Nullable so pre-chain rows (backfill) don't block writes.
    chain_seq: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    prev_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    entry_hash: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )

    def __repr__(self) -> str:
        return (
            f"<AuditLog {self.action} {self.resource_type}/{self.resource_id}"
            f" by {self.actor_id}>"
        )


class AuditCheckpoint(Base):
    """
    Signed checkpoint over the audit hash chain.

    Binds a chain position (chain_seq + entry_hash) with an Ed25519 signature
    from the deployment's checkpoint key. Because the signature is asymmetric,
    a third party (e.g. a bank examiner) can verify a checkpoint — and thus the
    integrity of the whole chain up to that point — with the public key alone,
    without the ability to forge one. Anchoring the head periodically means an
    operator cannot silently rewrite history *between* checkpoints either.
    """
    __tablename__ = "audit_checkpoints"
    __table_args__ = (
        Index("ix_audit_checkpoint_seq", "chain_seq"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    chain_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    signature: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    public_key_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )


class HashChainState(Base):
    """
    Serialization point + checkpoint for a named hash chain (e.g. "audit").

    Holds the last assigned sequence number and last entry hash. The audit
    chain listener locks this row (SELECT ... FOR UPDATE on Postgres; SQLite
    serializes writers natively) while appending, so concurrent transactions
    cannot fork the chain by grabbing the same prev_hash.
    """
    __tablename__ = "hash_chain_state"

    chain_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )


# ── Ingest Deduplication ───────────────────────────────────────────────────────

class IngestBatch(Base):
    """
    Tracks received batch IDs to prevent duplicate ingest on agent retry.
    Agent retries on network failure — without this, a retry after a partial
    write could double-count records.

    Pruned after 48 hours — that window covers any realistic retry scenario.
    """
    __tablename__ = "ingest_batches"
    __table_args__ = (
        Index("ix_ingest_batch_received", "received_at"),
    )

    batch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    app_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id"), nullable=False
    )
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    received_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )

# ── Governance Policies ────────────────────────────────────────────────────────

class GovernancePolicy(Base):

    """
    Runtime enforcement policy. Evaluated synchronously before every AI call
    via POST /api/v1/policy/evaluate.

    Scope hierarchy (broader → narrower):
        platform  — applies to all teams and apps (platform_admin only)
        team      — applies to all apps in a team (team_admin+)
        app       — applies to one specific app

    On evaluate, policies are loaded for the calling app ordered by:
        1. scope specificity (app > team > platform)
        2. priority ASC within the same scope (lower number = evaluated first)
    Every applicable policy is evaluated; the most restrictive outcome wins
    (deny > throttle > degradation-ladder downshift > allow).

    policy_type:
        budget_cap         — deny when cumulative spend exceeds config.cap_usd
        rate_limit         — deny/throttle when call_count exceeds config.max_calls
        model_allowlist    — deny if requested model is NOT in config.models[]
        model_denylist     — deny if requested model IS in config.models[]
        provider_block     — deny if provider matches config.providers[]
        environment_block  — deny if environment matches config.environments[]
        token_cap          — deny if tokens exceeds config.max_tokens
        latency_cap        — deny/warn if latency exceeds config.max_ms (rolling average)
        degradation_ladder — progressive model downshift as budget % is consumed

    effect:
        deny     — return 403 to the evaluate call; agent raises PolicyViolation
        throttle — return 429 with retry_after; agent sleeps and retries once
        warn     — return 200 allow but fire an alert; does not block the call

    config JSONB (policy_type specific):
        budget_cap:    {"cap_usd": "100.00", "period": "daily"}
        rate_limit:    {"max_calls": 1000, "window_seconds": 3600}
        latency_cap:   {"max_ms": 2000, "lookback_minutes": 10}
        model_*list:   {"models": ["gpt-4o", "claude-opus-4-6"]}
        provider_block: {"providers": ["openai"]}
        degradation_ladder: {"budget_usd": "500.00", "period": "monthly",
            "tiers": [{"pct": 70, "model": "claude-sonnet-4-5-20251022"},
                      {"pct": 90, "model": "claude-haiku-4-5-20251001"},
                      {"pct": 100, "action": "deny"}]}

    action JSONB (optional, returned to agent on deny/throttle):
        {
          "message": "Daily budget exceeded. Contact platform team.",
          "suggested_model": "claude-3-haiku-20240307",
          "retry_after_seconds": 3600
        }
    """
    __tablename__ = "governance_policies"
    __table_args__ = (
        Index("ix_policies_team_id", "team_id"),
        Index("ix_policies_app_id", "app_id"),
        Index("ix_policies_scope_priority", "scope", "priority"),
        Index("ix_policies_active", "is_active"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )

    # Scope anchors — both null = platform scope
    team_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id"), nullable=True
    )
    app_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id"), nullable=True
    )

    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    # "platform" | "team" | "app"

    policy_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # "budget_cap" | "rate_limit" | "model_allowlist" | "model_denylist"
    # "provider_block" | "environment_block" | "token_cap" | "degradation_ladder"

    effect: Mapped[str] = mapped_column(String(16), nullable=False, default="deny")
    # "deny" | "throttle" | "warn"

    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    # Lower = evaluated first within same scope. Range 1–999.

    # Suggested model to switch to if this policy blocks the call
    suggested_model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    conditions: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )
    config: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False
    )
    action: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_by: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    decisions: Mapped[list["PolicyDecision"]] = relationship(
        back_populates="policy", foreign_keys="PolicyDecision.policy_id"
    )

    def __repr__(self) -> str:
        return f"<GovernancePolicy {self.name!r} type={self.policy_type} effect={self.effect}>"


# ── Policy Decisions ───────────────────────────────────────────────────────────

class PolicyDecision(Base):
    """
    Immutable record of every policy evaluation that produced deny or throttle.
    'allow' decisions without a policy match are not recorded (too high volume).
    'warn'-effect policy matches are recorded regardless of decision.

    This is the compliance audit trail for enforcement actions.
    Every denied AI call has a row here explaining exactly why.

    Retained indefinitely — never pruned. Equivalent in importance to audit_log.
    """
    __tablename__ = "policy_decisions"
    __table_args__ = (
        Index("ix_pd_app_id_decided", "app_id", "decided_at"),
        Index("ix_pd_team_id_decided", "team_id", "decided_at"),
        Index("ix_pd_policy_id", "policy_id"),
        Index("ix_pd_decision", "decision", "decided_at"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )

    # policy_id null = no policy matched but app is suspended (enforcement_state)
    policy_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("governance_policies.id"), nullable=True
    )
    app_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id"), nullable=False
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id"), nullable=False
    )

    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    # "allow" | "deny" | "throttle"

    reason: Mapped[str] = mapped_column(String(512), nullable=False)
    # Human-readable explanation returned to the agent

    # What the agent sent
    request_provider: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    request_model: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    request_environment: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    request_estimated_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    request_estimated_cost: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 8), nullable=True
    )

    # Spend context at time of evaluation (for budget_cap decisions)
    spend_at_decision: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 8), nullable=True
    )
    spend_limit: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 8), nullable=True
    )

    evaluation_latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    decided_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )

    policy: Mapped[Optional[GovernancePolicy]] = relationship(
        back_populates="decisions", foreign_keys=[policy_id]
    )
    app: Mapped[App] = relationship(back_populates="policy_decisions")

    def __repr__(self) -> str:
        return (
            f"<PolicyDecision {self.decision} app={self.app_id} "
            f"policy={self.policy_id} reason={self.reason!r}>"
        )


# ── Real-Time Spend Counters ───────────────────────────────────────────────────

class RealTimeSpend(Base):
    """
    Per-app, per-period running spend totals updated synchronously on every
    ingest batch. Used by the policy engine to evaluate budget_cap policies
    without waiting for the 60-second aggregation cycle.

    window_key is a human-readable composite: "daily:2026-03-02"
    This makes it easy to query and expire without timestamp math in the engine.

    Updated via PostgreSQL upsert with atomic increments on every ingest call.
    The engine reads this table on every evaluate call — keep it small and indexed.

    Retention: rows are pruned 7 days after window_end by the maintenance task.
    We keep 7 days so budget_cap policies with weekly periods work correctly.
    """
    __tablename__ = "real_time_spend"
    __table_args__ = (
        UniqueConstraint("app_id", "period", "window_key", name="uq_rts_app_window"),
        Index("ix_rts_app_id", "app_id"),
        Index("ix_rts_team_id", "team_id"),
        Index("ix_rts_window_end", "window_end"),
        Index("ix_rts_lookup", "app_id", "period", "window_key"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    app_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id"), nullable=False
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id"), nullable=False
    )

    period: Mapped[str] = mapped_column(String(16), nullable=False)
    # "hourly" | "daily" | "monthly"

    window_key: Mapped[str] = mapped_column(String(32), nullable=False)
    # "hourly:2026-03-02T14" | "daily:2026-03-02" | "monthly:2026-03"

    window_start: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False
    )
    window_end: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False
    )

    total_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=0
    )
    call_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    # ── Hierarchy + session support (Phase 2) ─────────────────────────────────
    session_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
        comment="Agent session ID for session-level spend tracking.",
    )
    hierarchy_level: Mapped[str] = mapped_column(
        String(32), server_default="app", nullable=False,
        comment="Hierarchy level: app | team | session.",
    )
    hierarchy_key: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True,
        comment="Identifier for this hierarchy level (session_id or team_id).",
    )

    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<RealTimeSpend app={self.app_id} {self.period}:{self.window_key} "
            f"cost=${self.total_cost}>"
        )


# ── Session Budgets (Phase 2 — Agentic) ──────────────────────────────────────

class SessionBudget(Base):
    """
    Per-session budget caps for multi-step agent sessions.
    Created by POST /api/v1/evaluate/sessions.

    Agent creates a session budget at start, then each /evaluate call checks
    cumulative spend against this cap. Hard deny when exceeded.

    Critical for LangGraph/CrewAI/ReAct agents that run many steps in one
    logical session — prevents runaway loops from burning through budgets.
    """
    __tablename__ = "session_budgets"
    __table_args__ = (
        UniqueConstraint("session_id", "app_id", name="uq_session_budget"),
        Index("ix_session_budget_app", "app_id"),
        Index("ix_session_budget_team", "team_id"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    app_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id"), nullable=False
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id"), nullable=False
    )
    max_budget_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False,
        comment="Hard cap for this session.",
    )
    current_spend_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), server_default="0", nullable=False,
        comment="Running spend total, updated atomically on each /evaluate allow.",
    )
    reset_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
        comment="Optional: auto-reset spend at this time.",
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<SessionBudget session={self.session_id} "
            f"${self.current_spend_usd}/${self.max_budget_usd}>"
        )


# ── App Topology ───────────────────────────────────────────────────────────────

class AppTopology(Base):
    """
    Latest discovered environment topology for each app.
    One row per app — overwritten on each topology report from the agent.

    Populated by POST /api/v1/topology from the agent's EnvironmentScanner.
    The agent runs a full scan on startup and sends deltas on each heartbeat.

    ai_summary — Claude-generated natural language description of what this
    service does, which AI providers it uses, and its architectural role.
    Generated once on first report and regenerated when the raw snapshot
    changes significantly (detected by hash comparison).

    raw_snapshot — the full structured snapshot from the agent, unmodified.
    Stored for debugging and future analysis without rerunning the agent.
    """
    __tablename__ = "app_topology"
    __table_args__ = (
        Index("ix_topology_app_id", "app_id"),
        Index("ix_topology_team_id", "team_id"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    app_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id"), unique=True, nullable=False
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id"), nullable=False
    )

    # Runtime environment
    runtime: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # "python" | "node" | "java" | "go" | "ruby" | "rust"

    python_version: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    platform: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Deployment context
    deployment_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # "kubernetes" | "aws_ecs" | "aws_lambda" | "docker" | "gcp" | "azure" | "bare_metal"

    cloud_provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    detected_environment: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # Auto-detected: "production" | "staging" | "development" | "ci"
    k8s_flavor: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # "eks" | "gke" | "aks" | "openshift" | empty
    container_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    k8s_namespace: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    k8s_deployment: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)

    # Discovered components
    web_framework: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # "fastapi" | "flask" | "django" | "express" | "rails" | etc.

    ai_providers: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )
    # {"anthropic": "0.25.0", "openai": "1.14.0"}

    ai_frameworks: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )
    # {"langchain": "0.1.4", "llama-index": "0.10.0"}

    api_routes: Mapped[Optional[list]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )
    # [{"path": "/api/v1/chat", "methods": ["POST"], "likely_ai": true}, ...]

    service_dependencies: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )
    # {"postgres": "postgresql://...", "redis": "redis://...", "s3_bucket": "my-bucket"}

    infrastructure_packages: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )
    # {"sqlalchemy": "2.0.0", "celery": "5.3.0", "redis": "5.0.0"}

    worker_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    hostname: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)

    # AI-generated summary
    ai_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ai_summary_generated_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )

    # Hash of raw_snapshot — used to detect meaningful changes
    snapshot_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Full raw data from the agent — unmodified
    raw_snapshot: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )

    first_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<AppTopology app={self.app_id} "
            f"framework={self.web_framework} "
            f"deployment={self.deployment_type}>"
        )


# ── SystemSetting ──────────────────────────────────────────────────────────────

class SystemSetting(Base):
    """
    Admin-configurable key/value store for runtime orchestrator behaviour.
    Keys are dot-namespaced: "task.anomaly_scan.interval_seconds".
    Values are stored as TEXT — callers cast to int/float/bool as needed.
    Hot-reloadable: tasks check their interval from this table each cycle.
    """
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_by: Mapped[str] = mapped_column(String(128), nullable=False,
                                             server_default="system")
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )

    def as_int(self, default: int = 0) -> int:
        try:
            return int(self.value)
        except (ValueError, TypeError):
            return default

    def as_float(self, default: float = 0.0) -> float:
        try:
            return float(self.value)
        except (ValueError, TypeError):
            return default

    def as_bool(self, default: bool = True) -> bool:
        return self.value.lower() in ("true", "1", "yes", "on")

    def __repr__(self) -> str:
        return f"<SystemSetting {self.key}={self.value!r}>"


# ── AnomalyEvent ───────────────────────────────────────────────────────────────

class AnomalyEvent(Base):
    """
    A detected statistical anomaly in spend, call volume, or token usage.
    Generated by the anomaly_scan background task (z-score vs 14-day baseline).
    ai_explanation is generated once via Claude API and cached on the row.
    """
    __tablename__ = "anomaly_events"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True,
                                     default=_uuid)
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id"), nullable=False
    )
    app_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id"), nullable=True
    )
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    z_score: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    baseline_value: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    actual_value: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    window_hours: Mapped[int] = mapped_column(Integer, nullable=False,
                                               server_default="1")
    ai_explanation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    acknowledged_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )

    __table_args__ = (
        Index("ix_anomaly_team_detected", "team_id", "detected_at"),
        Index("ix_anomaly_app_detected", "app_id", "detected_at"),
        Index("ix_anomaly_severity", "severity", "detected_at"),
    )

    def __repr__(self) -> str:
        return (f"<AnomalyEvent {self.severity} z={self.z_score} "
                f"metric={self.metric} app={self.app_id}>")


# ── SpendForecast ──────────────────────────────────────────────────────────────

class SpendForecast(Base):
    """
    Linear regression spend forecast per team.
    Computed every N seconds by the forecast background task.
    Always read the latest row per team_id (order by computed_at DESC LIMIT 1).
    """
    __tablename__ = "spend_forecasts"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True,
                                     default=_uuid)
    team_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id"), nullable=True
    )
    period_label: Mapped[str] = mapped_column(String(32), nullable=False)
    mtd_actual: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    forecast_eom: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    forecast_eoq: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 8),
                                                              nullable=True)
    forecast_eoy: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 8),
                                                              nullable=True)
    trend_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), nullable=True)
    r_squared: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4), nullable=True)
    basis_days: Mapped[int] = mapped_column(Integer, nullable=False,
                                             server_default="28")
    computed_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_forecast_team_computed", "team_id", "computed_at"),
    )

    def __repr__(self) -> str:
        return (f"<SpendForecast team={self.team_id} "
                f"eom=${self.forecast_eom} r²={self.r_squared}>")


# ── OptimizationRecommendation ─────────────────────────────────────────────────

class OptimizationRecommendation(Base):
    """
    Per-app model substitution recommendation.
    Generated by the recommendations background task.
    dismissed_at — user dismissed; excluded from active feed.
    applied_at   — admin applied the suggested policy; mark as resolved.
    """
    __tablename__ = "optimization_recommendations"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True,
                                     default=_uuid)
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id"), nullable=False
    )
    app_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id"), nullable=False
    )
    current_model: Mapped[str] = mapped_column(String(128), nullable=False)
    suggested_model: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    call_volume_basis: Mapped[int] = mapped_column(BigInteger, nullable=False)
    estimated_monthly_savings: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False
    )
    confidence: Mapped[str] = mapped_column(String(16), nullable=False,
                                             server_default="medium")
    recommendation_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
    dismissed_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    applied_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )

    __table_args__ = (
        Index("ix_rec_team_app", "team_id", "app_id"),
        Index("ix_rec_savings", "estimated_monthly_savings"),
    )

    def __repr__(self) -> str:
        return (f"<OptimizationRecommendation app={self.app_id} "
                f"{self.current_model}→{self.suggested_model} "
                f"~${self.estimated_monthly_savings}/mo>")


# ── Routing Engine ────────────────────────────────────────────────────────────


class RoutingFingerprint(Base):
    """
    Tracks call-site shapes for intelligent model routing.

    Lifecycle: observe → calibrating → routing → drift_flagged → observe (re-entry)
    Each row represents a unique (app_id, system_prompt_hash, call_site_id) triple.
    """
    __tablename__ = "routing_fingerprints"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid
    )
    app_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id"), nullable=False
    )
    fingerprint_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    system_prompt_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    call_site_id: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True
    )
    task_type: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )
    # 'extraction' | 'summarization' | 'classification' | 'generation' | 'unknown'

    phase: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="observe"
    )
    # 'observe' | 'calibrating' | 'routing' | 'drift_flagged' | 'excluded'

    observe_call_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    observe_threshold: Mapped[int] = mapped_column(
        Integer, nullable=False, default=200, server_default="200"
    )

    routing_confidence: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default="0.0"
    )
    conformal_threshold: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    cheap_model: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    expensive_model: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    cheap_model_agreement_rate: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    calibration_sample_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # Centroid data — serialized numpy arrays for drift detection
    calibration_centroid: Mapped[Optional[bytes]] = mapped_column(
        LargeBinary, nullable=True
    )
    calibration_centroid_variance: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    rolling_centroid: Mapped[Optional[bytes]] = mapped_column(
        LargeBinary, nullable=True
    )
    rolling_centroid_updated_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )

    # JSON centroid columns — multi-dimensional feature vectors
    calibration_centroid_json: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )
    rolling_centroid_json: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )

    drift_score: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default="0.0"
    )
    drift_threshold: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    confidence_decay_factor: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0, server_default="1.0"
    )

    # Developer overrides
    force_model: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    allow_routing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    max_misroute_rate: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.01, server_default="0.01"
    )

    # Runtime counters
    total_routed_calls: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    total_escalations: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    total_validator_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # Token bucket bounds for conformal checks (JSONB list [low, high])
    input_token_bucket_bounds: Mapped[Optional[list]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), onupdate=func.now(),
        nullable=False
    )

    __table_args__ = (
        Index("ix_routing_fp_app", "app_id"),
        Index("ix_routing_fp_phase", "phase"),
        Index("ix_routing_fp_hash", "fingerprint_hash"),
    )

    def __repr__(self) -> str:
        return (f"<RoutingFingerprint {self.fingerprint_hash[:8]}… "
                f"phase={self.phase} conf={self.routing_confidence:.2f}>")


class RoutingOutcome(Base):
    """
    Immutable record of every routing decision.
    Used by calibrator and drift monitor to compute agreement rates and detect drift.
    """
    __tablename__ = "routing_outcomes"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid
    )
    fingerprint_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    app_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    request_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True
    )
    routed_to: Mapped[str] = mapped_column(
        String(16), nullable=False
    )
    # 'cheap' | 'expensive' | 'escalated'

    cheap_model: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    expensive_model: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )

    input_token_count: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    output_token_count: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    input_token_bucket: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )

    structural_check_passed: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True
    )
    conformal_check_passed: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True
    )
    validator_passed: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True
    )

    escalation_reason: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    # 'validator_fail' | 'conformal_fail' | 'structural_fail' | null

    cheap_latency_ms: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    expensive_latency_ms: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )

    cost_cheap: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 8), nullable=True
    )
    cost_expensive: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 8), nullable=True
    )
    cost_saved: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 8), nullable=True
    )

    input_embedding_norm: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    nonconformity_score: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )

    # Calibration enrichment
    sampled_user_message: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    layer: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True
    )
    # 'table' | 'generic' | 'none' — routing decision source

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_routing_out_fp", "fingerprint_hash"),
        Index("ix_routing_out_app", "app_id"),
        Index("ix_routing_out_created", "created_at"),
    )

    def __repr__(self) -> str:
        return (f"<RoutingOutcome {self.fingerprint_hash[:8]}… "
                f"→{self.routed_to} saved=${self.cost_saved or 0:.4f}>")


# ── RBAC Roles ────────────────────────────────────────────────────────────────

class RbacRole(Base):
    """
    Role definition with fine-grained allow/deny permission lists.

    allow — permissions this role grants (e.g. ["apps:read", "billing:write"])
    deny  — permissions explicitly blocked (overrides allow, including inherited)

    Resolution: effective = union(all allow) - union(all deny)
    Deny always wins.

    is_system roles (admin, operator, viewer, etc.) cannot be deleted.
    Custom roles can be created, cloned, and deleted by platform admins.
    """
    __tablename__ = "rbac_roles"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    allow: Mapped[list] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False,
        comment="Permissions this role grants",
    )
    deny: Mapped[list] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False,
        server_default="[]",
        comment="Permissions explicitly denied (overrides allow)",
    )

    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false()
    )

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(),
        onupdate=func.now(), nullable=False
    )

    assignments: Mapped[list["RbacAssignment"]] = relationship(
        back_populates="role",
    )

    def __repr__(self) -> str:
        return f"<RbacRole {self.name!r} system={self.is_system}>"


class RbacAssignment(Base):
    """
    Maps a user to a role, optionally scoped to a team.

    team_id = NULL → platform-level assignment (applies globally).
    team_id set   → role applies only within that team subtree.

    A user can have multiple assignments (e.g. team_lead on Team A,
    viewer on Team B, executive at platform level).
    """
    __tablename__ = "rbac_assignments"
    __table_args__ = (
        UniqueConstraint("user_id", "role_id", "team_id", name="uq_rbac_assignment"),
        Index("ix_rbac_user_id", "user_id"),
        Index("ix_rbac_team_id", "team_id"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    user_id: Mapped[str] = mapped_column(String(256), nullable=False)
    role_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("rbac_roles.id"), nullable=False
    )
    team_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id"), nullable=True
    )
    assigned_by: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )

    role: Mapped[RbacRole] = relationship(back_populates="assignments")

    def __repr__(self) -> str:
        scope = f"team={self.team_id}" if self.team_id else "platform"
        return f"<RbacAssignment user={self.user_id} role={self.role_id} {scope}>"


# ── Users ─────────────────────────────────────────────────────────────────────

class User(Base):
    """
    Platform user account. Linked to SSO/JWT via external_id.

    Users are created either:
      - By accepting an invitation (email link flow)
      - By first JWT login with auto-provisioning (SSO flow)

    email is the canonical identifier for invitations and display.
    external_id maps to the JWT 'sub' claim for SSO integration.
    """
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    external_id: Mapped[Optional[str]] = mapped_column(
        String(256), unique=True, nullable=True,
        comment="SSO/IdP subject identifier (JWT sub claim)",
    )
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    avatar_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=true()
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(),
        onupdate=func.now(), nullable=False
    )

    # SCIM 2.0 enterprise-user extension
    # (urn:ietf:params:scim:schemas:extension:enterprise:2.0:User).
    # Entra/Okta send sub-attributes — employeeNumber, costCenter, organization,
    # division, department, manager{…} — that don't map to first-class columns.
    # We persist the whole extension here so it durably round-trips across
    # restarts and workers (not an in-process store). Telemetry/metadata only —
    # never PII. See orchestrator/api/scim.py enterprise-ext helpers.
    scim_enterprise_ext: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )

    # Relationships
    memberships: Mapped[list["TeamMembership"]] = relationship(
        back_populates="user",
    )
    preferences: Mapped[Optional["UserPreference"]] = relationship(
        back_populates="user", uselist=False,
    )

    def __repr__(self) -> str:
        return f"<User {self.email!r} active={self.is_active}>"


class TeamMembership(Base):
    """
    Assigns a user to a team. A user can belong to multiple teams.
    Separate from RBAC — membership controls data visibility,
    roles control what actions are permitted.
    """
    __tablename__ = "team_memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "team_id", name="uq_team_membership"),
        Index("ix_tm_user_id", "user_id"),
        Index("ix_tm_team_id", "team_id"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    added_by: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="memberships")
    team: Mapped["Team"] = relationship(back_populates="memberships")

    def __repr__(self) -> str:
        return f"<TeamMembership user={self.user_id} team={self.team_id}>"


class UserPreference(Base):
    """
    Per-user saved preferences: default view, pinned apps, layout, theme.
    One row per user (1:1 relationship).
    """
    __tablename__ = "user_preferences"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )
    default_team_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id", ondelete="SET NULL"),
        nullable=True,
    )
    default_view: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True,
        comment="Preferred dashboard view: overview|devops|executive|billing",
    )
    pinned_app_ids: Mapped[list] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False,
        server_default="[]",
        comment="List of app UUIDs the user is responsible for",
    )
    dashboard_layout: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True,
        comment="Custom widget layout / column preferences",
    )
    notification_prefs: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False,
        server_default='{"email": true, "in_app": true}',
        comment="Per-channel notification opt-in",
    )
    timezone: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, server_default="UTC"
    )
    theme: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True, server_default="system",
        comment="UI theme: light|dark|system",
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(),
        onupdate=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="preferences")

    def __repr__(self) -> str:
        return f"<UserPreference user={self.user_id} view={self.default_view}>"


class Invitation(Base):
    """
    Pending user invitation. Supports multiple delivery channels
    (email, Slack, Teams) via pluggable providers.

    Flow:
      1. Admin creates invitation → token generated → sent via channel
      2. Recipient clicks link → token validated → user created
      3. Role assignment created automatically from invitation

    Tokens expire after a configurable period (default 7 days).
    """
    __tablename__ = "invitations"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("rbac_roles.id"), nullable=False
    )
    team_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=True,
        comment="Team scope for the assigned role. NULL = platform-level.",
    )
    invited_by: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(
        String(128), nullable=False,
        comment="bcrypt hash of the invitation token",
    )
    token_prefix: Mapped[str] = mapped_column(
        String(24), nullable=False,
        comment="First chars of token for identification",
    )
    channel: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="email",
        comment="Delivery channel: email|slack|teams",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending",
        comment="pending|accepted|expired|revoked",
    )
    expires_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False
    )
    accepted_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )

    role: Mapped[RbacRole] = relationship()

    def __repr__(self) -> str:
        return f"<Invitation {self.email!r} status={self.status} channel={self.channel}>"


# ── Finance Models ────────────────────────────────────────────────────────────
# Phase 4 — Finance Intelligence
# Cost-center hierarchy, chargeback invoices, scheduled reports, scenario configs.


class CostCenter(Base):
    """
    Organisational cost-center for finance reporting.
    Maps teams to budget owners and departments.
    Supports hierarchical structure: department > division > cost-center.
    """
    __tablename__ = "cost_centers"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    code: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False,
        comment="External cost-center code (e.g. CC-4100, ENG-ML)",
    )
    department: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True,
        comment="Department name for grouping (e.g. Engineering, Sales)",
    )
    division: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True,
        comment="Division within department (e.g. Platform, Product, Infrastructure)",
    )
    budget_owner_name: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True,
        comment="Name of the person responsible for this cost-center's budget",
    )
    budget_owner_email: Mapped[Optional[str]] = mapped_column(
        String(320), nullable=True,
        comment="Email of budget owner — used for scheduled report delivery",
    )
    parent_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("cost_centers.id", ondelete="SET NULL"),
        nullable=True,
        comment="Parent cost-center for hierarchical org structures",
    )
    budget_monthly_usd: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 8), nullable=True,
        comment="Monthly budget allocation for this cost-center",
    )
    budget_quarterly_usd: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 8), nullable=True,
        comment="Quarterly budget allocation for this cost-center",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, server_default=true(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(),
        onupdate=func.now(), nullable=False,
    )

    parent: Mapped[Optional["CostCenter"]] = relationship(
        remote_side="CostCenter.id",
        foreign_keys="CostCenter.parent_id",
    )
    teams: Mapped[list["TeamCostCenter"]] = relationship(back_populates="cost_center")

    def __repr__(self) -> str:
        return f"<CostCenter {self.code!r} dept={self.department}>"


class TeamCostCenter(Base):
    """
    Maps teams to cost-centers. A team can belong to one cost-center.
    This join table allows cost-center reassignment without schema migration.
    """
    __tablename__ = "team_cost_centers"
    __table_args__ = (
        UniqueConstraint("team_id", name="uq_team_cost_center_team"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    cost_center_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("cost_centers.id", ondelete="CASCADE"),
        nullable=False,
    )
    allocation_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), server_default="100.00", nullable=False,
        comment="Percentage of this team's cost allocated to this cost-center (for split billing)",
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )

    cost_center: Mapped[CostCenter] = relationship(back_populates="teams")

    def __repr__(self) -> str:
        return f"<TeamCostCenter team={self.team_id} cc={self.cost_center_id}>"


class ChargebackInvoice(Base):
    """
    Generated chargeback invoice record.
    Each row represents one generated invoice (PDF/CSV).
    """
    __tablename__ = "chargeback_invoices"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    cost_center_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("cost_centers.id", ondelete="SET NULL"),
        nullable=True,
        comment="Cost-center this invoice covers. NULL = org-wide.",
    )
    period_start: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False
    )
    period_end: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False
    )
    total_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False
    )
    line_items: Mapped[Optional[dict]] = mapped_column(
        JSON, nullable=True,
        comment="Structured breakdown: [{team, app, provider, model, cost, tokens}]",
    )
    format: Mapped[str] = mapped_column(
        String(8), server_default="json", nullable=False,
        comment="Output format: json, csv, pdf",
    )
    status: Mapped[str] = mapped_column(
        String(16), server_default="generated", nullable=False,
        comment="generated, delivered, acknowledged",
    )
    delivered_to: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="Delivery destination (email, Slack channel, webhook URL)",
    )
    generated_by: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True,
        comment="User or system that triggered generation",
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<ChargebackInvoice {self.id[:8]} {self.period_start.date()}..{self.period_end.date()} ${self.total_cost_usd}>"


class FinanceReport(Base):
    """
    Scheduled finance report configuration.
    Defines what reports are generated, how often, and where they are delivered.
    """
    __tablename__ = "finance_reports"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    name: Mapped[str] = mapped_column(
        String(256), nullable=False,
        comment="Human-readable report name (e.g. 'Monthly Engineering AI Spend')",
    )
    report_type: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment="Type: chargeback, burn_rate, variance, audit_trail",
    )
    schedule: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment="Frequency: daily, weekly, monthly, quarterly",
    )
    cost_center_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("cost_centers.id", ondelete="SET NULL"),
        nullable=True,
        comment="Scope to specific cost-center. NULL = all.",
    )
    delivery_channel: Mapped[str] = mapped_column(
        String(32), server_default="email", nullable=False,
        comment="Delivery method: email, slack, webhook",
    )
    delivery_target: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="Destination: email address, Slack channel, webhook URL",
    )
    format: Mapped[str] = mapped_column(
        String(8), server_default="pdf", nullable=False,
        comment="Output format: json, csv, pdf",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, server_default=true(), nullable=False
    )
    last_run_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    created_by: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(),
        onupdate=func.now(), nullable=False,
    )

    def __repr__(self) -> str:
        return f"<FinanceReport {self.name!r} type={self.report_type} schedule={self.schedule}>"


class ForecastConfig(Base):
    """
    Forecast engine configuration per team or org-wide.
    Stores the preferred forecast method and optional custom ML endpoint.
    """
    __tablename__ = "forecast_configs"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    team_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=True, unique=True,
        comment="Team scope. NULL = org-wide default.",
    )
    method: Mapped[str] = mapped_column(
        String(32), server_default="auto", nullable=False,
        comment="Forecast method: auto, builtin, custom_ml, ai",
    )
    custom_ml_url: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="URL of customer's ML prediction endpoint",
    )
    custom_ml_auth_encrypted: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="Fernet-encrypted Authorization header for ML endpoint",
    )
    custom_ml_timeout_seconds: Mapped[int] = mapped_column(
        Integer, server_default=text("30"), nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, server_default=true(), nullable=False
    )
    created_by: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(),
        onupdate=func.now(), nullable=False,
    )

    def __repr__(self) -> str:
        return f"<ForecastConfig method={self.method!r} team_id={self.team_id}>"


class BillingActual(Base):
    """
    Provider invoice totals, imported by an operator through
    ``POST /api/v1/finance/reconciliation/import`` (JSON) or ``.../import/csv``.
    Modus does not fetch invoices from providers automatically.
    Used for reconciliation: compare Modus's tracked costs against real invoices.
    """
    __tablename__ = "billing_actuals"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    provider: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="Cloud/AI provider: aws, gcp, azure, openai, anthropic",
    )
    service: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True,
        comment="Specific service (e.g. 'Amazon Bedrock', 'OpenAI API')",
    )
    period_start: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False
    )
    period_end: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False
    )
    actual_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False
    )
    inferred_cost_usd: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 8), nullable=True,
        comment="Modus's inferred cost for the same period — populated during reconciliation",
    )
    delta_usd: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 8), nullable=True,
        comment="actual - inferred. Positive = Modus under-counted.",
    )
    delta_pct: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="(actual - inferred) / actual * 100",
    )
    raw_data: Mapped[Optional[dict]] = mapped_column(
        JSON, nullable=True,
        comment="Original invoice line item data from the provider",
    )
    reconciled_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_billing_actuals_provider_period", "provider", "period_start"),
        # Natural key used by the import endpoints for idempotent upserts.
        # ``service`` is stored as "" (not NULL) when absent so the key is
        # enforceable on both PostgreSQL and SQLite.
        UniqueConstraint(
            "provider", "service", "period_start", "period_end",
            name="uq_billing_actuals_natural_key",
        ),
    )

    def __repr__(self) -> str:
        return f"<BillingActual {self.provider} {self.period_start.date()} actual=${self.actual_cost_usd}>"


class ScenarioConfig(Base):
    """
    Saved what-if scenario configurations.
    Users can save and re-run scenarios to track projected vs actual outcomes.
    """
    __tablename__ = "scenario_configs"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    name: Mapped[str] = mapped_column(
        String(256), nullable=False,
        comment="Human-readable scenario name",
    )
    scenario_type: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment="Type: model_swap, team_add, usage_scale, budget_change",
    )
    parameters: Mapped[dict] = mapped_column(
        JSON, nullable=False,
        comment="Scenario parameters (varies by type)",
    )
    result: Mapped[Optional[dict]] = mapped_column(
        JSON, nullable=True,
        comment="Last computed result: projected delta, affected teams, etc.",
    )
    created_by: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(),
        onupdate=func.now(), nullable=False,
    )

    def __repr__(self) -> str:
        return f"<ScenarioConfig {self.name!r} type={self.scenario_type}>"


# ── Billing Connections ───────────────────────────────────────────────────────

class BillingConnection(Base):
    """
    Provider connection for billing reconciliation.

    Stores connection metadata and encrypted credentials for cloud and AI
    providers. NOTE: nothing pulls billing data through these connections yet;
    invoice totals are imported with ``POST /api/v1/finance/reconciliation/import``.

    Credentials in ``credentials_encrypted`` are encrypted at rest by
    ``orchestrator.core.credential_crypto`` (authenticated Fernet, values
    prefixed ``enc:v1:``). Writes fail closed when no encryption key is set.

    provider_type: aws | gcp | azure | oracle | snowflake | databricks | openai | anthropic | custom
    auth_type:     api_key | oauth2 | bearer_token | iam_role | service_account
    status:        pending | connected | error | disconnected
    """
    __tablename__ = "billing_connections"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    provider_type: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_name: Mapped[str] = mapped_column(String(256), nullable=False)
    service_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # "ai" | "compute" | "storage" | "database" | "other"

    api_endpoint: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    auth_type: Mapped[str] = mapped_column(String(32), nullable=False, default="api_key")
    # Encrypted JSON credential blob. Written via
    # orchestrator.core.credential_crypto.encrypt_credential (authenticated
    # Fernet, versioned "enc:v1:" prefix). Column type stays Text — the
    # encrypted payload is base64 text, so no migration is required.
    credentials_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending"
    )
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sync_schedule: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # "daily" | "weekly" | "monthly"

    config: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )
    # Provider-specific config: region, project_id, subscription_id, etc.

    created_by: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(),
        onupdate=func.now(), nullable=False,
    )

    def __repr__(self) -> str:
        return f"<BillingConnection {self.provider_name!r} type={self.provider_type} status={self.status}>"


# ── Notification Channel Config ───────────────────────────────────────────────

class NotificationChannelConfig(Base):
    """
    Persisted notification channel configuration.
    Stored as a single row (scope='global') with JSONB config blob.
    Survives restarts.
    """
    __tablename__ = "notification_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    scope: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, default="global",
        doc="Config scope. 'global' for platform-wide config.",
    )
    config_json: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        doc="Full notification config as JSON (Slack, Teams, Email, PagerDuty, Webhook).",
    )
    updated_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(),
        onupdate=func.now(), nullable=False,
    )

    def __repr__(self) -> str:
        return f"<NotificationChannelConfig scope={self.scope!r}>"


# ── Phase 5: Agentic Cost Attribution ─────────────────────────────────────────

class AttributionSession(Base):
    """
    One row per completed agent session with computed attribution metrics.

    Created by the warm-path attribution processor after a session's last
    span event exceeds the session timeout (default 120s). Contains
    aggregate metrics and the serialized call graph for visualization.

    framework_tier: "structured" (LangGraph/CrewAI/AutoGen — Tier 1, 80-90% accuracy)
                    or "custom" (bespoke agent code — Tier 2, 50-60% accuracy).
    """
    __tablename__ = "attribution_sessions"
    __table_args__ = (
        Index("ix_attrsess_app_started", "app_id", "started_at"),
        Index("ix_attrsess_team_started", "team_id", "started_at"),
        Index("ix_attrsess_status", "status"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    session_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    app_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id"), nullable=False
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id"), nullable=False
    )

    framework_tier: Mapped[str] = mapped_column(
        String(16), nullable=False, default="custom",
        comment="'structured' (Tier 1) or 'custom' (Tier 2)",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending",
        comment="'pending' | 'processed' | 'stale'",
    )

    total_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), server_default="0", nullable=False
    )
    total_calls: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    total_input_tokens: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    total_output_tokens: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)

    retry_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), server_default="0", nullable=False,
        comment="Total cost of retry calls in this session.",
    )
    defensive_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), server_default="0", nullable=False,
        comment="Total cost of fallback/defensive subgraph calls.",
    )
    attribution_confidence: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="0.0-1.0 confidence in attribution accuracy.",
    )

    graph_json: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True,
        comment="Serialized call graph for trace visualization.",
    )

    started_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<AttributionSession session={self.session_id} "
            f"tier={self.framework_tier} calls={self.total_calls} "
            f"cost=${self.total_cost}>"
        )


class AttributionNode(Base):
    """
    Per-node attribution scores within a session.

    One row per unique call_id (logical node) in the session graph.
    Contains Tree Shapley attributed costs, amplification factor,
    retry tax, and defensive spend metrics.

    amplification_factor: downstream_cost / direct_cost. High values
    indicate a node whose output verbosity is driving downstream costs.

    retry_tax: cost of retries caused by this node's ambiguous output,
    attributed to this node rather than the retrying node.

    defensive_spend: cost of fallback subgraphs triggered by this
    node's failure, attributed here rather than to the fallback nodes.
    """
    __tablename__ = "attribution_nodes"
    __table_args__ = (
        Index("ix_attrnode_session", "session_id", "call_id"),
        Index("ix_attrnode_app_amp", "app_id", "amplification_factor"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    session_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    call_id: Mapped[str] = mapped_column(String(32), nullable=False)
    parent_call_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    node_label: Mapped[str] = mapped_column(
        String(256), nullable=False,
        comment="Human label: provider/model or span name.",
    )
    provider: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    direct_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), server_default="0", nullable=False,
        comment="Cost of this node's own LLM calls.",
    )
    attributed_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), server_default="0", nullable=False,
        comment="Tree Shapley attributed cost (includes downstream share).",
    )
    amplification_factor: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="downstream_cost / direct_cost. High = optimization target.",
    )
    retry_tax: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), server_default="0", nullable=False,
    )
    defensive_spend: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), server_default="0", nullable=False,
    )
    confidence: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, comment="0.0-1.0 per-node attribution confidence.",
    )

    input_tokens: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    call_count: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    is_retry: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_defensive: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    app_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), nullable=False, comment="Denormalized for query perf.",
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), nullable=False, comment="Denormalized for query perf.",
    )

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<AttributionNode call={self.call_id} label={self.node_label} "
            f"AF={self.amplification_factor} cost=${self.attributed_cost}>"
        )


# ── Governance Loop ───────────────────────────────────────────────────────────

class GovernanceProposal(Base):
    """
    Auto-generated policy proposals from the governance loop.

    The governance loop analyzes usage patterns hourly and proposes YAML
    policy changes. Each proposal includes the current and proposed YAML,
    a rationale, and estimated savings. Proposals can be applied (creating
    or updating a GovernancePolicy) or dismissed.

    source indicates how the proposal was generated:
      - rule_engine: deterministic pattern detection
      - rewind: learned from a RewindEvent failure
      - slm: generated by customer's local SLM (optional)
    """
    __tablename__ = "governance_proposals"
    __table_args__ = (
        Index("ix_govprop_team_status", "team_id", "status"),
        Index("ix_govprop_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    app_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id", ondelete="SET NULL"),
        nullable=True,
        comment="Null for team-wide proposals.",
    )

    proposal_type: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="model_downshift, budget_tighten, model_denylist, "
                "amplification_gate, time_policy, rate_limit_suggest, "
                "rewind_learned",
    )
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="info",
        comment="info, warning, critical",
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)

    current_yaml: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="Existing policy YAML if modifying an existing policy.",
    )
    proposed_yaml: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="Proposed policy YAML to apply.",
    )
    data_snapshot: Mapped[Optional[dict]] = mapped_column(
        JSON, nullable=True,
        comment="Usage data that triggered this proposal.",
    )
    estimated_savings_usd: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 8), nullable=True,
        comment="Estimated monthly savings if proposal is applied.",
    )

    source: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="rule_engine",
        comment="rule_engine, rewind, slm",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending",
        comment="pending, applied, dismissed, expired",
    )

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
    applied_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    applied_by: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    dismissed_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    dismissed_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
        comment="Auto-expire stale proposals.",
    )

    def __repr__(self) -> str:
        return (
            f"<GovernanceProposal type={self.proposal_type} "
            f"status={self.status} severity={self.severity}>"
        )


class RewindEvent(Base):
    """
    SDK-triggered rewind events for post-call anomaly rollback.

    When the SDK detects a post-call anomaly (circuit breaker trip, output
    validation failure, budget suspension mid-session), it triggers registered
    rewind hooks and captures the failure context. This data is POSTed to the
    orchestrator for experience distillation — the governance loop analyzes
    failure patterns and proposes preventive policies.
    """
    __tablename__ = "rewind_events"
    __table_args__ = (
        Index("ix_rewind_app_time", "app_id", "created_at"),
        Index("ix_rewind_team_time", "team_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    session_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    app_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id", ondelete="CASCADE"),
        nullable=False,
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )

    trigger_reason: Mapped[str] = mapped_column(
        String(128), nullable=False,
        comment="circuit_breaker, output_validation, budget_suspension, manual",
    )
    actions_rolled_back: Mapped[Optional[list]] = mapped_column(
        JSON, nullable=True,
        comment="List of {tool_name, success, error} dicts.",
    )
    failure_context: Mapped[Optional[dict]] = mapped_column(
        JSON, nullable=True,
        comment="Tool call sequence, error details, tokens consumed.",
    )
    policy_diff_generated: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="If distillation produced a governance proposal, its YAML.",
    )

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<RewindEvent session={self.session_id} "
            f"trigger={self.trigger_reason}>"
        )


# ── Trajectory Forecasting ────────────────────────────────────────────────────

class SessionFingerprint(Base):
    """
    Statistical profile of agent session patterns per app.

    Computed hourly by the trajectory fingerprint background task from
    historical usage_records. The SDK receives these via heartbeat and
    uses them for local Monte-Carlo simulation to predict session costs.

    fingerprint_hash: SHA-256 of (app_id, session_pattern_type) — groups
    sessions with similar tool-call patterns.
    """
    __tablename__ = "session_fingerprints"
    __table_args__ = (
        Index("ix_sessfp_app", "app_id"),
        Index("ix_sessfp_team_time", "team_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    app_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id", ondelete="CASCADE"),
        nullable=False,
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    fingerprint_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True,
        comment="SHA-256 of session pattern grouping.",
    )

    avg_calls_per_session: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="0",
    )
    std_calls: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="0",
    )
    avg_cost_per_call: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, server_default="0",
    )
    std_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, server_default="0",
    )
    call_count_p95: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0",
    )
    branching_factor: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="1.0",
        comment="Avg child calls per parent call. >1 = agentic branching.",
    )
    avg_tool_calls: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="0",
    )
    tool_failure_rate: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="0",
    )
    sample_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0",
        comment="Number of sessions used to compute this fingerprint.",
    )

    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
        onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"<SessionFingerprint app={self.app_id} "
            f"avg_calls={self.avg_calls_per_session} "
            f"samples={self.sample_count}>"
        )


class TrajectoryDecision(Base):
    """
    Record of a trajectory simulation decision.

    Created when the trajectory governor blocks or degrades a call
    based on Monte-Carlo simulation results.
    """
    __tablename__ = "trajectory_decisions"
    __table_args__ = (
        Index("ix_trajdec_session", "session_id"),
        Index("ix_trajdec_app_time", "app_id", "computed_at"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    session_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True,
    )
    app_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), nullable=False,
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), nullable=False,
    )

    simulation_method: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment="linear, monte_carlo, slm",
    )
    p50_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False,
    )
    p95_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False,
    )
    breach_probability: Mapped[float] = mapped_column(
        Float, nullable=False,
    )
    decision: Mapped[str] = mapped_column(
        String(16), nullable=False,
        comment="allow, block, degrade",
    )
    suggested_action: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True,
    )

    computed_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<TrajectoryDecision session={self.session_id} "
            f"decision={self.decision} p95=${self.p95_cost}>"
        )


# ── Enforcement Attestations ──────────────────────────────────────────────────

class EnforcementAttestation(Base):
    """
    Cryptographic attestation for policy enforcement decisions.

    Every non-allow policy decision generates an HMAC-SHA256 signed
    attestation that can be verified offline by auditors without
    access to the original prompt or response content.

    Attestations are batched into Merkle trees every 5 minutes for
    efficient batch verification.
    """
    __tablename__ = "enforcement_attestations"
    __table_args__ = (
        Index("ix_attest_decision", "decision_id"),
        Index("ix_attest_batch", "merkle_batch_id"),
        Index("ix_attest_time", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    decision_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("policy_decisions.id", ondelete="CASCADE"),
        nullable=False,
    )

    attestation_hash: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="SHA-256 of canonical attestation payload.",
    )
    signature: Mapped[str] = mapped_column(
        String(128), nullable=False,
        comment="HMAC-SHA256 signature of the attestation payload.",
    )
    nonce: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="Random nonce for replay prevention.",
    )
    key_source: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="env",
        comment="tpm, env, tee",
    )
    algorithm: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="hmac-sha256",
    )
    payload_json: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="Canonical JSON of the attestation payload.",
    )

    merkle_batch_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
        comment="Batch ID linking to MerkleRoot. Set on flush.",
    )
    merkle_proof: Mapped[Optional[dict]] = mapped_column(
        JSON, nullable=True,
        comment="Merkle inclusion proof (list of sibling hashes).",
    )

    verified_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
    )
    verified_by: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True,
    )

    # Phase 8a: Post-Quantum attestation columns. NOTE: the default tier is an
    # HMAC-SHA-512 hash-chain commitment (a symmetric MAC — not a publicly-
    # verifiable signature); real ML-DSA-65 signatures are the optional Tier 2.
    pqc_signature: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="PQC attestation value, hex-encoded: HMAC-SHA-512 commitment "
                "(default tier) or ML-DSA-65 signature (Tier 2). NULL until migrated.",
    )
    pqc_algorithm: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
        comment="Algorithm used (e.g. pqc-shim-hmac-sha512, ml-dsa-65).",
    )
    pqc_public_key_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
        comment="Stable key identifier (ML-DSA public key id, or shim key id "
                "for the HMAC tier — the HMAC tier has no public key).",
    )

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<EnforcementAttestation decision={self.decision_id} "
            f"algo={self.algorithm}>"
        )


class MerkleRoot(Base):
    """
    Merkle tree root hashes for batch attestation verification.

    Every 5 minutes, pending attestations are batched into a Merkle tree.
    The root hash is stored here. Auditors can verify any attestation's
    inclusion in the tree using the proof stored on EnforcementAttestation.
    """
    __tablename__ = "merkle_roots"
    __table_args__ = (
        Index("ix_merkle_time", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    batch_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False,
    )
    root_hash: Mapped[str] = mapped_column(
        String(64), nullable=False,
    )
    leaf_count: Mapped[int] = mapped_column(
        Integer, nullable=False,
    )
    period_start: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False,
    )
    period_end: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<MerkleRoot batch={self.batch_id} "
            f"leaves={self.leaf_count} root={self.root_hash[:16]}...>"
        )


# ── Policy Prover ─────────────────────────────────────────────────────────────

class PolicyProof(Base):
    """
    Formal verification proof certificates for governance policies.

    Generated by the neuro-symbolic policy prover (bounded enumeration
    or Z3 SMT solver). Proves that a policy configuration prevents
    budget violations under all possible trajectories.
    """
    __tablename__ = "policy_proofs"
    __table_args__ = (
        Index("ix_proof_policy", "policy_id"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    policy_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("governance_policies.id", ondelete="CASCADE"),
        nullable=False,
    )

    proof_type: Mapped[str] = mapped_column(
        String(16), nullable=False,
        comment="bounded, smt",
    )
    proof_status: Mapped[str] = mapped_column(
        String(16), nullable=False,
        comment="proven, disproven, timeout, unknown",
    )
    proof_certificate: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="JSON proof certificate.",
    )
    counterexample: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="JSON showing violating trajectory if disproven.",
    )
    variables_checked: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0",
    )
    max_depth: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0",
    )
    solver_time_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0",
    )

    proven_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<PolicyProof policy={self.policy_id} "
            f"status={self.proof_status} type={self.proof_type}>"
        )


# ── Phase 8a: Post-Quantum Attestation ────────────────────────────────────────

class PQCMigrationLog(Base):
    """
    Audit trail for PQC migration of existing attestations.
    Each row records one attestation re-signed with a PQC algorithm.
    """
    __tablename__ = "pqc_migration_log"
    __table_args__ = (
        Index("ix_pqcmig_attestation", "attestation_id"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid,
    )
    attestation_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("enforcement_attestations.id", ondelete="CASCADE"),
        nullable=False,
    )
    old_algorithm: Mapped[str] = mapped_column(String(64), nullable=False)
    new_algorithm: Mapped[str] = mapped_column(String(64), nullable=False)
    old_signature: Mapped[str] = mapped_column(Text, nullable=False)
    new_signature: Mapped[str] = mapped_column(Text, nullable=False)
    migrated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )


# ── Phase 8b: Trajectory Compliance Receipts ─────────────────────────────────

class TrajectoryProof(Base):
    """
    Tamper-evident compliance receipt (SHA-256 hash chain; the optional
    Tier-2 pairing value is experimental, not a production zk-SNARK)
    attesting that an agent trajectory complies with all active policies.
    One receipt per sampled session. NOT a zero-knowledge proof.
    """
    __tablename__ = "trajectory_proofs"
    __table_args__ = (
        Index("ix_trajproof_session", "session_id"),
        Index("ix_trajproof_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid,
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id", ondelete="SET NULL"),
        nullable=True,
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )

    proof_type: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment="hash_chain, snark",
    )
    proof_status: Mapped[str] = mapped_column(
        String(16), nullable=False,
        comment="valid, invalid, pending",
    )
    proof_data: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="Hex-encoded proof bytes.",
    )
    public_inputs_json: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="JSON: policy hashes, trajectory summary.",
    )
    verification_key_hash: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
    )
    merkle_root: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
        comment="Anchored to attestation Merkle tree.",
    )
    circuit_size: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0",
    )
    prover_time_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0",
    )
    verified_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )

    def __repr__(self) -> str:
        return f"<TrajectoryProof session={self.session_id} status={self.proof_status}>"


# ── Phase 8c: TRiSM Multi-Agent Sentinel ─────────────────────────────────────

class TRiSMThreatEvent(Base):
    """
    Detected agentic threat event (OWASP 2026 vectors).
    Each row is one threat detection with severity, evidence, and rollback plan.
    """
    __tablename__ = "trism_threat_events"
    __table_args__ = (
        Index("ix_trism_session", "session_id"),
        Index("ix_trism_created", "created_at"),
        Index("ix_trism_severity", "severity"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid,
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id", ondelete="SET NULL"),
        nullable=True,
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )

    threat_type: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="context_poisoning, goal_hijack, cascade_failure, "
                "communication_anomaly, prompt_injection, tool_misuse",
    )
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False,
        comment="info, warning, critical, blocked",
    )
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    detection_method: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment="pattern, statistical, slm",
    )
    indicators_json: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="JSON evidence that triggered detection.",
    )
    rollback_plan_json: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="JSON auto-generated rollback steps.",
    )
    action_taken: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="alert",
        comment="alert, block, degrade, rewind",
    )
    related_rewind_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("rewind_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<TRiSMThreatEvent type={self.threat_type} "
            f"severity={self.severity}>"
        )


class TRiSMPattern(Base):
    """
    Configurable threat detection patterns for the TRiSM sentinel.
    Builtin patterns are seeded on startup; custom patterns added by admins.
    """
    __tablename__ = "trism_patterns"
    __table_args__ = (
        UniqueConstraint("pattern_name", name="uq_trism_pattern_name"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid,
    )
    pattern_name: Mapped[str] = mapped_column(String(128), nullable=False)
    threat_type: Mapped[str] = mapped_column(String(64), nullable=False)
    detection_rules_json: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="JSON rules for this pattern.",
    )
    risk_weight: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="1.0",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=true(),
    )
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="builtin",
        comment="builtin, custom, owasp",
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1",
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(),
        onupdate=func.now(), nullable=False,
    )


# ── Phase 8d: Constitutional Evolution Engine ────────────────────────────────

class EvolutionGeneration(Base):
    """
    One generation of the constitutional evolution engine.
    Tracks population fitness, best genome, and simulation stats.
    """
    __tablename__ = "evolution_generations"
    __table_args__ = (
        Index("ix_evogen_team", "team_id"),
        Index("ix_evogen_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid,
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    generation_number: Mapped[int] = mapped_column(Integer, nullable=False)
    population_size: Mapped[int] = mapped_column(Integer, nullable=False)
    best_fitness: Mapped[float] = mapped_column(Float, nullable=False)
    avg_fitness: Mapped[float] = mapped_column(Float, nullable=False)
    best_genome_yaml: Mapped[str] = mapped_column(Text, nullable=False)
    mutations_applied: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="JSON list of mutation descriptions.",
    )
    simulation_stats_json: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="JSON: agent_count, steps, cost_savings.",
    )
    prover_results_json: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="JSON: Z3 proof results for best genome.",
    )
    elapsed_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<EvolutionGeneration gen={self.generation_number} "
            f"fitness={self.best_fitness:.4f}>"
        )


class EvolutionProposal(Base):
    """
    Policy proposal generated by the constitutional evolution engine.
    Best-fitness genomes become proposals that can be accepted/rejected.
    """
    __tablename__ = "evolution_proposals"
    __table_args__ = (
        Index("ix_evoprop_team_status", "team_id", "status"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid,
    )
    generation_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("evolution_generations.id", ondelete="CASCADE"),
        nullable=False,
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    proposal_yaml: Mapped[str] = mapped_column(Text, nullable=False)
    fitness_score: Mapped[float] = mapped_column(Float, nullable=False)
    constitutional_diff: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="Human-readable diff from current policies.",
    )
    rationale: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="LLM-generated rationale if Tier 3.",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending",
        comment="pending, accepted, rejected",
    )
    accepted_by: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )


# ── Phase 8e: Neuromorphic Edge Enforcement ──────────────────────────────────

class NeuromorphicMetrics(Base):
    """
    Aggregated metrics for neuromorphic enforcement mode.
    Tracks latency, power, and spike efficiency per app over time windows.
    """
    __tablename__ = "neuromorphic_metrics"
    __table_args__ = (
        Index("ix_neurometric_app", "app_id"),
        Index("ix_neurometric_period", "period_start"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid,
    )
    app_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id", ondelete="SET NULL"),
        nullable=True,
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    evaluation_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0",
    )
    avg_latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    avg_power_mw: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="Hardware-only. Null for software emulation.",
    )
    spike_efficiency: Mapped[float] = mapped_column(
        Float, nullable=False,
        comment="Ratio of active neurons to total.",
    )
    topology_hash: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="SHA-256 of compiled SNN topology.",
    )
    hardware_backend: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="software",
        comment="software, loihi, speck, none",
    )
    period_start: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False,
    )
    period_end: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Phase 9 — Advanced Cryptographic Governance & Cross-Deployment Intelligence
# ══════════════════════════════════════════════════════════════════════════════


# ── Feature 1: ZK-PoE Chained Audit Ledger ──────────────────────────────────

class PoELedgerEntry(Base):
    """
    Immutable hash-linked ledger entry for enforcement decisions.
    Each entry chains to the previous via prev_hash, creating a tamper-evident
    sequence verifiable offline by auditors without access to prompts/responses.
    """
    __tablename__ = "poe_ledger_entries"
    __table_args__ = (
        Index("ix_poe_session_seq", "session_id", "seq_num"),
        Index("ix_poe_team_created", "team_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid,
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    seq_num: Mapped[int] = mapped_column(Integer, nullable=False)
    prev_hash: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
        comment="SHA-256 of previous entry. Null for genesis entry.",
    )
    entry_hash: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="SHA-256(prev_hash + decision_hash + timestamp).",
    )
    decision_hash: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="SHA-256 of the enforcement decision payload.",
    )
    trajectory_proof_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("trajectory_proofs.id", ondelete="SET NULL"),
        nullable=True,
    )
    proof_type: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="hash_chain",
        comment="hash_chain | snark",
    )
    tee_quote: Mapped[Optional[bytes]] = mapped_column(
        LargeBinary, nullable=True,
        comment="TEE attestation quote (TDX or SEV-SNP) if hardware present.",
    )
    tee_platform: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True,
        comment="tdx | sev-snp | null",
    )
    risk_level: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="medium",
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )


class PoEMerkleAnchor(Base):
    """
    Periodic Merkle anchor for batches of PoE ledger entries.
    Enables efficient batch verification (<5ms for 1000 entries).
    """
    __tablename__ = "poe_merkle_anchors"
    __table_args__ = (
        Index("ix_poe_anchor_team", "team_id", "anchored_at"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid,
    )
    team_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id", ondelete="SET NULL"),
        nullable=True,
    )
    merkle_root: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_count: Mapped[int] = mapped_column(Integer, nullable=False)
    first_entry_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), nullable=True,
    )
    last_entry_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), nullable=True,
    )
    anchored_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )


# ── Chain-of-Thought (CoT) Ledger ────────────────────────────────────────────

class CoTLedgerEntry(Base):
    """
    Tamper-proof, hash-chained audit trail for governance decisions.
    Records the reasoning behind every autonomous policy proposal, evolution
    decision, and anomaly detection — enabling governance officers to audit
    WHY the self-evolving engine made each decision.

    Each entry chains to the previous via prev_hash (SHA-256), creating
    a verifiable, immutable sequence. No entry can be modified, deleted, or
    reordered without breaking the chain.

    Append-only. No UPDATE or DELETE operations permitted.
    """
    __tablename__ = "cot_ledger_entries"
    __table_args__ = (
        Index("ix_cot_team_created", "team_id", "created_at"),
        Index("ix_cot_team_seq", "team_id", "seq_num"),
        Index("ix_cot_decision_type", "decision_type", "created_at"),
        Index("ix_cot_linked_proposal", "linked_proposal_id"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid,
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), nullable=False,
        comment="Team this decision applies to. No FK — entries survive team deletion.",
    )
    seq_num: Mapped[int] = mapped_column(
        Integer, nullable=False,
        comment="Position in the hash chain for this team.",
    )
    prev_hash: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
        comment="SHA-256 of previous entry. Null for genesis entry.",
    )
    entry_hash: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="SHA-256(prev_hash + decision_type + trigger + evidence_hash + summary).",
    )

    # ── What happened ──────────────────────────────────────────────────────
    decision_type: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment="governance_proposal | evolution_proposal | anomaly_signal | policy_applied | policy_dismissed",
    )
    trigger: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="What started the analysis: pattern_detection, evolution_cycle, rewind_event, anomaly_detection, manual",
    )
    decision_summary: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="Human-readable summary of the final decision.",
    )

    # ── The reasoning chain (structured JSON) ──────────────────────────────
    evidence_snapshot: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True,
        comment="Data points analyzed: costs, call counts, breach counts, thresholds.",
    )
    rules_evaluated: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True,
        comment="[{rule, fired, confidence, detail}] — which detection rules ran.",
    )
    reasoning_steps: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True,
        comment="Ordered list of logical inferences that led to the decision.",
    )
    alternatives_considered: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True,
        comment="Other actions evaluated and why they were rejected.",
    )

    # ── Linked entities (no FKs — entries must survive deletion) ───────────
    linked_proposal_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True,
        comment="GovernanceProposal or EvolutionProposal ID.",
    )
    linked_policy_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True,
        comment="GovernancePolicy ID if policy was created/modified.",
    )
    linked_evolution_gen_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True,
        comment="EvolutionGeneration ID if from evolution cycle.",
    )

    # ── Regulatory compliance tags ─────────────────────────────────────────
    regulatory_tags: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True,
        comment='["eu_ai_act_article_14", "nist_ai_rmf", "iso_42001"]',
    )

    # ── Timestamp ──────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )


# ── Feature 2: NeuroAI Assurance Co-Evolution ────────────────────────────────

class NeuroAssuranceMetric(Base):
    """
    Software-simulated assurance metrics from SNN evaluation.
    Energy figures are illustrative constants, not measurements (research
    demo — no hardware energy benefit on standard servers).
    """
    __tablename__ = "neuro_assurance_metrics"
    __table_args__ = (
        Index("ix_neuro_assurance_team", "team_id", "recorded_at"),
        Index("ix_neuro_assurance_app", "app_id", "recorded_at"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid,
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    app_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id", ondelete="SET NULL"),
        nullable=True,
    )
    session_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    topology_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    energy_per_spike: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stdp_drift: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="Spike-timing-dependent plasticity drift metric.",
    )
    embodied_efficiency_score: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="Calls per watt-hour equivalent.",
    )
    spike_rate_hz: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    active_neuron_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    inference_latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    hardware_backend: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="software",
    )
    recorded_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )


class NeuroComplianceReport(Base):
    """
    Compliance report generated from aggregated NeuroAssuranceMetric data.
    Metrics are software-simulated; energy figures are illustrative
    constants, not physics measurements.
    """
    __tablename__ = "neuro_compliance_reports"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid,
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    app_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id", ondelete="SET NULL"),
        nullable=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metric_count: Mapped[int] = mapped_column(Integer, nullable=False)
    from_ts: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    to_ts: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )


# ── Feature 3: Federated ZK-Constitutional Optimizer ─────────────────────────

class FederationConsent(Base):
    """
    Operator consent record for federated constitutional evolution.
    No outbound federation traffic occurs without an active consent record.
    """
    __tablename__ = "federation_consent"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid,
    )
    participation_mode: Mapped[str] = mapped_column(
        String(16), nullable=False,
        comment="consumer | participant | consortium_hub",
    )
    disclosure_text_hash: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="SHA-256 of the consent disclosure text shown to operator.",
    )
    consented_by: Mapped[str] = mapped_column(String(256), nullable=False)
    consented_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )
    withdrawn_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
    )
    instance_nonce_key: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="Rolling key for anonymous submission nonces.",
    )


class FederationSyncLog(Base):
    """Log of federation sync operations (submissions and receipts)."""
    __tablename__ = "federation_sync_log"
    __table_args__ = (
        Index("ix_fed_sync_synced", "synced_at"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid,
    )
    direction: Mapped[str] = mapped_column(
        String(8), nullable=False,
        comment="submit | receive",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False,
        comment="success | error | skipped",
    )
    delta_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    generation_from: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    generation_to: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )


class FederationMergedResult(Base):
    """Merged constitutional evolution results from the federation network."""
    __tablename__ = "federation_merged_results"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid,
    )
    result_version: Mapped[str] = mapped_column(String(16), nullable=False)
    gene_improvements: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True,
        comment="Gene name -> improvement delta (numeric only, no policy content).",
    )
    participating_instances: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0",
        comment="Aggregate count of participating instances. No identifiers.",
    )
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )


class ConsortiumPeer(Base):
    """Registered peer in a private federation consortium."""
    __tablename__ = "consortium_peers"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid,
    )
    peer_endpoint: Mapped[str] = mapped_column(String(512), nullable=False)
    peer_public_key_hash: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="SHA-256 of peer's public key for request authentication.",
    )
    peer_alias: Mapped[str] = mapped_column(String(128), nullable=False)
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=true(),
    )
    registered_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )


# ── Federation Peer Management ───────────────────────────────────────────────

class FederationPeer(Base):
    """Managed federation peer — subsidiary Modus instance."""
    __tablename__ = "federation_peers"
    __table_args__ = (
        Index("ix_fed_peer_team", "team_id"),
        Index("ix_fed_peer_status", "status"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid,
    )
    team_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    peer_url: Mapped[str] = mapped_column(String(512), nullable=False)
    api_key_hash: Mapped[str] = mapped_column(
        String(128), nullable=False,
        comment="SHA-256 hex digest of the peer API key.",
    )
    api_key_prefix: Mapped[str] = mapped_column(
        String(8), nullable=False,
        comment="First 8 characters of the API key for display.",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="active",
        comment="active | paused | error | unreachable",
    )
    last_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
    )
    last_heartbeat_latency_ms: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
    )
    last_sync_status: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True,
        comment="success | error",
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True,
        comment="Aggregate metrics from peer.",
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )


class FederationPeerSyncLog(Base):
    """Sync history for a managed federation peer."""
    __tablename__ = "federation_peer_sync_log"
    __table_args__ = (
        Index("ix_fed_psync_peer_at", "peer_id", "synced_at"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid,
    )
    peer_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("federation_peers.id", ondelete="CASCADE"),
        nullable=False,
    )
    direction: Mapped[str] = mapped_column(
        String(16), nullable=False,
        comment="push | pull | heartbeat",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False,
        comment="success | error | timeout",
    )
    latency_ms: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )


# ── Feature 5: Threshold Approval Swarm Governance (Preview) ─────────────────
# NOTE: threshold (k-of-n) sign-off, NOT privacy-preserving MPC.

class SwarmConfig(Base):
    """Configuration for a threshold (k-of-n) swarm sign-off session
    (Preview; not privacy-preserving MPC)."""
    __tablename__ = "swarm_configs"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid,
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    swarm_name: Mapped[str] = mapped_column(String(256), nullable=False)
    party_ids: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True,
        comment="List of party identifiers (hashed, no PII).",
    )
    shared_policy_ids: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True,
        comment="List of governance_policies IDs for joint evaluation.",
    )
    threshold_k: Mapped[int] = mapped_column(
        Integer, nullable=False,
        comment="Minimum parties required for threshold sign-off (k).",
    )
    total_parties_n: Mapped[int] = mapped_column(Integer, nullable=False)
    evaluation_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="additive",
        comment="additive | shamir",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=true(),
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), onupdate=func.now(),
        nullable=False,
    )


class SwarmSession(Base):
    """Active swarm governance evaluation session."""
    __tablename__ = "swarm_sessions"
    __table_args__ = (
        Index("ix_swarm_sess_swarm_status", "swarm_id", "status"),
        Index("ix_swarm_sess_team", "team_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid,
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    swarm_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("swarm_configs.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_ref: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="External session or request reference.",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="open",
        comment="open | evaluating | complete | expired",
    )
    contributions_received: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0",
    )
    mpc_result: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
    )


class SwarmContribution(Base):
    """Secret-shared policy input from a party in a swarm session."""
    __tablename__ = "swarm_contributions"
    __table_args__ = (
        UniqueConstraint("session_id", "party_hash", name="uq_swarm_party"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid,
    )
    session_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("swarm_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    party_hash: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="HMAC-SHA256 of party_id with per-swarm salt.",
    )
    share_data: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="Fernet-encrypted Shamir share.",
    )
    submitted_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )


class PolicyDecisionRecord(Base):
    """
    Signed Policy Decision Record (PDR) from a threshold (k-of-n) sign-off.
    Carries an HMAC attestation signature; the ``mpc_proof`` field is a
    placeholder, not a sound zero-knowledge proof. Not privacy-preserving MPC.
    """
    __tablename__ = "policy_decision_records"
    __table_args__ = (
        Index("ix_pdr_swarm_issued", "swarm_id", "issued_at"),
        Index("ix_pdr_team_issued", "team_id", "issued_at"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid,
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    swarm_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("swarm_configs.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("swarm_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    decision: Mapped[str] = mapped_column(
        String(8), nullable=False,
        comment="allow | deny",
    )
    party_count: Mapped[int] = mapped_column(Integer, nullable=False)
    threshold_met: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false(),
    )
    mpc_proof: Mapped[str] = mapped_column(Text, nullable=False)
    attestation_signature: Mapped[str] = mapped_column(String(256), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )


class PQCReadinessScore(Base):
    """
    Post-quantum cryptography readiness assessment score.
    Tracks migration progress from classical to PQC algorithms.
    """
    __tablename__ = "pqc_readiness_scores"
    __table_args__ = (
        Index("ix_pqc_readiness_team_assessed", "team_id", "assessed_at"),
        Index("ix_pqc_readiness_app", "app_id", "assessed_at"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid,
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    app_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id", ondelete="CASCADE"),
        nullable=True,
    )
    score: Mapped[float] = mapped_column(
        Float, nullable=False,
        comment="PQC readiness score 0-100",
    )
    classical_key_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    hybrid_key_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    pqc_key_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    weakest_algorithm: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    recommendations: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="JSON array of recommended actions",
    )
    assessed_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )


class HNDLRiskAssessment(Base):
    """
    Harvest-Now-Decrypt-Later risk assessment for sessions.
    Evaluates quantum vulnerability of data in transit.
    """
    __tablename__ = "hndl_risk_assessments"
    __table_args__ = (
        Index("ix_hndl_risk_team_assessed", "team_id", "assessed_at"),
        Index("ix_hndl_risk_level", "team_id", "risk_level"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid,
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), nullable=True,
    )
    algorithm_detected: Mapped[str] = mapped_column(String(64), nullable=False)
    key_size_bits: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_quantum_break_year: Mapped[int] = mapped_column(Integer, nullable=False)
    risk_level: Mapped[str] = mapped_column(
        String(16), nullable=False,
        comment="safe | monitor | urgent | critical",
    )
    data_sensitivity: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'standard'"),
        comment="standard | confidential | secret | top_secret",
    )
    recommendation: Mapped[str] = mapped_column(Text, nullable=False)
    enforcement_action: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'allowed'"),
        comment="allowed | warned | degraded | blocked",
    )
    assessed_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )


class AgentIdentity(Base):
    """
    PQC-rooted cryptographic identity for AI agents.
    Each agent gets a unique keypair derived from the platform root key
    via HKDF. Keys are ML-KEM-768 shim (HMAC-based) until real PQC libraries ship.
    """
    __tablename__ = "agent_identities"
    __table_args__ = (
        UniqueConstraint("agent_fingerprint", name="uq_agent_fingerprint"),
        Index("ix_agent_identity_team_app", "team_id", "app_id"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid,
    )
    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    app_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("apps.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="SHA-256 fingerprint of agent public key",
    )
    public_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    key_algorithm: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'hmac-shim-v1'"),
        comment="ml-kem-768 | hmac-shim-v1",
    )
    platform_chain_hash: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="Hash linking to platform root key chain",
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False,
    )
    rotated_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
    )
