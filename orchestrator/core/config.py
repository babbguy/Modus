"""
Modus — Configuration
==============================
All configuration via environment variables.
Pydantic-settings validates and types every value at startup.

Required in production:
    DATABASE_URL          — PostgreSQL or SQLite connection string
    MASTER_API_KEY        — Master key for app registration

Optional with sensible defaults for everything else.

Environment variable prefix: MODUS_
Example: MODUS_DATABASE_URL sets database_url.

SQLite mode (new in Phase 2):
    Set DATABASE_URL=sqlite+aiosqlite:///modus.db for zero-config
    single-file deployment. Perfect for laptops, VPS, Docker Compose.
"""

from __future__ import annotations

import socket
from functools import lru_cache
from ipaddress import ip_address
from typing import Literal, Union
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MODUS_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ────────────────────────────────────────────────────────────
    app_name: str = "Modus Orchestrator"
    version: str = "1.1.0"
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    disable_dashboard: bool = False

    # ── Network ────────────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8080
    workers: int = 1
    # workers > 1 is handled by the container orchestrator (k8s replicas),
    # not by uvicorn worker processes, to keep the health/readiness model clean.

    # ── Database ───────────────────────────────────────────────────────────────
    database_url: str = Field(
        default="sqlite+aiosqlite:///modus.db",
        description=(
            "Database DSN. Supports:\n"
            "  PostgreSQL: postgresql+asyncpg://user:password@host:5432/dbname\n"
            "  SQLite:     sqlite+aiosqlite:///path/to/file.db\n"
            "SQLite mode: zero config, single-file, < $3/month at 10M tokens/day."
        ),
    )

    db_pool_size: int = Field(10, ge=1, le=100)
    db_max_overflow: int = Field(20, ge=0, le=200)
    db_pool_timeout: int = Field(30, ge=5, le=120)
    db_pool_recycle: int = Field(1800, ge=60)
    # 1800s = 30 minutes. Prevents stale connections on managed DB services
    # that close idle connections (RDS default: 8 hours, but safer to recycle).

    # ── Security ───────────────────────────────────────────────────────────────
    master_api_key: str = Field(
        default="mds_master_dev_placeholder_key_change_me",
        description="Master key for app registration. Keep secret. Never use as an app key.",
        min_length=20,
    )

    api_key_prefix: str = "mds_"
    master_key_prefix: str = "mds_master_"

    # CORS — comma-separated origins. Typed as a union with ``str`` so that
    # pydantic-settings falls back to the raw string when the env value is not
    # JSON; parse_cors_origins then splits it, and the result is always a list.
    cors_origins: Union[list[str], str] = Field(
        default=["http://localhost:3001", "http://localhost:8000", "http://localhost:8080"],
        description="Allowed CORS origins for the dashboard.",
    )

    # ── Auth middleware ────────────────────────────────────────────────────────
    # Option A: stub issues a static platform-admin identity.
    # Option B: set auth_mode = "jwt" and configure jwt_secret / jwt_issuer.
    auth_mode: Literal["stub", "jwt"] = "stub"
    jwt_secret: str = ""
    jwt_issuer: str = ""
    jwt_audience: str = "modus"

    # ── Ingest ─────────────────────────────────────────────────────────────────
    max_batch_size: int = Field(1000, ge=1, le=10000)
    # Maximum records per ingest payload. Larger batches are rejected with 413.

    ingest_dedup_ttl_hours: int = Field(48, ge=1, le=168)
    # How long to retain batch IDs for deduplication.

    # ── Aggregation ────────────────────────────────────────────────────────────
    aggregate_interval_seconds: int = Field(60, ge=10, le=3600)
    # How often the background aggregation task runs.

    heartbeat_stale_minutes: int = Field(5, ge=1, le=60)
    # An app is considered offline if no heartbeat in this window.

    # ── Pricing sync ───────────────────────────────────────────────────────────
    pricing_sync_interval_hours: int = Field(24, ge=1, le=168)
    pricing_fallback_to_bundled: bool = True
    # Use bundled pricing.json if sync fails. Essential for air-gap deployments.

    pricing_live_fetch_enabled: bool = False
    # Off by default (Law 2, zero egress). When True, pricing sync may reach
    # public provider endpoints to detect newly-released models. Bundled prices
    # are always the source of truth and always sync regardless of this flag.

    # ── Prometheus metrics ─────────────────────────────────────────────────────
    metrics_enabled: bool = True
    metrics_path: str = "/metrics"

    # ── Logging ────────────────────────────────────────────────────────────────
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "text"] = "json"
    # JSON in production (log aggregators expect it), text in development.

    # ── Scheduled maintenance ──────────────────────────────────────────────────
    prune_heartbeats_days: int = Field(30, ge=1, le=365)
    prune_batches_hours: int = Field(48, ge=1, le=168)
    prune_real_time_spend_days: int = Field(7, ge=1, le=90)

    # ── Storage retention ─────────────────────────────────────────────────────
    # Usage is counted into the hourly AND daily aggregates when it is
    # ingested, so retention only deletes detail the aggregates already hold:
    # raw usage_records older than compaction_after_hours, and hourly
    # aggregates older than hourly_aggregate_retention_days (daily rows are
    # kept). Keeps DB size bounded at any call volume.
    compaction_after_hours: int = Field(24, ge=1, le=168)
    hourly_aggregate_retention_days: int = Field(7, ge=2, le=90)

    # ── Nomus Regulatory Engine (OPTIONAL — OFF by default) ──────────────────
    nomus_url: str = ""
    # Base URL of a Nomus service (https://github.com/babbguy/Nomus).
    # Empty = disabled. Opt-in only.
    # When enabled: pulls regulatory rulesets TO the customer.
    # Customer data NEVER leaves their infrastructure.
    nomus_api_key: str = ""
    # Nomus API key (nk_live_* prefix). Used to authenticate with Nomus.
    # When empty + nomus_url set, requests are sent without authentication.
    nomus_sync_interval_seconds: int = Field(21600, ge=300, le=86400)
    # How often to pull updated rulesets (default: 6 hours = 21600s).
    nomus_auto_sync: bool = True
    # Automatically sync rulesets on a schedule. Disable for manual-only sync.
    nomus_webhook_secret: str = ""
    # HMAC-SHA256 shared secret for verifying inbound Nomus webhooks.
    # Set via MODUS_NOMUS_WEBHOOK_SECRET. Falls back to nomus_api_key if empty.

    # ── Global LLM Assistant ─────────────────────────────────────────────────
    assistant_provider: str = ""           # anthropic, openai, google, ollama
    assistant_model: str = ""              # claude-sonnet-4-6, gpt-4o, gemini-2.0-flash, llama3.1, etc.
    assistant_api_key: str = ""            # customer's own API key (encrypted at rest recommended)
    assistant_enabled: bool = False        # disabled until provider + key configured
    assistant_ollama_url: str = "http://localhost:11434"
    # Local Ollama endpoint for the 'ollama' provider — an air-gap-safe
    # assistant that sends dashboard context to a model on the customer's own
    # infrastructure, with no egress to a third-party LLM.

    # ── Feature flags ──────────────────────────────────────────────────────────
    # These gate features that are architecturally present but not yet
    # production-ready. Flip via env var without a code deploy.
    feature_anomaly_detection: bool = False
    feature_nl_query: bool = False
    feature_agentic_remediation: bool = False

    # ── Phase 8 feature settings ────────────────────────────────────────────────
    # Post-Quantum attestation
    pqc_attestation_enabled: bool = False
    pqc_attestation_tier: str = "auto"  # auto | simulated | real

    # Trajectory compliance receipts (tamper-evident hash chain, not ZK)
    zk_proofs_enabled: bool = False
    zk_proof_tier: str = "auto"  # auto | hash_chain | snark
    zk_proof_sample_rate: float = 0.01  # 1% of sessions by default

    # TRiSM Multi-Agent Sentinel
    trism_enabled: bool = False
    trism_scan_threshold: float = 0.3
    trism_auto_block: bool = False

    # Constitutional Evolution Engine
    evolution_enabled: bool = False
    evolution_population_size: int = 100
    evolution_generations_per_run: int = 10
    evolution_agent_count: int = 200
    evolution_tier: str = "auto"  # auto | genetic | neuro_symbolic | llm_guided

    # Neuromorphic Edge Enforcement
    neuromorphic_enabled: bool = False
    neuromorphic_backend: str = "software"  # software | loihi | speck | auto

    # ── Phase 9 feature settings ────────────────────────────────────────────────
    # Proof-of-Enforcement Chained Audit Ledger (hash chain; Tier-2 "snark" is
    # an experimental label, not a production zk-SNARK)
    poe_ledger_enabled: bool = False
    poe_sample_rate: float = 0.1                     # 10% of sessions sampled
    poe_high_risk_always_prove: bool = True           # high-risk calls always get a proof
    poe_snark_enabled: bool = False                   # Tier 2: include SNARK in proof
    poe_merkle_anchor_interval_seconds: int = 300     # anchor every 5 minutes

    # Chain-of-Thought Governance Ledger
    cot_ledger_enabled: bool = True                   # always on — governance decisions are infrequent

    # NeuroAI Assurance Co-Evolution
    neuro_assurance_enabled: bool = False
    neuro_assurance_co_evolution: bool = True          # feed SNN metrics into evolution fitness
    neuro_assurance_energy_weight: float = 0.1
    neuro_assurance_drift_weight: float = 0.1
    neuro_assurance_efficiency_weight: float = 0.05
    neuro_assurance_benchmark_interval_seconds: int = 3600

    # Federated ZK-Constitutional Optimizer (OPT-IN ONLY)
    # CRITICAL: federation_enabled and federation_participation_enabled both
    # default to False. No outbound connections unless explicitly opted in.
    federation_enabled: bool = False
    federation_participation_enabled: bool = False     # contribute deltas (requires consent)
    federation_aggregator_url: str = ""                # operator-provided; no default
    federation_consortium_mode: bool = False            # self-hosted hub mode
    federation_consortium_endpoint: str = ""
    federation_sync_interval_hours: int = 24
    federation_consent_required: bool = True            # always True at runtime

    # Threshold Approval Swarm Governance (Preview; not privacy-preserving MPC)
    swarm_governance_enabled: bool = False
    swarm_session_ttl_minutes: int = 60
    swarm_mpc_mode: str = "additive"                   # additive | shamir
    swarm_max_parties: int = 10

    # ── Phase 10: PQC Assessment & HNDL ─────────────────────────────────────────
    pqc_assessment_enabled: bool = False
    pqc_hndl_enabled: bool = False
    pqc_hndl_block_classical: bool = False
    pqc_hndl_quantum_horizon_years: int = Field(10, ge=1, le=50)
    pqc_audit_compression_enabled: bool = False
    pqc_slh_dsa_enabled: bool = False
    pqc_agent_identity_enabled: bool = False

    # ── Rate limiting ──────────────────────────────────────────────────────────
    # Two endpoint classes, each a per-key sliding window (key = the
    # X-Modus-APIKey header, else the client IP). 0 disables a class's limit.
    #   default: every other /api/* route (dashboard, admin, team-token calls)
    #   sdk:     machine traffic from registered apps: /api/v1/ingest,
    #            /heartbeat, /policy/evaluate, /routing/outcomes, /topology,
    #            /governance/rewind-event, /otel, and GET /policies with an
    #            app key. One evaluate per LLM call plus flushes/heartbeats,
    #            so the SDK class is sized for thousands of calls per minute.
    rate_limit_per_minute: int = Field(200, ge=0, description="Default class: requests per minute per key")
    rate_limit_burst: int = Field(50, ge=0, description="Default class: burst allowance above the per-minute rate")
    rate_limit_sdk_per_minute: int = Field(6000, ge=0, description="SDK class: requests per minute per app key")
    rate_limit_sdk_burst: int = Field(1000, ge=0, description="SDK class: burst allowance above the per-minute rate")

    # ── Enforcement / policy engine ────────────────────────────────────────────
    # Master switch. Set to False to disable all policy enforcement while
    # keeping the evaluate endpoint available (it returns allow for everything).
    # Useful for gradual rollouts: deploy first, enforce later.
    enforcement_enabled: bool = True

    # Fail-CLOSED by default: on a DB error / evaluation failure, evaluate
    # returns DENY, not allow. Compliance posture over availability — a
    # governance product must never silently stop enforcing when its backing
    # store is unavailable. Operators who explicitly prefer availability can set
    # MODUS_ENFORCEMENT_FAIL_OPEN=true, but the safe default is closed.
    enforcement_fail_open: bool = False

    # Maximum time in ms the evaluate endpoint may take before returning allow
    # regardless of policy result. Prevents enforcement from causing latency
    # spikes in the calling application.
    enforcement_timeout_ms: int = Field(500, ge=50, le=5000)

    # ── Routing engine ─────────────────────────────────────────────────────────
    routing_enabled: bool = True
    # Master switch for the routing engine. Routing background tasks are skipped
    # when disabled. SDK routing table will be empty.

    routing_calibrator_interval_seconds: int = Field(600, ge=60, le=7200)
    # How often the calibrator promotes fingerprints (observe → routing).

    routing_drift_monitor_interval_seconds: int = Field(300, ge=60, le=3600)
    # How often the drift monitor checks for input distribution drift.

    routing_default_observe_threshold: int = Field(200, ge=10, le=10000)
    # Number of calls at a call site before calibration begins.

    routing_cheap_model_default: str = "claude-haiku-4-5-20251001"
    # Default cheap model when no per-fingerprint model is set.

    routing_similarity_threshold: float = Field(0.85, ge=0.5, le=1.0)
    # Embedding cosine similarity threshold for cheap/expensive agreement.

    routing_confidence_decay_rate: float = Field(0.85, ge=0.5, le=0.99)
    # Confidence decay per drift cycle (1 - decay_rate = % lost per cycle).

    routing_min_calibration_samples: int = Field(10, ge=5, le=100)
    # Minimum calibration samples required before promoting to routing.

    # ── AI Summary Engine ────────────────────────────────────────────────────
    summary_agent: str = ""
    # Which AI provider for topology summarization.
    # ⚠️  SECURITY: External providers (anthropic, openai, google, deepseek) send
    # customer topology data outside your infrastructure. Only enable if you have
    # explicit permission to share service metadata with third-party AI providers.
    # Options: anthropic, openai, google, deepseek, ollama
    # Default (empty): local text-based fallback, no external calls

    summary_api_key: SecretStr = SecretStr("")
    # Unified API key routed to whichever provider is active.
    # For Ollama (local), leave empty.

    summary_model_id: str = ""
    # Override the default model. If empty, each provider uses its own default.
    # Examples: claude-haiku-4-5-20251001, gpt-4o-mini, gemini-2.0-flash

    summary_base_url: str = ""
    # Custom base URL. Required for Ollama (e.g. http://localhost:11434).
    # Optional override for other providers (e.g. Azure OpenAI endpoint).

    # ── Encryption ───────────────────────────────────────────────────────────
    encryption_key: str = ""
    # Fernet key for encrypting stored credentials (billing connections, etc.).
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # When empty, credentials are stored as plaintext (development only).

    # ── Scale (Phase 4d) ──────────────────────────────────────────────────────
    replica_database_url: str = ""
    # PostgreSQL read replica DSN. When set, dashboard queries and aggregation
    # reads are routed to this replica. Write operations always go to the primary.
    # Example: postgresql+asyncpg://user:password@replica:5432/modus

    partition_usage_records: bool = False
    # Enable declarative monthly partitioning on usage_records (PostgreSQL only).
    # Automatically creates new partitions via background task. Requires PG 11+.
    # No-op on SQLite. Recommended at ~50M rows.

    region_id: str = ""
    # Unique identifier for this orchestrator instance's region (e.g. "us-east-1").
    # Used for multi-region deployments with cross-region aggregation.
    # Empty = single-region deployment (default).

    # ── Integrations (Phase 4e) ───────────────────────────────────────────────
    app_pause_endpoint_enabled: bool = False
    # Allow orchestrator to call back to apps' pause endpoints on critical threshold.

    # ── Gateway Proxy (Phase 6) ───────────────────────────────────────────
    gateway_enabled: bool = False
    # Enable the language-agnostic gateway proxy. Apps change their
    # provider BASE_URL to point at /gateway/openai/v1 or /gateway/anthropic.

    gateway_openai_base_url: str = "https://api.openai.com"
    # Upstream OpenAI API URL. Override for Azure OpenAI or compatible APIs.

    gateway_anthropic_base_url: str = "https://api.anthropic.com"
    # Upstream Anthropic API URL.

    gateway_max_body_bytes: int = Field(10_485_760, ge=65536)
    # Max proxied request body (default 10 MiB). Caps memory on a small VPS —
    # oversized bodies are rejected with 413 rather than fully buffered.

    # ── Conductor Push (Org-Level Aggregation) ──────────────────────────────────
    conductor_url: str = ""
    # URL of the Conductor service (e.g., http://conductor:8090).
    # When set, the Orchestrator pushes aggregated data to the Conductor
    # on a regular cadence for org-wide dashboard views.
    # Empty = standalone mode (no Conductor).

    conductor_secret: str = ""
    # Shared secret for authenticating pushes to the Conductor.
    # Must match the CONDUCTOR_CONDUCTOR_SECRET env var on the Conductor.

    conductor_push_interval_seconds: int = Field(60, ge=10, le=600)
    # How often to push aggregated data to the Conductor.

    conductor_instance_id: str = ""
    # Unique instance ID for this Orchestrator. Auto-generated if empty.
    # Used by the Conductor to identify this Orchestrator in its registry.

    conductor_instance_name: str = ""
    # Human-readable name for this Orchestrator (e.g., "payments-api-prod").
    # Defaults to app_name if empty.

    @field_validator("conductor_url")
    @classmethod
    def validate_conductor_url(cls, v: str) -> str:
        """Validate that Conductor URL points to internal infrastructure only."""
        if not v:  # Empty is OK (feature disabled)
            return v

        # Parse URL to extract host
        parsed = urlparse(v)
        host = parsed.hostname or parsed.netloc.split(":")[0]

        # Allow localhost
        if host in ("localhost", "127.0.0.1", "::1"):
            return v

        # Allow .internal / .local domains (typically DNS-only, not public)
        if host.endswith((".internal", ".local")):
            return v

        # Try to resolve and check if it's a private IP
        try:
            ip = ip_address(host)
            if ip.is_private or ip.is_loopback:
                return v
            raise ValueError(
                f"Conductor URL must point to internal infrastructure. "
                f"Host {host} resolves to public IP {ip}. "
                f"Conductor must be customer-hosted."
            )
        except ValueError as e:
            if "does not appear to be" not in str(e):
                raise
            # Not a valid IP, try DNS resolution
            try:
                resolved_ip = socket.gethostbyname(host)
                ip = ip_address(resolved_ip)
                if not (ip.is_private or ip.is_loopback):
                    raise ValueError(
                        f"Conductor URL must point to internal infrastructure. "
                        f"Host {host} resolves to public IP {resolved_ip}. "
                        f"Conductor must be customer-hosted."
                    )
            except socket.gaierror:
                # DNS resolution failed — allow it (might be set before DNS is configured)
                pass

        return v

    # ── Plugins ────────────────────────────────────────────────────────────────
    plugins_enabled: bool = True
    # Set to False to skip loading plugins/ on startup.

    @property
    def is_sqlite(self) -> bool:
        """True if using SQLite backend (single-file mode)."""
        return "sqlite" in self.database_url.lower()

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        # SQLite mode — validate and normalise
        if "sqlite" in v.lower():
            if not v.startswith("sqlite"):
                v = f"sqlite+aiosqlite:///{v}"
            if "+aiosqlite" not in v:
                v = v.replace("sqlite://", "sqlite+aiosqlite://", 1)
            return v

        # PostgreSQL mode — original validation
        if not v.startswith(("postgresql+asyncpg://", "postgresql://", "postgres://")):
            raise ValueError(
                "DATABASE_URL must be a PostgreSQL or SQLite connection string.\n"
                "  PostgreSQL: postgresql+asyncpg://user:password@host:5432/dbname\n"
                "  SQLite:     sqlite+aiosqlite:///modus.db"
            )
        # Normalise to asyncpg driver
        if v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql+asyncpg://", 1)
        elif v.startswith("postgresql://"):
            v = v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: str | list) -> list[str]:
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    @field_validator("auth_mode")
    @classmethod
    def validate_auth_mode(cls, v: str, info) -> str:
        # Read `environment` from the model's own already-validated value so the
        # field default and this validator share a single source of truth. A
        # separate os.environ read (with its own default) could disagree with
        # the `environment` field default and silently change the security
        # posture. `environment` is declared before `auth_mode`, so it is
        # present in info.data here; fall back to the field default if absent.
        env = info.data.get("environment", "development")

        # Stub auth issues a static platform-admin identity to every request
        # with no token. It is only safe in local development. Outside
        # development it is forbidden unless an operator sets an explicit,
        # unambiguous opt-in env var — anything less would be a silent
        # fail-open into unauthenticated admin access.
        if v == "stub" and env != "development":
            import os
            allow_insecure = os.environ.get("MODUS_ALLOW_INSECURE_AUTH") == "1"
            if not allow_insecure:
                import logging
                logging.getLogger(__name__).critical(
                    "FATAL: auth_mode='stub' is forbidden when environment=%r. "
                    "Stub auth grants unauthenticated platform-admin access to "
                    "every request. Set MODUS_AUTH_MODE=jwt and configure "
                    "MODUS_JWT_SECRET, or set MODUS_ALLOW_INSECURE_AUTH=1 "
                    "to explicitly opt in (NOT recommended outside isolated test "
                    "environments). Refusing to start.",
                    env,
                )
                raise ValueError(
                    f"auth_mode='stub' is not allowed when environment='{env}'. "
                    "Set MODUS_AUTH_MODE=jwt and configure JWT secrets, or set "
                    "MODUS_ALLOW_INSECURE_AUTH=1 to explicitly opt in."
                )
        return v

    @field_validator("master_api_key")
    @classmethod
    def validate_master_key(cls, v: str) -> str:
        if not v.startswith("mds_master_"):
            raise ValueError(
                "MASTER_API_KEY must start with 'mds_master_'. "
                "Generate with: python -c \"import secrets; "
                "print('mds_master_' + secrets.token_urlsafe(32))\""
            )
        if v == "mds_master_dev_placeholder_key_change_me":
            import os
            env = os.environ.get("MODUS_ENVIRONMENT", "production")
            if env == "production":
                raise ValueError(
                    "FATAL: Using the placeholder master API key in production is forbidden. "
                    "Generate a real key with: python -c \"import secrets; "
                    "print('mds_master_' + secrets.token_urlsafe(32))\""
                )
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return cached Settings instance.
    Calling get_settings() anywhere in the codebase returns the same instance.
    Use dependency injection in FastAPI handlers for testability:

        def handler(settings: Settings = Depends(get_settings)):
            ...
    """
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
