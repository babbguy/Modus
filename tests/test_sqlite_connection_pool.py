# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0
"""
Tests -- a SQLite file database reuses its connections.

The SQLite engine used NullPool, so every session opened a new aiosqlite
connection (a thread, a file handle and five PRAGMAs). On a 1-CPU server that
tripled the CPU cost of /api/v1/policy/evaluate, and the release gate saw SDK
evaluate calls pass their 3 s timeout. In-memory databases keep NullPool,
because each connection to ``:memory:`` is a different database.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import event, text
from sqlalchemy.pool import NullPool

from orchestrator.core.config import get_settings
from orchestrator.db.session import build_engine

pytestmark = pytest.mark.asyncio


async def test_file_database_reuses_connections(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "database_url", f"sqlite+aiosqlite:///{tmp_path / 'pool.db'}")
    engine = build_engine()
    opened = []
    event.listen(engine.sync_engine, "connect", lambda *_: opened.append(1))
    try:
        assert not isinstance(engine.pool, NullPool)
        for _ in range(20):
            async with engine.connect() as conn:
                assert (await conn.execute(text("SELECT 1"))).scalar() == 1
        assert len(opened) == 1
    finally:
        await engine.dispose()


async def test_file_database_pool_serves_concurrent_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "database_url", f"sqlite+aiosqlite:///{tmp_path / 'pool2.db'}")
    engine = build_engine()
    try:
        async def one(i: int) -> int:
            async with engine.connect() as conn:
                return (await conn.execute(text(f"SELECT {i}"))).scalar()
        assert await asyncio.gather(*[one(i) for i in range(12)]) == list(range(12))
    finally:
        await engine.dispose()


async def test_in_memory_database_keeps_nullpool(monkeypatch):
    monkeypatch.setattr(get_settings(), "database_url", "sqlite+aiosqlite:///:memory:")
    engine = build_engine()
    try:
        assert isinstance(engine.pool, NullPool)
    finally:
        await engine.dispose()
