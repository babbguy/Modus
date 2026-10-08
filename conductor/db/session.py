"""
Modus Conductor — Database Session Management
=====================================================
Async SQLAlchemy engine and session factory for the Conductor's own database.

Same patterns as the Orchestrator session module:
  - PostgreSQL (production): asyncpg driver, connection pool tuning
  - SQLite (dev/single-file): aiosqlite driver, NullPool, WAL mode
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from conductor.core.config import settings

logger = logging.getLogger(__name__)


def sqlite_dt(dt: datetime) -> str:
    """Format a datetime for safe SQLite string comparison.

    Same helper as the Orchestrator — ensures T-separator format
    for lexicographic comparison accuracy.
    """
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def build_engine() -> AsyncEngine:
    if settings.is_sqlite:
        from sqlalchemy.pool import NullPool
        logger.info("Using SQLite backend: %s", settings.database_url)
        return create_async_engine(
            settings.database_url,
            echo=settings.debug,
            connect_args={"check_same_thread": False},
            poolclass=NullPool,
        )

    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=True,
        echo=settings.debug,
    )


async def init_db() -> None:
    """Initialise the Conductor's database engine and session factory."""
    global _engine, _session_factory

    logger.info("Initialising Conductor database")
    _engine = build_engine()
    _session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    if settings.is_sqlite:
        from conductor.db.models import Base
        from sqlalchemy import event

        @event.listens_for(_engine.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA cache_size=-64000")
            cursor.execute("PRAGMA temp_store=MEMORY")
            cursor.close()

        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        logger.info("Conductor SQLite tables created/verified (WAL mode enabled)")
    else:
        # The conductor has no Alembic chain; create_all (checkfirst) is its
        # schema mechanism on PostgreSQL too — without this, a fresh Postgres
        # deploy comes up with an empty schema.
        from conductor.db.models import Base

        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        logger.info(
            "Conductor PostgreSQL tables created/verified",
            extra={
                "pool_size": settings.db_pool_size,
                "max_overflow": settings.db_max_overflow,
            },
        )


async def close_db() -> None:
    global _engine
    if _engine is not None:
        logger.info("Closing Conductor database connection pool")
        await _engine.dispose()
        _engine = None


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session per request."""
    if _session_factory is None:
        raise RuntimeError("Conductor session factory not initialised. Call init_db() first.")

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
    """Async context manager for sessions outside FastAPI DI (background tasks)."""
    if _session_factory is None:
        raise RuntimeError("Conductor session factory not initialised. Call init_db() first.")

    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
