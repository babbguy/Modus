"""
Modus — bcrypt checks for issued secrets
=========================================
Copyright 2026 babbguy
SPDX-License-Identifier: FSL-1.1-ALv2

App keys, team registration tokens and invitation tokens are stored as bcrypt
hashes. bcrypt reads at most 72 bytes of input; since bcrypt 5 it raises
ValueError for longer input instead of silently truncating it. Every secret
Modus issues is shorter than 72 bytes, so a longer presented secret cannot be
one of them and is rejected like any other wrong secret.
"""
from __future__ import annotations

import bcrypt

BCRYPT_MAX_BYTES = 72


def secret_matches(secret: str, hashed: str) -> bool:
    """Whether ``secret`` matches the stored bcrypt hash ``hashed``.

    Returns False for a secret longer than bcrypt can check. A malformed stored
    hash still raises ValueError. Slow by design: call it via asyncio.to_thread.
    """
    raw = secret.encode()
    if len(raw) > BCRYPT_MAX_BYTES:
        return False
    return bcrypt.checkpw(raw, hashed.encode())
