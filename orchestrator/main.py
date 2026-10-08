"""
Modus Orchestrator v1.0.0
==================================
Production-grade FastAPI application.

Architecture:
    - Async SQLAlchemy 2.x + PostgreSQL (replaces in-memory store)
    - Alembic migrations (run at deploy time)
    - Auth middleware with Option A stub / Option B JWT hook
    - Prometheus metrics on /metrics
    - Structured JSON logging
    - Background tasks: aggregation, heartbeat pruning, batch ID pruning
    - Full /api/v1/ contract — additive changes only, breaking changes to /api/v2/

Startup sequence:
    1. Validate configuration (pydantic-settings raises on missing required vars)
    2. Configure logging
    3. Initialise database connection pool
    4. Run pending Alembic migrations (dev only — prod runs migrations in CI)
    5. Start background tasks
    6. Register routers
    7. Set Prometheus app info

Shutdown sequence:
    1. Signal background tasks to stop
    2. Wait for in-flight tasks to complete
    3. Close database connection pool

Modules:
    - orchestrator.core.tasks     — background task loops
    - orchestrator.core.lifespan  — FastAPI lifespan (startup / shutdown)
    - orchestrator.api.routes     — router registration
"""

from __future__ import annotations

import logging
import time
import threading as _threading

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from orchestrator.core.config import settings
from orchestrator.core.lifespan import lifespan
from orchestrator.api.routes import register_routers
from orchestrator.metrics.prometheus import (
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_TOTAL,
)

logger = logging.getLogger(__name__)


# ── Rate limiting ──────────────────────────────────────────────────────────────


class _RateLimiter:
    """Thread-safe per-key rate limiter using fixed-window counters.

    Uses O(1) memory per key (two counters + window timestamp) instead of
    storing every individual request timestamp. Approximates a sliding window
    by interpolating between the previous and current fixed windows.
    """

    def __init__(self, limit: int = 200, burst: int = 50, window: float = 60.0, cleanup_interval: float = 300.0):
        self._limit = limit
        self._burst = burst
        self._window = window
        self._cleanup_interval = cleanup_interval
        # key -> (window_start, prev_count, curr_count)
        self._counters: dict[str, tuple[float, int, int]] = {}
        self._lock = _threading.Lock()
        self._last_cleanup = 0.0

    def _get_window_start(self, now: float) -> float:
        return now - (now % self._window)

    def check(self, key: str) -> bool:
        if self._limit <= 0:
            return True  # class limit disabled (configured as 0)
        import time as _time
        now = _time.monotonic()
        with self._lock:
            # Periodic cleanup of stale keys to prevent memory leak
            if now - self._last_cleanup > self._cleanup_interval:
                self._cleanup(now)
                self._last_cleanup = now

            window_start = self._get_window_start(now)
            entry = self._counters.get(key)

            if entry is None:
                # First request for this key
                self._counters[key] = (window_start, 0, 1)
                return True

            entry_window, prev_count, curr_count = entry

            if window_start > entry_window + self._window:
                # Two or more windows have passed — reset completely
                self._counters[key] = (window_start, 0, 1)
                return True

            if window_start > entry_window:
                # Moved to next window — rotate counters
                prev_count = curr_count
                curr_count = 0
                entry_window = window_start

            # Sliding window approximation: weight previous window by
            # the fraction of it that overlaps with our sliding range
            elapsed_in_window = now - window_start
            prev_weight = max(0.0, 1.0 - elapsed_in_window / self._window)
            estimated = prev_weight * prev_count + curr_count

            effective_limit = self._limit + self._burst
            if estimated >= effective_limit:
                return False

            curr_count += 1
            self._counters[key] = (entry_window, prev_count, curr_count)
            return True

    def retry_after_seconds(self) -> int:
        """Seconds until the current window rolls over (Retry-After hint)."""
        import math
        import time as _time
        now = _time.monotonic()
        return max(1, math.ceil(self._window - (now % self._window)))

    def _cleanup(self, now: float) -> None:
        """Remove keys with no recent hits. Called under lock."""
        cutoff = now - 2 * self._window
        stale = [k for k, (ws, _, _) in self._counters.items() if ws < cutoff]
        for k in stale:
            del self._counters[k]


_rate_limiter = _RateLimiter(
    limit=settings.rate_limit_per_minute,
    burst=settings.rate_limit_burst,
)
# Machine traffic from registered apps (SDK / OTLP exporters). Separate class
# so normal SDK volume (one evaluate per LLM call + flushes + heartbeats) is
# never throttled by the dashboard-sized default limit.
_sdk_rate_limiter = _RateLimiter(
    limit=settings.rate_limit_sdk_per_minute,
    burst=settings.rate_limit_sdk_burst,
)

# POST endpoints the SDK / exporters call with an app credential.
_SDK_POST_PATHS = frozenset({
    "/api/v1/ingest",
    "/api/v1/heartbeat",
    "/api/v1/policy/evaluate",
    "/api/v1/routing/outcomes/batch",
    "/api/v1/topology",
    "/api/v1/governance/rewind-event",
    "/api/v1/v1/traces",
})
# GET endpoints the SDK polls with an app credential (policy sync).
_SDK_GET_PATHS = frozenset({"/api/v1/policies"})


def _app_credential(request: Request) -> str | None:
    """The app key/session token on a request, if it carries one."""
    from orchestrator.core.session_token import SESSION_TOKEN_PREFIX
    key = request.headers.get("x-modus-apikey")
    if not key:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            key = auth[7:].strip()
    if key and key.startswith((settings.api_key_prefix, SESSION_TOKEN_PREFIX)):
        return key
    return None


def _rate_limit_class(request: Request) -> tuple[str, "_RateLimiter", str]:
    """Return (class name, limiter, bucket key) for an /api request."""
    path = request.url.path.rstrip("/") or "/"
    app_key = _app_credential(request)
    if app_key and (
        (request.method == "POST" and path in _SDK_POST_PATHS)
        or (request.method == "GET" and path in _SDK_GET_PATHS)
    ):
        return "sdk", _sdk_rate_limiter, "sdk:" + app_key
    key = request.headers.get("x-modus-apikey") or (request.client.host if request.client else "?")
    return "default", _rate_limiter, key


# ── Application factory ────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(
        title="Modus Orchestrator",
        description=(
            "AI cost tracking and observability platform. "
            "Receives usage data from instrumented applications, "
            "computes costs, evaluates thresholds, and serves the dashboard."
        ),
        version=settings.version,
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url="/redoc" if settings.environment != "production" else None,
        openapi_url="/openapi.json" if settings.environment != "production" else None,
        lifespan=lifespan,
    )

    # ── Global exception handler ──────────────────────────────────────────────
    @app.exception_handler(Exception)
    async def _global_exception_handler(request, exc):
        logger.error("Unhandled exception on %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    # ── CORS ───────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        allow_headers=["Authorization", "Content-Type", "X-Modus-APIKey", "X-Request-ID"],
    )

    # ── Dev mode: disable browser caching for dashboard assets ─────────────
    if settings.environment != "production":
        @app.middleware("http")
        async def dev_no_cache_middleware(request: Request, call_next) -> Response:
            response = await call_next(request)
            if request.url.path.startswith("/dashboard/") or request.url.path == "/":
                response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                response.headers["Pragma"] = "no-cache"
                response.headers["Expires"] = "0"
            return response

    # ── Security headers middleware ───────────────────────────────────────────
    @app.middleware("http")
    async def security_headers_middleware(request: Request, call_next) -> Response:
        response = await call_next(request)
        if settings.environment == "production":
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        if settings.region_id:
            response.headers["X-Modus-Region"] = settings.region_id
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        # CSP: lock down resource origins to 'self'. The dashboard vendors every
        # asset locally (dashboard/vendor/ for JS/CSS, dashboard/webfonts/ for
        # fonts) to stay air-gap safe — no CDN or external font origins are
        # loaded, so none are allowed. 'unsafe-inline' is retained because the
        # dashboard uses inline scripts/styles in its static HTML.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "font-src 'self'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        return response

    # ── Request metrics middleware ─────────────────────────────────────────────
    @app.middleware("http")
    async def metrics_middleware(request: Request, call_next) -> Response:
        if request.url.path.startswith("/api/") and request.client:
            rate_class, limiter, rate_key = _rate_limit_class(request)
            if not limiter.check(rate_key):
                retry_after = limiter.retry_after_seconds()
                logger.warning(
                    "Rate limit exceeded (class=%s key=%s path=%s) — 429, Retry-After %ss",
                    rate_class, rate_key[:12] + "…", request.url.path, retry_after,
                )
                HTTP_REQUESTS_TOTAL.labels(
                    method=request.method,
                    path=_normalise_path(request.url.path),
                    status_code=429,
                ).inc()
                return JSONResponse(
                    {"detail": "Rate limit exceeded", "rate_limit_class": rate_class},
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                )
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start

        # Normalise path — replace UUIDs with {id} to prevent cardinality explosion
        path = _normalise_path(request.url.path)

        HTTP_REQUESTS_TOTAL.labels(
            method=request.method,
            path=path,
            status_code=response.status_code,
        ).inc()
        HTTP_REQUEST_DURATION_SECONDS.labels(
            method=request.method,
            path=path,
        ).observe(duration)

        return response

    # ── Routers ────────────────────────────────────────────────────────────────
    register_routers(app)

    return app


import re

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def _normalise_path(path: str) -> str:
    """
    Replace UUID segments in paths with {id} placeholder.
    Prevents Prometheus cardinality explosion from per-resource paths.

    /api/v1/apps/3f2504e0-4f89-11d3-9a0c-0305e82c3301 → /api/v1/apps/{id}
    """
    return _UUID_RE.sub("{id}", path)


# ── Module-level app instance ──────────────────────────────────────────────────

app = create_app()


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "orchestrator.main:app",
        host=settings.host,
        port=settings.port,
        workers=settings.workers,
        log_config=None,  # We configure logging ourselves
        access_log=False,  # We log via middleware
        loop="uvloop" if __import__("sys").platform != "win32" else "auto",
    )
