"""
Modus — Ingest API Router
==================================
POST /api/v1/ingest    — receive usage records from agents
POST /api/v1/heartbeat — receive agent liveness signal

These are the highest-volume endpoints. Design priorities:
    1. Fast — minimal processing on the hot path, bulk insert
    2. Idempotent — duplicate batch_ids are silently ignored
    3. Resilient — validation errors on individual records do not reject the batch
    4. Secure — every request authenticated via per-app mds_ key
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import get_app_identity
from orchestrator.core.config import settings
from orchestrator.db.models import App
from orchestrator.db.session import get_session
from orchestrator.metrics.prometheus import (
    INGEST_BATCH_SIZE,
    INGEST_DURATION_SECONDS,
    INGEST_RECORDS_TOTAL,
    INGEST_REQUESTS_TOTAL,
    TOTAL_COST_INGESTED,
    TOTAL_TOKENS_INGESTED,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Request / Response schemas ────────────────────────────────────────────────

# Metadata keys that carry an agent session id. The SDK sends
# ``mds_session_id`` (plus ``mds_call_id`` / ``mds_parent_id`` span ids that the
# attribution engine reads from the stored metadata); ``session_id`` is
# accepted for hand-written integrations.
_SESSION_META_KEYS = ("mds_session_id", "session_id")
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9._:\-]{1,64}")


def _session_id_of(metadata: Optional[dict]) -> Optional[str]:
    if not metadata:
        return None
    for k in _SESSION_META_KEYS:
        sid = metadata.get(k)
        if sid:
            return sid
    return None


class UsageRecordIn(BaseModel):
    """Single usage record from agent payload."""

    provider: str = Field(..., max_length=64)
    resource_type: str = Field(..., max_length=64)
    model: Optional[str] = Field(None, max_length=128)
    operation: Optional[str] = Field(None, max_length=128)

    input_tokens: Optional[int] = Field(None, ge=0)
    output_tokens: Optional[int] = Field(None, ge=0)
    total_tokens: Optional[int] = Field(None, ge=0)

    input_cost: Optional[Decimal] = Field(None, ge=0)
    output_cost: Optional[Decimal] = Field(None, ge=0)
    total_cost: Decimal = Field(Decimal("0"), ge=0)

    duration_ms: Optional[int] = Field(None, ge=0)
    timestamp: datetime

    metadata: Optional[dict] = None

    @field_validator("metadata")
    @classmethod
    def check_session_meta(cls, v: Optional[dict]) -> Optional[dict]:
        # The session id is copied into usage_records.session_id
        # (VARCHAR(64)); reject a value that cannot be stored intact rather
        # than truncating it and merging unrelated sessions.
        if v:
            for k in _SESSION_META_KEYS:
                sid = v.get(k)
                if sid is None:
                    continue
                if not isinstance(sid, str) or not _SESSION_ID_RE.fullmatch(sid):
                    raise ValueError(
                        f"metadata.{k} must be 1-64 characters of [A-Za-z0-9._:-]"
                    )
        return v

    @field_validator("timestamp", mode="before")
    @classmethod
    def ensure_utc(cls, v):
        if isinstance(v, str):
            v = datetime.fromisoformat(v)
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

    @field_validator("provider")
    @classmethod
    def lowercase_provider(cls, v: str) -> str:
        return v.lower().strip()


class AggregateIn(BaseModel):
    """Pre-aggregated usage summary from SDK-side aggregation."""
    provider: str = Field(..., max_length=64)
    model: Optional[str] = Field(None, max_length=128)
    operation: Optional[str] = Field(None, max_length=128)
    resource_type: str = Field(..., max_length=64)

    call_count: int = Field(..., ge=1)
    input_tokens: int = Field(0, ge=0)
    output_tokens: int = Field(0, ge=0)
    total_tokens: int = Field(0, ge=0)

    input_cost: Decimal = Field(Decimal("0"), ge=0)
    output_cost: Decimal = Field(Decimal("0"), ge=0)
    total_cost: Decimal = Field(Decimal("0"), ge=0)

    duration_ms_sum: int = Field(0, ge=0)
    duration_ms_min: Optional[int] = Field(None, ge=0)
    duration_ms_max: Optional[int] = Field(None, ge=0)
    duration_ms_avg: Optional[int] = Field(None, ge=0)

    window_start: datetime
    window_end: datetime

    @field_validator("window_start", "window_end", mode="before")
    @classmethod
    def ensure_utc_agg(cls, v):
        if isinstance(v, str):
            v = datetime.fromisoformat(v)
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

    @field_validator("provider")
    @classmethod
    def lowercase_provider_agg(cls, v: str) -> str:
        return v.lower().strip()

    @model_validator(mode="after")
    def check_window(self) -> "AggregateIn":
        if self.window_end < self.window_start:
            raise ValueError("window_end must not be before window_start")
        return self


class IngestPayload(BaseModel):
    """
    Batch payload from a single agent flush.

    Supports two formats:
      - Raw (default): "records" list of individual usage records
      - Aggregated: "format"="aggregated", "aggregates" list + "traces" list

    The SDK sends aggregated by default (v2+). Raw is backward-compatible.
    """
    batch_id: str = Field(..., min_length=8, max_length=64)
    agent_version: Optional[str] = Field(None, max_length=32)
    sdk_versions: Optional[dict] = None

    # Raw format (v1 — backward compatible)
    records: Optional[list[UsageRecordIn]] = None

    # Aggregated format (v2 — SDK-side aggregation)
    format: Optional[str] = None  # "aggregated" | None
    aggregates: Optional[list[AggregateIn]] = None
    traces: Optional[list[UsageRecordIn]] = None
    # True (SDK >= this release): every traced call is also inside
    # ``aggregates``, so traces are detail only and never counted again.
    # Absent/False (older SDKs): policy-violation and error traces were NOT
    # folded into the aggregates, so those traces are counted on their own.
    traces_counted_in_aggregates: Optional[bool] = None

    @field_validator("records")
    @classmethod
    def check_batch_size(cls, v: Optional[list]) -> Optional[list]:
        if v is not None and len(v) > settings.max_batch_size:
            raise ValueError(
                f"Batch exceeds maximum size of {settings.max_batch_size} records. "
                f"Split into smaller batches."
            )
        return v


class IngestResponse(BaseModel):
    accepted: int
    rejected: int
    duplicate: bool = False
    batch_id: str


class HeartbeatPayload(BaseModel):
    agent_version: Optional[str] = Field(None, max_length=32)
    instrumented_providers: Optional[list[str]] = None
    host_info: Optional[dict] = None


class RoutingFingerprintSync(BaseModel):
    """Lightweight fingerprint entry sent to SDK via heartbeat response."""
    fingerprint_hash: str
    phase: str
    routing_confidence: float = 0.0
    conformal_threshold: Optional[float] = None
    cheap_model: Optional[str] = None
    expensive_model: Optional[str] = None
    force_model: Optional[str] = None
    allow_routing: bool = True
    max_misroute_rate: float = 0.01
    input_token_bucket_bounds: Optional[list] = None


class HeartbeatResponse(BaseModel):
    status: str = "ok"
    server_time: datetime
    routing_fingerprints: list[RoutingFingerprintSync] = []


# ── Key verification ───────────────────────────────────────────────────────────
#
# Two auth paths:
#
#   mst_...  Session token (issued by self-register)
#            Verified by HMAC decode — <0.1ms, zero DB reads.
#            This is the path every self-registered agent uses.
#
#   mds_...  Stable key (issued by master-key registration)
#            Verified by bcrypt with an in-process LRU cache.
#            Cache hit: <0.1ms. Cache miss: ~250ms (bcrypt), then cached.
#            Used by manually-registered apps and the legacy path.

import asyncio as _asyncio

# LRU cache for stable mds_ key verification.
# Key: (raw_key_hash_hex,) — we hash the raw key before caching to avoid
# storing plaintext keys in memory. SHA-256 of the key is safe to cache.
# Value: App.id (string UUID)
# Capacity: 1024 entries (covers ~1000 registered apps with room for churn)
# Eviction: LRU — apps that haven't had traffic recently are evicted first.
# TTL: no explicit TTL — entries are invalidated on key rotation via cache clear.
# Security: a bcrypt-verified entry means the key was valid at verify time.
#           If a key is rotated, the old cache entry will return an app that
#           no longer accepts that key — but _verify_stable_key re-fetches the
#           app from DB by UUID and re-checks prefix, so stale entries self-heal.

_KEY_CACHE: dict[str, str] = {}   # raw_key_sha256_hex -> app UUID
_KEY_CACHE_MAX = 1024
_KEY_CACHE_LOCK = threading.Lock()

import hashlib as _hashlib


def _cache_key(raw_key: str) -> str:
    return _hashlib.sha256(raw_key.encode()).hexdigest()


def invalidate_key_cache(app_uuid: Optional[str] = None) -> None:
    """
    Invalidate key cache entries. Call after key rotation.
    If app_uuid is given, evict only that app's entry.
    Otherwise clear the entire cache (e.g. after a bulk deactivation).
    """
    if app_uuid is None:
        with _KEY_CACHE_LOCK:
            _KEY_CACHE.clear()
        with _APP_CACHE_LOCK:
            _APP_CACHE.clear()
        return
    with _KEY_CACHE_LOCK:
        stale = [k for k, v in _KEY_CACHE.items() if v == app_uuid]
        for k in stale:
            _KEY_CACHE.pop(k, None)
    with _APP_CACHE_LOCK:
        _APP_CACHE.pop(app_uuid, None)


# ── App snapshot cache for session token & stable key paths ─────────────────────
#
# We cache a lightweight snapshot of the App (plain object, not ORM-bound)
# to avoid DetachedInstanceError when using NullPool (each session gets its own
# connection, so ORM objects from one session can't be read in another).

import time as _time


@dataclass
class _AppSnapshot:
    """Lightweight, session-independent copy of an App row for caching."""
    id: str
    app_id: str
    team_id: str
    is_active: bool
    deleted_at: object
    api_key_prefix: str
    api_key_hash: str
    environment: str
    enforcement_state: str


_APP_CACHE_TTL = 300  # 5 minutes
_APP_CACHE_MAX = 2048
_APP_CACHE: dict[str, tuple[_AppSnapshot, float]] = {}
_APP_CACHE_LOCK = threading.Lock()


def _snapshot_from_app(app: "App") -> _AppSnapshot:
    return _AppSnapshot(
        id=str(app.id),
        app_id=str(app.app_id) if app.app_id else "",
        team_id=str(app.team_id),
        is_active=app.is_active,
        deleted_at=app.deleted_at,
        api_key_prefix=app.api_key_prefix or "",
        api_key_hash=app.api_key_hash or "",
        environment=getattr(app, "environment", ""),
        enforcement_state=getattr(app, "enforcement_state", "active"),
    )


def _app_cache_get(app_uuid: str) -> Optional[_AppSnapshot]:
    with _APP_CACHE_LOCK:
        entry = _APP_CACHE.get(app_uuid)
        if entry is None:
            return None
        snap, expiry = entry
        if _time.monotonic() > expiry:
            _APP_CACHE.pop(app_uuid, None)
            return None
        return snap


def _app_cache_set(app: "App") -> None:
    snap = _snapshot_from_app(app)
    _snap_cache_set(snap)


def _snap_cache_set(snap: _AppSnapshot) -> None:
    with _APP_CACHE_LOCK:
        if len(_APP_CACHE) >= _APP_CACHE_MAX:
            cutoff = _time.monotonic()
            expired = [k for k, (_, exp) in _APP_CACHE.items() if exp < cutoff]
            if expired:
                for k in expired:
                    _APP_CACHE.pop(k, None)
            else:
                keys = list(_APP_CACHE.keys())
                for k in keys[: len(keys) // 4]:
                    _APP_CACHE.pop(k, None)
        _APP_CACHE[snap.id] = (snap, _time.monotonic() + _APP_CACHE_TTL)


def pre_cache_registration(
    app_uuid: str,
    app_id: str,
    team_id: str,
    environment: str,
    api_key: str,
    api_key_prefix: str,
    api_key_hash: str,
) -> None:
    """
    Pre-populate the ingest key/app caches at registration time.
    This bridges the gap between registration (returns key immediately)
    and the write queue flushing the App row to the database.
    Without this, workers using the key before the DB flush get 401s.
    """
    snap = _AppSnapshot(
        id=app_uuid,
        app_id=app_id,
        team_id=team_id,
        is_active=True,
        deleted_at=None,
        api_key_prefix=api_key_prefix,
        api_key_hash=api_key_hash,
        environment=environment,
        enforcement_state="active",
    )
    _snap_cache_set(snap)

    # Also populate the key cache so _verify_stable_key hits cache immediately
    ck = _cache_key(api_key)
    with _KEY_CACHE_LOCK:
        if len(_KEY_CACHE) >= _KEY_CACHE_MAX:
            oldest = next(iter(_KEY_CACHE))
            _KEY_CACHE.pop(oldest, None)
        _KEY_CACHE[ck] = app_uuid


async def _verify_session_token(raw_token: str, db: AsyncSession):
    """
    Verify a mst_ session token.
    Hot path: HMAC verify (<0.1ms) + cache lookup (<0.01ms).
    Cold path (first call or after 5m TTL): HMAC verify + one DB read, then cached.
    Returns an _AppSnapshot (from cache) or App ORM object (from DB).
    """
    from orchestrator.core.session_token import verify, SessionTokenError
    try:
        app_uuid, _team_id = verify(raw_token)
    except SessionTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid session token: {exc}",
        )

    # Try cache first — returns _AppSnapshot, fully detached from any session
    snap = _app_cache_get(app_uuid)
    if snap is not None:
        return snap

    # Cache miss — fetch from DB (first call for this app, or after TTL expiry)
    app = await db.get(App, app_uuid)
    if app is None or not app.is_active or app.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="App not found or inactive.",
        )
    _app_cache_set(app)
    return _snapshot_from_app(app)


async def _verify_stable_key(raw_key: str, db: AsyncSession):
    """
    Verify a mds_ stable key.
    Checks in-process cache first; falls back to bcrypt on miss.
    Returns an _AppSnapshot (from cache) or creates one from the DB App.
    """
    ck = _cache_key(raw_key)
    cached_uuid = _KEY_CACHE.get(ck)

    if cached_uuid:
        # Try snapshot cache first (no DB hit)
        snap = _app_cache_get(cached_uuid)
        if snap is not None and snap.is_active and snap.deleted_at is None and snap.api_key_prefix == raw_key[:16]:
            return snap
        # Try DB if no snapshot cached
        if snap is None:
            app = await db.get(App, cached_uuid)
            if app and app.is_active and app.deleted_at is None and app.api_key_prefix == raw_key[:16]:
                _app_cache_set(app)
                return _snapshot_from_app(app)
        # Cache entry stale — evict and fall through to bcrypt
        _KEY_CACHE.pop(ck, None)

    # Prefix lookup (indexed — fast)
    prefix = raw_key[:16]
    result = await db.execute(
        select(App).where(
            App.api_key_prefix == prefix,
            App.is_active == True,
            App.deleted_at.is_(None),
        )
    )
    app = result.scalar_one_or_none()
    if app is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key.")

    # Bcrypt in thread pool — blocks ~250ms, keeps event loop free
    key_bytes = raw_key.encode()
    hash_bytes = app.api_key_hash.encode()
    valid = await _asyncio.get_running_loop().run_in_executor(
        None, bcrypt.checkpw, key_bytes, hash_bytes
    )
    if not valid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key.")

    # Populate caches
    with _KEY_CACHE_LOCK:
        if len(_KEY_CACHE) >= _KEY_CACHE_MAX:
            oldest = next(iter(_KEY_CACHE))
            _KEY_CACHE.pop(oldest, None)
        _KEY_CACHE[ck] = str(app.id)
    _app_cache_set(app)

    return _snapshot_from_app(app)


async def _verify_app_key(raw_key: str, db: AsyncSession):
    """
    Dispatch to the correct verification path based on token prefix.
    mst_ → fast HMAC session token path (self-registered agents)
    mds_ → cached bcrypt stable key path (master-key registered apps)
    """
    from orchestrator.core.session_token import SESSION_TOKEN_PREFIX
    if raw_key.startswith(SESSION_TOKEN_PREFIX):
        return await _verify_session_token(raw_key, db)
    if raw_key.startswith(settings.api_key_prefix):
        return await _verify_stable_key(raw_key, db)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid key format. Expected mst_ session token or mds_ API key.",
    )


# ── Ingest endpoint ────────────────────────────────────────────────────────────

@router.post(
    "/ingest",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest usage records from an agent",
)
async def ingest_records(
    payload: IngestPayload,
    request: Request,
    raw_key: str = Depends(get_app_identity),
    db: AsyncSession = Depends(get_session),
) -> IngestResponse:
    start = time.perf_counter()

    # ── Backpressure check — reject early if write queue is saturated ─────
    from orchestrator.core.write_queue import queue_over_pressure
    if queue_over_pressure():
        INGEST_REQUESTS_TOTAL.labels(status="backpressure").inc()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server is under heavy load. Retry shortly.",
            headers={"Retry-After": "5"},
        )

    try:
        app = await _verify_app_key(raw_key, db)
    except HTTPException:
        INGEST_REQUESTS_TOTAL.labels(status="rejected").inc()
        raise

    # ── Route to the correct ingest path based on payload format ─────────
    if payload.format == "aggregated":
        result = await _ingest_aggregated(payload, app)
    else:
        result = await _ingest_raw(payload, app)

    duration = time.perf_counter() - start
    INGEST_DURATION_SECONDS.observe(duration)
    INGEST_REQUESTS_TOTAL.labels(status="success").inc()

    return result


def _record_dict(rec: UsageRecordIn, app, batch_id: str) -> dict:
    """Validated usage record -> usage_records row values."""
    return dict(
        app_id=str(app.id),
        team_id=str(app.team_id),
        provider=rec.provider,
        resource_type=rec.resource_type,
        model=rec.model,
        operation=rec.operation,
        input_tokens=rec.input_tokens,
        output_tokens=rec.output_tokens,
        total_tokens=rec.total_tokens,
        input_cost=rec.input_cost,
        output_cost=rec.output_cost,
        total_cost=rec.total_cost,
        duration_ms=rec.duration_ms,
        timestamp=rec.timestamp,
        metadata_=rec.metadata,
        session_id=_session_id_of(rec.metadata),
        batch_id=batch_id,
    )


def _observe_usage(provider: str, resource_type: str, calls: int, cost, in_tok, out_tok) -> None:
    """Prometheus counters (in-memory, no DB)."""
    INGEST_RECORDS_TOTAL.labels(provider=provider, resource_type=resource_type).inc(calls)
    if cost:
        TOTAL_COST_INGESTED.labels(provider=provider).inc(float(cost))
    if in_tok:
        TOTAL_TOKENS_INGESTED.labels(provider=provider, token_type="input").inc(in_tok)
    if out_tok:
        TOTAL_TOKENS_INGESTED.labels(provider=provider, token_type="output").inc(out_tok)


async def _enqueue_or_503(item) -> None:
    """Hand the batch to the writer; never answer 202 for a dropped batch."""
    from orchestrator.core.write_queue import enqueue
    # enqueue() returns False when the queue is full and the item was dropped.
    if await enqueue(item) is False:
        INGEST_REQUESTS_TOTAL.labels(status="backpressure").inc()
        logger.error(
            "Ingest batch not queued (write queue full)",
            extra={"batch_id": getattr(item, "batch_id", None)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server is under heavy load. Retry shortly.",
            headers={"Retry-After": "5"},
        )


async def _ingest_raw(payload: IngestPayload, app) -> IngestResponse:
    """Raw ingest path: one usage record per call. Every record is counted."""
    from orchestrator.core.write_queue import IngestItem

    records = payload.records or []
    INGEST_BATCH_SIZE.observe(len(records))

    record_dicts = [_record_dict(rec, app, payload.batch_id) for rec in records]
    batch_cost = sum((rec.total_cost or Decimal("0") for rec in records), Decimal("0"))
    for rec in records:
        _observe_usage(rec.provider, rec.resource_type, 1, rec.total_cost,
                       rec.input_tokens, rec.output_tokens)

    if record_dicts:
        await _enqueue_or_503(IngestItem(
            app_id=str(app.id),
            team_id=str(app.team_id),
            batch_id=payload.batch_id,
            records=record_dicts,
            record_count=len(record_dicts),
            agent_version=payload.agent_version,
            sdk_versions=payload.sdk_versions,
            total_cost=batch_cost,
            total_input_tokens=sum(r.input_tokens or 0 for r in records),
            total_output_tokens=sum(r.output_tokens or 0 for r in records),
            total_duration_ms=sum(r.duration_ms or 0 for r in records),
            count_records=True,
            source="ingest",
        ))

    return IngestResponse(
        accepted=len(record_dicts),
        rejected=0,
        duplicate=False,
        batch_id=payload.batch_id,
    )


async def _ingest_aggregated(payload: IngestPayload, app) -> IngestResponse:
    """
    Aggregated ingest path: SDK-side pre-aggregated summaries + traces.

    This is the billion-call scale path. Instead of N individual records,
    the SDK sends ~tens of aggregate summaries per flush window.

    Aggregates -> counted once into the hourly + daily aggregate rows of the
                  summary's UTC hour (no raw records are created for them).
    Traces     -> stored as usage_records for detail (sessions, attribution,
                  violations, sampled calls). They are NOT counted again: the
                  calls they describe are already inside the aggregates. Only
                  for older SDKs (no ``traces_counted_in_aggregates``) are
                  policy-violation / error traces counted, because those SDKs
                  left them out of the aggregates.

    Everything is one write-queue item under one batch-id claim, so a retried
    flush is skipped as a whole and never double counts.
    """
    from orchestrator.core.usage_rollup import hour_floor, increment_from_record
    from orchestrator.core.write_queue import IngestItem

    aggregates = payload.aggregates or []
    traces = payload.traces or []
    legacy_traces = not payload.traces_counted_in_aggregates

    increments: list[dict] = []
    accepted = 0
    batch_cost = Decimal("0")
    batch_in = batch_out = batch_dur = 0

    for agg in aggregates:
        hour_start = hour_floor(agg.window_start)
        if hour_floor(agg.window_end) != hour_start:
            # Current SDKs bucket per UTC hour; an older SDK's bucket can
            # straddle an hour boundary and is booked to its starting hour.
            logger.info(
                "Aggregate window spans more than one UTC hour; booked to %s",
                hour_start.isoformat(), extra={"batch_id": payload.batch_id},
            )
        increments.append({
            "app_id": str(app.id),
            "team_id": str(app.team_id),
            "provider": agg.provider,
            "model": agg.model,
            "resource_type": agg.resource_type,
            "hour_start": hour_start,
            "call_count": agg.call_count,
            "input_tokens": agg.input_tokens,
            "output_tokens": agg.output_tokens,
            "total_tokens": agg.total_tokens,
            "input_cost": agg.input_cost,
            "output_cost": agg.output_cost,
            "total_cost": agg.total_cost,
            "duration_ms_sum": agg.duration_ms_sum,
            "min_duration_ms": agg.duration_ms_min,
            "max_duration_ms": agg.duration_ms_max,
        })
        accepted += agg.call_count
        batch_cost += agg.total_cost
        batch_in += agg.input_tokens
        batch_out += agg.output_tokens
        batch_dur += agg.duration_ms_sum
        _observe_usage(agg.provider, agg.resource_type, agg.call_count, agg.total_cost,
                       agg.input_tokens, agg.output_tokens)

    trace_dicts = []
    for rec in traces:
        row = _record_dict(rec, app, payload.batch_id)
        trace_dicts.append(row)
        meta = rec.metadata or {}
        if legacy_traces and (meta.get("_policy_violation") or meta.get("_error")):
            increments.append(increment_from_record(row))
            accepted += 1
            batch_cost += rec.total_cost or Decimal("0")
            batch_in += rec.input_tokens or 0
            batch_out += rec.output_tokens or 0
            batch_dur += rec.duration_ms or 0
            _observe_usage(rec.provider, rec.resource_type, 1, rec.total_cost,
                           rec.input_tokens, rec.output_tokens)

    INGEST_BATCH_SIZE.observe(len(aggregates) + len(traces))

    if increments or trace_dicts:
        await _enqueue_or_503(IngestItem(
            app_id=str(app.id),
            team_id=str(app.team_id),
            batch_id=payload.batch_id,
            records=trace_dicts,
            record_count=len(trace_dicts),
            agent_version=payload.agent_version,
            sdk_versions=payload.sdk_versions,
            total_cost=batch_cost,
            total_input_tokens=batch_in,
            total_output_tokens=batch_out,
            total_duration_ms=batch_dur,
            usage=increments,
            count_records=False,
            source="sdk",
        ))

    return IngestResponse(
        accepted=accepted,
        rejected=0,
        duplicate=False,
        batch_id=payload.batch_id,
    )


# ── Heartbeat endpoint ─────────────────────────────────────────────────────────

@router.post(
    "/heartbeat",
    response_model=HeartbeatResponse,
    status_code=status.HTTP_200_OK,
    summary="Agent liveness signal",
)
async def heartbeat(
    payload: HeartbeatPayload,
    raw_key: str = Depends(get_app_identity),
    db: AsyncSession = Depends(get_session),
) -> HeartbeatResponse:
    app = await _verify_app_key(raw_key, db)

    now = datetime.now(timezone.utc)

    # Push DB writes (heartbeat row + app metadata update) to the write queue
    from orchestrator.core.write_queue import enqueue, HeartbeatItem
    await enqueue(HeartbeatItem(
        app_id=str(app.id),
        team_id=str(app.team_id),
        agent_version=payload.agent_version,
        instrumented_providers=payload.instrumented_providers,
        host_info=payload.host_info,
        timestamp=now,
    ))

    # Attach routing fingerprints for this app (if routing engine is enabled)
    routing_fps: list[RoutingFingerprintSync] = []
    try:
        from orchestrator.core.config import settings as _settings
        if _settings.routing_enabled:
            from orchestrator.db.models import RoutingFingerprint
            fp_stmt = select(RoutingFingerprint).where(
                RoutingFingerprint.app_id == str(app.id),
                RoutingFingerprint.phase.in_(["routing", "observe", "drift_flagged", "excluded"]),
            )
            fp_result = await db.execute(fp_stmt)
            for fp in fp_result.scalars().all():
                routing_fps.append(RoutingFingerprintSync(
                    fingerprint_hash=fp.fingerprint_hash,
                    phase=fp.phase,
                    routing_confidence=fp.routing_confidence,
                    conformal_threshold=fp.conformal_threshold,
                    cheap_model=fp.cheap_model,
                    expensive_model=fp.expensive_model,
                    force_model=fp.force_model,
                    allow_routing=fp.allow_routing,
                    max_misroute_rate=fp.max_misroute_rate,
                    input_token_bucket_bounds=fp.input_token_bucket_bounds,
                ))
    except Exception as exc:
        logger.debug("Failed to load routing fingerprints for heartbeat: %s", exc)

    return HeartbeatResponse(server_time=now, routing_fingerprints=routing_fps)
