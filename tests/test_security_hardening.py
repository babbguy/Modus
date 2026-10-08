"""
Tests — Security fail-closed hardening.

Covers the config-level auth_mode gate (H1/L4) and the in-code stub-auth
defense-in-depth guard (H1). Nomus fail-closed
behaviour is covered in test_nomus_client.py.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator.core.config import Settings


def _make_settings(**overrides) -> Settings:
    # Provide a non-placeholder master key so the master-key validator never
    # interferes with what we are actually asserting about auth_mode.
    base = {
        "database_url": "sqlite+aiosqlite:///:memory:",
        "master_api_key": "mds_master_test_key_not_a_placeholder_value",
    }
    base.update(overrides)
    return Settings(**base)


# ── auth_mode validator (H1 + L4) ────────────────────────────────────────────


def test_stub_allowed_in_development():
    s = _make_settings(environment="development", auth_mode="stub")
    assert s.auth_mode == "stub"


@pytest.mark.parametrize("env", ["staging", "production"])
def test_stub_forbidden_outside_development(env, monkeypatch):
    monkeypatch.delenv("MODUS_ALLOW_INSECURE_AUTH", raising=False)
    with pytest.raises(ValidationError) as exc:
        _make_settings(environment=env, auth_mode="stub")
    assert "stub" in str(exc.value)


@pytest.mark.parametrize("env", ["staging", "production"])
def test_stub_allowed_outside_development_with_explicit_optin(env, monkeypatch):
    monkeypatch.setenv("MODUS_ALLOW_INSECURE_AUTH", "1")
    s = _make_settings(environment=env, auth_mode="stub")
    assert s.auth_mode == "stub"


@pytest.mark.parametrize("env", ["development", "staging", "production"])
def test_jwt_always_allowed(env, monkeypatch):
    monkeypatch.delenv("MODUS_ALLOW_INSECURE_AUTH", raising=False)
    s = _make_settings(environment=env, auth_mode="jwt")
    assert s.auth_mode == "jwt"


def test_validator_uses_model_environment_not_os_environ(monkeypatch):
    # L4: the validator must read `environment` from the model's own value, so
    # a divergent MODUS_ENVIRONMENT env var cannot flip the security posture.
    monkeypatch.setenv("MODUS_ENVIRONMENT", "production")
    monkeypatch.delenv("MODUS_ALLOW_INSECURE_AUTH", raising=False)
    # Explicit kwarg wins over the env var; development is the effective value.
    s = _make_settings(environment="development", auth_mode="stub")
    assert s.auth_mode == "stub"


# ── in-code stub resolver guard (H1 defense-in-depth) ────────────────────────


def _restore_env(settings, value):
    object.__setattr__(settings, "environment", value)


def test_resolve_stub_raises_outside_dev_without_optin(monkeypatch):
    from fastapi import HTTPException

    from orchestrator.core import auth
    from orchestrator.core.config import settings

    monkeypatch.delenv("MODUS_ALLOW_INSECURE_AUTH", raising=False)
    original = settings.environment
    try:
        object.__setattr__(settings, "environment", "production")
        with pytest.raises(HTTPException) as exc:
            auth._resolve_stub(None)  # type: ignore[arg-type]
        assert exc.value.status_code == 401
    finally:
        _restore_env(settings, original)


def test_resolve_stub_allows_dev(monkeypatch):
    from orchestrator.core import auth
    from orchestrator.core.config import settings

    original = settings.environment
    try:
        object.__setattr__(settings, "environment", "development")
        identity = auth._resolve_stub(None)  # type: ignore[arg-type]
        assert identity.is_platform_admin
    finally:
        _restore_env(settings, original)


def test_resolve_stub_allows_outside_dev_with_optin(monkeypatch):
    from orchestrator.core import auth
    from orchestrator.core.config import settings

    monkeypatch.setenv("MODUS_ALLOW_INSECURE_AUTH", "1")
    original = settings.environment
    try:
        object.__setattr__(settings, "environment", "staging")
        identity = auth._resolve_stub(None)  # type: ignore[arg-type]
        assert identity.is_platform_admin
    finally:
        _restore_env(settings, original)
