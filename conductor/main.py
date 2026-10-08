"""
Modus Conductor v1.0.0
==============================
Org-level data aggregation service.

Architecture:
    Dashboard  ←  Conductor  ←  Orchestrator(s)  ←  Agent(s)

The Conductor:
  - Receives pushed aggregates from all Orchestrators in the org
  - Validates data completeness and reconciles totals
  - Serves dashboard-compatible API endpoints (same contract as Orchestrator)
  - Uses tiered caching for dashboard performance
  - Federation-ready: same protocol for future multi-region support

Startup sequence:
    1. Configure logging
    2. Initialise database
    3. Start background tasks (reconciliation, maintenance)
    4. Register routers (Conductor API + Dashboard API + static files)

Four Laws:
    - Zero external dependencies (stdlib + SQLAlchemy/FastAPI only)
    - Zero data leaving customer infrastructure
    - Works on a $5/mo VPS
    - Non-blocking (all async)
"""

from __future__ import annotations

import logging
import time
import threading as _threading

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from conductor.core.config import settings
from conductor.core.lifespan import lifespan
from conductor.api.routes import register_routers

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
        now = time.monotonic()
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


# ── Application factory ───────────────────────────────────────────────────────


def create_app() -> FastAPI:
    app = FastAPI(
        title="Modus Conductor",
        description=(
            "Org-level data aggregation service. "
            "Collects, reconciles, and serves clean data from all Orchestrators "
            "across the organisation for the Dashboard."
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

    # ── CORS ──────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        allow_headers=[
            "Authorization", "Content-Type",
            "X-Orchestrator-Instance-ID", "X-Request-ID",
        ],
    )

    # ── Security headers middleware ──────────────────────────────────────────
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
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        return response

    # ── Request metrics + rate limiting middleware ────────────────────────────
    @app.middleware("http")
    async def metrics_middleware(request: Request, call_next) -> Response:
        if request.url.path.startswith("/api/") and request.client:
            rate_key = request.headers.get("x-orchestrator-instance-id") or request.client.host
            if not _rate_limiter.check(rate_key):
                return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start

        # Log slow requests
        if duration > 1.0:
            logger.warning(
                "Slow request: %s %s took %.2fs",
                request.method, request.url.path, duration,
            )
        return response

    # ── Routers ──────────────────────────────────────────────────────────────
    register_routers(app)

    return app


# ── Module-level app instance ─────────────────────────────────────────────────

app = create_app()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "conductor.main:app",
        host=settings.host,
        port=settings.port,
        workers=settings.workers,
        log_config=None,
        access_log=False,
        loop="uvloop" if __import__("sys").platform != "win32" else "auto",
    )
