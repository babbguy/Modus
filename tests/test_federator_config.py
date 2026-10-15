"""
Federator settings, the slim write handler's JWT check and the static
publisher the slim image runs.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import jwt
import pytest

from federator import write_handler
from federator.config import FederatorSettings

ROOT = Path(__file__).resolve().parent.parent
SECRET = "test-federator-secret-0123456789abcdef"


def _settings(monkeypatch, **env: str) -> FederatorSettings:
    monkeypatch.setenv("FEDERATOR_ENVIRONMENT", "development")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return FederatorSettings(_env_file=None)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://a.example.com,https://b.example.com", ["https://a.example.com", "https://b.example.com"]),
        (" https://a.example.com , https://b.example.com ,", ["https://a.example.com", "https://b.example.com"]),
        ('["https://a.example.com", "https://b.example.com"]', ["https://a.example.com", "https://b.example.com"]),
        ("https://a.example.com", ["https://a.example.com"]),
        ("*", ["*"]),
        ("", []),
    ],
)
def test_cors_origins_from_env(monkeypatch, raw, expected):
    assert _settings(monkeypatch, FEDERATOR_CORS_ORIGINS=raw).cors_origins == expected


def test_cors_origins_default_is_empty(monkeypatch):
    monkeypatch.delenv("FEDERATOR_CORS_ORIGINS", raising=False)
    assert _settings(monkeypatch).cors_origins == []


# ── Slim write handler: same token claims as the full service ────────────────


@pytest.fixture
def writer_auth(monkeypatch):
    monkeypatch.setattr(write_handler, "JWT_SECRET", SECRET)
    monkeypatch.setattr(write_handler, "JWT_ISSUER", "modus-orchestrator")
    monkeypatch.setattr(write_handler, "JWT_AUDIENCE", "modus-federator")


def _token(**claims) -> str:
    base = {"sub": "org-1", "exp": int(time.time()) + 300, "iss": "modus-orchestrator", "aud": "modus-federator"}
    base.update(claims)
    return jwt.encode({k: v for k, v in base.items() if v is not None}, SECRET, algorithm="HS256")


def test_writer_accepts_token_the_full_service_accepts(writer_auth):
    claims = write_handler._verify_jwt(_token())
    assert claims is not None and claims["sub"] == "org-1"


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "someone-else"},
        {"iss": "someone-else"},
        {"aud": None},
        {"iss": None},
        {"exp": int(time.time()) - 60},
    ],
)
def test_writer_rejects_wrong_or_missing_claims(writer_auth, claims):
    assert write_handler._verify_jwt(_token(**claims)) is None


def test_writer_defaults_match_full_service_defaults(monkeypatch):
    monkeypatch.setenv("FEDERATOR_JWT_SECRET", SECRET)
    s = FederatorSettings(_env_file=None)
    assert (s.jwt_issuer, s.jwt_audience) == ("modus-orchestrator", "modus-federator")
    source = (ROOT / "federator" / "write_handler.py").read_text(encoding="utf-8")
    assert '"FEDERATOR_JWT_ISSUER", "modus-orchestrator"' in source
    assert '"FEDERATOR_JWT_AUDIENCE", "modus-federator"' in source


# ── Static publisher: runs without pydantic (the slim image has none) ────────

_PUBLISH_WITHOUT_PYDANTIC = """
import sqlite3, sys
for name in ("pydantic", "pydantic_settings", "fastapi", "sqlalchemy"):
    sys.modules[name] = None  # any import of these now raises ImportError
from federator import write_handler
from federator.core import static_publisher
db = sys.argv[1]
conn = sqlite3.connect(db)
write_handler._ensure_tables(conn)
conn.close()
static_publisher.publish_all(db)
"""


def test_static_publisher_runs_without_pydantic(tmp_path):
    import json
    import os

    static_dir = tmp_path / "static"
    env = {**os.environ, "FEDERATOR_STATIC_DIR": str(static_dir), "PYTHONPATH": str(ROOT)}
    env.pop("FEDERATOR_JWT_SECRET", None)
    result = subprocess.run(
        [sys.executable, "-c", _PUBLISH_WITHOUT_PYDANTIC, str(tmp_path / "federator.db")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    portal = json.loads((static_dir / "portal.json").read_text())
    assert portal["version"] == FederatorSettings.model_fields["version"].default
    assert set(portal["available_industries"]) == write_handler.ALLOWED_INDUSTRIES
    assert set(FederatorSettings.model_fields["allowed_industries"].default) == write_handler.ALLOWED_INDUSTRIES
    assert (static_dir / "benchmarks.json").exists()
    assert (static_dir / "audit" / "merkle-root.json").exists()
