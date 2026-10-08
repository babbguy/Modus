"""
Modus — Database Session Management
============================================
Async SQLAlchemy engine and session factory.

Supports two backends:
  - PostgreSQL (production): asyncpg driver, connection pool tuning
  - SQLite (dev/single-file): aiosqlite driver, zero-config deploy

Write-heavy endpoints (ingest, heartbeat) use the write queue
(orchestrator.core.write_queue) instead of direct sessions to avoid
SQLite lock contention. Sessions here are used for reads and low-frequency
admin writes (registration, thresholds, dashboard queries).

Pool sizing guidance (pool_size, max_overflow and pool_timeout also apply
to a SQLite file; pool_recycle is PostgreSQL only):
  - pool_size: number of persistent connections. Set to match your
    PostgreSQL max_connections / number of orchestrator replicas.
    Default 10 is appropriate for a single replica behind a small cluster.
  - max_overflow: burst connections above pool_size. These are created
    on demand and closed when returned to pool.
  - pool_timeout: seconds to wait for a connection before raising.
  - pool_recycle: seconds before a connection is recycled. Prevents
    "server closed the connection unexpectedly" from idle timeouts.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from datetime import datetime, timezone

from orchestrator.core.config import settings

logger = logging.getLogger(__name__)


def sqlite_dt(dt: datetime):
    """Format a datetime as a bound parameter for a raw-SQL timestamp comparison.

    SQLite has no timestamp type: SQLAlchemy stores ``DateTime`` columns as
    text in the form ``2026-03-18 14:30:00.000000`` (space separator, UTC) and
    raw SQL compares them as strings. A bound parameter must therefore use the
    same format -- an ISO ``T`` separator sorts after the space and silently
    excludes a row that sits exactly on the boundary (for example the daily
    aggregate for the 1st of the month at 00:00:00 in a month-to-date query).

    On PostgreSQL the driver (asyncpg) requires a real ``datetime`` for a
    ``timestamptz`` parameter and rejects strings (finance/insights queries
    returned HTTP 500), so a timezone-aware UTC ``datetime`` truncated to whole
    seconds is returned there instead.
    """
    from orchestrator.core.config import settings

    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    if settings.is_sqlite:
        return dt.replace(tzinfo=None, microsecond=0).strftime("%Y-%m-%d %H:%M:%S.%f")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.replace(microsecond=0)

# Module-level engine and session factory — initialised once at startup.
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

# Read replica engine (Phase 4d) — optional, for dashboard/aggregation reads.
_replica_engine: AsyncEngine | None = None
_replica_session_factory: async_sessionmaker[AsyncSession] | None = None


def build_engine() -> AsyncEngine:
    """
    Build the SQLAlchemy async engine from settings.

    Automatically detects PostgreSQL vs SQLite and configures appropriately.
    Called once during application startup. Subsequent calls to get_engine()
    return the cached instance.
    """
    if settings.is_sqlite:
        logger.info("Using SQLite backend: %s", settings.database_url)
        if ":memory:" in settings.database_url or "mode=memory" in settings.database_url:
            # Every connection to an in-memory database is a separate, empty
            # database, so a pool would hand out unrelated databases.
            from sqlalchemy.pool import NullPool
            return create_async_engine(
                settings.database_url,
                echo=settings.debug,
                connect_args={"check_same_thread": False},
                poolclass=NullPool,
            )
        # SQLite file: keep connections open in a pool. Without one (NullPool)
        # every session opened a new aiosqlite connection: a new thread, a new
        # file handle and five PRAGMAs, on every request and every background
        # query. That tripled the CPU cost of /api/v1/policy/evaluate (about
        # 22 ms instead of 7 ms per call on one core) and pushed SDK calls
        # past their 3 s timeout under modest traffic on a 1-CPU server.
        # WAL mode (set via the connect listener in init_db) lets pooled
        # readers run alongside the writer; busy_timeout covers write waits.
        return create_async_engine(
            settings.database_url,
            echo=settings.debug,
            connect_args={"check_same_thread": False},
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout,
        )

    # PostgreSQL mode — full pool tuning
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=True,
        echo=settings.debug,
        echo_pool=settings.debug,
        json_serializer=None,
        json_deserializer=None,
    )


def get_engine() -> AsyncEngine:
    """Return the module-level engine. Raises if not initialised."""
    if _engine is None:
        raise RuntimeError(
            "Database engine not initialised. Call init_db() at application startup."
        )
    return _engine


async def init_db() -> None:
    """
    Initialise the database engine and session factory.
    Called once in the FastAPI lifespan handler.

    For SQLite mode, also creates all tables automatically (no Alembic needed).
    """
    global _engine, _session_factory

    logger.info("Initialising database connection pool")
    _engine = build_engine()
    _session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # Tamper-evidence: chain every AuditLog write into a global hash chain.
    # Installed on the sessionmaker so all sessions (any DB backend) are covered.
    from orchestrator.core.audit_chain import install_audit_chain
    install_audit_chain(_session_factory)

    # SQLite: auto-create tables + enable WAL mode for concurrent reads/writes
    if settings.is_sqlite:
        from orchestrator.db.models import Base
        from sqlalchemy import event

        # Set pragmas on every new connection
        @event.listens_for(_engine.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA cache_size=-64000")  # 64MB
            cursor.execute("PRAGMA temp_store=MEMORY")
            cursor.close()

        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # Additive column migrations — safe to re-run (no-op if column exists)
        _additive_migrations = [
            "ALTER TABLE cost_centers ADD COLUMN division VARCHAR(256)",
            "ALTER TABLE teams ADD COLUMN budget_monthly_usd NUMERIC(18,8)",
            "ALTER TABLE teams ADD COLUMN budget_quarterly_usd NUMERIC(18,8)",
            "ALTER TABLE teams ADD COLUMN cost_center_id UUID REFERENCES cost_centers(id) ON DELETE SET NULL",
            "ALTER TABLE billing_connections ADD COLUMN last_error TEXT",
            "ALTER TABLE apps ADD COLUMN pause_endpoint_url VARCHAR(512)",
            "ALTER TABLE teams ADD COLUMN department VARCHAR(256)",
            "ALTER TABLE thresholds ADD COLUMN user_id UUID REFERENCES users(id) ON DELETE SET NULL",
            "ALTER TABLE thresholds ADD COLUMN cost_center_id UUID REFERENCES cost_centers(id) ON DELETE SET NULL",
            "ALTER TABLE thresholds ADD COLUMN degradation_model VARCHAR(128)",
            "ALTER TABLE thresholds ADD COLUMN degradation_enabled BOOLEAN DEFAULT 0",
            # Scale Pack: SDK-side aggregation columns for usage_aggregates
            "ALTER TABLE usage_aggregates ADD COLUMN input_cost NUMERIC(18,8) DEFAULT 0",
            "ALTER TABLE usage_aggregates ADD COLUMN output_cost NUMERIC(18,8) DEFAULT 0",
            "ALTER TABLE usage_aggregates ADD COLUMN min_duration_ms INTEGER",
            "ALTER TABLE usage_aggregates ADD COLUMN max_duration_ms INTEGER",
            "ALTER TABLE usage_aggregates ADD COLUMN duration_ms_sum BIGINT",
            "ALTER TABLE usage_aggregates ADD COLUMN source VARCHAR(16)",
            # Phase 8a: PQC columns on enforcement_attestations
            "ALTER TABLE enforcement_attestations ADD COLUMN pqc_signature TEXT",
            "ALTER TABLE enforcement_attestations ADD COLUMN pqc_algorithm VARCHAR(64)",
            "ALTER TABLE enforcement_attestations ADD COLUMN pqc_public_key_id VARCHAR(64)",
            # Topology: environment auto-detection and K8s flavor
            "ALTER TABLE app_topology ADD COLUMN detected_environment VARCHAR(32)",
            "ALTER TABLE app_topology ADD COLUMN k8s_flavor VARCHAR(32)",
            # Audit hash chain (tamper-evidence)
            "ALTER TABLE audit_log ADD COLUMN chain_seq BIGINT",
            "ALTER TABLE audit_log ADD COLUMN prev_hash VARCHAR(64)",
            "ALTER TABLE audit_log ADD COLUMN entry_hash VARCHAR(64)",
        ]
        async with _engine.begin() as conn:
            for stmt in _additive_migrations:
                try:
                    await conn.execute(text(stmt))
                except (OperationalError, ProgrammingError):
                    pass  # Column already exists
        logger.info("SQLite tables created/verified (WAL mode enabled)")
    else:
        logger.info(
            "Database pool ready",
            extra={
                "pool_size": settings.db_pool_size,
                "max_overflow": settings.db_max_overflow,
            },
        )

    # ── Read replica (Phase 4d) ───────────────────────────────────────────────
    global _replica_engine, _replica_session_factory
    if settings.replica_database_url and not settings.is_sqlite:
        logger.info("Initialising read replica: %s", settings.replica_database_url[:40] + "...")
        _replica_engine = create_async_engine(
            settings.replica_database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout,
            pool_recycle=settings.db_pool_recycle,
            pool_pre_ping=True,
            echo=settings.debug,
        )
        _replica_session_factory = async_sessionmaker(
            _replica_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        logger.info("Read replica pool ready")


async def close_db() -> None:
    """Dispose the connection pool. Called in FastAPI shutdown lifespan."""
    global _engine, _replica_engine
    if _replica_engine is not None:
        logger.info("Closing read replica pool")
        await _replica_engine.dispose()
        _replica_engine = None
    if _engine is not None:
        logger.info("Closing database connection pool")
        await _engine.dispose()
        _engine = None


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that yields a database session per request.

    Usage:
        @router.get("/")
        async def handler(db: AsyncSession = Depends(get_session, scope="function")):
            ...

    The session is committed on success and rolled back on exception.
    The connection is always returned to the pool on exit.

    Always declare it as ``Depends(get_session, scope="function")``. With the
    default "request" scope FastAPI runs the code after ``yield`` only once the
    response has been sent, so a client would receive 201/200 before the commit
    -- an immediate follow-up request could miss the write, and a failed commit
    would go unreported. tests/test_commit_before_response.py enforces this.
    """
    if _session_factory is None:
        raise RuntimeError("Session factory not initialised. Call init_db() first.")

    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def get_session_ctx() -> AsyncGenerator[AsyncSession, None]:
    """
    Async context manager for database sessions outside FastAPI dependency injection.
    Use this in background tasks and service-layer code.

    Usage:
        async with get_session_ctx() as db:
            result = await db.execute(select(Model))
    """
    if _session_factory is None:
        raise RuntimeError("Session factory not initialised. Call init_db() first.")

    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_read_session() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency for read-only queries (dashboard, aggregation reads).

    Routes to read replica if configured (MODUS_REPLICA_DATABASE_URL),
    otherwise falls back to the primary database. This allows dashboard
    queries to avoid contending with write-heavy ingest operations.
    """
    factory = _replica_session_factory or _session_factory
    if factory is None:
        raise RuntimeError("Session factory not initialised. Call init_db() first.")

    async with factory() as session:
        try:
            yield session
        finally:
            await session.close()
