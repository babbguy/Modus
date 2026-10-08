"""
Modus Federator — Database Session
=========================================
Async SQLAlchemy session factory with auto-migration on startup.
Same pattern as orchestrator/db/session.py.
"""
from __future__ import annotations

import logging
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from federator.config import settings
from federator.db.models import Base

logger = logging.getLogger(__name__)

_engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_pre_ping=True,
)

async_session_factory = async_sessionmaker(
    _engine, class_=AsyncSession, expire_on_commit=False,
)


async def init_db() -> None:
    """Create all tables on startup (idempotent)."""
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Federator database initialized")


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields a session with auto-commit on success."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_read_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — read-only session (no commit)."""
    async with async_session_factory() as session:
        yield session
