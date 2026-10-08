"""
Tests for orchestrator.core.pqc_identity — PQC-rooted agent identity engine.

Covers: key generation, signing, verification, and async DB operations
(ensure, rotate, get) using mocked AsyncSession.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core.pqc_identity import (
    _KEY_LENGTH,
    _make_fingerprint,
    ensure_agent_identity,
    generate_agent_keypair,
    get_agent_identity,
    rotate_agent_identity,
    sign_with_agent_key,
    verify_agent_signature,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

_ROOT_KEY = b"test-platform-root-key-32-bytes!"  # 32 bytes
_SHORT_KEY = b"short"  # < 16 bytes
_FINGERPRINT = "abc123def456"
_ALT_FINGERPRINT = "xyz789uvw012"
_TEAM_ID = "team-0001-0001-0001"
_APP_ID = "app-0001-0001-0001"


def _mock_identity(
    fingerprint: str = _FINGERPRINT,
    revoked: bool = False,
) -> MagicMock:
    """Create a mock AgentIdentity ORM object."""
    identity = MagicMock()
    identity.id = "id-0001"
    identity.team_id = _TEAM_ID
    identity.app_id = _APP_ID
    identity.agent_fingerprint = fingerprint
    identity.public_key_hash = hashlib.sha256(b"mock-key").hexdigest()
    identity.key_algorithm = "hmac-shim-v1"
    identity.platform_chain_hash = hashlib.sha256(b"mock-chain").hexdigest()
    identity.created_at = datetime(2026, 3, 14, tzinfo=timezone.utc)
    identity.rotated_at = None
    identity.revoked_at = datetime(2026, 3, 14, tzinfo=timezone.utc) if revoked else None
    return identity


def _mock_db_session(existing_identity=None) -> AsyncMock:
    """Create a mock AsyncSession that returns the given identity on query."""
    db = AsyncMock()
    result = MagicMock()
    scalars = MagicMock()
    scalars.first.return_value = existing_identity
    result.scalars.return_value = scalars
    db.execute.return_value = result
    # session.add() is synchronous in SQLAlchemy — use MagicMock to avoid
    # "coroutine never awaited" warnings from AsyncMock.
    db.add = MagicMock()
    return db


# ── generate_agent_keypair ───────────────────────────────────────────────────


class TestGenerateKeypair:
    def test_generate_keypair_deterministic(self) -> None:
        """Same inputs always produce the same keypair."""
        kp1 = generate_agent_keypair(_FINGERPRINT, _ROOT_KEY)
        kp2 = generate_agent_keypair(_FINGERPRINT, _ROOT_KEY)

        assert kp1["public_key_hash"] == kp2["public_key_hash"]
        assert kp1["platform_chain_hash"] == kp2["platform_chain_hash"]
        assert kp1["derived_key"] == kp2["derived_key"]
        assert kp1["key_algorithm"] == "hmac-shim-v1"

    def test_generate_keypair_different_fingerprints(self) -> None:
        """Different fingerprints produce different keys."""
        kp1 = generate_agent_keypair(_FINGERPRINT, _ROOT_KEY)
        kp2 = generate_agent_keypair(_ALT_FINGERPRINT, _ROOT_KEY)

        assert kp1["public_key_hash"] != kp2["public_key_hash"]
        assert kp1["derived_key"] != kp2["derived_key"]
        assert kp1["platform_chain_hash"] != kp2["platform_chain_hash"]

    def test_generate_keypair_short_key_rejected(self) -> None:
        """Root key shorter than 16 bytes raises ValueError."""
        with pytest.raises(ValueError, match="at least 16 bytes"):
            generate_agent_keypair(_FINGERPRINT, _SHORT_KEY)

    def test_generate_keypair_empty_fingerprint_rejected(self) -> None:
        """Empty fingerprint raises ValueError."""
        with pytest.raises(ValueError, match="non-empty"):
            generate_agent_keypair("", _ROOT_KEY)

    def test_generate_keypair_none_key_rejected(self) -> None:
        """None root key raises ValueError."""
        with pytest.raises(ValueError, match="at least 16 bytes"):
            generate_agent_keypair(_FINGERPRINT, b"")

    def test_generate_keypair_fields_complete(self) -> None:
        """Returned dict has all expected fields."""
        kp = generate_agent_keypair(_FINGERPRINT, _ROOT_KEY)
        assert set(kp.keys()) == {
            "agent_fingerprint",
            "public_key_hash",
            "key_algorithm",
            "platform_chain_hash",
            "derived_key",
        }
        assert kp["agent_fingerprint"] == _FINGERPRINT
        assert len(kp["public_key_hash"]) == 64  # SHA-256 hex
        assert len(kp["platform_chain_hash"]) == 64
        assert len(kp["derived_key"]) == _KEY_LENGTH


# ── sign / verify ────────────────────────────────────────────────────────────


class TestSignAndVerify:
    def test_sign_and_verify_roundtrip(self) -> None:
        """Sign then verify succeeds."""
        payload = "enforce:allow:policy-42:2026-03-14T00:00:00Z"
        bundle = sign_with_agent_key(payload, _FINGERPRINT, _ROOT_KEY)

        assert verify_agent_signature(payload, bundle, _ROOT_KEY) is True
        assert bundle["algorithm"] == "hmac-shim-v1"
        assert bundle["agent_fingerprint"] == _FINGERPRINT
        assert "signed_at" in bundle

    def test_verify_wrong_payload_fails(self) -> None:
        """Modified payload fails verification."""
        payload = "enforce:allow:policy-42"
        bundle = sign_with_agent_key(payload, _FINGERPRINT, _ROOT_KEY)

        assert verify_agent_signature("tampered-payload", bundle, _ROOT_KEY) is False

    def test_verify_wrong_key_fails(self) -> None:
        """Different root key fails verification."""
        payload = "some-important-decision"
        bundle = sign_with_agent_key(payload, _FINGERPRINT, _ROOT_KEY)

        wrong_key = b"different-root-key-32-bytes!!!!!"
        assert verify_agent_signature(payload, bundle, wrong_key) is False

    def test_verify_malformed_bundle(self) -> None:
        """Bad sig_bundle returns False without crashing."""
        assert verify_agent_signature("data", {}, _ROOT_KEY) is False
        assert verify_agent_signature("data", {"garbage": True}, _ROOT_KEY) is False
        assert verify_agent_signature("data", None, _ROOT_KEY) is False  # type: ignore[arg-type]

    def test_verify_empty_inputs(self) -> None:
        """Empty strings / None returns False."""
        assert verify_agent_signature("", {"signature": "x", "agent_fingerprint": "y"}, _ROOT_KEY) is False
        assert verify_agent_signature("data", {"signature": "", "agent_fingerprint": ""}, _ROOT_KEY) is False

    def test_sign_empty_fingerprint_rejected(self) -> None:
        """Signing with empty fingerprint raises ValueError."""
        with pytest.raises(ValueError, match="non-empty"):
            sign_with_agent_key("payload", "", _ROOT_KEY)


# ── ensure_agent_identity ────────────────────────────────────────────────────


class TestEnsureIdentity:
    @pytest.mark.asyncio
    async def test_ensure_identity_creates_new(self) -> None:
        """Creates a new identity when none exists in DB."""
        db = _mock_db_session(existing_identity=None)

        await ensure_agent_identity(db, _TEAM_ID, _APP_ID, _ROOT_KEY)

        # Should have called db.add and db.flush
        assert db.add.called
        assert db.flush.called

        # The added object should be an AgentIdentity-like call
        added_obj = db.add.call_args[0][0]
        assert added_obj.team_id == _TEAM_ID
        assert added_obj.app_id == _APP_ID
        assert added_obj.key_algorithm == "hmac-shim-v1"
        assert len(added_obj.agent_fingerprint) == 64

    @pytest.mark.asyncio
    async def test_ensure_identity_returns_existing(self) -> None:
        """Returns existing non-revoked identity without creating a new one."""
        existing = _mock_identity()
        db = _mock_db_session(existing_identity=existing)

        result = await ensure_agent_identity(db, _TEAM_ID, _APP_ID, _ROOT_KEY)

        assert result["id"] == "id-0001"
        assert result["agent_fingerprint"] == _FINGERPRINT
        assert not db.add.called
        assert not db.flush.called

    @pytest.mark.asyncio
    async def test_ensure_identity_empty_team_rejected(self) -> None:
        """Empty team_id raises ValueError."""
        db = _mock_db_session()
        with pytest.raises(ValueError, match="non-empty"):
            await ensure_agent_identity(db, "", _APP_ID, _ROOT_KEY)

    @pytest.mark.asyncio
    async def test_ensure_identity_short_key_rejected(self) -> None:
        """Short root key raises ValueError."""
        db = _mock_db_session()
        with pytest.raises(ValueError, match="at least 16 bytes"):
            await ensure_agent_identity(db, _TEAM_ID, _APP_ID, _SHORT_KEY)


# ── rotate_agent_identity ────────────────────────────────────────────────────


class TestRotateIdentity:
    @pytest.mark.asyncio
    async def test_rotate_identity_revokes_old(self) -> None:
        """Old identity gets revoked_at set during rotation."""
        existing = _mock_identity()
        assert existing.revoked_at is None

        db = _mock_db_session(existing_identity=existing)

        await rotate_agent_identity(db, _TEAM_ID, _APP_ID, _ROOT_KEY)

        # Old identity should have been revoked
        assert existing.revoked_at is not None

        # New identity should have been added
        assert db.add.called
        assert db.flush.called
        new_obj = db.add.call_args[0][0]
        assert new_obj.team_id == _TEAM_ID
        assert new_obj.app_id == _APP_ID
        # New fingerprint should differ from old
        assert new_obj.agent_fingerprint != existing.agent_fingerprint

    @pytest.mark.asyncio
    async def test_rotate_identity_no_existing(self) -> None:
        """Rotation with no existing identity still creates a new one."""
        db = _mock_db_session(existing_identity=None)

        await rotate_agent_identity(db, _TEAM_ID, _APP_ID, _ROOT_KEY)

        assert db.add.called
        assert db.flush.called

    @pytest.mark.asyncio
    async def test_rotate_identity_empty_app_rejected(self) -> None:
        """Empty app_id raises ValueError."""
        db = _mock_db_session()
        with pytest.raises(ValueError, match="non-empty"):
            await rotate_agent_identity(db, _TEAM_ID, "", _ROOT_KEY)


# ── get_agent_identity ───────────────────────────────────────────────────────


class TestGetIdentity:
    @pytest.mark.asyncio
    async def test_get_identity_found(self) -> None:
        """Returns identity dict when active identity exists."""
        existing = _mock_identity()
        db = _mock_db_session(existing_identity=existing)

        result = await get_agent_identity(db, _APP_ID)

        assert result is not None
        assert result["id"] == "id-0001"
        assert result["app_id"] == _APP_ID

    @pytest.mark.asyncio
    async def test_get_identity_not_found(self) -> None:
        """Returns None when no active identity exists."""
        db = _mock_db_session(existing_identity=None)

        result = await get_agent_identity(db, _APP_ID)

        assert result is None

    @pytest.mark.asyncio
    async def test_get_identity_empty_app_id(self) -> None:
        """Returns None for empty app_id."""
        db = _mock_db_session()

        result = await get_agent_identity(db, "")

        assert result is None


# ── _make_fingerprint ────────────────────────────────────────────────────────


class TestMakeFingerprint:
    def test_fingerprint_deterministic(self) -> None:
        """Same inputs produce same fingerprint."""
        fp1 = _make_fingerprint(_TEAM_ID, _APP_ID, _ROOT_KEY)
        fp2 = _make_fingerprint(_TEAM_ID, _APP_ID, _ROOT_KEY)
        assert fp1 == fp2

    def test_fingerprint_different_inputs(self) -> None:
        """Different inputs produce different fingerprints."""
        fp1 = _make_fingerprint(_TEAM_ID, _APP_ID, _ROOT_KEY)
        fp2 = _make_fingerprint("other-team", _APP_ID, _ROOT_KEY)
        assert fp1 != fp2
