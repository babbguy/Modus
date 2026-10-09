"""
Modus Agent SDK v1.1.0
=============================
AI cost governance for Python applications.

ZERO-CONFIG — two env vars, one import, first line:

    pip install modus-agent

    export MODUS_URL=https://modus.your-company.com
    export MODUS_TEAM_TOKEN=mds_team_abc123

    # FIRST LINE of your entry point (before any AI SDK imports):
    import modus

SUPPORTED PROVIDERS (auto-instrumented when SDK is installed):
  Anthropic   — claude-haiku, claude-sonnet, claude-opus (sync+async+streaming)
  OpenAI      — gpt-4o, o1, o3, gpt-3.5-turbo, embeddings (sync+async+streaming)
  xAI / Grok  — grok-3, grok-2 via openai SDK (automatic, no extra config)
  Google      — gemini-2.0-flash, gemini-1.5-pro (google-generativeai / google-genai)
  AWS Bedrock — all models via boto3 bedrock-runtime (sync+streaming)
  Groq        — llama-3, mixtral, gemma, deepseek (sync+async+streaming)
  Mistral     — mistral-large, codestral, mixtral (sync+async)
  Cohere      — command-r-plus, command-r (sync+async)
  Azure OAI   — via openai SDK with AzureOpenAI client (automatic)

ANY OTHER PROVIDER — use agent.record() manually:
    agent = modus.get_agent()
    agent.record(provider="my-llm", model="my-model",
                 input_tokens=500, output_tokens=200)

NOTE: import modus must be the FIRST import in your entry point.
      Place it before any anthropic / openai / langchain imports.

Policy violations raise PolicyViolationError:
    from modus import PolicyViolationError
    try:
        response = anthropic_client.messages.create(...)
    except PolicyViolationError as e:
        # e.reason, e.suggested_model, e.decision
        if e.suggested_model:
            response = anthropic_client.messages.create(model=e.suggested_model, ...)
"""

from __future__ import annotations

import atexit
import collections
import fnmatch
import hashlib
import json
import logging
import os
import random
import re
import ssl
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Any, Optional
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

logger = logging.getLogger(__name__)

__version__ = "1.1.0"


@lru_cache(maxsize=1)
def _get_tls_context() -> ssl.SSLContext:
    """TLS 1.2+ for all outbound HTTPS connections.

    Uses ssl.create_default_context() which loads system CA certs automatically
    and is portable across platforms. TLS 1.2 minimum (not 1.3) because many
    customer environments have corporate proxies that terminate at TLS 1.2.
    """
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _urlopen_tls(req: urllib_request.Request, timeout: float) -> Any:
    """urlopen wrapper that enforces TLS 1.3 for HTTPS URLs."""
    url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
    if url.startswith("https://"):
        return urllib_request.urlopen(req, timeout=timeout, context=_get_tls_context())
    return urllib_request.urlopen(req, timeout=timeout)


def _safe_int(env_var: str, default: int) -> int:
    raw = os.getenv(env_var)
    if raw is None:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        logger.warning("Modus: invalid integer for %s=%r, using default %d", env_var, raw, default)
        return default


def _safe_float(env_var: str, default: float) -> float:
    raw = os.getenv(env_var)
    if raw is None:
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        logger.warning("Modus: invalid float for %s=%r, using default %s", env_var, raw, default)
        return default


# ── Public exception ───────────────────────────────────────────────────────────

class PolicyViolationError(Exception):
    """
    Raised when a governance policy denies an AI provider call.

    Attributes:
        decision:        "deny" | "throttle"
        reason:          Human-readable explanation
        policy_id:       UUID of the matching policy (may be None)
        policy_name:     Name of the matching policy (may be None)
        suggested_model: Alternative model the policy recommends (may be None)
        message:         Custom message from the policy definition (may be None)
    """
    def __init__(self, decision: str, reason: str,
                 policy_id: Optional[str] = None,
                 policy_name: Optional[str] = None,
                 suggested_model: Optional[str] = None,
                 message: Optional[str] = None,
                 retry_after_seconds: Optional[int] = None):
        self.decision = decision
        self.reason = reason
        self.policy_id = policy_id
        self.policy_name = policy_name
        self.suggested_model = suggested_model
        self.message = message
        self.retry_after_seconds = retry_after_seconds
        parts = [f"PolicyViolation({decision}): {reason}"]
        if suggested_model:
            parts.append(f"Suggested model: {suggested_model}")
        if retry_after_seconds:
            parts.append(f"Retry after: {retry_after_seconds}s")
        if message:
            parts.append(message)
        super().__init__(" | ".join(parts))


def _retry_after_seconds(exc: HTTPError) -> Optional[float]:
    """Parse a numeric Retry-After header (seconds) from an HTTP error."""
    try:
        raw = exc.headers.get("Retry-After") if exc.headers is not None else None
        if raw is None:
            return None
        val = float(str(raw).strip())
        return val if val >= 0 else None
    except (TypeError, ValueError, AttributeError):
        return None


# HTTP statuses on which an ingest batch is kept and retried (with backoff)
# rather than dropped. 4xx outside this set means the payload itself is
# invalid, so re-sending it can never succeed.
_INGEST_RETRYABLE = frozenset({401, 403, 408, 425, 429, 500, 502, 503, 504})

# Session ids accepted by the orchestrator (usage_records.session_id is 64 chars).
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9._:\-]{1,64}")

# Maximum usage records per raw-format ingest payload (server default
# max_batch_size is 1000).
_RAW_CHUNK = 500


@dataclass
class _PendingBatch:
    """A serialized ingest payload awaiting delivery.

    The body (and its batch_id) is frozen when the batch is built, so every
    retry re-sends the SAME batch_id and the orchestrator's batch-id dedup
    guarantees the usage is counted once even if an earlier attempt was
    accepted but its response was lost.
    """
    batch_id: str
    body: bytes
    attempts: int = 0


# ── Internal data types ────────────────────────────────────────────────────────

@dataclass
class _UsageRecord:
    provider: str
    resource_type: str
    model: Optional[str]
    operation: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    total_tokens: Optional[int]
    input_cost: Optional[Decimal]
    output_cost: Optional[Decimal]
    total_cost: Decimal
    duration_ms: Optional[int]
    timestamp: str
    metadata: Optional[dict] = None


@dataclass
class _AggregationBucket:
    """
    Pre-aggregated usage summary for a (provider, model, operation, resource_type) key
    within a time window. Reduces N raw records to 1 summary on flush.

    SDK-side aggregation: the foundation of billion-call scale.
    Instead of sending every record, we accumulate counters and flush summaries.
    """
    provider: str
    model: Optional[str]
    operation: Optional[str]
    resource_type: str
    call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    input_cost: Decimal = Decimal("0")
    output_cost: Decimal = Decimal("0")
    total_cost: Decimal = Decimal("0")
    duration_ms_sum: int = 0
    duration_ms_min: Optional[int] = None
    duration_ms_max: Optional[int] = None
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    hour: Optional[str] = None  # UTC hour "YYYY-MM-DDTHH" the bucket covers

    def accumulate(self, record: _UsageRecord) -> None:
        """Merge a single usage record into this bucket. O(1), lock-free within bucket."""
        self.call_count += 1
        self.input_tokens += record.input_tokens or 0
        self.output_tokens += record.output_tokens or 0
        self.total_tokens += record.total_tokens or 0
        self.input_cost += record.input_cost or Decimal("0")
        self.output_cost += record.output_cost or Decimal("0")
        self.total_cost += record.total_cost or Decimal("0")
        if record.duration_ms is not None:
            self.duration_ms_sum += record.duration_ms
            if self.duration_ms_min is None or record.duration_ms < self.duration_ms_min:
                self.duration_ms_min = record.duration_ms
            if self.duration_ms_max is None or record.duration_ms > self.duration_ms_max:
                self.duration_ms_max = record.duration_ms
        if self.window_start is None or record.timestamp < self.window_start:
            self.window_start = record.timestamp
        if self.window_end is None or record.timestamp > self.window_end:
            self.window_end = record.timestamp

    def to_dict(self) -> dict:
        """Serialize for the aggregated ingest payload."""
        return {
            "provider": self.provider,
            "model": self.model,
            "operation": self.operation,
            "resource_type": self.resource_type,
            "call_count": self.call_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "input_cost": str(self.input_cost),
            "output_cost": str(self.output_cost),
            "total_cost": str(self.total_cost),
            "duration_ms_sum": self.duration_ms_sum,
            "duration_ms_min": self.duration_ms_min,
            "duration_ms_max": self.duration_ms_max,
            "duration_ms_avg": (
                self.duration_ms_sum // self.call_count
                if self.call_count > 0 else None
            ),
            "window_start": self.window_start,
            "window_end": self.window_end,
        }


class _EvaluateCache:
    """Short-lived bounded cache for policy evaluate responses. TTL: 2s default.
    Uses OrderedDict for O(1) eviction instead of list.remove()."""
    def __init__(self, ttl: float = 2.0, max_entries: int = 256):
        self._ttl = ttl
        self._max_entries = max_entries
        self._store: collections.OrderedDict = collections.OrderedDict()
        self._lock = threading.Lock()

    def _key(self, provider: str, model: Optional[str], env: str) -> str:
        return hashlib.sha256(f"{provider}:{model or ''}:{env}".encode()).hexdigest()

    def get(self, provider: str, model: Optional[str], env: str) -> Optional[dict]:
        k = self._key(provider, model, env)
        with self._lock:
            entry = self._store.get(k)
            if entry and entry[1] > time.monotonic():
                self._store.move_to_end(k)
                return entry[0]
            self._store.pop(k, None)
            return None

    def set(self, provider: str, model: Optional[str], env: str, resp: dict) -> None:
        k = self._key(provider, model, env)
        with self._lock:
            if k in self._store:
                self._store.move_to_end(k)
            while len(self._store) >= self._max_entries:
                self._store.popitem(last=False)
            self._store[k] = (resp, time.monotonic() + self._ttl)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


class _ResponseCache:
    """
    Exact-match response cache using SHA-256 prompt hashing + bounded LRU.

    Intercepts outbound AI calls and returns cached responses for identical
    prompts. No vector store, no embeddings, no new infrastructure.

    Cache key: SHA-256(provider + model + serialized messages/prompt).
    TTL-based expiry with configurable max entries.

    Thread-safe via lock. Memory-bounded — evicts oldest on capacity.
    Uses OrderedDict for O(1) eviction.
    """

    def __init__(self, max_entries: int = 1000, ttl_seconds: float = 3600.0):
        self._max_entries = max_entries
        self._ttl = ttl_seconds
        self._store: collections.OrderedDict[str, tuple[Any, float]] = collections.OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _hash_key(provider: str, model: str, messages: Any) -> str:
        """Compute deterministic cache key from prompt content."""
        raw = json.dumps(
            {"p": provider, "m": model, "msg": messages},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, provider: str, model: str, messages: Any) -> Optional[Any]:
        """Return cached response or None."""
        key = self._hash_key(provider, model, messages)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return None
            response, expiry = entry
            if time.monotonic() > expiry:
                # Expired
                del self._store[key]
                self.misses += 1
                return None
            self._store.move_to_end(key)
            self.hits += 1
            return response

    def put(self, provider: str, model: str, messages: Any, response: Any) -> None:
        """Store a response in the cache."""
        key = self._hash_key(provider, model, messages)
        with self._lock:
            # Evict if at capacity
            if key in self._store:
                self._store.move_to_end(key)
            while len(self._store) >= self._max_entries:
                self._store.popitem(last=False)

            self._store[key] = (response, time.monotonic() + self._ttl)

    def stats(self) -> dict:
        """Return cache statistics."""
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 4) if total > 0 else 0.0,
            "entries": len(self._store),
            "max_entries": self._max_entries,
        }

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self.hits = 0
            self.misses = 0


# ── Agent ──────────────────────────────────────────────────────────────────────

class ModusAgent:
    """
    Main agent. Handles self-registration, enforcement, and usage reporting.

    Preferred usage:
        import modus          # bootstraps automatically

    Direct usage (when you need a handle):
        from modus.agent import ModusAgent
        agent = ModusAgent.from_env().start()

    Availability guarantee (fail_open):
        The SDK NEVER blocks the customer's application because of Modus. By
        default ``fail_open=True``: if the orchestrator is unreachable, times
        out, returns an error, or an unexpected error occurs anywhere in the
        enforcement path, the original LLM call proceeds unmodified. Explicit
        policy denies (budget exceeded, blocked model) still block — fail_open
        governs *unreachable* decisions, not explicit ones.

        Operators who prefer availability-blocking enforcement can opt into
        fail-closed with ``ModusAgent(..., fail_open=False)`` or the
        ``MODUS_FAIL_OPEN=false`` environment variable. This is the client-side
        mirror of the server's fail-closed default (MODUS_ENFORCEMENT_FAIL_OPEN).
    """

    def __init__(
        self,
        orchestrator_url: str,
        team_token: str,
        app_id: Optional[str] = None,
        app_name: Optional[str] = None,
        environment: Optional[str] = None,
        fail_open: bool = True,
        flush_interval: int = 30,
        evaluate_cache_ttl: float = 2.0,
        timeout: float = 3.0,
        local_budget_cap_usd: Optional[float] = None,
        auto_optimize: bool = False,
        scrub_pii: bool = False,
        max_buffer_size: int = 10000,
        response_cache_enabled: bool = False,
        response_cache_max_entries: int = 1000,
        response_cache_ttl: float = 3600.0,
        aggregation_enabled: bool = True,
        trace_sample_rate: float = 0.01,
    ):
        self.orchestrator_url = orchestrator_url.rstrip("/")
        self.team_token = team_token
        self.environment = environment or os.getenv("MODUS_ENVIRONMENT", "production")
        self.fail_open = fail_open
        self.flush_interval = flush_interval
        self.timeout = timeout

        # ── Local soft cap (fail-open safety net) ─────────────────────────────
        # When the gateway is unreachable and fail_open=True, this client-side
        # budget cap prevents runaway spend from agent loops.
        # Set via MODUS_LOCAL_BUDGET_CAP_USD or constructor param.
        self.local_budget_cap_usd = Decimal(str(
            local_budget_cap_usd
            or os.getenv("MODUS_LOCAL_BUDGET_CAP_USD", "0")
        ))
        self._local_spend_usd = Decimal("0")
        self._local_spend_lock = threading.Lock()
        self._gateway_consecutive_failures = 0
        self._gateway_last_success: float = time.monotonic()
        self._circuit_breaker_threshold = 5
        self._circuit_breaker_cooldown = 60.0
        self._circuit_breaker_tripped_at: Optional[float] = None

        self._app_id_hint = app_id
        self._app_name_hint = app_name
        self._api_key: Optional[str] = None
        self._app_uuid: Optional[str] = None
        self._app_id: Optional[str] = None
        self._team_id: Optional[str] = None

        self._records: list[_UsageRecord] = []
        self._records_lock = threading.Lock()
        self._cache = _EvaluateCache(ttl=evaluate_cache_ttl)
        self._instrumented: list[str] = []
        self._sdk_versions: dict[str, str] = {}
        self._auto_optimize = auto_optimize
        self._scrub_pii = scrub_pii
        self._max_buffer_size = max_buffer_size
        self._local_policies: list[dict] = []
        self._last_policy_sync: float = 0

        # ── PII Input Scanning — DISABLED ─────────────────────────────────────
        # PII scanning disabled — Modus does not process PII. Module
        # retained pending architectural review. Env vars are ignored.
        self._pii_scan_enabled = False
        self._pii_action = os.environ.get("MODUS_PII_ACTION", "redact")  # "redact" | "block" | "log"

        # ── Response cache (Phase 4c) ─────────────────────────────────────────
        # Exact-match SHA-256 cache. No vector store, no embeddings.
        # Enabled via MODUS_RESPONSE_CACHE="true" or constructor param.
        # Semantic mode (Phase 4): opt-in via MODUS_SEMANTIC_CACHE="true".
        # Uses ONNX embedding model for similarity matching (35-60% hit rate
        # vs 8-15% exact). Falls back to exact-match if onnxruntime not installed.
        self._response_cache_enabled = response_cache_enabled
        self._response_cache: Optional[_ResponseCache] = None
        self._semantic_cache_enabled = (
            os.getenv("MODUS_SEMANTIC_CACHE", "false").lower() == "true"
        )
        if self._response_cache_enabled:
            if self._semantic_cache_enabled:
                try:
                    from modus.semantic_cache import SemanticCache
                    self._response_cache = SemanticCache(
                        max_entries=response_cache_max_entries,
                        ttl_seconds=response_cache_ttl,
                    )
                    logger.info("Semantic response cache enabled")
                except Exception:
                    # Fall back to exact-match cache
                    self._response_cache = _ResponseCache(
                        max_entries=response_cache_max_entries,
                        ttl_seconds=response_cache_ttl,
                    )
            else:
                self._response_cache = _ResponseCache(
                    max_entries=response_cache_max_entries,
                    ttl_seconds=response_cache_ttl,
                )

        # ── Routing state (Phase 2) ──────────────────────────────────────────
        self._routing_enabled = os.getenv("MODUS_ROUTING_ENABLED", "true").lower() != "false"
        self._routing_generic_threshold = _safe_float("MODUS_ROUTING_GENERIC_THRESHOLD", 0.70)
        self._routing_outcomes: list[dict] = []
        self._routing_outcomes_lock = threading.Lock()

        # ── SDK-side aggregation (Scale Pack foundation) ─────────────────────
        # Instead of sending N raw records, accumulate into buckets keyed by
        # (provider, model, operation, resource_type) and flush summaries.
        # Reduces ingest traffic 100-1000x at high call volumes.
        # Policy violations and sampled traces always sent at full detail.
        self._aggregation_enabled = (
            aggregation_enabled
            if not os.getenv("MODUS_AGGREGATION_ENABLED")
            else os.getenv("MODUS_AGGREGATION_ENABLED", "true").lower() != "false"
        )
        self._trace_sample_rate = _safe_float("MODUS_TRACE_SAMPLE_RATE", trace_sample_rate)
        self._agg_buckets: dict[str, _AggregationBucket] = {}
        self._agg_lock = threading.Lock()
        self._agg_sampled: list[_UsageRecord] = []  # sampled raw traces
        self._trace_counter: int = 0  # monotonic counter for deterministic sampling

        # ── Ingest delivery (exactly-once with server batch-id dedup) ────────
        # Built payloads wait here until the orchestrator accepts them; a
        # retry re-sends the identical body (same batch_id). On 429/503 the
        # agent backs off (Retry-After or exponential, with jitter).
        self._pending_batches: list[_PendingBatch] = []
        self._max_pending_batches = _safe_int("MODUS_MAX_PENDING_BATCHES", 1000)
        self._flush_lock = threading.Lock()
        self._ingest_backoff_until: float = 0.0
        self._ingest_backoff_attempt: int = 0

        # ── Evaluate rate-limit handling ─────────────────────────────────────
        # A 429 from /policy/evaluate means "slow down", not "unreachable":
        # retry with jittered backoff inside the call's timeout budget, then
        # fall back to the last decision the server gave for the same
        # (provider, model, environment). It never trips the circuit breaker.
        self._evaluate_429_retries = _safe_int("MODUS_EVALUATE_429_RETRIES", 2)
        self._last_decisions: "collections.OrderedDict[tuple, tuple[float, dict]]" = collections.OrderedDict()
        self._last_decision_ttl = _safe_float("MODUS_LAST_DECISION_TTL", 300.0)
        self.rate_limited_count = 0  # evaluate/ingest 429s seen (diagnostics)

        # ── Attribution / span tracking (Phase 5) ────────────────────────────
        # Thread-local span stack: each thread has its own call chain.
        # Zero overhead when no session is active (stack is empty).
        self._span_local = threading.local()
        self._framework_tier: Optional[str] = None  # cached after first detection
        self._retry_circuit_max = _safe_int("MODUS_RETRY_CIRCUIT_MAX", 10)

        # ── Rewind engine (Phase: Autonomous Governance) ──────────────────
        # SDK-side rewind hooks for post-call anomaly rollback.
        # Zero overhead when no hooks registered (empty dict check).
        from modus.rewind import RewindRegistry
        self._rewind_registry = RewindRegistry()

        self._flush_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._rescan_thread: Optional[threading.Thread] = None
        self._last_topology_hash: Optional[str] = None
        self._shutdown = threading.Event()
        self._started = False

    @classmethod
    def from_env(cls) -> "ModusAgent":
        """Construct from environment variables."""
        url = os.getenv("MODUS_URL", os.getenv("MODUS_ORCHESTRATOR_URL", ""))
        token = os.getenv("MODUS_TEAM_TOKEN", "")
        if not url or not token:
            missing = []
            if not url: missing.append("MODUS_URL")
            if not token: missing.append("MODUS_TEAM_TOKEN")
            raise ValueError(f"Missing: {', '.join(missing)}")
        return cls(
            orchestrator_url=url,
            team_token=token,
            app_id=os.getenv("MODUS_APP_ID"),
            app_name=os.getenv("MODUS_APP_NAME"),
            environment=os.getenv("MODUS_ENVIRONMENT", "production"),
            fail_open=os.getenv("MODUS_FAIL_OPEN", "true").lower() != "false",
            flush_interval=_safe_int("MODUS_FLUSH_INTERVAL", 30),
            evaluate_cache_ttl=_safe_float("MODUS_EVALUATE_CACHE_TTL", 2.0),
            timeout=_safe_float("MODUS_TIMEOUT", 3.0),
            local_budget_cap_usd=_safe_float("MODUS_LOCAL_BUDGET_CAP_USD", 0.0) or None,
            max_buffer_size=_safe_int("MODUS_MAX_BUFFER_SIZE", 10000),
            auto_optimize=os.getenv("MODUS_AUTO_OPTIMIZE", "false").lower() == "true",
            scrub_pii=os.getenv("MODUS_SCRUB_PII", "false").lower() == "true",
            response_cache_enabled=os.getenv("MODUS_RESPONSE_CACHE", "false").lower() == "true",
            response_cache_max_entries=_safe_int("MODUS_RESPONSE_CACHE_MAX_ENTRIES", 1000),
            response_cache_ttl=_safe_float("MODUS_RESPONSE_CACHE_TTL", 3600.0),
            aggregation_enabled=os.getenv("MODUS_AGGREGATION_ENABLED", "true").lower() != "false",
            trace_sample_rate=_safe_float("MODUS_TRACE_SAMPLE_RATE", 0.01),
        )

    # ── Rewind hooks (Autonomous Governance) ─────────────────────────────

    def register_rewind_hook(
        self,
        tool_name: str,
        rollback_fn,
    ) -> None:
        """
        Register a rewind hook for a tool.

        When a post-call anomaly is detected (circuit breaker trip, output
        validation failure, budget suspension), all registered rewind hooks
        fire in LIFO order to roll back side effects.

        Args:
            tool_name: Unique identifier (e.g., "database", "file_system")
            rollback_fn: Callable(context_dict) -> bool. Must be idempotent.

        Example::

            agent.register_rewind_hook("database",
                lambda ctx: db.rollback(ctx.get("savepoint")))
        """
        self._rewind_registry.register(tool_name, rollback_fn)

    def unregister_rewind_hook(self, tool_name: str) -> None:
        """Remove a rewind hook."""
        self._rewind_registry.unregister(tool_name)

    def _trigger_rewind(
        self,
        trigger_reason: str,
        error_detail: Optional[str] = None,
    ) -> None:
        """
        Internal: trigger rewind on anomaly. Called by circuit breaker,
        output validator, and budget suspension code paths.
        """
        if not self._rewind_registry.has_hooks:
            return  # zero overhead when no hooks registered

        from modus.rewind import RewindContext, report_rewind_event

        # Build context from current session state
        stack = self._get_span_stack()
        session_id = stack[-1].get("session_id") if stack else None

        context = RewindContext(
            trigger_reason=trigger_reason,
            session_id=session_id,
            error_detail=error_detail,
            tokens_consumed=0,
            cost_consumed=float(self._local_spend_usd),
        )

        actions = self._rewind_registry.trigger(context)

        # Fire-and-forget: report to orchestrator for distillation
        if self._api_key and self.orchestrator_url:
            try:
                threading.Thread(
                    target=report_rewind_event,
                    args=(
                        self.orchestrator_url,
                        self._api_key,
                        self._app_id or "",
                        self._team_id or "",
                        context,
                        actions,
                    ),
                    daemon=True,
                ).start()
            except Exception:
                pass  # never fail the caller

    def start(self) -> "ModusAgent":
        """
        1. Discover environment  2. Self-register  3. Report topology
        4. Instrument all AI SDKs  5. Start background threads
        """
        if self._started:
            return self

        # Step 1 — discover
        snapshot = None
        try:
            from modus.discovery import EnvironmentScanner
            snapshot = EnvironmentScanner().scan(
                app_id=self._app_id_hint,
                app_name=self._app_name_hint,
                environment=self.environment,
                agent_version=__version__,
            )
            self._app_id_hint = snapshot.app_id
            self._app_name_hint = snapshot.app_name
        except Exception as exc:
            logger.debug("Modus: discovery failed: %s", exc)
            import re
            import pathlib
            self._app_id_hint = self._app_id_hint or re.sub(
                r"[^a-z0-9\-]", "",
                pathlib.Path.cwd().name.lower().replace("_", "-")
            ) or "unnamed-app"
            self._app_name_hint = (self._app_name_hint
                                   or self._app_id_hint.replace("-", " ").title())

        # Step 2 — self-register
        if not self._self_register():
            logger.warning(
                "Modus: registration failed — check MODUS_URL "
                "and MODUS_TEAM_TOKEN. Governance disabled."
            )
            self._started = True
            return self

        # Step 3 — topology
        if snapshot:
            self._last_topology_hash = snapshot.content_hash()
            try:
                self._report_topology(snapshot)
            except Exception as exc:
                logger.debug("Modus: topology failed: %s", exc)

        # Step 4 — detect framework tier for attribution (Phase 5)
        self._framework_tier = self._detect_framework_tier()

        # Step 5 — instrument every supported SDK
        self._instrument_anthropic()    # Anthropic (sync + async + stream)
        self._instrument_openai()       # OpenAI / xAI Grok / Azure (sync + async + stream)
        self._instrument_deepseek()    # DeepSeek (covered via OpenAI instrumentation)
        self._instrument_boto3()        # AWS Bedrock (sync + streaming)
        self._instrument_google_genai() # Google Gemini (sync + async)
        self._instrument_groq()         # Groq (sync + async + stream)
        self._instrument_mistral()      # Mistral (sync + async)
        self._instrument_cohere()       # Cohere (sync + async)

        # Step 6 — start background threads
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()

        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

        self._policy_thread = threading.Thread(target=self._policy_sync_loop, daemon=True)
        self._policy_thread.start()

        self._rescan_thread = threading.Thread(target=self._rescan_loop, daemon=True)
        self._rescan_thread.start()

        self._started = True
        detected = ", ".join(self._instrumented) if self._instrumented else "none detected"
        logger.info(
            "Modus active | app=%s env=%s providers=[%s] fail_open=%s v%s",
            self._app_id, self.environment, detected, self.fail_open, __version__,
        )
        if not self._instrumented:
            logger.info(
                "Modus: no AI SDK found yet. Install anthropic, openai, "
                "boto3, google-generativeai, groq, mistralai, or cohere and they "
                "will be tracked automatically. Use agent.record() for any other provider."
            )
        atexit.register(self._shutdown_flush)
        return self

    # ── Self-registration ──────────────────────────────────────────────────────

    def _self_register(self) -> bool:
        payload = json.dumps({
            "app_id": self._app_id_hint,
            "app_name": self._app_name_hint or self._app_id_hint,
            "environment": self.environment,
            "agent_version": __version__,
        }).encode()
        try:
            req = urllib_request.Request(
                f"{self.orchestrator_url}/api/v1/self-register",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Modus-TeamToken": self.team_token,
                    "User-Agent": f"ModusAgent/{__version__}",
                },
                method="POST",
            )
            with _urlopen_tls(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
            self._api_key = data["api_key"]
            self._app_uuid = data["app_uuid"]
            self._app_id = data["app_id"]
            self._team_id = data["team_id"]
            action = "registered" if data.get("registered") else "reconnected"
            logger.info("Modus: %s '%s' in team '%s'",
                        action, self._app_id, data.get("team_slug", ""))
            return True
        except Exception as exc:
            logger.error("Modus: registration failed: %s", exc)
            return False

    def _sync_policies(self) -> None:
        """Fetch active policies for this app to enable local enforcement."""
        if not self._api_key:
            return
        try:
            req = urllib_request.Request(
                f"{self.orchestrator_url}/api/v1/policies/sync",
                data=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "X-Modus-APIKey": self._api_key,
                    "User-Agent": f"ModusAgent/{__version__}",
                },
                method="POST",
            )
            with _urlopen_tls(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
            policies = data.get("policies", [])
            if not isinstance(policies, list):
                raise ValueError("policy sync response has no 'policies' list")
            self._local_policies = policies
            self._last_policy_sync = time.monotonic()
            logger.debug("Modus: synced %d local policies", len(self._local_policies))
        except Exception as exc:
            # Keep the last good policy set; the gateway remains the source of truth.
            logger.warning("Modus: policy sync failed (keeping %d cached policies): %s",
                           len(self._local_policies), exc)

    def _policy_sync_loop(self) -> None:
        """Background thread to keep local policies fresh (every 5 mins)."""
        self._sync_policies()
        while not self._shutdown.is_set():
            self._shutdown.wait(timeout=300)
            if not self._shutdown.is_set():
                self._sync_policies()

    # ── Topology ───────────────────────────────────────────────────────────────

    def _report_topology(self, snapshot) -> None:
        if not self._api_key:
            return
        snap_dict = snapshot.to_dict() if hasattr(snapshot, "to_dict") else dict(snapshot)
        try:
            req = urllib_request.Request(
                f"{self.orchestrator_url}/api/v1/topology",
                data=json.dumps({"snapshot": snap_dict}).encode(),
                headers={"Content-Type": "application/json",
                         "X-Modus-APIKey": self._api_key,
                         "User-Agent": f"ModusAgent/{__version__}"},
                method="POST",
            )
            with _urlopen_tls(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            summary = data.get("ai_summary", "")
            if summary:
                logger.info("Modus topology: %s", summary)
        except Exception as exc:
            logger.debug("Modus: topology report failed: %s", exc)

    # ── Topology Re-scan ─────────────────────────────────────────────────────

    def _rescan_loop(self) -> None:
        """
        Background thread: periodically re-scan the environment for changes.

        Detects:
          - New packages installed (pip install adds new AI SDK, database driver, etc.)
          - New service dependencies (new env vars like DATABASE_URL, REDIS_URL)
          - New API routes added to the web framework
          - Infrastructure changes (deployment type, cloud metadata)

        Only pushes to Orchestrator when something actually changed (hash comparison).
        Runs every 5 minutes — lightweight scan, zero impact on app performance.
        """
        while not self._shutdown.is_set():
            self._shutdown.wait(timeout=300)  # 5 minutes
            if self._shutdown.is_set():
                break
            try:
                from modus.discovery import EnvironmentScanner
                snapshot = EnvironmentScanner().scan(
                    app_id=self._app_id_hint,
                    app_name=self._app_name_hint,
                    environment=self.environment,
                    agent_version=__version__,
                )
                new_hash = snapshot.content_hash()
                if new_hash != self._last_topology_hash:
                    logger.info(
                        "Modus: environment change detected (hash %s → %s), reporting topology",
                        self._last_topology_hash, new_hash,
                    )
                    self._report_topology(snapshot)
                    self._last_topology_hash = new_hash
            except Exception as exc:
                logger.debug("Modus: re-scan failed: %s", exc)

    # ── Enforcement ────────────────────────────────────────────────────────────

    def enforce(
        self,
        provider: str,
        model: Optional[str] = None,
        estimated_tokens: Optional[int] = None,
        estimated_cost: Optional[Decimal] = None,
        resource_type: str = "llm_call",
        bypass_cache: bool = False,
    ) -> Optional[str]:
        """
        Pre-call governance check. Raises PolicyViolationError to block the call.
        Silent no-op if agent is not registered (fail-open).

        Returns: suggested_model if the server recommends a downshift (degradation
        ladder), None otherwise.  Callers may honour this to proactively route to
        a cheaper model before hitting the hard cap.

        Safety layers (evaluated in order):
          1. Local budget cap — client-side hard stop during gateway outages
          2. Gateway evaluate — full policy + hierarchy check
          3. Fail-open/closed — fallback when gateway unreachable
        """
        if not self._api_key:
            return

        # ── 1. Estimate cost from pricing table (before any budget checks) ────
        if estimated_cost is None and estimated_tokens is not None:
            try:
                from modus.pricing import estimate_tokens_cost
                estimated_cost = estimate_tokens_cost(provider, model, estimated_tokens)
            except Exception:
                pass

        # ── 2. Local budget cap (client-side safety net) ──────────────────────
        # Prevents runaway spend when the gateway is unreachable and fail_open
        # is True.  Checks both cumulative spend AND projected spend (current +
        # this call's estimate) so we reject calls that would breach the cap
        # without ever hitting the provider API.
        if self.local_budget_cap_usd > 0:
            with self._local_spend_lock:
                projected = self._local_spend_usd + (abs(estimated_cost) if estimated_cost else Decimal("0"))
                if projected > self.local_budget_cap_usd:
                    est_str = f" Estimated call cost: ${estimated_cost:.4f}." if estimated_cost else ""
                    raise PolicyViolationError(
                        decision="deny",
                        reason=(
                            f"Local SDK budget cap would be exceeded: "
                            f"${self._local_spend_usd:.4f} spent + "
                            f"${estimated_cost or 0:.4f} estimated = "
                            f"${projected:.4f} > "
                            f"${self.local_budget_cap_usd:.4f} cap.{est_str} "
                            f"Call blocked before hitting provider API."
                        ),
                        policy_name="sdk_budget_precheck",
                    )
                if self._local_spend_usd >= self.local_budget_cap_usd:
                    raise PolicyViolationError(
                        decision="deny",
                        reason=(
                            f"Local SDK budget cap exceeded: "
                            f"${self._local_spend_usd:.4f} >= "
                            f"${self.local_budget_cap_usd:.4f} cap. "
                            f"Reset the agent or raise the cap."
                        ),
                        policy_name="sdk_local_budget_cap",
                    )

        # ── 3. Local evaluation (fast-path / fallback) ────────────────────────
        local_result = self._evaluate_local(provider, model, estimated_tokens, estimated_cost)
        if local_result and local_result.get("decision") != "allow":
            # If local says deny/throttle, we stop immediately.
            # This is a hard-stop for known violations without a network roundtrip.
            result = local_result
        else:
            # ── 4. Gateway evaluate (source of truth) ─────────────────────────
            use_cache = (not bypass_cache
                         and estimated_tokens is None
                         and estimated_cost is None)

            if use_cache:
                cached = self._cache.get(provider, model, self.environment)
                if cached:
                    result = cached
                else:
                    result = self._call_evaluate(provider, model, estimated_tokens,
                                                 estimated_cost, resource_type)
                    if result.get("decision") == "allow":
                        self._cache.set(provider, model, self.environment, result)
            else:
                result = self._call_evaluate(provider, model, estimated_tokens,
                                             estimated_cost, resource_type)

        # ── 5. Track local spend on allow (for local cap accounting) ──────────
        decision = result.get("decision", "allow")
        if decision == "allow" and estimated_cost is not None:
            with self._local_spend_lock:
                self._local_spend_usd += abs(estimated_cost)

        if decision == "allow":
            # Return suggested_model if present (degradation ladder downshift)
            return result.get("suggested_model")

        if decision == "throttle":
            wait = min(result.get("retry_after_seconds", 60), 300)
            logger.warning("Modus: throttled by '%s' — retry after %ds",
                           result.get("policy_name", "?"), wait)

        if result.get("decision") in ("deny", "throttle"):
            raise PolicyViolationError(
                decision=result["decision"],
                reason=result.get("reason", "Blocked by governance policy."),
                policy_id=result.get("policy_id"),
                policy_name=result.get("policy_name"),
                message=result.get("message"),
                suggested_model=result.get("suggested_model"),
                retry_after_seconds=wait if decision == "throttle" else None,
            )

    def _enforce_with_retry(self, provider: str, model: Optional[str],
                           estimated_tokens: Optional[int] = None) -> str:
        """
        Enforce policy with automatic retry/substitution if auto_optimize is enabled.
        Returns the (possibly substituted) model name.

        Handles two downshift paths:
          1. Degradation ladder — server returns allow + suggested_model (budget %)
          2. Hard deny — PolicyViolationError with suggested_model (budget exceeded)
        """
        try:
            suggested = self.enforce(provider=provider, model=model,
                                     estimated_tokens=estimated_tokens)
            # Degradation ladder: server allowed but suggested a cheaper model
            if self._auto_optimize and suggested and suggested != model:
                logger.info("Modus: degradation ladder '%s' → '%s' (budget %%)",
                            model, suggested)
                return suggested
            return model
        except PolicyViolationError as e:
            # A PolicyViolationError is a *deliberate* decision — the server
            # (or the local budget cap) explicitly denied/throttled this call.
            # It propagates regardless of fail_open: fail_open governs what
            # happens when Modus cannot REACH a decision, not whether we honour
            # an explicit deny.
            if self._auto_optimize and e.suggested_model:
                logger.info("Modus: auto-optimizing '%s' from '%s' to '%s'",
                            provider, model, e.suggested_model)
                try:
                    # Re-enforce for the new model (to ensure it's also allowed)
                    self.enforce(provider=provider, model=e.suggested_model,
                                  estimated_tokens=estimated_tokens)
                    return e.suggested_model
                except PolicyViolationError:
                    # The substitute was also denied — propagate the original.
                    raise e
            raise e
        except Exception as e:  # noqa: BLE001 — deliberate catch-all
            # NEVER-BLOCK GUARANTEE: any *unexpected* error in the enforcement
            # path (a bug in local evaluation, pricing, the cache, an SDK
            # incompatibility, etc.) must not take down the customer's app.
            # When fail_open (the default), we log and let the ORIGINAL call
            # proceed unmodified. Only an operator who has explicitly opted into
            # fail-closed enforcement sees the error propagate and block.
            if self.fail_open:
                logger.error(
                    "Modus: unexpected enforcement error (%s) — allowing call "
                    "unmodified (fail-open). This is an SDK-side safeguard; the "
                    "customer application is never blocked by Modus internals.",
                    e,
                )
                return model
            logger.error(
                "Modus: unexpected enforcement error (%s) — blocking call "
                "(fail-closed). Set MODUS_FAIL_OPEN=true to prefer availability.",
                e,
            )
            raise

    def _scrub_pii_if_enabled(self, data: Any) -> Any:
        """Scrub PII from data if enabled (legacy output scrubbing)."""
        if not self._scrub_pii:
            return data
        try:
            from modus.pii import scrub_pii_recursive
            return scrub_pii_recursive(data)
        except Exception:
            return data

    def _scan_and_redact_pii(self, messages: Any) -> tuple[Any, list[dict]]:
        """
        Scan input messages for PII patterns. Returns (messages, findings).

        Only active when MODUS_PII_SCAN is set. Operates in-place on
        message dicts for performance (avoids deep copy on hot path).

        findings: list of {"type": <pattern_name>, "count": <int>}.
        Never logs or returns actual PII content.
        """
        if not self._pii_scan_enabled:
            return messages, []

        try:
            from modus.pii import scan_and_redact_pii, scan_pii
        except (ImportError, Exception):
            return messages, []

        all_findings: list[dict] = []
        action = self._pii_action

        if isinstance(messages, list):
            for msg in messages:
                if isinstance(msg, dict):
                    content = msg.get("content", "")
                    if isinstance(content, str) and content:
                        if action == "log":
                            # Log-only: scan but don't redact
                            findings = scan_pii(content)
                        else:
                            # Redact (or block — scan first, block later)
                            content, findings = scan_and_redact_pii(content)
                            msg["content"] = content
                        all_findings.extend(findings)
                    elif isinstance(content, list):
                        # Multi-part content (e.g. Anthropic content blocks)
                        for part in content:
                            if isinstance(part, dict):
                                text = part.get("text", "")
                                if isinstance(text, str) and text:
                                    if action == "log":
                                        findings = scan_pii(text)
                                    else:
                                        text, findings = scan_and_redact_pii(text)
                                        part["text"] = text
                                    all_findings.extend(findings)
                elif isinstance(msg, str) and msg:
                    # Plain string in list (e.g. Google GenAI contents)
                    if action == "log":
                        findings = scan_pii(msg)
                        all_findings.extend(findings)
                    else:
                        redacted, findings = scan_and_redact_pii(msg)
                        all_findings.extend(findings)
                        if findings:
                            # Can't modify string in-place; caller handles via return
                            messages[messages.index(msg)] = redacted
        elif isinstance(messages, str):
            if action == "log":
                all_findings.extend(scan_pii(messages))
            else:
                messages, findings = scan_and_redact_pii(messages)
                all_findings.extend(findings)

        return messages, all_findings

    def _apply_pii_scan(self, messages: Any, context: str = "input") -> Any:
        """
        Apply PII scanning to messages with configured action.

        Combines _scan_and_redact_pii with action handling (redact/block/log).
        Raises PolicyViolationError if action is "block" and PII is found.
        Returns (possibly redacted) messages otherwise.
        """
        if not self._pii_scan_enabled:
            # Still apply legacy scrubbing if enabled
            return self._scrub_pii_if_enabled(messages)

        messages, findings = self._scan_and_redact_pii(messages)

        if findings:
            summary = ", ".join(f"{f['type']}({f['count']})" for f in findings)
            if self._pii_action == "block":
                raise PolicyViolationError(
                    decision="deny",
                    reason=f"PII detected in {context}: {', '.join(f['type'] for f in findings)}",
                )
            # Log PII detection — never log actual content, only pattern names + counts
            logger.warning("Modus: PII detected in %s — %s", context, summary)

        return messages

    def _cache_check(self, provider: str, model: str, messages: Any) -> Optional[Any]:
        """Check response cache. Returns cached response or None."""
        if not self._response_cache:
            return None
        return self._response_cache.get(provider, model or "", messages)

    def _cache_store(self, provider: str, model: str, messages: Any, response: Any) -> None:
        """Store a response in the cache."""
        if not self._response_cache:
            return
        self._response_cache.put(provider, model or "", messages, response)

    @property
    def response_cache_stats(self) -> Optional[dict]:
        """Return response cache statistics, or None if cache is disabled."""
        if not self._response_cache:
            return None
        return self._response_cache.stats()

    def _evaluate_local(self, provider: str, model: Optional[str],
                        tokens: Optional[int], cost: Optional[Decimal]) -> Optional[dict]:
        """
        Evaluate against the locally synced policy set.
        Only covers stateless policies: model_allowlist, model_denylist, provider_block, environment_block.
        Stateful policies (budget, rate_limit) still require Gateway evaluate.
        """
        if not self._local_policies:
            return None

        # Same ordering the gateway uses: app > team > platform, then priority.
        scope_rank = {"app": 0, "team": 1, "platform": 2}
        ordered = sorted(
            self._local_policies,
            key=lambda p: (scope_rank.get(p.get("scope"), 99), p.get("priority", 100)),
        )
        for p in ordered:
            ptype = p.get("policy_type")
            cfg = p.get("config") or {}
            effect = p.get("effect", "deny")
            match = False

            if not self._local_conditions_match(p.get("conditions"), provider, model):
                continue

            if ptype == "model_allowlist":
                models = cfg.get("models", [])
                if not model or model not in models:
                    match = True
            elif ptype == "model_denylist":
                models = cfg.get("models", [])
                if model and model in models:
                    match = True
            elif ptype == "provider_block":
                providers = cfg.get("providers", [])
                if provider in providers:
                    match = True
            elif ptype == "environment_block":
                envs = cfg.get("environments", [])
                if self.environment in envs:
                    match = True

            if not match:
                continue
            if effect == "warn":
                # warn never blocks; the gateway records it.
                logger.warning("Modus: local policy '%s' warns for %s/%s",
                               p.get("name"), provider, model)
                continue
            action = p.get("action") or {}
            return {
                "decision": effect,
                "reason": f"Blocked by local policy: {p.get('name')}",
                "policy_id": p.get("id"),
                "policy_name": p.get("name"),
                "suggested_model": action.get("suggested_model") or p.get("suggested_model"),
                "message": action.get("message"),
                "retry_after_seconds": action.get("retry_after_seconds", 60)
                if effect == "throttle" else None,
            }
        return None

    def _local_conditions_match(self, conditions: Optional[dict], provider: str,
                                model: Optional[str]) -> bool:
        """Mirror of the gateway's condition matching (AND of all conditions)."""
        if not conditions:
            return True
        if "providers" in conditions and provider not in conditions["providers"]:
            return False
        if "model_pattern" in conditions:
            if not model or not fnmatch.fnmatch(model.lower(),
                                                str(conditions["model_pattern"]).lower()):
                return False
        if "environments" in conditions and self.environment not in conditions["environments"]:
            return False
        if "resource_types" in conditions and "llm_call" not in conditions["resource_types"]:
            return False
        return True

    def _call_evaluate(self, provider, model, estimated_tokens,
                       estimated_cost, resource_type) -> dict:
        # Circuit breaker — skip gateway call after repeated failures
        if self._circuit_breaker_tripped_at is not None:
            elapsed = time.monotonic() - self._circuit_breaker_tripped_at
            if elapsed < self._circuit_breaker_cooldown:
                return ({"decision": "allow", "reason": "Circuit breaker OPEN (fail-open)."}
                        if self.fail_open else
                        {"decision": "deny", "reason": "Circuit breaker OPEN (fail-closed)."})
            # Half-open: cooldown expired — allow one attempt through
            logger.info("Modus: circuit breaker half-open, retrying gateway.")
            self._circuit_breaker_tripped_at = None

        payload: dict[str, Any] = {
            "provider": provider, "resource_type": resource_type,
            "environment": self.environment,
        }
        if model: payload["model"] = model
        if estimated_tokens is not None: payload["estimated_tokens"] = estimated_tokens
        if estimated_cost is not None: payload["estimated_cost"] = str(estimated_cost)

        decision_key = (provider, model, self.environment)
        deadline = time.monotonic() + max(self.timeout, 0.5)
        attempt = 0
        while True:
            try:
                return self._post_evaluate(payload, decision_key)
            except HTTPError as exc:
                if exc.code != 429:
                    return self._evaluate_http_error(exc)
                # 429 = the orchestrator is up and asking us to slow down.
                # Not a failure: never counts toward the circuit breaker.
                self.rate_limited_count += 1
                attempt += 1
                retry_after = _retry_after_seconds(exc)
                if retry_after is not None:
                    delay = retry_after * random.uniform(1.0, 1.2)
                else:
                    delay = random.uniform(0.05, 0.1 * (2 ** attempt))
                if (attempt <= self._evaluate_429_retries
                        and time.monotonic() + delay < deadline):
                    logger.info(
                        "Modus: evaluate rate limited (429) — retry %d/%d in %.2fs",
                        attempt, self._evaluate_429_retries, delay,
                    )
                    time.sleep(delay)
                    continue
                return self._rate_limited_decision(decision_key)
            except URLError as exc:
                return self._evaluate_unreachable(exc)
            except Exception as exc:
                self._gateway_consecutive_failures += 1
                self._maybe_trip_circuit_breaker()
                logger.error("Modus: evaluate error: %s", exc)
                return ({"decision": "allow"} if self.fail_open else
                        {"decision": "deny", "reason": str(exc)})

    def _post_evaluate(self, payload: dict, decision_key: tuple) -> dict:
        req = urllib_request.Request(
            f"{self.orchestrator_url}/api/v1/policy/evaluate",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "X-Modus-APIKey": self._api_key,
                     "User-Agent": f"ModusAgent/{__version__}"},
            method="POST",
        )
        with _urlopen_tls(req, timeout=self.timeout) as resp:
            result = json.loads(resp.read().decode())
        # Gateway success — reset failure tracking and circuit breaker
        self._gateway_consecutive_failures = 0
        self._gateway_last_success = time.monotonic()
        self._circuit_breaker_tripped_at = None
        if isinstance(result, dict) and result.get("decision"):
            self._last_decisions[decision_key] = (time.monotonic(), result)
            self._last_decisions.move_to_end(decision_key)
            while len(self._last_decisions) > 256:
                self._last_decisions.popitem(last=False)
        return result

    def _rate_limited_decision(self, decision_key: tuple) -> dict:
        """Decision to use when evaluate stays rate limited past the retry budget.

        Keeps enforcing: reuse the server's most recent decision for the same
        (provider, model, environment) — a recent deny keeps denying. Only
        with no recent decision does fail_open/fail_closed apply, and that is
        logged as rate limiting, not as an outage.
        """
        entry = self._last_decisions.get(decision_key)
        if entry is not None and time.monotonic() - entry[0] <= self._last_decision_ttl:
            age = time.monotonic() - entry[0]
            result = dict(entry[1])
            logger.warning(
                "Modus: evaluate still rate limited (429) — enforcing last server "
                "decision '%s' from %.0fs ago for %s/%s.",
                result.get("decision"), age, decision_key[0], decision_key[1],
            )
            result["reason"] = (
                f"{result.get('reason') or ''} [last server decision, {age:.0f}s old; "
                "evaluate rate limited]"
            ).strip()
            return result
        logger.warning(
            "Modus: evaluate still rate limited (429) and no recent decision for "
            "%s/%s — %s (local policies and the local budget cap still apply).",
            decision_key[0], decision_key[1],
            "allowing (fail-open)" if self.fail_open else "denying (fail-closed)",
        )
        return ({"decision": "allow", "reason": "Orchestrator rate limited (fail-open)."}
                if self.fail_open else
                {"decision": "deny", "reason": "Orchestrator rate limited (fail-closed)."})

    def _evaluate_http_error(self, exc: HTTPError) -> dict:
        # Other HTTP errors — treat as unreachable
        self._gateway_consecutive_failures += 1
        self._maybe_trip_circuit_breaker()
        logger.warning("Modus: evaluate returned HTTP %d — %s", exc.code,
                       "allowing" if self.fail_open else "denying")
        return ({"decision": "allow", "reason": f"Orchestrator HTTP {exc.code} (fail-open)."}
                if self.fail_open else
                {"decision": "deny", "reason": f"Orchestrator HTTP {exc.code} (fail-closed)"})

    def _evaluate_unreachable(self, exc: URLError) -> dict:
        self._gateway_consecutive_failures += 1
        self._maybe_trip_circuit_breaker()
        logger.warning(
            "Modus: evaluate unreachable (%s) — %s | "
            "consecutive_failures=%d, last_success=%.0fs ago",
            exc, "allowing" if self.fail_open else "denying",
            self._gateway_consecutive_failures,
            time.monotonic() - self._gateway_last_success,
        )
        return ({"decision": "allow", "reason": "Orchestrator unreachable (fail-open)."}
                if self.fail_open else
                {"decision": "deny", "reason": f"Orchestrator unreachable (fail-closed): {exc}"})

    def _maybe_trip_circuit_breaker(self) -> None:
        if (self._gateway_consecutive_failures >= self._circuit_breaker_threshold
                and self._circuit_breaker_tripped_at is None):
            self._circuit_breaker_tripped_at = time.monotonic()
            logger.warning(
                "Modus: circuit breaker OPEN after %d consecutive failures. "
                "Gateway calls will be skipped for %.0fs.",
                self._gateway_consecutive_failures, self._circuit_breaker_cooldown,
            )
            # Trigger rewind hooks if registered
            self._trigger_rewind(
                "circuit_breaker",
                f"Gateway failed {self._gateway_consecutive_failures} consecutive times",
            )

    # ── Gateway health properties ─────────────────────────────────────────────

    @property
    def gateway_healthy(self) -> bool:
        """True if the gateway has responded successfully in the last 60 seconds."""
        return (
            self._gateway_consecutive_failures == 0
            or (time.monotonic() - self._gateway_last_success) < 60
        )

    @property
    def gateway_status(self) -> dict:
        """Diagnostic snapshot for monitoring integrations."""
        return {
            "healthy": self.gateway_healthy,
            "consecutive_failures": self._gateway_consecutive_failures,
            "seconds_since_last_success": round(
                time.monotonic() - self._gateway_last_success, 1
            ),
            "local_spend_usd": float(self._local_spend_usd),
            "local_budget_cap_usd": float(self.local_budget_cap_usd),
            "fail_open": self.fail_open,
            "response_cache": self._response_cache.stats() if self._response_cache else None,
        }

    def reset_local_spend(self) -> None:
        """Reset the local spend counter (e.g. at the start of a new billing period)."""
        with self._local_spend_lock:
            self._local_spend_usd = Decimal("0")

    # ── Usage recording ────────────────────────────────────────────────────────

    # ── Phase 5: Agentic Cost Attribution ────────────────────────────────────

    def _detect_framework_tier(self) -> str:
        """
        Detect whether the host application uses a structured agent framework.
        Checked once at start(), result cached for the process lifetime.

        Tier 1 (structured): LangGraph, CrewAI, AutoGen — graph topology known.
        Tier 2 (custom): Bespoke agent code — topology inferred heuristically.
        """
        import sys
        structured_frameworks = ("langgraph", "crewai", "autogen", "llama_index")
        for fw in structured_frameworks:
            if fw in sys.modules:
                return "structured"
        return "custom"

    def _get_span_stack(self) -> list:
        """Return the per-thread span stack, creating it if needed."""
        if not hasattr(self._span_local, "stack"):
            self._span_local.stack = []
        return self._span_local.stack

    def _current_span_meta(self) -> Optional[dict]:
        """
        Return span metadata dict for the current call context, or None
        if no session is active. ~50ns overhead (thread-local read).
        """
        stack = self._get_span_stack()
        if not stack:
            return None
        top = stack[-1]
        return {
            "mds_call_id": uuid.uuid4().hex[:16],
            "mds_parent_id": top.get("call_id"),
            "mds_session_id": top.get("session_id"),
            "mds_fw_tier": self._framework_tier or "custom",
            "mds_span_name": top.get("name"),
        }

    class _SessionCtx:
        """Context manager for agent sessions. Sets/clears the session on the span stack."""
        def __init__(self, agent: "ModusAgent", session_id: str, budget_usd: Optional[float]):
            self.agent = agent
            self.id = session_id
            self._budget_usd = budget_usd
            self._retry_count = 0

        def __enter__(self) -> "ModusAgent._SessionCtx":
            stack = self.agent._get_span_stack()
            stack.append({"session_id": self.id, "call_id": None, "name": "session"})
            return self

        def __exit__(self, *exc):
            stack = self.agent._get_span_stack()
            if stack:
                stack.pop()

    def session(self, session_id: Optional[str] = None,
                budget_usd: Optional[float] = None) -> _SessionCtx:
        """
        Context manager that sets the active session for span tracking.

        Usage:
            with agent.session(budget_usd=5.0) as sess:
                # All AI calls within this block are attributed to sess.id
                response = client.messages.create(...)

        Args:
            session_id: Explicit session ID, or auto-generated UUID.
            budget_usd: Optional per-session budget cap.
        """
        sid = session_id or uuid.uuid4().hex
        if not _SESSION_ID_RE.fullmatch(str(sid)):
            # The orchestrator stores session ids in a 64-char column and
            # rejects anything else; fail here, at the caller, not at flush.
            raise ValueError("session_id must be 1-64 characters of [A-Za-z0-9._:-]")
        return self._SessionCtx(self, str(sid), budget_usd)

    class _SpanCtx:
        """Context manager for a named span within a session."""
        def __init__(self, agent: "ModusAgent", name: Optional[str]):
            self.agent = agent
            self.call_id = uuid.uuid4().hex[:16]
            self._name = name

        def __enter__(self) -> "ModusAgent._SpanCtx":
            stack = self.agent._get_span_stack()
            if not stack:
                return self  # No active session — span is a no-op
            parent = stack[-1]
            stack.append({
                "session_id": parent.get("session_id"),
                "call_id": self.call_id,
                "name": self._name or "span",
            })
            return self

        def __exit__(self, *exc):
            stack = self.agent._get_span_stack()
            # Pop only if we pushed (stack has our entry)
            if stack and stack[-1].get("call_id") == self.call_id:
                stack.pop()

    def span(self, name: Optional[str] = None) -> _SpanCtx:
        """
        Context manager that creates a named span within the current session.
        LLM calls within this span get parent_call_id set automatically.

        Usage:
            with agent.session() as sess:
                with agent.span("planning"):
                    response = client.messages.create(...)
                with agent.span("execution"):
                    response = client.messages.create(...)
        """
        return self._SpanCtx(self, name)

    def build_prompt(self, fn=None, *, name: Optional[str] = None):
        """
        Decorator/context manager for Tier 1 attribution accuracy on bespoke code.

        Wraps a function or code block in a span, so all LLM calls within
        automatically get parent-child relationships tracked.

        As decorator:
            @agent.build_prompt(name="planner")
            def plan_step():
                return client.messages.create(...)

        As context manager:
            with agent.build_prompt(name="planner"):
                response = client.messages.create(...)
        """
        if fn is not None:
            # Used as @agent.build_prompt without parens (fn is the decorated function)
            import functools
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                with self.span(name=fn.__name__):
                    return fn(*args, **kwargs)
            return wrapper

        # Used as @agent.build_prompt(name="x") or as context manager
        if name is not None and callable(name):
            # Shouldn't happen but guard against misuse
            raise TypeError("build_prompt(name=...) requires a keyword argument")

        class _BuildPromptCtx:
            def __init__(ctx_self):
                ctx_self._span = self.span(name=name)
            def __enter__(ctx_self):
                ctx_self._span.__enter__()
                return ctx_self
            def __exit__(ctx_self, *exc):
                return ctx_self._span.__exit__(*exc)
            def __call__(ctx_self, fn):
                import functools
                @functools.wraps(fn)
                def wrapper(*args, **kwargs):
                    with self.span(name=name or fn.__name__):
                        return fn(*args, **kwargs)
                return wrapper

        return _BuildPromptCtx()

    def record(
        self,
        provider: str,
        resource_type: str = "llm_call",
        model: Optional[str] = None,
        operation: Optional[str] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        input_cost: Optional[Decimal] = None,
        output_cost: Optional[Decimal] = None,
        duration_ms: Optional[int] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        """
        Queue a usage event. Call this to track any provider not auto-instrumented.

        Costs are calculated from the bundled pricing table if not supplied,
        so you only need to provide token counts for accurate cost tracking.

        Example:
            agent.record(
                provider="my-internal-llm",
                model="finetuned-v3",
                input_tokens=800,
                output_tokens=200,
            )
        """
        # Calculate costs from pricing table if not supplied
        if input_cost is None and output_cost is None:
            try:
                from modus.pricing import estimate_cost
                input_cost, output_cost, total_cost = estimate_cost(
                    provider, model, input_tokens, output_tokens
                )
            except Exception:
                total_cost = Decimal("0")
        else:
            total_cost = (input_cost or Decimal("0")) + (output_cost or Decimal("0"))

        total_tokens = None
        if input_tokens is not None or output_tokens is not None:
            total_tokens = (input_tokens or 0) + (output_tokens or 0)

        # Inside agent.session()/span(): attach the session + span ids so the
        # call reaches the Sessions view and the attribution engine. Explicit
        # caller-supplied ids win.
        if not (metadata and metadata.get("mds_session_id")):
            span_meta = self._current_span_meta()
            if span_meta:
                metadata = {**span_meta, **(metadata or {})}

        rec = _UsageRecord(
            provider=provider, resource_type=resource_type,
            model=model, operation=operation,
            input_tokens=input_tokens, output_tokens=output_tokens,
            total_tokens=total_tokens, input_cost=input_cost,
            output_cost=output_cost, total_cost=total_cost,
            duration_ms=duration_ms,
            timestamp=datetime.now(timezone.utc).isoformat(),
            metadata=metadata,
        )

        if self._aggregation_enabled:
            self._aggregate_record(rec)
        else:
            with self._records_lock:
                if len(self._records) >= self._max_buffer_size:
                    self._records.pop(0)
                    logger.warning(
                        "Modus: record buffer full (%d). Dropping oldest record.",
                        self._max_buffer_size,
                    )
                self._records.append(rec)

    @staticmethod
    def _bucket_key(record: _UsageRecord) -> str:
        """Aggregation key: UTC hour + (provider, model, operation, resource_type).

        Including the hour keeps every bucket inside one UTC hour, so the
        orchestrator books it to the right hour and day (a bucket spanning
        23:59-00:00 would otherwise be booked entirely to the previous day).
        """
        hour = (record.timestamp or "")[:13]
        return f"{hour}|{record.provider}:{record.model or ''}:{record.operation or ''}:{record.resource_type}"

    def _aggregate_record(self, record: _UsageRecord) -> None:
        """
        Accumulate a record into its bucket and decide whether it is also
        sent at full detail as a trace.

        Every record is counted in exactly one bucket. Traces are detail only
        (the payload says ``traces_counted_in_aggregates``) and are kept for:
        policy violations, errors, calls inside an agent session (needed by
        the Sessions view / attribution engine), and a deterministic sample at
        ``_trace_sample_rate``.
        """
        meta = record.metadata or {}
        keep_trace = bool(
            meta.get("_policy_violation") or meta.get("_error") or meta.get("mds_session_id")
        )

        with self._agg_lock:
            if not keep_trace and self._trace_sample_rate > 0:
                # Deterministic sampling: keep every Nth record as a raw trace
                self._trace_counter += 1
                sample_interval = max(1, int(1.0 / self._trace_sample_rate))
                keep_trace = self._trace_counter % sample_interval == 0
            if keep_trace:
                if len(self._agg_sampled) >= self._max_buffer_size:
                    self._agg_sampled.pop(0)
                    logger.warning(
                        "Modus: trace buffer full (%d). Dropping oldest trace "
                        "(usage totals are unaffected).", self._max_buffer_size,
                    )
                self._agg_sampled.append(record)

            key = self._bucket_key(record)
            bucket = self._agg_buckets.get(key)
            if bucket is None:
                bucket = _AggregationBucket(
                    provider=record.provider,
                    model=record.model,
                    operation=record.operation,
                    resource_type=record.resource_type,
                    hour=(record.timestamp or "")[:13] or None,
                )
                self._agg_buckets[key] = bucket
            bucket.accumulate(record)

    def _rec(self, provider: str, model: Optional[str], operation: str,
             input_tokens: Optional[int], output_tokens: Optional[int],
             duration_ms: int, metadata: Optional[dict] = None) -> None:
        """Internal shorthand for all instrumentation methods.

        record() attaches the active session/span metadata.
        """
        self.record(provider=provider, model=model, operation=operation,
                    input_tokens=input_tokens, output_tokens=output_tokens,
                    duration_ms=duration_ms, metadata=metadata)

    def instrumented_providers(self) -> list[str]:
        """Return list of provider names that were successfully instrumented."""
        return list(self._instrumented)

    # ── Anthropic ─────────────────────────────────────────────────────────────

    def _instrument_anthropic(self) -> None:
        """
        Patches anthropic.resources.Messages.create (sync) and
        anthropic.resources.AsyncMessages.create (async).

        Captures input_tokens from message_start event and output_tokens
        from message_delta event so streaming is fully tracked.

        Also automatically covers LangChain's ChatAnthropic and
        LlamaIndex's Anthropic LLM — they call the same class method.
        """
        try:
            import anthropic
            self._sdk_versions["anthropic"] = getattr(anthropic, "__version__", "?")
            agent = self

            # ── sync ──────────────────────────────────────────────────────────
            _orig = anthropic.resources.Messages.create

            def _sync_create(self_sdk, *args, **kwargs):
                model = kwargs.get("model") or (args[0] if args else None)
                # ── Autonomous Optimization (Smart Retry) ──
                model = agent._enforce_with_retry(
                    provider="anthropic", model=model,
                    estimated_tokens=kwargs.get("max_tokens")
                )
                if "model" in kwargs:
                    kwargs["model"] = model
                elif args:
                    args = list(args)
                    args[0] = model
                    args = tuple(args)

                # ── PII Scrubbing ──
                if "messages" in kwargs:
                    kwargs["messages"] = agent._apply_pii_scan(kwargs["messages"])
                if "system" in kwargs:
                    kwargs["system"] = agent._apply_pii_scan(kwargs["system"])

                # ── Response Cache (non-streaming only) ──
                if not kwargs.get("stream"):
                    cache_key_msgs = (kwargs.get("system", ""), kwargs.get("messages", []))
                    cached = agent._cache_check("anthropic", model, cache_key_msgs)
                    if cached is not None:
                        logger.debug("Modus: response cache HIT for anthropic/%s", model)
                        agent._rec("anthropic", model, "messages.create.cached", 0, 0, 0)
                        return cached

                # ── Routing Intercept (non-streaming only) ──
                if not kwargs.get("stream"):
                    try:
                        routed, routed_resp = agent._try_route_sync(
                            "anthropic", model, kwargs, _orig, self_sdk)
                        if routed:
                            u = getattr(routed_resp, "usage", None)
                            agent._rec("anthropic",
                                       getattr(routed_resp, "model", model),
                                       "messages.create",
                                       getattr(u, "input_tokens", None),
                                       getattr(u, "output_tokens", None), 0)
                            return routed_resp
                    except Exception:
                        pass  # fail-open

                t = time.perf_counter()
                resp = _orig(self_sdk, *args, **kwargs)
                dur = int((time.perf_counter() - t) * 1000)
                if not kwargs.get("stream"):
                    u = getattr(resp, "usage", None)
                    agent._rec("anthropic",
                               model or getattr(resp, "model", None),
                               "messages.create",
                               getattr(u, "input_tokens", None),
                               getattr(u, "output_tokens", None),
                               dur,
                               {"stop_reason": getattr(resp, "stop_reason", None)})
                    # Store in response cache for future identical calls
                    agent._cache_store("anthropic", model, cache_key_msgs, resp)
                    return resp
                return _sync_stream(resp, model or "unknown", t)

            def _sync_stream(stream, model, t0):
                in_tok = out_tok = None
                try:
                    for event in stream:
                        yield event
                        etype = getattr(event, "type", None)
                        if etype == "message_start":
                            u = getattr(getattr(event, "message", None), "usage", None)
                            if u: in_tok = getattr(u, "input_tokens", None)
                        elif etype == "message_delta":
                            u = getattr(event, "usage", None)
                            if u: out_tok = getattr(u, "output_tokens", None)
                finally:
                    agent._rec("anthropic", model, "messages.create(stream)",
                               in_tok, out_tok, int((time.perf_counter() - t0) * 1000))

            anthropic.resources.Messages.create = _sync_create

            # ── async ──────────────────────────────────────────────────────────
            try:
                import asyncio as _aio
                _aorig = anthropic.resources.AsyncMessages.create

                async def _async_create(self_sdk, *args, **kwargs):
                    model = kwargs.get("model") or (args[0] if args else None)
                    # ── Autonomous Optimization (Smart Retry) ──
                    loop = _aio.get_running_loop()
                    model = await loop.run_in_executor(
                        None, lambda: agent._enforce_with_retry(
                            provider="anthropic", model=model,
                            estimated_tokens=kwargs.get("max_tokens")
                        )
                    )
                    if "model" in kwargs:
                        kwargs["model"] = model
                    elif args:
                        args = list(args)
                        args[0] = model
                        args = tuple(args)

                    # ── PII Scrubbing ──
                    if "messages" in kwargs:
                        kwargs["messages"] = agent._apply_pii_scan(kwargs["messages"])
                    if "system" in kwargs:
                        kwargs["system"] = agent._apply_pii_scan(kwargs["system"])

                    # ── Routing Intercept (non-streaming only) ──
                    if not kwargs.get("stream"):
                        try:
                            routed, routed_resp = await agent._try_route_async(
                                "anthropic", model, kwargs, _aorig, self_sdk)
                            if routed:
                                u = getattr(routed_resp, "usage", None)
                                agent._rec("anthropic",
                                           getattr(routed_resp, "model", model),
                                           "messages.create",
                                           getattr(u, "input_tokens", None),
                                           getattr(u, "output_tokens", None), 0)
                                return routed_resp
                        except Exception:
                            pass  # fail-open

                    t = time.perf_counter()
                    resp = await _aorig(self_sdk, *args, **kwargs)
                    dur = int((time.perf_counter() - t) * 1000)
                    if not kwargs.get("stream"):
                        u = getattr(resp, "usage", None)
                        agent._rec("anthropic",
                                   model or getattr(resp, "model", None),
                                   "messages.create",
                                   getattr(u, "input_tokens", None),
                                   getattr(u, "output_tokens", None),
                                   dur)
                        return resp
                    return _async_stream(resp, model or "unknown", t)

                async def _async_stream(stream, model, t0):
                    in_tok = out_tok = None
                    try:
                        async for event in stream:
                            yield event
                            etype = getattr(event, "type", None)
                            if etype == "message_start":
                                u = getattr(getattr(event, "message", None), "usage", None)
                                if u: in_tok = getattr(u, "input_tokens", None)
                            elif etype == "message_delta":
                                u = getattr(event, "usage", None)
                                if u: out_tok = getattr(u, "output_tokens", None)
                    finally:
                        agent._rec("anthropic", model, "messages.create(stream)",
                                   in_tok, out_tok, int((time.perf_counter() - t0) * 1000))

                anthropic.resources.AsyncMessages.create = _async_create
            except AttributeError:
                pass

            self._instrumented.append("anthropic")
            logger.debug("Modus: anthropic instrumented v%s",
                         self._sdk_versions["anthropic"])
        except ImportError:
            pass
        except Exception as exc:
            logger.warning("Modus: anthropic instrumentation failed: %s", exc)

    # ── OpenAI / xAI Grok / Azure ─────────────────────────────────────────────

    # Base URL → provider mapping for OpenAI-compatible SDKs
    _OPENAI_BASE_URL_PROVIDERS = {
        "api.x.ai": "xai",
        "api.together.xyz": "together",
        "api.perplexity.ai": "perplexity",
        "api.fireworks.ai": "fireworks",
        "api.deepinfra.com": "deepinfra",
        "api.groq.com": "groq",
        "api.mistral.ai": "mistral",
        "api.endpoints.anyscale.com": "anyscale",
        "integrate.api.nvidia.com": "nvidia",
        "api.deepseek.com": "deepseek",
        "generativelanguage.googleapis.com": "google",
        "api.cerebras.ai": "cerebras",
        "api.sambanova.ai": "sambanova",
        "openrouter.ai": "openrouter",
        "api.hyperbolic.xyz": "hyperbolic",
    }

    @staticmethod
    def _detect_oai_provider(self_sdk) -> str:
        """Detect the actual provider from an OpenAI-compatible client's base_url."""
        try:
            client = getattr(self_sdk, "_client", self_sdk)
            base_url = getattr(client, "_base_url", None) or getattr(client, "base_url", None)
            if base_url:
                host = str(base_url).split("//")[-1].split("/")[0].split(":")[0].lower()
                for pattern, prov in ModusAgent._OPENAI_BASE_URL_PROVIDERS.items():
                    if pattern in host:
                        return prov
        except Exception:
            pass
        return "openai"

    def _instrument_openai(self) -> None:
        """
        Patches openai.resources.chat.Completions.create (sync) and
        openai.resources.chat.AsyncCompletions.create (async).

        Automatically covers:
          - xAI Grok: uses openai SDK with base_url="https://api.x.ai/v1"
          - Azure OpenAI: uses openai SDK with AzureOpenAI client
          - Together AI, Anyscale, Perplexity (all OpenAI-compatible)
          - DeepSeek, Fireworks, NVIDIA, Cerebras, SambaNova, OpenRouter
          - LangChain ChatOpenAI, LlamaIndex OpenAI LLM

        For streaming, injects stream_options={"include_usage": True} so token
        counts are always present in the final chunk.
        """
        try:
            import openai
            self._sdk_versions["openai"] = getattr(openai, "__version__", "?")
            agent = self

            def _record_oai(model, resp, dur, op="chat.completions.create",
                            provider="openai"):
                u = getattr(resp, "usage", None)
                agent._rec(provider, model or getattr(resp, "model", None), op,
                           getattr(u, "prompt_tokens", None),
                           getattr(u, "completion_tokens", None), dur)

            # ── sync ──────────────────────────────────────────────────────────
            _orig = openai.resources.chat.Completions.create

            def _sync_create(self_sdk, *args, **kwargs):
                model = kwargs.get("model") or (args[0] if args else None)
                # Detect provider from base URL
                provider = agent._detect_oai_provider(self_sdk)
                # ── Autonomous Optimization (Smart Retry) ──
                model = agent._enforce_with_retry(
                    provider=provider, model=model,
                    estimated_tokens=kwargs.get("max_tokens") or
                                     kwargs.get("max_completion_tokens")
                )
                if "model" in kwargs:
                    kwargs["model"] = model
                elif args:
                    args = list(args)
                    args[0] = model
                    args = tuple(args)

                # ── PII Scrubbing ──
                if "messages" in kwargs:
                    kwargs["messages"] = agent._apply_pii_scan(kwargs["messages"])

                # ── Response Cache (non-streaming only) ──
                cache_key_msgs = kwargs.get("messages", [])
                if not kwargs.get("stream"):
                    cached = agent._cache_check(provider, model, cache_key_msgs)
                    if cached is not None:
                        logger.debug("Modus: response cache HIT for %s/%s", provider, model)
                        agent._rec(provider, model, "chat.completions.create.cached", 0, 0, 0)
                        return cached

                # ── Routing Intercept (non-streaming only) ──
                if not kwargs.get("stream"):
                    try:
                        routed, routed_resp = agent._try_route_sync(
                            provider, model, kwargs, _orig, self_sdk)
                        if routed:
                            _record_oai(model, routed_resp, 0, provider=provider)
                            return routed_resp
                    except Exception:
                        pass  # fail-open

                # Force usage in streaming response
                if kwargs.get("stream") and "stream_options" not in kwargs:
                    kwargs = {**kwargs, "stream_options": {"include_usage": True}}
                t = time.perf_counter()
                resp = _orig(self_sdk, *args, **kwargs)
                dur = int((time.perf_counter() - t) * 1000)
                if not kwargs.get("stream"):
                    _record_oai(model, resp, dur, provider=provider)
                    agent._cache_store(provider, model, cache_key_msgs, resp)
                    return resp
                return _sync_stream(resp, model or "unknown", t, provider)

            def _sync_stream(stream, model, t0, provider="openai"):
                last = None
                try:
                    for chunk in stream:
                        yield chunk
                        last = chunk
                finally:
                    if last is not None:
                        _record_oai(model, last, int((time.perf_counter() - t0) * 1000),
                                    "chat.completions.create(stream)",
                                    provider=provider)

            openai.resources.chat.Completions.create = _sync_create

            # ── async ──────────────────────────────────────────────────────────
            try:
                import asyncio as _aio
                _aorig = openai.resources.chat.AsyncCompletions.create

                async def _async_create(self_sdk, *args, **kwargs):
                    model = kwargs.get("model") or (args[0] if args else None)
                    # Detect provider from base URL
                    provider = agent._detect_oai_provider(self_sdk)
                    # ── Autonomous Optimization (Smart Retry) ──
                    loop = _aio.get_running_loop()
                    _prov, _mdl = provider, model
                    model = await loop.run_in_executor(
                        None, lambda: agent._enforce_with_retry(
                            provider=_prov, model=_mdl,
                            estimated_tokens=kwargs.get("max_tokens") or
                                             kwargs.get("max_completion_tokens")
                        )
                    )
                    if "model" in kwargs:
                        kwargs["model"] = model
                    elif args:
                        args = list(args)
                        args[0] = model
                        args = tuple(args)

                    # ── PII Scrubbing ──
                    if "messages" in kwargs:
                        kwargs["messages"] = agent._apply_pii_scan(kwargs["messages"])

                    # ── Routing Intercept (non-streaming only) ──
                    if not kwargs.get("stream"):
                        try:
                            routed, routed_resp = await agent._try_route_async(
                                provider, model, kwargs, _aorig, self_sdk)
                            if routed:
                                _record_oai(model, routed_resp, 0, provider=provider)
                                return routed_resp
                        except Exception:
                            pass  # fail-open

                    if kwargs.get("stream") and "stream_options" not in kwargs:
                        kwargs = {**kwargs, "stream_options": {"include_usage": True}}
                    t = time.perf_counter()
                    resp = await _aorig(self_sdk, *args, **kwargs)
                    dur = int((time.perf_counter() - t) * 1000)
                    if not kwargs.get("stream"):
                        _record_oai(model, resp, dur, provider=provider)
                        return resp
                    return _async_stream(resp, model or "unknown", t, provider)

                async def _async_stream(stream, model, t0, provider="openai"):
                    last = None
                    try:
                        async for chunk in stream:
                            yield chunk
                            last = chunk
                    finally:
                        if last is not None:
                            _record_oai(model, last, int((time.perf_counter() - t0) * 1000),
                                        "chat.completions.create(stream)",
                                        provider=provider)

                openai.resources.chat.AsyncCompletions.create = _async_create
            except AttributeError:
                pass

            self._instrumented.append("openai")
            logger.debug("Modus: openai instrumented v%s (covers xAI/Grok, Azure, DeepSeek, Together, Fireworks, NVIDIA, Cerebras, SambaNova, OpenRouter)",
                         self._sdk_versions["openai"])
        except ImportError:
            pass
        except Exception as exc:
            logger.warning("Modus: openai instrumentation failed: %s", exc)

    # ── DeepSeek ───────────────────────────────────────────────────────────────

    def _instrument_deepseek(self) -> None:
        """Instrument the DeepSeek Python SDK (deepseek package)."""
        try:
            import openai as _openai  # noqa: F401  # DeepSeek SDK inherits from OpenAI
            # DeepSeek SDK reuses OpenAI's Completions class with a custom base URL.
            # The base URL detection in _instrument_openai() already handles this
            # if users use `from openai import OpenAI; client = OpenAI(base_url="https://api.deepseek.com/v1")`.
            # This method handles the case where users import `deepseek` directly.
            try:
                import deepseek  # noqa: F401
                # If deepseek SDK is installed, it's already covered by OpenAI instrumentation
                # since it subclasses OpenAI. Just log it.
                logger.debug("Modus: deepseek SDK detected (covered via OpenAI instrumentation)")
            except ImportError:
                pass
        except ImportError:
            pass

    # ── AWS Bedrock ───────────────────────────────────────────────────────────

    def _instrument_boto3(self) -> None:
        """
        Instruments AWS Bedrock by patching boto3.client() and boto3.Session.client()
        so that any bedrock-runtime client created after import modus is tracked.

        Covers all Bedrock models: Claude, Titan, Llama, Mistral, Cohere, AI21, etc.
        Token counts are extracted from provider-specific response formats.
        """
        try:
            import boto3
            self._sdk_versions["bedrock"] = boto3.__version__
            agent = self

            def _patch_client(client):
                """Wrap invoke_model and invoke_model_with_response_stream."""
                import json as _json

                _orig_invoke = client.invoke_model
                _orig_stream = client.invoke_model_with_response_stream

                def _parse_tokens(body: dict, model_id: str):
                    """Extract (input_tokens, output_tokens) from Bedrock response body."""
                    # Anthropic on Bedrock
                    if "usage" in body:
                        u = body["usage"]
                        return u.get("input_tokens"), u.get("output_tokens")
                    # Amazon Titan
                    if "inputTextTokenCount" in body:
                        out = (body.get("results") or [{}])[0].get("tokenCount")
                        return body["inputTextTokenCount"], out
                    # Meta Llama
                    if "prompt_token_count" in body:
                        return body["prompt_token_count"], body.get("generation_token_count")
                    # Cohere
                    if "meta" in body and isinstance(body.get("meta"), dict):
                        t = body["meta"].get("tokens", {})
                        return t.get("input_tokens"), t.get("output_tokens")
                    return None, None

                def invoke_model(**kwargs):
                    model_id = kwargs.get("modelId", "unknown")
                    agent.enforce(provider="bedrock", model=model_id)
                    t = time.perf_counter()
                    response = _orig_invoke(**kwargs)
                    dur = int((time.perf_counter() - t) * 1000)
                    try:
                        raw = response["body"].read()
                        body = _json.loads(raw)
                        response["body"] = _ReusableBody(raw)
                        in_tok, out_tok = _parse_tokens(body, model_id)
                        agent._rec("bedrock", model_id, "invoke_model", in_tok, out_tok, dur)
                    except Exception:
                        agent._rec("bedrock", model_id, "invoke_model", None, None, dur)
                    return response

                def invoke_model_with_response_stream(**kwargs):
                    model_id = kwargs.get("modelId", "unknown")
                    agent.enforce(provider="bedrock", model=model_id)
                    t = time.perf_counter()
                    response = _orig_stream(**kwargs)
                    response["body"] = _BedrockStreamWrapper(
                        response["body"], model_id, t, agent)
                    return response

                client.invoke_model = invoke_model
                client.invoke_model_with_response_stream = invoke_model_with_response_stream

            # Patch boto3.client (module-level shorthand)
            _orig_client = boto3.client

            def _patched_client(service_name, *args, **kwargs):
                c = _orig_client(service_name, *args, **kwargs)
                if service_name == "bedrock-runtime":
                    _patch_client(c)
                return c

            boto3.client = _patched_client

            # Patch boto3.Session.client (used by apps that create explicit sessions)
            _orig_session_client = boto3.Session.client

            def _patched_session_client(self_sess, service_name, *args, **kwargs):
                c = _orig_session_client(self_sess, service_name, *args, **kwargs)
                if service_name == "bedrock-runtime":
                    _patch_client(c)
                return c

            boto3.Session.client = _patched_session_client

            self._instrumented.append("bedrock")
            logger.debug("Modus: bedrock (boto3) instrumented v%s",
                         self._sdk_versions["bedrock"])
        except ImportError:
            pass
        except Exception as exc:
            logger.warning("Modus: boto3/bedrock instrumentation failed: %s", exc)

    # ── Google Gemini ─────────────────────────────────────────────────────────

    def _instrument_google_genai(self) -> None:
        """
        Instruments Google Gemini via google-generativeai (legacy) or
        google-genai (new) SDK. Covers sync and async generate_content calls.
        """
        agent = self
        instrumented = False

        def _record_gemini(resp, model, dur, op):
            try:
                meta = getattr(resp, "usage_metadata", None)
                in_tok = getattr(meta, "prompt_token_count", None)
                out_tok = getattr(meta, "candidates_token_count", None)
                agent._rec("google", model, op, in_tok, out_tok, dur)
            except Exception:
                agent._rec("google", model, op, None, None, dur)

        # Try new google-genai SDK (google-genai package)
        try:
            import google.genai as _genai
            self._sdk_versions["gemini"] = getattr(_genai, "__version__", "?")
            try:
                from google.genai import models as _gm
                _orig = _gm.Models.generate_content

                def _patched(self_m, *args, **kwargs):
                    model = str(kwargs.get("model") or (args[0] if args else "gemini"))
                    # ── Autonomous Optimization (Smart Retry) ──
                    model = agent._enforce_with_retry(provider="google", model=model)
                    if "model" in kwargs:
                        kwargs["model"] = model
                    elif args:
                        args_list = list(args)
                        args_list[0] = model
                        args = tuple(args_list)

                    # ── PII Scrubbing ──
                    if "contents" in kwargs:
                        kwargs["contents"] = agent._apply_pii_scan(kwargs["contents"])

                    # ── Routing Intercept ──
                    if not kwargs.get("stream"):
                        try:
                            routed, routed_resp = agent._try_route_sync(
                                "google", model, kwargs, _orig, self_m)
                            if routed:
                                _record_gemini(routed_resp, model, 0, "generate_content")
                                return routed_resp
                        except Exception:
                            pass  # fail-open

                    t = time.perf_counter()
                    resp = _orig(self_m, *args, **kwargs)
                    _record_gemini(resp, model, int((time.perf_counter() - t) * 1000),
                                   "generate_content")
                    return resp

                _gm.Models.generate_content = _patched
                instrumented = True
            except (AttributeError, ImportError):
                pass
        except ImportError:
            pass

        # Fall back to legacy google-generativeai SDK
        if not instrumented:
            try:
                import google.generativeai as _genai
                self._sdk_versions["gemini"] = getattr(_genai, "__version__", "?")
                try:
                    from google.generativeai.generative_models import GenerativeModel
                    _orig = GenerativeModel.generate_content

                    def _patched(self_m, *args, **kwargs):
                        model = getattr(self_m, "model_name", None) or "gemini"
                        # ── Autonomous Optimization (Smart Retry) ──
                        model = agent._enforce_with_retry(provider="google", model=model)
                        # (Legacy SDK doesn't always support model swap this way, but we try)

                        # ── PII Scrubbing ──
                        if "contents" in kwargs:
                            kwargs["contents"] = agent._apply_pii_scan(kwargs["contents"])
                        elif args:
                            args_list = list(args)
                            args_list[0] = agent._apply_pii_scan(args_list[0])
                            args = tuple(args_list)

                        t = time.perf_counter()
                        resp = _orig(self_m, *args, **kwargs)
                        _record_gemini(resp, model, int((time.perf_counter() - t) * 1000),
                                       "generate_content")
                        return resp

                    GenerativeModel.generate_content = _patched

                    # async variant
                    try:
                        import asyncio as _aio
                        _aorig = GenerativeModel.generate_content_async

                        async def _async_patched(self_m, *args, **kwargs):
                            model = getattr(self_m, "model_name", None) or "gemini"
                            # ── Autonomous Optimization (Smart Retry) ──
                            loop = _aio.get_running_loop()
                            model = await loop.run_in_executor(
                                None, lambda: agent._enforce_with_retry(
                                    provider="google", model=model
                                )
                            )
                            # ── PII Scrubbing ──
                            if "contents" in kwargs:
                                kwargs["contents"] = agent._apply_pii_scan(kwargs["contents"])
                            elif args:
                                args_list = list(args)
                                args_list[0] = agent._apply_pii_scan(args_list[0])
                                args = tuple(args_list)

                            t = time.perf_counter()
                            resp = await _aorig(self_m, *args, **kwargs)
                            _record_gemini(resp, model, int((time.perf_counter() - t) * 1000),
                                           "generate_content_async")
                            return resp

                        GenerativeModel.generate_content_async = _async_patched
                    except AttributeError:
                        pass

                    instrumented = True
                except (AttributeError, ImportError):
                    pass
            except ImportError:
                pass

        if instrumented:
            self._instrumented.append("gemini")
            logger.debug("Modus: google-generativeai instrumented v%s",
                         self._sdk_versions.get("gemini", "?"))

    # ── Groq ──────────────────────────────────────────────────────────────────

    def _instrument_groq(self) -> None:
        """
        Instruments Groq SDK (sync + async + streaming).
        Covers Llama 3, Mixtral, Gemma, DeepSeek hosted on Groq.
        """
        try:
            import groq
            self._sdk_versions["groq"] = getattr(groq, "__version__", "?")
            agent = self

            def _record_groq(model, resp, dur, op="chat.completions.create"):
                u = getattr(resp, "usage", None)
                agent._rec("groq", model or getattr(resp, "model", None), op,
                           getattr(u, "prompt_tokens", None),
                           getattr(u, "completion_tokens", None), dur)

            _orig = groq.resources.chat.Completions.create

            def _sync_create(self_sdk, *args, **kwargs):
                model = kwargs.get("model") or (args[0] if args else None)
                agent.enforce(provider="groq", model=model,
                              estimated_tokens=kwargs.get("max_tokens"))

                # ── Routing Intercept (non-streaming only) ──
                if not kwargs.get("stream"):
                    try:
                        routed, routed_resp = agent._try_route_sync(
                            "groq", model, kwargs, _orig, self_sdk)
                        if routed:
                            _record_groq(model, routed_resp, 0)
                            return routed_resp
                    except Exception:
                        pass  # fail-open

                t = time.perf_counter()
                resp = _orig(self_sdk, *args, **kwargs)
                dur = int((time.perf_counter() - t) * 1000)
                if not kwargs.get("stream"):
                    _record_groq(model, resp, dur)
                    return resp
                return _sync_stream(resp, model or "unknown", t)

            def _sync_stream(stream, model, t0):
                last = None
                try:
                    for chunk in stream:
                        yield chunk
                        last = chunk
                finally:
                    if last is not None:
                        _record_groq(model, last, int((time.perf_counter() - t0) * 1000),
                                     "chat.completions.create(stream)")

            groq.resources.chat.Completions.create = _sync_create

            try:
                import asyncio as _aio
                _aorig = groq.resources.chat.AsyncCompletions.create

                async def _async_create(self_sdk, *args, **kwargs):
                    model = kwargs.get("model") or (args[0] if args else None)
                    loop = _aio.get_running_loop()
                    await loop.run_in_executor(
                        None, lambda: agent.enforce(provider="groq", model=model,
                                                    estimated_tokens=kwargs.get("max_tokens")))

                    # ── Routing Intercept (non-streaming only) ──
                    if not kwargs.get("stream"):
                        try:
                            routed, routed_resp = await agent._try_route_async(
                                "groq", model, kwargs, _aorig, self_sdk)
                            if routed:
                                _record_groq(model, routed_resp, 0)
                                return routed_resp
                        except Exception:
                            pass  # fail-open

                    t = time.perf_counter()
                    resp = await _aorig(self_sdk, *args, **kwargs)
                    dur = int((time.perf_counter() - t) * 1000)
                    if not kwargs.get("stream"):
                        _record_groq(model, resp, dur)
                        return resp
                    return _async_stream(resp, model or "unknown", t)

                async def _async_stream(stream, model, t0):
                    last = None
                    try:
                        async for chunk in stream:
                            yield chunk
                            last = chunk
                    finally:
                        if last is not None:
                            _record_groq(model, last, int((time.perf_counter() - t0) * 1000),
                                         "chat.completions.create(stream)")

                groq.resources.chat.AsyncCompletions.create = _async_create
            except AttributeError:
                pass

            self._instrumented.append("groq")
            logger.debug("Modus: groq instrumented v%s", self._sdk_versions["groq"])
        except ImportError:
            pass
        except Exception as exc:
            logger.warning("Modus: groq instrumentation failed: %s", exc)

    # ── Mistral ───────────────────────────────────────────────────────────────

    def _instrument_mistral(self) -> None:
        """
        Instruments Mistral SDK. Handles both old MistralClient and new Mistral class.
        """
        try:
            import mistralai
            self._sdk_versions["mistral"] = getattr(mistralai, "__version__", "?")
            agent = self
            patched_any = False

            def _record_mistral(model, resp, dur, op="chat"):
                u = getattr(resp, "usage", None)
                agent._rec("mistral", model or getattr(resp, "model", None), op,
                           getattr(u, "prompt_tokens", None),
                           getattr(u, "completion_tokens", None), dur)

            # New SDK: mistralai >= 1.0 — Mistral class with mistral.chat.complete
            try:
                from mistralai import Mistral  # noqa: F401
                # The new SDK uses a nested resource pattern
                # Patch at the models level used by chat.complete
                import mistralai.resources.chat as _mc
                if hasattr(_mc, "Chat"):
                    _orig = _mc.Chat.complete

                    def _patched_complete(self_r, *args, **kwargs):
                        model = kwargs.get("model") or (args[0] if args else None)
                        agent.enforce(provider="mistral", model=model,
                                      estimated_tokens=kwargs.get("max_tokens"))

                        # ── Routing Intercept ──
                        if not kwargs.get("stream"):
                            try:
                                routed, routed_resp = agent._try_route_sync(
                                    "mistral", model, kwargs, _orig, self_r)
                                if routed:
                                    _record_mistral(model, routed_resp, 0)
                                    return routed_resp
                            except Exception:
                                pass  # fail-open

                        t = time.perf_counter()
                        resp = _orig(self_r, *args, **kwargs)
                        _record_mistral(model, resp, int((time.perf_counter() - t) * 1000))
                        return resp

                    _mc.Chat.complete = _patched_complete
                    patched_any = True
            except (ImportError, AttributeError):
                pass

            # Legacy SDK: MistralClient
            if not patched_any:
                try:
                    from mistralai.client import MistralClient
                    _orig = MistralClient.chat

                    def _patched_chat(self_sdk, *args, **kwargs):
                        model = kwargs.get("model") or (args[0] if args else None)
                        agent.enforce(provider="mistral", model=model,
                                      estimated_tokens=kwargs.get("max_tokens"))

                        # ── Routing Intercept ──
                        if not kwargs.get("stream"):
                            try:
                                routed, routed_resp = agent._try_route_sync(
                                    "mistral", model, kwargs, _orig, self_sdk)
                                if routed:
                                    _record_mistral(model, routed_resp, 0)
                                    return routed_resp
                            except Exception:
                                pass  # fail-open

                        t = time.perf_counter()
                        resp = _orig(self_sdk, *args, **kwargs)
                        _record_mistral(model, resp, int((time.perf_counter() - t) * 1000))
                        return resp

                    MistralClient.chat = _patched_chat
                    patched_any = True
                except (ImportError, AttributeError):
                    pass

            if patched_any:
                self._instrumented.append("mistral")
                logger.debug("Modus: mistral instrumented v%s",
                             self._sdk_versions["mistral"])
        except ImportError:
            pass
        except Exception as exc:
            logger.warning("Modus: mistral instrumentation failed: %s", exc)

    # ── Cohere ────────────────────────────────────────────────────────────────

    def _instrument_cohere(self) -> None:
        """
        Instruments Cohere SDK v1 and v2 — client.chat() (sync + async).
        """
        try:
            import cohere
            self._sdk_versions["cohere"] = getattr(cohere, "__version__", "?")
            agent = self

            def _record_cohere(model, resp, dur, op="chat"):
                # v2 usage
                u = getattr(resp, "usage", None)
                tokens = getattr(u, "tokens", None) if u else None
                in_tok = getattr(tokens, "input_tokens", None) if tokens else None
                out_tok = getattr(tokens, "output_tokens", None) if tokens else None
                # v1 meta.tokens
                if in_tok is None:
                    meta = getattr(resp, "meta", None)
                    t2 = getattr(meta, "tokens", None) if meta else None
                    in_tok = getattr(t2, "input_tokens", None) if t2 else None
                    out_tok = getattr(t2, "output_tokens", None) if t2 else None
                agent._rec("cohere", model, op, in_tok, out_tok, dur)

            try:
                from cohere import Client
                _orig = Client.chat

                def _patched(self_sdk, *args, **kwargs):
                    model = kwargs.get("model") or "command-r"
                    agent.enforce(provider="cohere", model=model,
                                  estimated_tokens=kwargs.get("max_tokens"))

                    # ── Routing Intercept ──
                    if not kwargs.get("stream"):
                        try:
                            routed, routed_resp = agent._try_route_sync(
                                "cohere", model, kwargs, _orig, self_sdk)
                            if routed:
                                _record_cohere(model, routed_resp, 0)
                                return routed_resp
                        except Exception:
                            pass  # fail-open

                    t = time.perf_counter()
                    resp = _orig(self_sdk, *args, **kwargs)
                    _record_cohere(model, resp, int((time.perf_counter() - t) * 1000))
                    return resp

                Client.chat = _patched
            except (ImportError, AttributeError):
                pass

            try:
                import asyncio as _aio
                from cohere import AsyncClient
                _aorig = AsyncClient.chat

                async def _async_patched(self_sdk, *args, **kwargs):
                    model = kwargs.get("model") or "command-r"
                    loop = _aio.get_running_loop()
                    await loop.run_in_executor(
                        None, lambda: agent.enforce(provider="cohere", model=model))

                    # ── Routing Intercept ──
                    if not kwargs.get("stream"):
                        try:
                            routed, routed_resp = await agent._try_route_async(
                                "cohere", model, kwargs, _aorig, self_sdk)
                            if routed:
                                _record_cohere(model, routed_resp, 0)
                                return routed_resp
                        except Exception:
                            pass  # fail-open

                    t = time.perf_counter()
                    resp = await _aorig(self_sdk, *args, **kwargs)
                    _record_cohere(model, resp, int((time.perf_counter() - t) * 1000))
                    return resp

                AsyncClient.chat = _async_patched
            except (ImportError, AttributeError):
                pass

            self._instrumented.append("cohere")
            logger.debug("Modus: cohere instrumented v%s", self._sdk_versions["cohere"])
        except ImportError:
            pass
        except Exception as exc:
            logger.warning("Modus: cohere instrumentation failed: %s", exc)

    # ── Flush ──────────────────────────────────────────────────────────────────

    def flush(self) -> None:
        """Force an immediate flush of queued usage records. Useful in tests."""
        self._flush()

    def _flush_loop(self) -> None:
        while not self._shutdown.wait(timeout=self.flush_interval):
            self._flush()
            self._flush_routing_outcomes()
        self._flush(final=True)
        self._flush_routing_outcomes()

    def _flush(self, final: bool = False) -> None:
        if not self._api_key:
            return

        if self._aggregation_enabled:
            self._flush_aggregated(final=final)
        else:
            self._flush_raw(final=final)

    @staticmethod
    def _record_json(r: _UsageRecord) -> dict:
        return {
            "provider": r.provider,
            "resource_type": r.resource_type,
            "model": r.model,
            "operation": r.operation,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "total_tokens": r.total_tokens,
            "input_cost": str(r.input_cost) if r.input_cost is not None else None,
            "output_cost": str(r.output_cost) if r.output_cost is not None else None,
            "total_cost": str(r.total_cost),
            "duration_ms": r.duration_ms,
            "timestamp": r.timestamp,
            "metadata": r.metadata,
        }

    def _queue_batch(self, payload: dict) -> None:
        """Freeze a payload (with a fresh batch_id) into the pending queue."""
        batch_id = str(uuid.uuid4())
        body = json.dumps({"batch_id": batch_id, **payload}).encode()
        self._pending_batches.append(_PendingBatch(batch_id=batch_id, body=body))
        while len(self._pending_batches) > self._max_pending_batches:
            dropped = self._pending_batches.pop(0)
            logger.error(
                "Modus: %d ingest batches pending (orchestrator unreachable). "
                "DROPPING oldest batch %s — its usage is lost. Raise "
                "MODUS_MAX_PENDING_BATCHES or restore connectivity.",
                self._max_pending_batches + 1, dropped.batch_id,
            )

    def _build_raw_batches(self) -> None:
        with self._records_lock:
            if not self._records:
                return
            batch = self._records[:]
            self._records.clear()
        for k in range(0, len(batch), _RAW_CHUNK):
            self._queue_batch({
                "agent_version": __version__,
                "sdk_versions": self._sdk_versions,
                "records": [self._record_json(r) for r in batch[k:k + _RAW_CHUNK]],
            })

    def _build_aggregated_batch(self) -> None:
        """
        Aggregated payload format v2:
          - "aggregates": bucket summaries (1 per key per UTC hour) — these
            carry the counted usage of EVERY call
          - "traces": full-detail records (violations, errors, session calls,
            samples); detail only, already counted in the aggregates
          - "format": "aggregated" (tells orchestrator which ingest path to use)

        A billion calls/day becomes ~hundreds of aggregates per flush.
        """
        with self._agg_lock:
            if not self._agg_buckets and not self._agg_sampled:
                return
            buckets = list(self._agg_buckets.values())
            self._agg_buckets.clear()
            sampled = self._agg_sampled[:]
            self._agg_sampled.clear()
        self._queue_batch({
            "agent_version": __version__,
            "sdk_versions": self._sdk_versions,
            "format": "aggregated",
            "traces_counted_in_aggregates": True,
            "aggregates": [b.to_dict() for b in buckets],
            "traces": [self._record_json(r) for r in sampled],
        })

    def _ingest_backoff(self, retry_after: Optional[float]) -> float:
        """Next delay before retrying ingest: Retry-After or exponential, + jitter."""
        self._ingest_backoff_attempt += 1
        if retry_after is not None:
            base = min(retry_after, 300.0)
            delay = base + random.uniform(0, min(max(base, 1.0), 5.0))
        else:
            cap = min(300.0, float(2 ** min(self._ingest_backoff_attempt, 8)))
            delay = random.uniform(cap / 2, cap)
        self._ingest_backoff_until = time.monotonic() + delay
        return delay

    def _send_pending(self, ignore_backoff: bool = False) -> None:
        """Deliver pending batches in order; stop at the first retryable failure."""
        if not self._pending_batches:
            return
        if not ignore_backoff and time.monotonic() < self._ingest_backoff_until:
            logger.debug(
                "Modus: ingest backing off for %.1fs (%d batches pending)",
                self._ingest_backoff_until - time.monotonic(), len(self._pending_batches),
            )
            return
        while self._pending_batches:
            pb = self._pending_batches[0]
            pb.attempts += 1
            try:
                req = urllib_request.Request(
                    f"{self.orchestrator_url}/api/v1/ingest",
                    data=pb.body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Modus-APIKey": self._api_key,
                        "User-Agent": f"ModusAgent/{__version__}",
                    },
                    method="POST",
                )
                with _urlopen_tls(req, timeout=10) as resp:
                    result = json.loads(resp.read().decode())
                logger.debug(
                    "Modus: ingest batch %s accepted=%s (attempt %d)",
                    pb.batch_id, result.get("accepted", 0), pb.attempts,
                )
                self._pending_batches.pop(0)
                self._ingest_backoff_attempt = 0
                self._ingest_backoff_until = 0.0
            except HTTPError as exc:
                if exc.code not in _INGEST_RETRYABLE:
                    self._pending_batches.pop(0)
                    try:
                        detail = exc.read().decode(errors="replace")[:300]
                    except Exception:
                        detail = ""
                    logger.error(
                        "Modus: ingest batch %s rejected with HTTP %d and DROPPED "
                        "(not retryable): %s", pb.batch_id, exc.code, detail,
                    )
                    continue
                if exc.code == 429:
                    self.rate_limited_count += 1
                delay = self._ingest_backoff(_retry_after_seconds(exc))
                logger.warning(
                    "Modus: ingest batch %s got HTTP %d — kept, retrying in %.1fs "
                    "(%d batches pending)", pb.batch_id, exc.code, delay,
                    len(self._pending_batches),
                )
                return
            except Exception as exc:
                delay = self._ingest_backoff(None)
                logger.warning(
                    "Modus: ingest batch %s failed (%s) — kept, retrying in %.1fs "
                    "(%d batches pending)", pb.batch_id, exc, delay,
                    len(self._pending_batches),
                )
                return

    def _flush_raw(self, final: bool = False) -> None:
        """Flush buffered raw records (and retry pending batches).

        ``final`` (shutdown) makes one last delivery attempt even inside a
        backoff window.
        """
        if not self._api_key:
            return
        with self._flush_lock:
            self._build_raw_batches()
            self._send_pending(ignore_backoff=final)

    def _flush_aggregated(self, final: bool = False) -> None:
        """Flush aggregation buckets + traces (and retry pending batches).

        Raw records buffered before aggregation was enabled are drained too.
        """
        if not self._api_key:
            return
        with self._flush_lock:
            self._build_aggregated_batch()
            self._build_raw_batches()
            self._send_pending(ignore_backoff=final)

    def _shutdown_flush(self) -> None:
        self._shutdown.set()
        if self._flush_thread:
            self._flush_thread.join(timeout=15)

    # ── Heartbeat ──────────────────────────────────────────────────────────────

    def _heartbeat_loop(self) -> None:
        while not self._shutdown.wait(timeout=60):
            self._send_heartbeat()

    def _send_heartbeat(self) -> None:
        if not self._api_key:
            return
        try:
            payload = json.dumps({
                "agent_version": __version__,
                "instrumented_providers": self._instrumented,
                "sdk_versions": self._sdk_versions,
                "host_info": {
                    "python": f"{__import__('sys').version_info.major}.{__import__('sys').version_info.minor}",
                    "pid": __import__("os").getpid(),
                },
            }).encode()
            req = urllib_request.Request(
                f"{self.orchestrator_url}/api/v1/heartbeat",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Modus-APIKey": self._api_key,
                    "User-Agent": f"ModusAgent/{__version__}",
                },
                method="POST",
            )
            with _urlopen_tls(req, timeout=5) as resp:
                self._handle_heartbeat_response(resp)
        except HTTPError as exc:
            if exc.code == 429:
                self.rate_limited_count += 1
                logger.info("Modus: heartbeat rate limited (429); next heartbeat in 60s.")
            else:
                logger.debug("Modus: heartbeat: HTTP %d", exc.code)
        except Exception as exc:
            logger.debug("Modus: heartbeat: %s", exc)

    def _handle_heartbeat_response(self, resp) -> None:
        """Process heartbeat response — update routing table if present."""
        try:
            body = json.loads(resp.read().decode())
            routing_entries = body.get("routing_fingerprints", [])
            if routing_entries:
                from modus.routing_interceptor import update_routing_table
                update_routing_table(routing_entries)
        except Exception as exc:
            logger.debug("Modus: heartbeat response parse: %s", exc)

    # ── Routing intercept (Phase 2) ───────────────────────────────────────────

    def _try_route_sync(
        self, provider: str, model: str, kwargs: dict, orig_fn: Any, self_sdk: Any,
    ) -> tuple[bool, Any]:
        """
        Attempt intelligent routing to a cheaper model (sync).
        Returns (routed: bool, response: Any).
        Fail-open: never raises to caller.
        """
        try:
            if not self._routing_enabled or not self._app_id:
                return (False, None)

            from modus.routing_interceptor import (
                compute_fingerprint, make_routing_decision, _routing_context,
                should_sample_message, extract_last_user_message,
            )
            from modus.output_validator import OutputValidator

            system_prompt, user_prompt = self._extract_prompts_for_routing(provider, kwargs)
            if not system_prompt and not user_prompt:
                return (False, None)

            # Probabilistic message sampling for calibration
            sampled_msg = None
            if should_sample_message():
                sampled_msg = extract_last_user_message(provider, kwargs)

            call_site_id = _routing_context.call_site_id
            force_model_override = _routing_context.force_model
            fingerprint = compute_fingerprint(
                self._app_id, system_prompt or "default", call_site_id
            )

            # Force model from decorator takes absolute precedence
            if force_model_override:
                kw = dict(kwargs)
                kw["model"] = force_model_override
                t0 = time.perf_counter()
                resp = orig_fn(self_sdk, **kw)
                dur = int((time.perf_counter() - t0) * 1000)
                self._log_routing_outcome(
                    fingerprint_hash=fingerprint,
                    routed_to="force_model",
                    provider=provider,
                    original_model=model,
                    routed_model=force_model_override,
                    latency_ms=dur,
                    layer="table",
                    sampled_user_message=sampled_msg,
                )
                return (True, resp)

            decision = make_routing_decision(
                fingerprint_hash=fingerprint,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                provider=provider,
                model=model,
                generic_threshold=self._routing_generic_threshold,
            )

            if not decision.should_route or not decision.cheap_model:
                self._log_routing_outcome(
                    fingerprint_hash=fingerprint,
                    routed_to="expensive",
                    provider=provider,
                    original_model=model,
                    decision_source=decision.source,
                    decision_reason=decision.reason,
                    layer=decision.source,
                    sampled_user_message=sampled_msg,
                )
                return (False, None)

            # Attempt cheap model call
            kw = dict(kwargs)
            kw["model"] = decision.cheap_model
            t0 = time.perf_counter()
            cheap_resp = orig_fn(self_sdk, **kw)
            cheap_latency = int((time.perf_counter() - t0) * 1000)

            # Validate cheap model output
            resp_text = self._extract_response_text(provider, cheap_resp)
            passed, failure_reason = OutputValidator.validate_quick(resp_text)

            if passed:
                routed_label = "cheap" if decision.source == "table" else "cheap_provisional"
                self._log_routing_outcome(
                    fingerprint_hash=fingerprint,
                    routed_to=routed_label,
                    provider=provider,
                    original_model=model,
                    routed_model=decision.cheap_model,
                    cheap_latency_ms=cheap_latency,
                    decision_source=decision.source,
                    decision_confidence=decision.confidence,
                    layer=decision.source,
                    sampled_user_message=sampled_msg,
                )
                return (True, cheap_resp)

            # Validation failed — escalate (caller will proceed with original model)
            self._log_routing_outcome(
                fingerprint_hash=fingerprint,
                routed_to="escalated",
                provider=provider,
                original_model=model,
                routed_model=decision.cheap_model,
                cheap_latency_ms=cheap_latency,
                escalation_reason=failure_reason,
                decision_source=decision.source,
                layer=decision.source,
                sampled_user_message=sampled_msg,
            )
            return (False, None)

        except Exception as exc:
            logger.debug("Modus routing fail-open: %s", exc)
            return (False, None)

    async def _try_route_async(
        self, provider: str, model: str, kwargs: dict, orig_fn: Any, self_sdk: Any,
    ) -> tuple[bool, Any]:
        """
        Async version of _try_route_sync.
        Fail-open: never raises to caller.
        """
        try:
            if not self._routing_enabled or not self._app_id:
                return (False, None)

            from modus.routing_interceptor import (
                compute_fingerprint, make_routing_decision, _routing_context,
                should_sample_message, extract_last_user_message,
            )
            from modus.output_validator import OutputValidator

            system_prompt, user_prompt = self._extract_prompts_for_routing(provider, kwargs)
            if not system_prompt and not user_prompt:
                return (False, None)

            sampled_msg = None
            if should_sample_message():
                sampled_msg = extract_last_user_message(provider, kwargs)

            call_site_id = _routing_context.call_site_id
            force_model_override = _routing_context.force_model
            fingerprint = compute_fingerprint(
                self._app_id, system_prompt or "default", call_site_id
            )

            if force_model_override:
                kw = dict(kwargs)
                kw["model"] = force_model_override
                t0 = time.perf_counter()
                resp = await orig_fn(self_sdk, **kw)
                dur = int((time.perf_counter() - t0) * 1000)
                self._log_routing_outcome(
                    fingerprint_hash=fingerprint,
                    routed_to="force_model",
                    provider=provider,
                    original_model=model,
                    routed_model=force_model_override,
                    latency_ms=dur,
                    layer="table",
                    sampled_user_message=sampled_msg,
                )
                return (True, resp)

            decision = make_routing_decision(
                fingerprint_hash=fingerprint,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                provider=provider,
                model=model,
                generic_threshold=self._routing_generic_threshold,
            )

            if not decision.should_route or not decision.cheap_model:
                self._log_routing_outcome(
                    fingerprint_hash=fingerprint,
                    routed_to="expensive",
                    provider=provider,
                    original_model=model,
                    decision_source=decision.source,
                    decision_reason=decision.reason,
                    layer=decision.source,
                    sampled_user_message=sampled_msg,
                )
                return (False, None)

            kw = dict(kwargs)
            kw["model"] = decision.cheap_model
            t0 = time.perf_counter()
            cheap_resp = await orig_fn(self_sdk, **kw)
            cheap_latency = int((time.perf_counter() - t0) * 1000)

            resp_text = self._extract_response_text(provider, cheap_resp)
            passed, failure_reason = OutputValidator.validate_quick(resp_text)

            if passed:
                routed_label = "cheap" if decision.source == "table" else "cheap_provisional"
                self._log_routing_outcome(
                    fingerprint_hash=fingerprint,
                    routed_to=routed_label,
                    provider=provider,
                    original_model=model,
                    routed_model=decision.cheap_model,
                    cheap_latency_ms=cheap_latency,
                    decision_source=decision.source,
                    decision_confidence=decision.confidence,
                    layer=decision.source,
                    sampled_user_message=sampled_msg,
                )
                return (True, cheap_resp)

            self._log_routing_outcome(
                fingerprint_hash=fingerprint,
                routed_to="escalated",
                provider=provider,
                original_model=model,
                routed_model=decision.cheap_model,
                cheap_latency_ms=cheap_latency,
                escalation_reason=failure_reason,
                decision_source=decision.source,
                layer=decision.source,
                sampled_user_message=sampled_msg,
            )
            return (False, None)

        except Exception as exc:
            logger.debug("Modus routing fail-open: %s", exc)
            return (False, None)

    @staticmethod
    def _extract_prompts_for_routing(provider: str, kwargs: dict) -> tuple[str, str]:
        """Extract (system_prompt, user_prompt) for routing from provider-specific kwargs."""
        try:
            system_prompt = ""
            user_prompt = ""

            if provider == "anthropic":
                system = kwargs.get("system", "")
                if isinstance(system, str):
                    system_prompt = system
                elif isinstance(system, list):
                    system_prompt = " ".join(
                        b.get("text", "") for b in system if isinstance(b, dict)
                    )
                messages = kwargs.get("messages", [])
                if messages:
                    last = messages[-1]
                    if isinstance(last, dict):
                        content = last.get("content", "")
                        if isinstance(content, str):
                            user_prompt = content
                        elif isinstance(content, list):
                            user_prompt = " ".join(
                                b.get("text", "") for b in content
                                if isinstance(b, dict)
                            )

            elif provider in ("openai", "groq", "mistral"):
                messages = kwargs.get("messages", [])
                for msg in messages:
                    if isinstance(msg, dict):
                        role = msg.get("role", "")
                        content = msg.get("content", "") or ""
                        if role == "system" and not system_prompt:
                            system_prompt = content if isinstance(content, str) else str(content)
                        elif role == "user":
                            user_prompt = content if isinstance(content, str) else str(content)

            elif provider == "google":
                config = kwargs.get("config")
                if isinstance(config, dict):
                    system_prompt = str(config.get("system_instruction", ""))
                contents = kwargs.get("contents", "")
                user_prompt = str(contents) if contents else ""

            elif provider == "cohere":
                system_prompt = str(kwargs.get("preamble", "") or "")
                user_prompt = str(kwargs.get("message", "") or "")

            return (system_prompt, user_prompt)
        except Exception:
            return ("", "")

    @staticmethod
    def _extract_response_text(provider: str, resp: Any) -> str:
        """Extract response text from provider-specific response objects."""
        try:
            if provider == "anthropic":
                content = getattr(resp, "content", [])
                if content and hasattr(content[0], "text"):
                    return content[0].text
                return str(content) if content else ""

            if provider in ("openai", "groq", "mistral"):
                choices = getattr(resp, "choices", [])
                if choices:
                    msg = getattr(choices[0], "message", None)
                    if msg:
                        return getattr(msg, "content", "") or ""
                return ""

            if provider == "google":
                text = getattr(resp, "text", None)
                if text:
                    return text
                candidates = getattr(resp, "candidates", [])
                if candidates:
                    content = getattr(candidates[0], "content", None)
                    if content:
                        parts = getattr(content, "parts", [])
                        if parts:
                            return getattr(parts[0], "text", "")
                return ""

            if provider == "cohere":
                text = getattr(resp, "text", None)
                if text:
                    return text
                msg = getattr(resp, "message", None)
                if msg:
                    content = getattr(msg, "content", [])
                    if content and hasattr(content[0], "text"):
                        return content[0].text
                return ""

            return str(resp) if resp else ""
        except Exception:
            return ""

    def _log_routing_outcome(self, **kwargs: Any) -> None:
        """Buffer a routing outcome for the next flush cycle."""
        try:
            kwargs["timestamp"] = datetime.now(timezone.utc).isoformat()
            kwargs["app_id"] = self._app_id
            with self._routing_outcomes_lock:
                if len(self._routing_outcomes) >= self._max_buffer_size:
                    self._routing_outcomes.pop(0)
                self._routing_outcomes.append(kwargs)
        except Exception:
            pass

    def _flush_routing_outcomes(self) -> None:
        """Send buffered routing outcomes to orchestrator."""
        if not self._api_key:
            return
        with self._routing_outcomes_lock:
            if not self._routing_outcomes:
                return
            batch = self._routing_outcomes[:]
            self._routing_outcomes.clear()

        try:
            payload = json.dumps({"outcomes": batch}).encode()
            req = urllib_request.Request(
                f"{self.orchestrator_url}/api/v1/routing/outcomes/batch",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Modus-APIKey": self._api_key,
                    "User-Agent": f"ModusAgent/{__version__}",
                },
                method="POST",
            )
            with _urlopen_tls(req, timeout=10) as resp:
                resp.read()
                logger.debug("Modus: flushed %d routing outcomes", len(batch))
        except Exception as exc:
            logger.debug("Modus: routing outcome flush: %s", exc)
            with self._routing_outcomes_lock:
                self._routing_outcomes = batch + self._routing_outcomes


# ── Bedrock helpers ────────────────────────────────────────────────────────────

class _ReusableBody:
    """Wraps a bytes body so the caller can still call .read() after we've consumed it."""
    def __init__(self, data: bytes):
        self._data = data

    def read(self, amt=None):
        return self._data if amt is None else self._data[:amt]


class _BedrockStreamWrapper:
    """Wraps Bedrock streaming body to capture token usage from invocation metrics."""
    def __init__(self, stream, model_id: str, t0: float, agent: ModusAgent):
        self._stream = stream
        self._model_id = model_id
        self._t0 = t0
        self._agent = agent

    def __iter__(self):
        import json as _json
        in_tok = out_tok = None
        try:
            for event in self._stream:
                yield event
                chunk = event.get("chunk", {})
                body_bytes = chunk.get("bytes", b"")
                if body_bytes:
                    try:
                        data = _json.loads(body_bytes)
                        metrics = data.get("amazon-bedrock-invocationMetrics", {})
                        if metrics:
                            in_tok = metrics.get("inputTokenCount", in_tok)
                            out_tok = metrics.get("outputTokenCount", out_tok)
                        elif "usage" in data:
                            u = data["usage"]
                            in_tok = u.get("input_tokens", in_tok)
                            out_tok = u.get("output_tokens", out_tok)
                    except Exception:
                        pass
        finally:
            dur = int((time.perf_counter() - self._t0) * 1000)
            self._agent._rec("bedrock", self._model_id,
                             "invoke_model_with_response_stream",
                             in_tok, out_tok, dur)
