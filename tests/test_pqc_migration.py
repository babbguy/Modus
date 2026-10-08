"""
Tests for orchestrator.core.pqc_migration — attestation migration and audit logging.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from orchestrator.core.pqc_migration import (
    _MIN_BATCH_DELAY_S,
    migrate_attestations,
    migration_stats,
    verify_migrated_attestations,
)
from orchestrator.db.models import Base, EnforcementAttestation, PQCMigrationLog


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def pqc_engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def pqc_session(pqc_engine):
    factory = async_sessionmaker(pqc_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


# ── Helpers ──────────────────────────────────────────────────────────────────


def _mock_signer():
    signer = MagicMock()
    signer.algorithm = "dilithium3-shim"
    signer.sign.return_value = {
        "pqc_signature": "sig-abc123",
        "pqc_algorithm": "dilithium3-shim",
        "pqc_public_key_id": "key-1",
    }
    signer.verify.return_value = True
    return signer


async def _add_attestation(db, *, payload="test-payload", pqc_sig=None):
    att = EnforcementAttestation(
        id=str(uuid.uuid4()),
        decision_id=str(uuid.uuid4()),
        attestation_hash="a" * 64,
        nonce="n" * 32,
        payload_json=payload,
        signature="old-sig",
        algorithm="hmac-sha256",
        pqc_signature=pqc_sig,
    )
    db.add(att)
    await db.flush()
    return att


# ── migrate_attestations ─────────────────────────────────────────────────────


class TestMigrateAttestations:
    @pytest.mark.asyncio
    async def test_migrates_unsigned(self, pqc_session):
        await _add_attestation(pqc_session)
        signer = _mock_signer()

        result = await migrate_attestations(pqc_session, signer, batch_size=10, max_per_run=10)

        assert result["migrated"] == 1
        assert result["errors"] == 0
        assert result["elapsed_s"] > 0

    @pytest.mark.asyncio
    async def test_skips_already_signed(self, pqc_session):
        await _add_attestation(pqc_session, pqc_sig="existing-sig")
        signer = _mock_signer()

        result = await migrate_attestations(pqc_session, signer, batch_size=10, max_per_run=10)
        assert result["migrated"] == 0

    @pytest.mark.asyncio
    async def test_skips_empty_payload(self, pqc_session):
        await _add_attestation(pqc_session, payload="")
        signer = _mock_signer()

        result = await migrate_attestations(pqc_session, signer, batch_size=10, max_per_run=10)
        assert result["skipped"] == 1
        assert result["migrated"] == 0

    @pytest.mark.asyncio
    async def test_invalid_batch_size(self, pqc_session):
        signer = _mock_signer()
        with pytest.raises(ValueError, match="batch_size"):
            await migrate_attestations(pqc_session, signer, batch_size=0)

    @pytest.mark.asyncio
    async def test_max_per_run_caps(self, pqc_session):
        for _ in range(5):
            await _add_attestation(pqc_session)
        signer = _mock_signer()

        result = await migrate_attestations(pqc_session, signer, batch_size=2, max_per_run=3)
        assert result["migrated"] == 3

    @pytest.mark.asyncio
    async def test_writes_audit_log(self, pqc_session):
        await _add_attestation(pqc_session)
        signer = _mock_signer()

        await migrate_attestations(pqc_session, signer, batch_size=10, max_per_run=10)

        logs = (await pqc_session.execute(select(PQCMigrationLog))).scalars().all()
        assert len(logs) == 1
        assert logs[0].new_algorithm == "dilithium3-shim"


# ── verify_migrated_attestations ─────────────────────────────────────────────


class TestVerifyMigratedAttestations:
    @pytest.mark.asyncio
    async def test_verify_passes(self, pqc_session):
        att = await _add_attestation(pqc_session, pqc_sig="sig")
        # Also set pqc_algorithm
        att.pqc_algorithm = "dilithium3-shim"
        await pqc_session.flush()

        signer = _mock_signer()
        result = await verify_migrated_attestations(pqc_session, signer, batch_size=10, max_per_run=10)
        assert result["verified"] == 1
        assert result["failed"] == 0

    @pytest.mark.asyncio
    async def test_verify_fails(self, pqc_session):
        att = await _add_attestation(pqc_session, pqc_sig="bad-sig")
        att.pqc_algorithm = "dilithium3-shim"
        await pqc_session.flush()

        signer = _mock_signer()
        signer.verify.return_value = False
        result = await verify_migrated_attestations(pqc_session, signer, batch_size=10, max_per_run=10)
        assert result["failed"] == 1


# ── migration_stats ──────────────────────────────────────────────────────────


class TestMigrationStats:
    @pytest.mark.asyncio
    async def test_empty_db(self, pqc_session):
        stats = await migration_stats(pqc_session)
        assert stats["total"] == 0
        assert stats["migrated"] == 0
        assert stats["pending"] == 0
        assert stats["percent"] == 0.0

    @pytest.mark.asyncio
    async def test_mixed(self, pqc_session):
        await _add_attestation(pqc_session)  # pending
        await _add_attestation(pqc_session, pqc_sig="done")  # migrated
        await pqc_session.flush()

        stats = await migration_stats(pqc_session)
        assert stats["total"] == 2
        assert stats["migrated"] == 1
        assert stats["pending"] == 1
        assert stats["percent"] == 50.0


# ── Constants ────────────────────────────────────────────────────────────────


class TestConstants:
    def test_min_batch_delay(self):
        assert _MIN_BATCH_DELAY_S == 0.05
