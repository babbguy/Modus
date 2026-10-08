"""
Modus — Session Tokens
===============================
Short-lived agent session tokens. Issued by POST /api/v1/self-register,
used for all subsequent agent API calls (ingest, evaluate, topology, heartbeat).

Format:  mst_{base64url(payload_json)}.{base64url(hmac_sha256)}

Payload: {"a": "<app_uuid>", "t": "<team_id>", "e": <unix_expiry>}

Verification is pure CPU — base64 decode + HMAC compare + expiry check.
No database read. Target: <0.1ms per verification.

Why not JWT?
    Same security, no external dependency. The 'a' and 't' claims
    are all we need. No need for the full JWT machinery.

Security properties:
    - HMAC-SHA256 with a secret derived from master_api_key.
      Forging a token requires knowing master_api_key.
    - Expiry is embedded and checked server-side. Clients cannot
      extend tokens.
    - Token is bound to (app_uuid, team_id). Tokens from one app
      cannot be used for another.
    - Prefix 'mst_' distinguishes session tokens from stable 'mds_' keys,
      enabling fast routing in the verification dispatcher.

Secret derivation:
    session_secret = HMAC-SHA256(key=master_api_key, msg="modus-session-v1")
    This means rotating master_api_key invalidates all session tokens,
    which is the correct behaviour — treat it like rotating a signing key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Optional

from orchestrator.core.config import settings

SESSION_TOKEN_PREFIX = "mst_"
DEFAULT_TTL_SECONDS = 86_400  # 24 hours


# Cache the signing key at module load — master_api_key never changes at runtime.
_KEY: Optional[bytes] = None


def _key() -> bytes:
    """Derive and cache the HMAC signing key from master_api_key."""
    global _KEY
    if _KEY is None:
        _KEY = hmac.new(
            settings.master_api_key.encode(),
            b"modus-session-v1",
            hashlib.sha256,
        ).digest()
    return _KEY


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    pad = 4 - len(s) % 4
    if pad != 4:
        s += "=" * pad
    return base64.urlsafe_b64decode(s)


def generate(app_uuid: str, team_id: str, ttl: int = DEFAULT_TTL_SECONDS) -> str:
    """
    Generate a signed session token for an app.
    Safe to call on every self-register — each call produces a distinct token
    due to the embedded random nonce.
    """
    import secrets as _secrets
    payload = json.dumps(
        {
            "a": app_uuid,
            "t": team_id,
            "e": int(time.time()) + ttl,
            "n": _secrets.token_hex(8),   # nonce — ensures uniqueness within same second
        },
        separators=(",", ":"),
    ).encode()
    payload_b64 = _b64(payload)
    sig = hmac.new(_key(), payload_b64.encode(), hashlib.sha256).digest()
    return f"{SESSION_TOKEN_PREFIX}{payload_b64}.{_b64(sig)}"


class SessionTokenError(ValueError):
    pass


def verify(token: str) -> tuple[str, str]:
    """
    Verify and decode a session token.
    Returns (app_uuid, team_id) on success.
    Raises SessionTokenError on any failure (invalid format, bad sig, expired).

    Under 0.1ms on modern hardware. No I/O.
    """
    if not token.startswith(SESSION_TOKEN_PREFIX):
        raise SessionTokenError("Not a session token")

    body = token[len(SESSION_TOKEN_PREFIX):]
    parts = body.split(".", 1)
    if len(parts) != 2:
        raise SessionTokenError("Malformed token")

    payload_b64, sig_b64 = parts

    # Constant-time HMAC verification — prevents timing attacks
    expected_sig = hmac.new(_key(), payload_b64.encode(), hashlib.sha256).digest()
    try:
        actual_sig = _unb64(sig_b64)
    except Exception:
        raise SessionTokenError("Invalid signature encoding")

    if not hmac.compare_digest(expected_sig, actual_sig):
        raise SessionTokenError("Invalid signature")

    try:
        payload = json.loads(_unb64(payload_b64))
    except Exception:
        raise SessionTokenError("Invalid payload")

    if int(time.time()) > payload.get("e", 0):
        raise SessionTokenError("Token expired")

    return payload["a"], payload["t"]


def is_session_token(raw: str) -> bool:
    return raw.startswith(SESSION_TOKEN_PREFIX)
