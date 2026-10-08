#!/usr/bin/env python3
# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0
"""Fail if the migrated PostgreSQL schema differs from the ORM models.

Run after ``alembic upgrade heads`` against an otherwise empty database:

    MODUS_DATABASE_URL=postgresql+asyncpg://... python scripts/check_migrations.py

Exit status is 0 when the schema produced by the migrations matches
``orchestrator.db.models`` exactly, and 1 (with each difference printed) when
a model change has no matching migration.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from orchestrator.db.models import Base  # noqa: E402


def _diff(sync_conn) -> list:
    ctx = MigrationContext.configure(sync_conn, opts={"compare_type": True})
    return compare_metadata(ctx, Base.metadata)


async def main() -> int:
    url = os.environ.get("MODUS_DATABASE_URL", "")
    if not url.startswith("postgresql"):
        print("MODUS_DATABASE_URL must point at a PostgreSQL database.", file=sys.stderr)
        return 2
    url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            diffs = await conn.run_sync(_diff)
    finally:
        await engine.dispose()

    if not diffs:
        print("Migrations match the ORM models.")
        return 0
    print(f"{len(diffs)} difference(s) between migrations and models:")
    for diff in diffs:
        print(f"  {diff}")
    print("Generate a migration: alembic revision --autogenerate -m '...'")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
