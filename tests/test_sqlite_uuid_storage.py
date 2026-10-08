"""
UUID columns on SQLite: every value round-trips, and the stored form is the
same hyphenated string the ORM returns, so hand-written SQL agrees with it.

(A column declared "UUID" gets NUMERIC affinity in SQLite, which destroyed
all-digit values; and the stock type stored 32 hex characters, which raw SQL
compared and returned in a different form from the ORM.)
"""
from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

ALL_DIGITS = "12345678-1234-4123-8123-123456789012"
DIGITS_AND_E = "12345678-1234-4123-8123-1234567e9012"


async def _roundtrip(base, model, values: dict, uuid_value: str) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(base.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            session.add(model(id=uuid_value, **values))
            await session.commit()
        async with factory() as session:
            stored_type = (await session.execute(
                text(f"SELECT typeof(id) FROM {model.__tablename__}")
            )).scalar_one()
            read_back = (await session.execute(select(model.id))).scalar_one()
    finally:
        await engine.dispose()
    assert stored_type == "text"
    assert read_back == uuid_value


@pytest.mark.asyncio
async def test_raw_sql_matches_orm_ids_on_sqlite():
    """Hand-written SQL binds and returns the same hyphenated form the ORM uses."""
    from orchestrator.db.models import Base, Team

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            team = Team(slug="raw", name="Raw")
            session.add(team)
            await session.commit()
            orm_id = team.id
        async with factory() as session:
            found = (await session.execute(
                text("SELECT id FROM teams WHERE id = :id"), {"id": orm_id}
            )).scalar_one_or_none()
    finally:
        await engine.dispose()
    assert found == orm_id


@pytest.mark.asyncio
@pytest.mark.parametrize("uuid_value", [ALL_DIGITS, DIGITS_AND_E])
async def test_orchestrator_uuid_columns_roundtrip_on_sqlite(uuid_value):
    from orchestrator.db.models import Base, Team

    await _roundtrip(Base, Team, {"slug": "t", "name": "T"}, uuid_value)


@pytest.mark.asyncio
@pytest.mark.parametrize("uuid_value", [ALL_DIGITS, DIGITS_AND_E])
async def test_conductor_uuid_columns_roundtrip_on_sqlite(uuid_value):
    from conductor.db.models import Base, OrchestratorNode

    await _roundtrip(Base, OrchestratorNode, {"name": "n", "instance_id": "i"}, uuid_value)


def test_postgresql_keeps_native_uuid():
    from orchestrator.db.models import Team

    ddl = str(CreateTable(Team.__table__).compile(dialect=postgresql.dialect()))
    assert "id UUID NOT NULL" in ddl
