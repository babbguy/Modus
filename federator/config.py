"""
Modus Federator — Configuration
======================================
All configuration via environment variables.
Pydantic-settings validates and types every value at startup.

Environment variable prefix: FEDERATOR_
Example: FEDERATOR_DATABASE_URL sets database_url.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class FederatorSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FEDERATOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ────────────────────────────────────────────────────────────
    app_name: str = "Modus Federator"
    version: str = "1.0.0"
    environment: Literal["development", "staging", "production"] = "production"
    debug: bool = False

    # ── Network ────────────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8443

    # ── Database ───────────────────────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///federator.db"

    # ── Auth (JWT) ─────────────────────────────────────────────────────────────
    # JWKS URL for validating bearer JWTs (reserved; HMAC secret is used today)
    jwks_url: str = ""
    # Fallback: shared secret for HMAC-signed JWTs (dev/small deploy)
    jwt_secret: SecretStr = SecretStr("")
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "modus-orchestrator"
    jwt_audience: str = "modus-federator"

    # ── Rate Limiting ──────────────────────────────────────────────────────────
    rate_limit_submits_per_hour: int = 10
    rate_limit_reads_per_hour: int = 60
    rate_limit_burst: int = 5

    # ── Aggregation ────────────────────────────────────────────────────────────
    aggregation_interval_minutes: int = 60  # how often to recompute merged results
    min_contributors_for_result: int = 3    # privacy threshold — need >= N contributors

    # ── Security ───────────────────────────────────────────────────────────────
    max_payload_bytes: int = 65536  # 64KB max encrypted payload
    allowed_industries: list[str] = [
        "healthcare", "fintech", "retail", "manufacturing",
        "education", "government", "technology", "media",
        "energy", "logistics", "legal", "nonprofit", "other",
    ]

    # ── CORS ────────────────────────────────────────────────────────────────────
    cors_origins: list[str] = []
    # Allowed CORS origins. Empty list = no CORS allowed.
    # Set via FEDERATOR_CORS_ORIGINS as comma-separated origins.

    # ── TLS ────────────────────────────────────────────────────────────────────
    tls_cert_path: str = ""
    tls_key_path: str = ""

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: str | list) -> list[str]:
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    @field_validator("jwt_secret")
    @classmethod
    def validate_jwt_secret(cls, v: SecretStr, info) -> SecretStr:
        import os
        env = os.environ.get("FEDERATOR_ENVIRONMENT", "production")
        if env == "production" and not v.get_secret_value():
            raise ValueError(
                "FEDERATOR_JWT_SECRET must not be empty in production. "
                "Set a strong JWT secret for authentication."
            )
        return v


@lru_cache
def get_settings() -> FederatorSettings:
    return FederatorSettings()


class _LazySettings:
    """Lazy proxy that defers Settings instantiation to first attribute access."""

    def __getattr__(self, name: str):
        real = get_settings()
        globals()["settings"] = real
        return getattr(real, name)


settings: FederatorSettings = _LazySettings()  # type: ignore[assignment]
