"""
modus.routing_interceptor
===================================
SDK intercept layer for intelligent model routing.

Maintains an in-memory routing table populated from heartbeat responses.
Applies routing decisions before any LLM API call is made.

Zero added latency on the happy path — routing decisions for known fingerprints
resolve via in-memory dictionary lookup. No network calls at request time.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger("modus.routing")

# ── Fingerprint computation ──────────────────────────────────────────────────


def compute_fingerprint(
    app_id: str, system_prompt: str, call_site_id: Optional[str] = None
) -> str:
    """
    Compute a stable fingerprint for a call site shape.
    Uses SHA256 of (app_id, system_prompt_hash, call_site_id).
    System prompt is hashed separately so it can be tracked independently.
    """
    system_prompt_hash = hashlib.sha256(system_prompt.encode()).hexdigest()[:16]
    components = [app_id, system_prompt_hash, call_site_id or "default"]
    return hashlib.sha256(json.dumps(components).encode()).hexdigest()[:32]


def compute_system_prompt_hash(system_prompt: str) -> str:
    """SHA256 of system prompt content (first 16 hex chars)."""
    return hashlib.sha256(system_prompt.encode()).hexdigest()[:16]


# ── Token bucketing ──────────────────────────────────────────────────────────

BUCKET_BOUNDARIES = [0, 100, 250, 500, 1000, 2000, 4000, 8000, 16000, float("inf")]


def token_bucket(count: int) -> int:
    """Map a token count to a bucket index for conformal checking."""
    for i, boundary in enumerate(BUCKET_BOUNDARIES[1:], 1):
        if count < boundary:
            return i
    return len(BUCKET_BOUNDARIES) - 1


# ── Routing table (in-memory singleton) ──────────────────────────────────────


@dataclass
class RoutingEntry:
    """One row from routing_fingerprints, synced via heartbeat."""

    fingerprint_hash: str
    phase: str  # 'observe' | 'calibrating' | 'routing' | 'drift_flagged' | 'excluded'
    routing_confidence: float = 0.0
    conformal_threshold: Optional[float] = None
    cheap_model: Optional[str] = None
    expensive_model: Optional[str] = None
    force_model: Optional[str] = None
    allow_routing: bool = True
    max_misroute_rate: float = 0.01
    input_token_bucket_bounds: Optional[list] = None

    # Extended stats (not always populated)
    output_ratio_p10: Optional[float] = None


_routing_table: dict[str, RoutingEntry] = {}
_routing_table_lock = threading.RLock()


def update_routing_table(entries: list[dict]) -> None:
    """Called by heartbeat handler when orchestrator returns updated fingerprints."""
    with _routing_table_lock:
        for e in entries:
            _routing_table[e["fingerprint_hash"]] = RoutingEntry(
                fingerprint_hash=e["fingerprint_hash"],
                phase=e.get("phase", "observe"),
                routing_confidence=e.get("routing_confidence", 0.0),
                conformal_threshold=e.get("conformal_threshold"),
                cheap_model=e.get("cheap_model"),
                expensive_model=e.get("expensive_model"),
                force_model=e.get("force_model"),
                allow_routing=e.get("allow_routing", True),
                max_misroute_rate=e.get("max_misroute_rate", 0.01),
                input_token_bucket_bounds=e.get("input_token_bucket_bounds"),
            )


def get_routing_entry(fingerprint_hash: str) -> Optional[RoutingEntry]:
    """Thread-safe lookup of a routing entry by fingerprint hash."""
    with _routing_table_lock:
        return _routing_table.get(fingerprint_hash)


def get_routing_table_snapshot() -> dict[str, RoutingEntry]:
    """Return a shallow copy of the routing table for diagnostics."""
    with _routing_table_lock:
        return dict(_routing_table)


# ── Thread-local routing context (set by decorators) ─────────────────────────


class _RoutingContext(threading.local):
    force_model: Optional[str] = None
    call_site_id: Optional[str] = None
    max_misroute_rate: Optional[float] = None


_routing_context = _RoutingContext()


# ── Core intercept logic ─────────────────────────────────────────────────────


class RoutingInterceptor:
    """
    Wraps provider API calls. Decides whether to route to cheap model,
    pass through to expensive model, or escalate after cheap model fails.
    """

    def __init__(
        self,
        app_id: str,
        log_outcome_fn: Optional[Callable] = None,
        call_model_fn: Optional[Callable] = None,
    ):
        self.app_id = app_id
        self._log_outcome = log_outcome_fn
        self._call_model = call_model_fn

    def intercept(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        call_site_id: Optional[str] = None,
        **kwargs: Any,
    ) -> dict:
        """
        Main intercept entry point. Returns the response dict regardless of
        which model ultimately served it.
        """
        request_id = str(uuid.uuid4())

        # Use decorator context if available
        effective_call_site = (
            _routing_context.call_site_id or call_site_id
        )
        effective_force_model = _routing_context.force_model

        fingerprint = compute_fingerprint(
            self.app_id, system_prompt, effective_call_site
        )
        entry = get_routing_entry(fingerprint)

        input_tokens = self._estimate_tokens(system_prompt + user_prompt)

        # Force model override from decorator takes absolute precedence
        if effective_force_model:
            return self._do_call(
                effective_force_model, system_prompt, user_prompt, **kwargs
            )["response"]

        # Phase: no entry yet, or observe mode — pass through and log
        if entry is None or entry.phase == "observe":
            result = self._do_call(model, system_prompt, user_prompt, **kwargs)
            self._log(
                fingerprint_hash=fingerprint,
                app_id=self.app_id,
                request_id=request_id,
                routed_to="expensive",
                input_token_count=input_tokens,
                expensive_latency_ms=result.get("latency_ms"),
                cost_saved=0.0,
                entry=entry,
            )
            return result["response"]

        # Force model in fingerprint entry
        if entry.force_model:
            return self._do_call(
                entry.force_model, system_prompt, user_prompt, **kwargs
            )["response"]

        # Phase: routing — apply routing decision
        if entry.phase == "routing" and entry.allow_routing:
            return self._attempt_cheap_route(
                entry, fingerprint, system_prompt, user_prompt,
                model, input_tokens, request_id, **kwargs
            )

        # Phase: drift_flagged, calibrating, excluded — pass through
        result = self._do_call(model, system_prompt, user_prompt, **kwargs)
        self._log(
            fingerprint_hash=fingerprint,
            app_id=self.app_id,
            request_id=request_id,
            routed_to="expensive",
            input_token_count=input_tokens,
            expensive_latency_ms=result.get("latency_ms"),
            cost_saved=0.0,
            escalation_reason=f"phase_{entry.phase}",
            entry=entry,
        )
        return result["response"]

    def _attempt_cheap_route(
        self,
        entry: RoutingEntry,
        fingerprint: str,
        system_prompt: str,
        user_prompt: str,
        original_model: str,
        input_tokens: int,
        request_id: str,
        **kwargs: Any,
    ) -> dict:
        """Attempt routing to cheap model with validation and escalation."""
        from modus.output_validator import OutputValidator

        bucket = token_bucket(input_tokens)
        nonconformity_score = self._compute_nonconformity(bucket, entry)

        # Conformal check: is this request within calibrated distribution?
        if (
            entry.conformal_threshold is not None
            and nonconformity_score > entry.conformal_threshold
        ):
            result = self._do_call(
                original_model, system_prompt, user_prompt, **kwargs
            )
            self._log(
                fingerprint_hash=fingerprint,
                app_id=self.app_id,
                request_id=request_id,
                routed_to="expensive",
                input_token_count=input_tokens,
                conformal_check_passed=False,
                escalation_reason="conformal_fail",
                expensive_latency_ms=result.get("latency_ms"),
                cost_saved=0.0,
                nonconformity_score=nonconformity_score,
                entry=entry,
            )
            return result["response"]

        # Attempt cheap model
        t0 = time.monotonic()
        cheap_result = self._do_call(
            entry.cheap_model, system_prompt, user_prompt, **kwargs
        )
        cheap_latency = int((time.monotonic() - t0) * 1000)

        # Output validation
        validation = OutputValidator.validate(
            response=cheap_result["response"],
            fingerprint=entry,
            input_tokens=input_tokens,
        )

        if validation.passed:
            cost_saved = self._estimate_cost_delta(
                input_tokens,
                cheap_result.get("output_tokens", 0),
                entry.cheap_model,
                entry.expensive_model,
            )
            self._log(
                fingerprint_hash=fingerprint,
                app_id=self.app_id,
                request_id=request_id,
                routed_to="cheap",
                input_token_count=input_tokens,
                output_token_count=cheap_result.get("output_tokens"),
                input_token_bucket=bucket,
                validator_passed=True,
                conformal_check_passed=True,
                structural_check_passed=validation.structural_passed,
                cheap_model=entry.cheap_model,
                expensive_model=entry.expensive_model,
                cheap_latency_ms=cheap_latency,
                cost_saved=cost_saved,
                nonconformity_score=nonconformity_score,
                entry=entry,
            )
            return cheap_result["response"]

        # Validator failed — escalate to expensive model
        t1 = time.monotonic()
        expensive_result = self._do_call(
            entry.expensive_model or original_model,
            system_prompt,
            user_prompt,
            **kwargs,
        )
        expensive_latency = int((time.monotonic() - t1) * 1000)

        self._log(
            fingerprint_hash=fingerprint,
            app_id=self.app_id,
            request_id=request_id,
            routed_to="escalated",
            input_token_count=input_tokens,
            input_token_bucket=bucket,
            validator_passed=False,
            conformal_check_passed=True,
            structural_check_passed=validation.structural_passed,
            escalation_reason=validation.failure_reason,
            cheap_model=entry.cheap_model,
            expensive_model=entry.expensive_model,
            cheap_latency_ms=cheap_latency,
            expensive_latency_ms=expensive_latency,
            cost_saved=0.0,
            nonconformity_score=nonconformity_score,
            entry=entry,
        )
        return expensive_result["response"]

    @staticmethod
    def _compute_nonconformity(bucket: int, entry: RoutingEntry) -> float:
        """
        Lightweight nonconformity score for conformal threshold check.
        Uses token bucket distance from calibration bounds.
        No embedding inference at runtime.
        """
        if not entry.input_token_bucket_bounds:
            return 0.0
        low, high = entry.input_token_bucket_bounds
        if low <= bucket <= high:
            return 0.0
        dist = min(abs(bucket - low), abs(bucket - high))
        return min(dist / max(high - low, 1), 1.0)

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Approximation: 1 token ~ 4 characters."""
        return max(1, len(text) // 4)

    def _do_call(
        self, model: str, system_prompt: str, user_prompt: str, **kwargs: Any
    ) -> dict:
        """
        Call a model via the registered call_model_fn.
        Returns {'response': ..., 'latency_ms': int, 'output_tokens': int}
        """
        if self._call_model is None:
            raise RuntimeError(
                "RoutingInterceptor requires a call_model_fn to execute model calls"
            )
        t0 = time.monotonic()
        result = self._call_model(model, system_prompt, user_prompt, **kwargs)
        if "latency_ms" not in result:
            result["latency_ms"] = int((time.monotonic() - t0) * 1000)
        return result

    def _log(self, **kwargs: Any) -> None:
        """Fire-and-forget outcome log to orchestrator. Must not block."""
        # Remove entry (not a DB field) before logging
        kwargs.pop("entry", None)
        if self._log_outcome:
            try:
                self._log_outcome(**kwargs)
            except Exception as exc:
                logger.debug("Routing outcome log failed: %s", exc)

    @staticmethod
    def _estimate_cost_delta(
        input_tokens: int,
        output_tokens: int,
        cheap_model: Optional[str],
        expensive_model: Optional[str],
    ) -> "Decimal":  # noqa: F821 — Decimal imported inside function body
        """
        Estimates cost saved by routing to cheap model.
        Uses the SDK's authoritative pricing table for lookups.
        Returns Decimal — never float for money.
        """
        from decimal import Decimal
        from modus.pricing import _lookup

        if not cheap_model or not expensive_model:
            return Decimal("0")

        # Use SDK pricing table (authoritative source)
        # _lookup expects (provider, model) — we infer provider from model name
        cheap_provider = _infer_provider(cheap_model)
        expensive_provider = _infer_provider(expensive_model)

        cheap_inp, cheap_out = _lookup(cheap_provider, cheap_model)
        expensive_inp, expensive_out = _lookup(expensive_provider, expensive_model)

        _1000 = Decimal("1000")
        cheap_cost = (
            Decimal(input_tokens) * cheap_inp / _1000
            + Decimal(output_tokens) * cheap_out / _1000
        )
        expensive_cost = (
            Decimal(input_tokens) * expensive_inp / _1000
            + Decimal(output_tokens) * expensive_out / _1000
        )
        return max(Decimal("0"), expensive_cost - cheap_cost)


# ── Provider inference for pricing lookups ────────────────────────────────

_MODEL_PROVIDER_MAP: dict[str, str] = {
    "claude": "anthropic",
    "gpt": "openai",
    "o1": "openai",
    "o3": "openai",
    "o4": "openai",
    "gemini": "google",
    "llama": "groq",
    "mixtral": "mistral",
    "mistral": "mistral",
    "command": "cohere",
    "grok": "xai",
    "deepseek": "deepseek",
    "titan": "bedrock",
    "gemma": "groq",
    "dbrx": "databricks",
}


def _infer_provider(model: str) -> str:
    """Infer the provider from a model name for pricing lookups."""
    model_lower = model.lower()
    for prefix, provider in _MODEL_PROVIDER_MAP.items():
        if model_lower.startswith(prefix):
            return provider
    return "openai"  # conservative default


# ── Structural feature extraction (Phase 2) ──────────────────────────────

import re as _re


def extract_structural_features(system_prompt: str, user_prompt: str) -> dict:
    """
    Extract structural features from prompts for the generic classifier.
    Features are fast to compute (no tokenizer needed).
    """
    sys_len = len(system_prompt)
    usr_len = len(user_prompt)
    return {
        "system_prompt_length": sys_len,
        "user_prompt_length": usr_len,
        "system_prompt_tokens": max(1, sys_len // 4),
        "user_prompt_tokens": max(1, usr_len // 4),
        "total_tokens": max(1, (sys_len + usr_len) // 4),
        "has_json_instruction": 1 if _re.search(r"json|JSON|structured", system_prompt) else 0,
        "has_code_instruction": 1 if _re.search(r"code|program|function|class|def\s", system_prompt) else 0,
        "has_classification": 1 if _re.search(r"classif|categoriz|label|tag", system_prompt, _re.IGNORECASE) else 0,
        "has_extraction": 1 if _re.search(r"extract|parse|identify|find", system_prompt, _re.IGNORECASE) else 0,
        "prompt_ratio": usr_len / max(sys_len, 1),
        "system_word_count": len(system_prompt.split()),
        "user_word_count": len(user_prompt.split()),
    }


# ── Default cheap model per provider ──────────────────────────────────────

PROVIDER_CHEAP_MODELS: dict[str, str] = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
    "google": "gemini-2.0-flash",
    "groq": "llama-3.1-8b-instant",
    "mistral": "mistral-small-latest",
    "cohere": "command-r",
}


def _default_cheap_model(provider: str) -> Optional[str]:
    """Return the default cheap model for a provider."""
    return PROVIDER_CHEAP_MODELS.get(provider)


# ── Routing decision ─────────────────────────────────────────────────────


@dataclass
class RoutingDecision:
    """Result of the two-layer routing decision."""

    should_route: bool
    confidence: float
    cheap_model: Optional[str]
    source: str  # 'generic', 'table', or 'none'
    reason: str


# ── Generic classifier (Layer 0) ─────────────────────────────────────────


class GenericClassifier:
    """
    Layer 0: Bundled decision tree for day-one routing before calibration.

    Loads a pickled model from routing_classifier.pkl. If the model is not
    available, predict() returns 0.0 (effectively disabling generic routing
    until the pkl is shipped).

    Thread-safe singleton.
    """

    _instance: Optional["GenericClassifier"] = None
    _init_lock = threading.Lock()

    def __init__(self) -> None:
        self._model = None
        self._load_model()

    @classmethod
    def get_instance(cls) -> "GenericClassifier":
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _load_model(self) -> None:
        """Load the bundled classifier from routing_classifier.pkl."""
        try:
            import pickle
            import pathlib

            # Restricted unpickler — only allow sklearn/numpy types (defense-in-depth)
            _SAFE_PREFIXES = ("sklearn.", "numpy", "builtins", "collections", "_codecs")

            class _RestrictedModelUnpickler(pickle.Unpickler):
                def find_class(self, module: str, name: str) -> type:
                    if any(module.startswith(p) for p in _SAFE_PREFIXES):
                        return super().find_class(module, name)
                    raise pickle.UnpicklingError(
                        f"Blocked: {module}.{name}"
                    )

            model_path = pathlib.Path(__file__).parent / "routing_classifier.pkl"
            if model_path.exists():
                with open(model_path, "rb") as f:
                    self._model = _RestrictedModelUnpickler(f).load()
                logger.debug("Generic routing classifier loaded from %s", model_path)
        except Exception as exc:
            logger.debug("Generic classifier not loaded: %s", exc)

    def predict(self, features: dict) -> float:
        """
        Return a provisional confidence score between 0.0 and 1.0.
        If no model is loaded, returns 0.0 (fail-open: no provisional routing).
        """
        if self._model is None:
            return 0.0
        try:
            score = float(self._model.predict(features))
            return max(0.0, min(1.0, score))
        except Exception:
            return 0.0


# ── Two-layer routing decision ───────────────────────────────────────────


def make_routing_decision(
    fingerprint_hash: str,
    system_prompt: str,
    user_prompt: str,
    provider: str,
    model: str,
    generic_threshold: float = 0.70,
) -> RoutingDecision:
    """
    Two-layer routing decision:
      Layer 1 (authoritative): Per-customer routing table from heartbeat
      Layer 0 (provisional):   Generic classifier for day-one routing

    Layer 1 takes precedence when an entry exists.
    """
    # Layer 1: Per-customer routing table
    entry = get_routing_entry(fingerprint_hash)

    if entry is not None:
        # Force model override
        if entry.force_model:
            return RoutingDecision(
                should_route=True,
                confidence=1.0,
                cheap_model=entry.force_model,
                source="table",
                reason="force_model_override",
            )

        # Routing phase with sufficient confidence
        if entry.phase == "routing" and entry.allow_routing:
            threshold = entry.conformal_threshold or 0.5
            if entry.routing_confidence >= threshold:
                return RoutingDecision(
                    should_route=True,
                    confidence=entry.routing_confidence,
                    cheap_model=entry.cheap_model or _default_cheap_model(provider),
                    source="table",
                    reason="calibrated_routing",
                )
            return RoutingDecision(
                should_route=False,
                confidence=entry.routing_confidence,
                cheap_model=None,
                source="table",
                reason="confidence_below_threshold",
            )

        # Non-routing phases (observe, calibrating, drift_flagged, excluded)
        return RoutingDecision(
            should_route=False,
            confidence=0.0,
            cheap_model=None,
            source="table",
            reason=f"phase_{entry.phase}",
        )

    # Layer 0: Generic classifier (provisional)
    features = extract_structural_features(system_prompt, user_prompt)
    classifier = GenericClassifier.get_instance()
    provisional_confidence = classifier.predict(features)

    if provisional_confidence >= generic_threshold:
        return RoutingDecision(
            should_route=True,
            confidence=provisional_confidence,
            cheap_model=_default_cheap_model(provider),
            source="generic",
            reason="generic_classifier_provisional",
        )

    return RoutingDecision(
        should_route=False,
        confidence=provisional_confidence,
        cheap_model=None,
        source="none",
        reason="below_generic_threshold",
    )


# ── Message sampling for calibration ─────────────────────────────────────

import random

# Sampling rate: what fraction of requests include the user message
# in routing outcomes for calibration. Low rate to minimize payload size.
_SAMPLING_RATE = 0.10  # 10% of requests
_MAX_SAMPLED_LENGTH = 512  # Truncate to first 512 chars


def should_sample_message() -> bool:
    """Probabilistic check: should this request's user message be sampled?"""
    return random.random() < _SAMPLING_RATE


def extract_last_user_message(provider: str, kwargs: dict) -> Optional[str]:
    """
    Extract and truncate the last user message from provider-specific kwargs.
    Returns None if no user message found or on error.
    """
    try:
        text = _extract_user_text(provider, kwargs)
        if text:
            return text[:_MAX_SAMPLED_LENGTH]
        return None
    except Exception:
        return None


def _extract_user_text(provider: str, kwargs: dict) -> Optional[str]:
    """Provider-specific user message extraction."""
    if provider in ("anthropic",):
        messages = kwargs.get("messages", [])
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(block.get("text", ""))
                    return " ".join(parts) if parts else None
        return None

    if provider in ("openai", "groq", "mistral"):
        messages = kwargs.get("messages", [])
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(block.get("text", ""))
                    return " ".join(parts) if parts else None
        return None

    if provider == "google":
        contents = kwargs.get("contents", [])
        if isinstance(contents, str):
            return contents
        if isinstance(contents, list):
            for content in reversed(contents):
                if isinstance(content, dict):
                    role = content.get("role", "user")
                    if role == "user":
                        parts = content.get("parts", [])
                        for part in parts:
                            if isinstance(part, dict) and "text" in part:
                                return part["text"]
                            if isinstance(part, str):
                                return part
        return None

    if provider == "cohere":
        message = kwargs.get("message")
        if isinstance(message, str):
            return message
        return None

    return None
