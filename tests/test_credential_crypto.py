"""
Tests for orchestrator.core.credential_crypto — encryption-at-rest helper.

Covers: round-trip (native Fernet key + derived passphrase), version prefix,
legacy plaintext read tolerance, fail-closed writes on missing key, and
tamper/wrong-key detection.
"""

import pytest
from cryptography.fernet import Fernet

from orchestrator.core import credential_crypto as cc
from orchestrator.core.config import get_settings

# The runtime resolves ``settings.encryption_key`` to this cached singleton, so
# patching it here is what _get_cipher() actually reads.
settings = get_settings()


def _set_key(monkeypatch, value):
    monkeypatch.setattr(settings, "encryption_key", value)
    cc.reset_cipher_cache()


@pytest.fixture(autouse=True)
def _clean_cipher_cache():
    """Ensure each test starts and ends with a clean cipher cache."""
    cc.reset_cipher_cache()
    yield
    cc.reset_cipher_cache()


@pytest.fixture
def fernet_key(monkeypatch):
    """Configure a valid native Fernet key for the duration of a test."""
    key = Fernet.generate_key().decode()
    _set_key(monkeypatch, key)
    return key


@pytest.fixture
def passphrase_key(monkeypatch):
    """Configure an arbitrary (non-Fernet) passphrase to exercise HKDF."""
    _set_key(monkeypatch, "a-plain-english-passphrase")
    return "a-plain-english-passphrase"


@pytest.fixture
def no_key(monkeypatch):
    """Configure an empty encryption key (development default)."""
    _set_key(monkeypatch, "")


# ── Round-trip ────────────────────────────────────────────────────────────────

def test_roundtrip_native_fernet_key(fernet_key):
    secret = '{"api_key": "sk-test-12345", "region": "us-east-1"}'
    token = cc.encrypt_credential(secret)
    assert token != secret
    assert cc.decrypt_credential(token) == secret


def test_roundtrip_derived_passphrase(passphrase_key):
    secret = "service-account-bearer-token"
    token = cc.encrypt_credential(secret)
    assert token != secret
    assert cc.decrypt_credential(token) == secret


def test_encrypted_value_has_version_prefix(fernet_key):
    token = cc.encrypt_credential("hello")
    assert token.startswith("enc:v1:")
    assert cc.is_encrypted(token) is True


def test_ciphertext_is_non_deterministic(fernet_key):
    # Fernet uses a random IV, so two encryptions differ but both decrypt back.
    a = cc.encrypt_credential("same-input")
    b = cc.encrypt_credential("same-input")
    assert a != b
    assert cc.decrypt_credential(a) == cc.decrypt_credential(b) == "same-input"


def test_derivation_is_deterministic_across_cache_reset(passphrase_key):
    token = cc.encrypt_credential("value")
    cc.reset_cipher_cache()  # force cipher rebuild from the same key
    assert cc.decrypt_credential(token) == "value"


# ── Legacy plaintext read path ────────────────────────────────────────────────

def test_legacy_plaintext_read_returns_unchanged(fernet_key, caplog):
    legacy = '{"api_key": "written-before-encryption"}'
    with caplog.at_level("WARNING"):
        assert cc.decrypt_credential(legacy) == legacy
    assert any("legacy" in r.message.lower() for r in caplog.records)


def test_legacy_plaintext_read_without_key(no_key):
    # Reads of legacy rows are tolerated even with no key configured.
    legacy = "plain-secret"
    assert cc.decrypt_credential(legacy) == legacy


def test_is_encrypted_false_for_plaintext():
    assert cc.is_encrypted("plain") is False
    assert cc.is_encrypted(None) is False


# ── Fail-closed writes ────────────────────────────────────────────────────────

def test_write_refused_when_no_key(no_key):
    with pytest.raises(cc.CredentialEncryptionError) as exc:
        cc.encrypt_credential("secret")
    assert "MODUS_ENCRYPTION_KEY" in str(exc.value)


def test_encrypt_rejects_none(fernet_key):
    with pytest.raises(cc.CredentialEncryptionError):
        cc.encrypt_credential(None)


def test_encrypt_rejects_non_str(fernet_key):
    with pytest.raises(cc.CredentialEncryptionError):
        cc.encrypt_credential({"not": "a string"})


# ── Tamper / wrong-key detection ──────────────────────────────────────────────

def test_tamper_detection(fernet_key):
    token = cc.encrypt_credential("secret")
    # Flip a character inside the token body.
    body = token[len("enc:v1:"):]
    tampered = "enc:v1:" + ("A" if body[0] != "A" else "B") + body[1:]
    with pytest.raises(cc.CredentialDecryptionError):
        cc.decrypt_credential(tampered)


def test_wrong_key_detection(monkeypatch):
    _set_key(monkeypatch, Fernet.generate_key().decode())
    token = cc.encrypt_credential("secret")

    # Rotate to a different key without re-encrypting — decryption must fail.
    _set_key(monkeypatch, Fernet.generate_key().decode())
    with pytest.raises(cc.CredentialDecryptionError):
        cc.decrypt_credential(token)


def test_decrypt_encrypted_value_without_key(fernet_key, monkeypatch):
    token = cc.encrypt_credential("secret")
    # Key removed after the value was written — must fail loudly, not silently.
    _set_key(monkeypatch, "")
    with pytest.raises(cc.CredentialDecryptionError):
        cc.decrypt_credential(token)


# ── None passthrough ──────────────────────────────────────────────────────────

def test_decrypt_none_returns_none(fernet_key):
    assert cc.decrypt_credential(None) is None
