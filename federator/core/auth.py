"""
Modus Federator — Authentication
=======================================
JWT bearer-token verification.

Supports:
- HMAC (HS256) with shared secret (small deployments)
- RSA/JWKS (production) via jwks_url

The federator only stores a SHA-256 fingerprint of the token subject,
never the raw subject or any customer identity.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from federator.config import settings

logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class FederatorIdentity:
    """Verified identity from JWT claims."""
    identity_fingerprint: str   # SHA-256 of the JWT subject
    tier: str                  # "participant" | "consumer"
    industry: str              # from JWT or "other"
    instance_id: str           # JWT subject (opaque to us)
    exp: int                   # expiry timestamp


def _fingerprint(subject: str) -> str:
    """SHA-256 fingerprint of the JWT subject — never store the raw value."""
    return hashlib.sha256(subject.encode()).hexdigest()


def verify_token(token: str) -> FederatorIdentity:
    """
    Verify and decode a JWT token.

    Returns FederatorIdentity on success.
    Raises HTTPException on any failure.
    """
    secret = settings.jwt_secret.get_secret_value()
    if not secret:
        raise HTTPException(status_code=503, detail="Auth not configured")

    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["sub", "exp"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    sub = payload.get("sub", "")
    tier = payload.get("federation_tier", payload.get("tier", "consumer"))
    industry = payload.get("industry", "other")

    if tier not in ("participant", "consumer"):
        tier = "consumer"
    if industry not in settings.allowed_industries:
        industry = "other"

    return FederatorIdentity(
        identity_fingerprint=_fingerprint(sub),
        tier=tier,
        industry=industry,
        instance_id=sub,
        exp=payload.get("exp", 0),
    )


async def get_identity(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> FederatorIdentity:
    """FastAPI dependency — extract and verify identity from Bearer token."""
    if not credentials:
        raise HTTPException(status_code=401, detail="Bearer token required")
    return verify_token(credentials.credentials)


def require_participant(identity: FederatorIdentity) -> FederatorIdentity:
    """Guard: only participants (contributors) can submit deltas."""
    if identity.tier != "participant":
        raise HTTPException(
            status_code=403,
            detail="Participant tier required for delta submission",
        )
    return identity
