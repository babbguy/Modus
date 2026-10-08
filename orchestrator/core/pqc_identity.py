"""
Modus — Agent Identity Engine (HMAC shim)
=============================================
Copyright 2026 babbguy
SPDX-License-Identifier: Apache-2.0

Generates and manages symmetric-key identities for AI agents using HKDF
key derivation and HMAC-SHA-256. Note: this is a symmetric-MAC shim, NOT a
public-key or post-quantum scheme — the ML-KEM-768 name refers only to the
future primitive this shim stands in for; "signatures" here are HMAC tags
that are not publicly verifiable. Keys are derived deterministically from a
platform root key, so no key material needs to be stored or transmitted —
only the fingerprint and public key hash are persisted.

Stdlib only. Zero dependencies beyond attestation_engine (HKDF).
All key operations target < 1ms.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.attestation_engine import _hkdf_derive
from orchestrator.db.models import AgentIdentity

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

_DOMAIN_AGENT_KEY = b"modus-agent-identity-v1"
_DOMAIN_AGENT_SIGN = b"modus-agent-sign-v1"
_KEY_LENGTH = 32  # 256-bit derived keys


# ── Key Generation ───────────────────────────────────────────────────────────


def generate_agent_keypair(
    agent_fingerprint: str,
    platform_root_key: bytes,
) -> dict:
    """
    Derive an agent-specific keypair from the platform root key via HKDF.

    Since ML-KEM is shimmed as symmetric HMAC, the "public key" is represented
    by its SHA-256 hash. The derived key itself is never persisted — it can be
    re-derived deterministically from the same inputs.

    Returns a dict with: agent_fingerprint, public_key_hash, key_algorithm,
    platform_chain_hash, derived_key.
    """
    # Guard clauses
    if not agent_fingerprint:
        raise ValueError("agent_fingerprint must be non-empty")
    if not platform_root_key or len(platform_root_key) < 16:
        raise ValueError("platform_root_key must be at least 16 bytes")

    # Derive agent-specific key via HKDF with domain separation
    derived_key: bytes = _hkdf_derive(
        platform_root_key,
        info=_DOMAIN_AGENT_KEY + b":" + agent_fingerprint.encode(),
        length=_KEY_LENGTH,
    )

    # Public key hash: SHA-256 of derived key (verifiable identifier)
    public_key_hash: str = hashlib.sha256(derived_key).hexdigest()

    # Platform chain hash: ties identity to platform root
    platform_chain_hash: str = hashlib.sha256(
        platform_root_key + agent_fingerprint.encode()
    ).hexdigest()

    return {
        "agent_fingerprint": agent_fingerprint,
        "public_key_hash": public_key_hash,
        "key_algorithm": "hmac-shim-v1",
        "platform_chain_hash": platform_chain_hash,
        "derived_key": derived_key,
    }


# ── Signing ──────────────────────────────────────────────────────────────────


def sign_with_agent_key(
    payload: str,
    agent_fingerprint: str,
    platform_root_key: bytes,
) -> dict:
    """
    Sign a payload string using the agent's derived key.

    Uses HMAC-SHA256 with domain separation (_DOMAIN_AGENT_SIGN) to prevent
    cross-protocol signature reuse.

    Returns a signature bundle with: signature, agent_fingerprint, algorithm,
    signed_at.
    """
    if not agent_fingerprint:
        raise ValueError("agent_fingerprint must be non-empty")
    if not platform_root_key or len(platform_root_key) < 16:
        raise ValueError("platform_root_key must be at least 16 bytes")

    # Re-derive agent key (deterministic — same as generate_agent_keypair)
    derived_key: bytes = _hkdf_derive(
        platform_root_key,
        info=_DOMAIN_AGENT_KEY + b":" + agent_fingerprint.encode(),
        length=_KEY_LENGTH,
    )

    # HMAC-SHA256 with domain separation
    signature: str = hmac.new(
        derived_key,
        _DOMAIN_AGENT_SIGN + b":" + payload.encode(),
        hashlib.sha256,
    ).hexdigest()

    return {
        "signature": signature,
        "agent_fingerprint": agent_fingerprint,
        "algorithm": "hmac-shim-v1",
        "signed_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Verification ─────────────────────────────────────────────────────────────


def verify_agent_signature(
    payload: str,
    sig_bundle: dict,
    platform_root_key: bytes,
) -> bool:
    """
    Verify a signature bundle against the given payload and platform root key.

    Re-derives the agent key from the fingerprint in sig_bundle, recomputes
    the HMAC, and uses constant-time comparison.

    Returns True if valid, False on any error (malformed bundle, wrong key,
    tampered payload, etc.). Never raises.
    """
    try:
        if not payload or not sig_bundle or not platform_root_key:
            return False

        fingerprint: str = sig_bundle.get("agent_fingerprint", "")
        signature: str = sig_bundle.get("signature", "")

        if not fingerprint or not signature:
            return False

        if len(platform_root_key) < 16:
            return False

        # Re-derive agent key
        derived_key: bytes = _hkdf_derive(
            platform_root_key,
            info=_DOMAIN_AGENT_KEY + b":" + fingerprint.encode(),
            length=_KEY_LENGTH,
        )

        # Recompute expected signature
        expected: str = hmac.new(
            derived_key,
            _DOMAIN_AGENT_SIGN + b":" + payload.encode(),
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(expected, signature)

    except Exception:
        logger.debug("Agent signature verification failed", exc_info=True)
        return False


# ── DB Identity Management ───────────────────────────────────────────────────


def _make_fingerprint(team_id: str, app_id: str, platform_root_key: bytes) -> str:
    """Generate a deterministic fingerprint from team, app, and platform key."""
    key_hash: str = hashlib.sha256(platform_root_key).hexdigest()
    return hashlib.sha256(
        (team_id + app_id + key_hash).encode()
    ).hexdigest()


def _identity_to_dict(identity: AgentIdentity) -> dict:
    """Convert an AgentIdentity ORM object to a plain dict."""
    return {
        "id": identity.id,
        "team_id": identity.team_id,
        "app_id": identity.app_id,
        "agent_fingerprint": identity.agent_fingerprint,
        "public_key_hash": identity.public_key_hash,
        "key_algorithm": identity.key_algorithm,
        "platform_chain_hash": identity.platform_chain_hash,
        "created_at": identity.created_at.isoformat() if identity.created_at else None,
        "rotated_at": identity.rotated_at.isoformat() if identity.rotated_at else None,
        "revoked_at": identity.revoked_at.isoformat() if identity.revoked_at else None,
    }


async def ensure_agent_identity(
    db: AsyncSession,
    team_id: str,
    app_id: str,
    platform_root_key: bytes,
) -> dict:
    """
    Ensure an agent identity exists for the given team/app combination.

    If a non-revoked identity already exists, return it. Otherwise, generate
    a new keypair and persist the identity to the database.

    Returns a dict with the identity fields.
    """
    # Guard clauses
    if not team_id or not app_id:
        raise ValueError("team_id and app_id must be non-empty")
    if not platform_root_key or len(platform_root_key) < 16:
        raise ValueError("platform_root_key must be at least 16 bytes")

    fingerprint: str = _make_fingerprint(team_id, app_id, platform_root_key)

    try:
        # Check for existing non-revoked identity
        result = await db.execute(
            select(AgentIdentity).where(
                AgentIdentity.agent_fingerprint == fingerprint,
                AgentIdentity.revoked_at.is_(None),
            )
        )
        existing: Optional[AgentIdentity] = result.scalars().first()

        if existing is not None:
            return _identity_to_dict(existing)

        # Generate new keypair
        keypair: dict = generate_agent_keypair(fingerprint, platform_root_key)

        identity = AgentIdentity(
            team_id=team_id,
            app_id=app_id,
            agent_fingerprint=fingerprint,
            public_key_hash=keypair["public_key_hash"],
            key_algorithm=keypair["key_algorithm"],
            platform_chain_hash=keypair["platform_chain_hash"],
        )
        db.add(identity)
        await db.flush()

        logger.info(
            "Created agent identity fingerprint=%s team=%s app=%s",
            fingerprint[:16],
            team_id[:8],
            app_id[:8],
        )

        return _identity_to_dict(identity)

    except ValueError:
        raise
    except Exception:
        logger.error("Failed to ensure agent identity", exc_info=True)
        raise


async def rotate_agent_identity(
    db: AsyncSession,
    team_id: str,
    app_id: str,
    platform_root_key: bytes,
) -> dict:
    """
    Rotate an agent's identity by revoking the existing one and creating
    a new identity with a fresh fingerprint (using current timestamp as
    entropy).

    Returns the new identity dict.
    """
    # Guard clauses
    if not team_id or not app_id:
        raise ValueError("team_id and app_id must be non-empty")
    if not platform_root_key or len(platform_root_key) < 16:
        raise ValueError("platform_root_key must be at least 16 bytes")

    now = datetime.now(timezone.utc)

    try:
        # Find and revoke existing identity
        _make_fingerprint(team_id, app_id, platform_root_key)
        result = await db.execute(
            select(AgentIdentity).where(
                AgentIdentity.team_id == team_id,
                AgentIdentity.app_id == app_id,
                AgentIdentity.revoked_at.is_(None),
            )
        )
        existing: Optional[AgentIdentity] = result.scalars().first()

        if existing is not None:
            existing.revoked_at = now
            logger.info(
                "Revoked agent identity fingerprint=%s",
                existing.agent_fingerprint[:16],
            )

        # Generate new fingerprint using timestamp as entropy
        timestamp_entropy: str = str(now.timestamp())
        key_hash: str = hashlib.sha256(platform_root_key).hexdigest()
        new_fingerprint: str = hashlib.sha256(
            (team_id + app_id + key_hash + timestamp_entropy).encode()
        ).hexdigest()

        # Generate new keypair with the new fingerprint
        keypair: dict = generate_agent_keypair(new_fingerprint, platform_root_key)

        new_identity = AgentIdentity(
            team_id=team_id,
            app_id=app_id,
            agent_fingerprint=new_fingerprint,
            public_key_hash=keypair["public_key_hash"],
            key_algorithm=keypair["key_algorithm"],
            platform_chain_hash=keypair["platform_chain_hash"],
        )
        db.add(new_identity)
        await db.flush()

        logger.info(
            "Created rotated agent identity fingerprint=%s team=%s app=%s",
            new_fingerprint[:16],
            team_id[:8],
            app_id[:8],
        )

        return _identity_to_dict(new_identity)

    except ValueError:
        raise
    except Exception:
        logger.error("Failed to rotate agent identity", exc_info=True)
        raise


async def get_agent_identity(
    db: AsyncSession,
    app_id: str,
) -> dict | None:
    """
    Retrieve the active (non-revoked) agent identity for a given app.

    Returns a dict with identity fields, or None if no active identity exists.
    """
    if not app_id:
        return None

    try:
        result = await db.execute(
            select(AgentIdentity).where(
                AgentIdentity.app_id == app_id,
                AgentIdentity.revoked_at.is_(None),
            )
        )
        identity: Optional[AgentIdentity] = result.scalars().first()

        if identity is None:
            return None

        return _identity_to_dict(identity)

    except Exception:
        logger.error("Failed to get agent identity for app=%s", app_id, exc_info=True)
        return None
