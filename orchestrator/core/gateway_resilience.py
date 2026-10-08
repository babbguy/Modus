"""
Modus — Gateway Resilience
=============================
Retry-with-backoff and a per-provider circuit breaker for the LLM proxy.

The gateway is positioned as a CISO chokepoint in front of production LLM
traffic, so a single upstream brownout must not take the whole path down. This
module provides:

  - a lightweight in-process circuit breaker per provider: after N consecutive
    upstream failures the circuit opens for a cooldown, failing fast instead of
    piling connections against a dead upstream;
  - bounded retries with exponential backoff for transient errors
    (ConnectError / TimeoutException), never for a normal HTTP response.

Stdlib + httpx only (httpx is already the gateway's client). Non-blocking:
backoff uses asyncio.sleep.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable, Optional

import httpx

logger = logging.getLogger(__name__)


class CircuitOpenError(Exception):
    """Raised when a provider's circuit is open (failing fast)."""

    def __init__(self, provider: str, retry_after_s: float) -> None:
        super().__init__(f"circuit open for provider {provider!r}")
        self.provider = provider
        self.retry_after_s = retry_after_s


class _Circuit:
    __slots__ = ("failures", "opened_at")

    def __init__(self) -> None:
        self.failures = 0
        self.opened_at: Optional[float] = None


class CircuitBreaker:
    """Per-provider circuit breaker.

    A monotonic clock is injected so the breaker is deterministically testable
    without wall-clock sleeps.
    """

    def __init__(
        self,
        *,
        fail_threshold: int = 5,
        cooldown_s: float = 30.0,
        clock: Callable[[], float] = None,
    ) -> None:
        self._fail_threshold = fail_threshold
        self._cooldown_s = cooldown_s
        self._clock = clock or _default_clock
        self._circuits: dict[str, _Circuit] = {}

    def _c(self, provider: str) -> _Circuit:
        return self._circuits.setdefault(provider, _Circuit())

    def reset(self) -> None:
        """Clear all circuit state (used for test isolation)."""
        self._circuits.clear()

    def is_open(self, provider: str) -> bool:
        c = self._c(provider)
        if c.opened_at is None:
            return False
        if self._clock() - c.opened_at >= self._cooldown_s:
            # Cooldown elapsed → half-open: allow a trial request.
            c.opened_at = None
            c.failures = 0
            return False
        return True

    def record_success(self, provider: str) -> None:
        c = self._c(provider)
        c.failures = 0
        c.opened_at = None
        _set_state_metric(provider, False)

    def record_failure(self, provider: str) -> None:
        c = self._c(provider)
        c.failures += 1
        if c.failures >= self._fail_threshold and c.opened_at is None:
            c.opened_at = self._clock()
            logger.warning(
                "Gateway circuit OPEN for provider %s after %d consecutive "
                "failures; failing fast for %.0fs",
                provider, c.failures, self._cooldown_s,
            )
            _set_state_metric(provider, True)

    def cooldown_remaining(self, provider: str) -> float:
        c = self._c(provider)
        if c.opened_at is None:
            return 0.0
        return max(0.0, self._cooldown_s - (self._clock() - c.opened_at))


def _default_clock() -> float:
    import time
    return time.monotonic()


def _set_state_metric(provider: str, is_open: bool) -> None:
    try:
        from orchestrator.metrics.prometheus import GATEWAY_CIRCUIT_STATE
        GATEWAY_CIRCUIT_STATE.labels(provider=provider).set(1 if is_open else 0)
    except Exception:
        pass  # metrics optional


# Module-level breaker shared by the gateway proxy handlers.
_breaker = CircuitBreaker()


def get_breaker() -> CircuitBreaker:
    return _breaker


async def call_with_resilience(
    provider: str,
    do_request: Callable[[], Awaitable[httpx.Response]],
    *,
    max_retries: int = 2,
    base_backoff_s: float = 0.2,
    breaker: Optional[CircuitBreaker] = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> httpx.Response:
    """Execute an upstream request with circuit breaking + bounded retries.

    Retries ONLY transient transport errors (ConnectError, TimeoutException),
    never a returned HTTP response (a 500 from the provider is the provider's
    answer, not a Modus transport failure). Raises CircuitOpenError when the
    provider circuit is open.
    """
    b = breaker or _breaker
    if b.is_open(provider):
        raise CircuitOpenError(provider, b.cooldown_remaining(provider))

    attempt = 0
    while True:
        try:
            resp = await do_request()
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            b.record_failure(provider)
            if attempt >= max_retries or b.is_open(provider):
                raise
            backoff = base_backoff_s * (2 ** attempt)
            logger.debug(
                "Gateway upstream %s transient error (%s); retry %d/%d in %.2fs",
                provider, type(exc).__name__, attempt + 1, max_retries, backoff,
            )
            await sleep(backoff)
            attempt += 1
            continue
        else:
            b.record_success(provider)
            return resp


@asynccontextmanager
async def stream_with_resilience(
    provider: str,
    open_stream: Callable[[], "httpx._client.StreamContextManager"],
    *,
    max_retries: int = 2,
    base_backoff_s: float = 0.2,
    breaker: Optional[CircuitBreaker] = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncIterator[httpx.Response]:
    """Open an upstream *streaming* connection with circuit breaking + bounded
    retries on the CONNECTION-ESTABLISHMENT phase only.

    ``open_stream`` is a zero-arg callable returning httpx's streaming context
    manager, e.g. ``lambda: client.stream("POST", url, headers=h, content=b)``.

    Retry is applied ONLY while establishing the connection (before the response
    is handed back). Once we yield the response, bytes may already be flowing to
    the client, so no retry is possible — this mirrors the non-streaming path's
    "never retry a returned response" rule. Establishment success/failure feeds
    the same per-provider circuit breaker as ``call_with_resilience`` so a dead
    upstream trips the breaker for both streaming and non-streaming traffic.

    Raises CircuitOpenError when the provider circuit is already open, and
    re-raises ConnectError/TimeoutException if establishment fails after retries.
    """
    b = breaker or _breaker
    if b.is_open(provider):
        raise CircuitOpenError(provider, b.cooldown_remaining(provider))

    attempt = 0
    while True:
        cm = open_stream()
        try:
            resp = await cm.__aenter__()
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            b.record_failure(provider)
            if attempt >= max_retries or b.is_open(provider):
                raise
            backoff = base_backoff_s * (2 ** attempt)
            logger.debug(
                "Gateway upstream %s stream connect error (%s); retry %d/%d in %.2fs",
                provider, type(exc).__name__, attempt + 1, max_retries, backoff,
            )
            await sleep(backoff)
            attempt += 1
            continue
        else:
            # Connection established (response headers received). Record success
            # and hand the live response to the caller. From here we cannot retry.
            b.record_success(provider)
            try:
                yield resp
            except BaseException as exc:  # noqa: BLE001 — forward to httpx close
                if not await cm.__aexit__(type(exc), exc, exc.__traceback__):
                    raise
            else:
                await cm.__aexit__(None, None, None)
            return
