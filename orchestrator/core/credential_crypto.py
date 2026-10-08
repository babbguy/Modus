"""
Modus — Credential Encryption-at-Rest Helper
================================================
Authenticated symmetric encryption for credentials stored at rest (billing
connection credentials, and any future secret material persisted to the
database).

Why this module exists
----------------------
The database may hold provider credentials (API keys, service-account JSON,
bearer tokens). Those values must never sit in the database as plaintext.
This helper encrypts them before they are written and decrypts them on the
rare occasion they must be used — while guaranteeing that:

* **Writes fail closed.** If no encryption key is configured, storing a new
  credential raises :class:`CredentialEncryptionError` rather than silently
  persisting plaintext. The operator is told to set ``MODUS_ENCRYPTION_KEY``.
* **Reads tolerate legacy plaintext.** Rows written before encryption was
  enabled (dev databases) are detected by the *absence* of the version marker
  and returned unchanged, with a warning logged. This keeps existing dev data
  usable without a migration.
* **Tampering is detected.** Fernet tokens are authenticated (HMAC-SHA256). A
  modified or truncated ciphertext raises :class:`CredentialDecryptionError`
  instead of returning garbage.

On-disk format
--------------
Encrypted values are prefixed with a version marker::

    enc:v1:<fernet-token>

The prefix makes encrypted values self-describing: reads can distinguish an
encrypted payload from a legacy plaintext row without guessing, and future
schemes can bump the version (``enc:v2:``) for key rotation without ambiguity.

Cipher & key derivation
------------------------
The underlying primitive is :class:`cryptography.fernet.Fernet` — AES-128-CBC
for secrecy plus HMAC-SHA256 for authenticity, with a random IV per
message. Fernet requires a 32-byte url-safe-base64 key.

``settings.encryption_key`` (env ``MODUS_ENCRYPTION_KEY``) is accepted in
two forms:

1. **A native Fernet key** — the value produced by the documented
   ``Fernet.generate_key()`` command. It is used directly, preserving
   compatibility with keys operators already generated.
2. **Any other string** (a passphrase, a hex secret, etc.). Because Fernet
   demands an exact key format, an arbitrary value is run through
   **HKDF-SHA256** (RFC 5869) with a fixed application salt/info to derive a
   deterministic 32-byte key, which is then url-safe-base64 encoded for Fernet.
   HKDF is chosen over a bare SHA-256 hash because it is the standard,
   purpose-built key-derivation construction and yields uniform key material.

Derivation is deterministic, so the same configured key always produces the
same cipher — encryption and decryption stay consistent across restarts.

Zero external dependencies beyond ``cryptography``, which is already a pinned
service dependency (``orchestrator/requirements.txt``). This module is
non-blocking and CPU-only — safe on a $5/mo VPS.
"""

from __future__ import annotations

import base64
import logging

logger = logging.getLogger(__name__)

# Version marker prepended to every encrypted value. The trailing colon is part
# of the prefix so ``value[len(_VERSION_PREFIX):]`` yields the bare token.
_VERSION_PREFIX = "enc:v1:"

# Fixed, non-secret parameters for HKDF derivation. These are constants (not
# secrets); their only job is domain separation and determinism.
_HKDF_SALT = b"modus.credential_crypto.v1"
_HKDF_INFO = b"modus-credential-encryption-at-rest"

# Cache the constructed cipher keyed on the raw configured key string, so a key
# change (e.g. in tests, or a config reload) transparently rebuilds the cipher
# instead of serving a stale one.
_cipher_cache: dict[str, object] = {}


class CredentialCryptoError(Exception):
    """Base class for credential-crypto failures."""


class CredentialEncryptionError(CredentialCryptoError):
    """Raised when a credential cannot be encrypted (e.g. no key configured)."""


class CredentialDecryptionError(CredentialCryptoError):
    """Raised when an encrypted credential fails to decrypt (tamper / wrong key)."""


_NO_KEY_MESSAGE = (
    "MODUS_ENCRYPTION_KEY is not set. Refusing to store credentials as "
    "plaintext. Generate a key and set it before creating connections:\n"
    '  python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"'
)


def _derive_fernet_key(raw_key: str) -> bytes:
    """
    Turn an arbitrary configured key into a valid Fernet key.

    If ``raw_key`` is already a valid Fernet key it is used verbatim; otherwise
    a deterministic 32-byte key is derived via HKDF-SHA256 and url-safe-base64
    encoded. Returns the base64 key bytes accepted by ``Fernet(...)``.
    """
    from cryptography.fernet import Fernet

    key_bytes = raw_key.encode("utf-8") if isinstance(raw_key, str) else raw_key

    # Try to use the value directly — this is the happy path for keys generated
    # via the documented Fernet.generate_key() command.
    try:
        Fernet(key_bytes)  # validates length/format
        return key_bytes
    except Exception:
        pass

    # Not a native Fernet key — derive deterministically via HKDF-SHA256.
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_HKDF_SALT,
        info=_HKDF_INFO,
    )
    derived = hkdf.derive(key_bytes)
    return base64.urlsafe_b64encode(derived)


def _get_cipher():
    """
    Build (or fetch cached) the Fernet cipher from settings.

    Raises :class:`CredentialEncryptionError` if no key is configured. Callers
    on the read path that must tolerate a missing key check
    :func:`is_encrypted` first and never reach here for legacy plaintext.
    """
    from orchestrator.core.config import settings

    raw_key = settings.encryption_key or ""
    if not raw_key:
        raise CredentialEncryptionError(_NO_KEY_MESSAGE)

    cached = _cipher_cache.get(raw_key)
    if cached is not None:
        return cached

    from cryptography.fernet import Fernet

    try:
        cipher = Fernet(_derive_fernet_key(raw_key))
    except Exception as exc:  # pragma: no cover - defensive
        raise CredentialEncryptionError(
            f"MODUS_ENCRYPTION_KEY is invalid: {exc}"
        ) from exc

    _cipher_cache[raw_key] = cipher
    return cipher


def is_encrypted(value: str | None) -> bool:
    """Return True if ``value`` carries the encrypted version marker."""
    return isinstance(value, str) and value.startswith(_VERSION_PREFIX)


def encrypt_credential(plaintext: str) -> str:
    """
    Encrypt a credential string for storage at rest.

    Returns a versioned, authenticated token (``enc:v1:<fernet-token>``).

    Fails closed: raises :class:`CredentialEncryptionError` if no encryption
    key is configured, rather than persisting plaintext.
    """
    if plaintext is None:
        raise CredentialEncryptionError("Cannot encrypt None.")
    if not isinstance(plaintext, str):
        raise CredentialEncryptionError(
            f"Expected str, got {type(plaintext).__name__}."
        )

    cipher = _get_cipher()
    token = cipher.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _VERSION_PREFIX + token


def decrypt_credential(stored: str | None) -> str | None:
    """
    Decrypt a stored credential back to plaintext.

    Behavior by input:

    * ``None`` -> ``None`` (no credential stored).
    * A legacy plaintext row (no ``enc:v1:`` prefix) -> returned unchanged, with
      a warning logged. This tolerates dev databases written before encryption
      was enabled.
    * An ``enc:v1:`` value -> authenticated-decrypted. Raises
      :class:`CredentialDecryptionError` if the token is tampered with, the key
      is wrong, or no key is configured.
    """
    if stored is None:
        return None
    if not isinstance(stored, str):
        raise CredentialDecryptionError(
            f"Expected str, got {type(stored).__name__}."
        )

    if not is_encrypted(stored):
        logger.warning(
            "Reading a legacy PLAINTEXT credential (no %s marker). Re-save this "
            "connection with MODUS_ENCRYPTION_KEY set to encrypt it at rest.",
            _VERSION_PREFIX,
        )
        return stored

    token = stored[len(_VERSION_PREFIX):]

    try:
        cipher = _get_cipher()
    except CredentialEncryptionError as exc:
        # We hold an encrypted value but have no key to open it. Fail loudly —
        # returning the ciphertext would be a silent failure.
        raise CredentialDecryptionError(
            "Encrypted credential present but MODUS_ENCRYPTION_KEY is not "
            "configured; cannot decrypt."
        ) from exc

    from cryptography.fernet import InvalidToken

    try:
        return cipher.decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise CredentialDecryptionError(
            "Credential failed authentication — data was tampered with or the "
            "encryption key does not match."
        ) from exc


def reset_cipher_cache() -> None:
    """Clear the cached cipher. Intended for tests that swap the configured key."""
    _cipher_cache.clear()
