"""
Conductor settings and deployment wiring.

CORS origins parse the way the orchestrator's do (comma-separated, a JSON
list, ``*`` or empty), and the image and the production Compose file probe a
health route the Conductor serves and keep its database on the data volume.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conductor.core.config import Settings

ROOT = Path(__file__).resolve().parent.parent


def _settings(monkeypatch, **env: str) -> Settings:
    monkeypatch.setenv("CONDUCTOR_ENVIRONMENT", "development")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


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
    assert _settings(monkeypatch, CONDUCTOR_CORS_ORIGINS=raw).cors_origins == expected


def test_cors_origins_default_is_a_list(monkeypatch):
    monkeypatch.delenv("CONDUCTOR_CORS_ORIGINS", raising=False)
    origins = _settings(monkeypatch).cors_origins
    assert isinstance(origins, list) and "http://localhost:8090" in origins


def test_database_url_from_env(monkeypatch):
    s = _settings(monkeypatch, CONDUCTOR_DATABASE_URL="sqlite:///data/conductor.db")
    assert s.database_url == "sqlite+aiosqlite:///data/conductor.db"


def _conductor_routes() -> set[str]:
    from conductor.main import create_app

    return {route.path for route in create_app().routes}


def test_image_healthcheck_probes_a_served_route():
    dockerfile = (ROOT / "Dockerfile.conductor").read_text(encoding="utf-8")
    paths = re.findall(r"HEALTHCHECK[^\n]*\n\s*CMD curl[^\n]*http://localhost:8090(/\S*)", dockerfile)
    assert paths, "Dockerfile.conductor has no curl HEALTHCHECK"
    assert set(paths) <= _conductor_routes()


def _compose_conductor_service() -> str:
    compose = (ROOT / "deploy" / "docker-compose.prod.yml").read_text(encoding="utf-8")
    match = re.search(r"^  conductor:\n(.*?)(?=^  \S|^\S)", compose, re.M | re.S)
    assert match, "no conductor service in deploy/docker-compose.prod.yml"
    return match.group(1)


def test_compose_healthcheck_probes_a_served_route():
    paths = re.findall(r"http://localhost:8090(/[^\"\s]*)", _compose_conductor_service())
    assert paths, "the conductor service has no HTTP health check"
    assert set(paths) <= _conductor_routes()


def test_compose_sets_conductor_variables_and_keeps_the_database_on_the_volume():
    service = _compose_conductor_service()
    # The Conductor reads CONDUCTOR_*; a MODUS_* variable here would be ignored.
    assert not re.search(r"^\s*- MODUS_", service, re.M)
    db_url = re.search(r"- CONDUCTOR_DATABASE_URL=\$\{MODUS_CONDUCTOR_DATABASE_URL:-([^}]+)\}", service)
    assert db_url, "CONDUCTOR_DATABASE_URL is not set from MODUS_CONDUCTOR_DATABASE_URL"
    # Relative SQLite path from WORKDIR /app -> /app/data, the mounted volume.
    assert db_url.group(1) == "sqlite+aiosqlite:///data/conductor.db"
    assert re.search(r"- conductor_data:/app/data\b", service)
    for name in ("CONDUCTOR_ENVIRONMENT", "CONDUCTOR_AUTH_MODE", "CONDUCTOR_JWT_SECRET", "CONDUCTOR_CONDUCTOR_SECRET"):
        assert re.search(rf"^\s*- {name}=", service, re.M), name
