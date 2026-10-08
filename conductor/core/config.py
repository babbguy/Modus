"""
Modus Conductor — Configuration
======================================
All configuration via environment variables.
Pydantic-settings validates and types every value at startup.

Required in production:
    CONDUCTOR_DATABASE_URL  — PostgreSQL or SQLite connection string

Optional with sensible defaults for everything else.

Environment variable prefix: CONDUCTOR_
Example: CONDUCTOR_DATABASE_URL sets database_url.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CONDUCTOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ────────────────────────────────────────────────────────────
    app_name: str = "Modus Conductor"
    version: str = "1.0.0"
    environment: Literal["development", "staging", "production"] = "production"
    debug: bool = False

    # ── Network ────────────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8090
    workers: int = 1

    # ── Database ───────────────────────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///conductor.db"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 30
    db_pool_recycle: int = 1800

    # ── Auth ───────────────────────────────────────────────────────────────────
    # Shared secret between Orchestrators and this Conductor.
    # Orchestrators include this as Bearer token when pushing data.
    conductor_secret: str = ""

    # Auth mode for dashboard users (same as orchestrator: stub or jwt)
    auth_mode: Literal["stub", "jwt"] = "stub"
    jwt_secret: str = ""
    jwt_issuer: str = "modus"
    jwt_audience: str = "modus"

    # ── CORS ───────────────────────────────────────────────────────────────────
    cors_origins: list[str] = [
        "http://localhost:3001",
        "http://localhost:8080",
        "http://localhost:8090",
    ]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: str | list) -> list[str]:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @field_validator("auth_mode")
    @classmethod
    def validate_auth_mode(cls, v: str, info) -> str:
        env = (info.data or {}).get("environment", "production")
        if env == "production" and v == "stub":
            import logging
            logging.getLogger(__name__).critical(
                "FATAL: auth_mode='stub' is forbidden in production. "
                "Set CONDUCTOR_AUTH_MODE=jwt and configure CONDUCTOR_JWT_SECRET. "
                "Refusing to start with stub auth in production."
            )
            raise ValueError(
                "auth_mode='stub' is not allowed when environment='production'. "
                "Set CONDUCTOR_AUTH_MODE=jwt and configure JWT secrets."
            )
        return v

    @field_validator("conductor_secret")
    @classmethod
    def validate_conductor_secret(cls, v: str, info) -> str:
        env = (info.data or {}).get("environment", "production")
        if env == "production" and not v:
            raise ValueError(
                "CONDUCTOR_CONDUCTOR_SECRET must not be empty in production. "
                "Set a strong shared secret for Orchestrator-to-Conductor auth."
            )
        return v

    # ── Rate Limiting ──────────────────────────────────────────────────────────
    rate_limit_per_minute: int = 200
    rate_limit_burst: int = 50

    # ── Orchestrator Registry ──────────────────────────────────────────────────
    # How long before an Orchestrator is considered stale (no heartbeat)
    orchestrator_stale_minutes: int = 10

    # ── Data Push ──────────────────────────────────────────────────────────────
    # Maximum age of data before Conductor flags it as stale
    data_staleness_threshold_minutes: int = 15

    # ── Cache TTLs ─────────────────────────────────────────────────────────────
    cache_hot_ttl_seconds: int = 60       # KPI cards, burn rate
    cache_warm_ttl_seconds: int = 300     # Charts, breakdowns
    cache_cold_ttl_seconds: int = 900     # Historical reports

    # ── Reconciliation ─────────────────────────────────────────────────────────
    reconciliation_interval_seconds: int = 60

    # ── Completeness ───────────────────────────────────────────────────────────
    # Minimum percentage of Orchestrators that must report before data is
    # considered complete. Below this threshold, dashboard shows a warning.
    completeness_threshold_pct: float = 95.0

    # ── Logging ────────────────────────────────────────────────────────────────
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "text"] = "json"

    # ── Metrics ────────────────────────────────────────────────────────────────
    metrics_enabled: bool = True
    metrics_path: str = "/metrics"

    # ── Region ─────────────────────────────────────────────────────────────────
    # For future multi-region federation: identifies this Conductor's region.
    region_id: str = ""

    # ── Computed properties ────────────────────────────────────────────────────

    @property
    def is_sqlite(self) -> bool:
        return "sqlite" in self.database_url.lower()

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        if v.startswith("sqlite://") and "aiosqlite" not in v:
            v = v.replace("sqlite://", "sqlite+aiosqlite://", 1)
        if v.startswith("postgresql://"):
            v = v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


class _LazySettings:
    """Lazy proxy that defers Settings instantiation to first attribute access.

    This avoids reading environment variables and performing validation at
    module import time, which can cause issues during testing, CLI tooling,
    and circular imports.
    """

    def __getattr__(self, name: str):
        # Replace ourselves with the real instance on first access
        real = get_settings()
        globals()["settings"] = real
        return getattr(real, name)


# Module-level alias for convenience in non-DI contexts.
# Lazily initialized on first attribute access.
settings: Settings = _LazySettings()  # type: ignore[assignment]
