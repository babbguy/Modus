# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0
"""Shared pieces of the release gate: check results, HTTP client, JWT.

Standard library only.
"""
from __future__ import annotations

import base64
import collections
import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

Q8 = Decimal("0.00000001")


def log(msg: str) -> None:
    print(f"[gate {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Results ───────────────────────────────────────────────────────────────────

@dataclass
class Check:
    phase: str
    name: str
    ok: bool
    expected: str = ""
    observed: str = ""


@dataclass
class Results:
    checks: list[Check] = field(default_factory=list)
    metrics: dict[str, str] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, phase: str, name: str, ok: bool, expected: Any = "", observed: Any = "") -> bool:
        with self._lock:
            self.checks.append(Check(phase, name, bool(ok), _short(expected), _short(observed)))
        if not ok:
            log(f"FAIL [{phase}] {name}: expected {_short(expected)!r}, observed {_short(observed)!r}")
        return bool(ok)

    def equal(self, phase: str, name: str, expected: Any, observed: Any) -> bool:
        return self.add(phase, name, _same(expected, observed), expected, observed)

    def decimal_equal(self, phase: str, name: str, expected: Decimal, observed: Any) -> bool:
        """Exact comparison at 8 decimal places (NUMERIC(18,8))."""
        ok = False
        try:
            ok = Decimal(str(observed)).quantize(Q8) == Decimal(expected).quantize(Q8)
        except (InvalidOperation, TypeError, ValueError):
            ok = False
        return self.add(phase, name, ok, Decimal(expected).quantize(Q8), observed)

    def error(self, phase: str, name: str, exc: BaseException) -> None:
        self.add(phase, name, False, "no exception", f"{type(exc).__name__}: {exc}")

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


def _same(expected: Any, observed: Any) -> bool:
    if isinstance(expected, Decimal):
        try:
            return Decimal(str(observed)) == expected
        except (InvalidOperation, TypeError, ValueError):
            return False
    return expected == observed


def _short(v: Any, n: int = 160) -> str:
    s = v if isinstance(v, str) else repr(v) if not isinstance(v, (int, Decimal)) else str(v)
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 3] + "..."


# ── HTTP ──────────────────────────────────────────────────────────────────────

class HTTPResult:
    def __init__(self, status: int, body: Any, headers: dict[str, str], elapsed: float):
        self.status = status
        self.body = body
        self.headers = headers
        self.elapsed = elapsed

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def __repr__(self) -> str:
        return f"HTTP {self.status} {_short(self.body, 200)}"


class Pacer:
    """Client-side sliding-window pacing.

    The orchestrator's default rate-limit class allows 200 requests per minute
    (+50 burst) per key or per client IP. The gate's admin traffic stays under
    that documented default instead of raising the limit, so a 429 seen by the
    gate is a real finding, not self-inflicted.
    """

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._times: collections.deque[float] = collections.deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._times and now - self._times[0] > 60:
                    self._times.popleft()
                if len(self._times) < self.per_minute:
                    self._times.append(now)
                    return
                delay = 60 - (now - self._times[0]) + 0.05
            time.sleep(max(delay, 0.05))


class Client:
    """Tiny JSON HTTP client that records every status it sees."""

    def __init__(self, base: str, headers: Optional[dict[str, str]] = None,
                 pacer: Optional[Pacer] = None, timeout: float = 30.0):
        self.base = base.rstrip("/")
        self.headers = dict(headers or {})
        self.pacer = pacer
        self.timeout = timeout
        self.statuses: collections.Counter[int] = collections.Counter()
        self.server_errors: list[str] = []
        self.rate_limited: list[str] = []
        self._lock = threading.Lock()

    def request(self, method: str, path: str, body: Any = None,
                headers: Optional[dict[str, str]] = None, timeout: Optional[float] = None,
                paced: bool = True) -> HTTPResult:
        if paced and self.pacer is not None:
            self.pacer.wait()
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Accept": "application/json", **self.headers, **(headers or {})}
        if data is not None:
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                status, raw, rh = r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            status, raw, rh = e.code, e.read(), dict(e.headers or {})
        elapsed = time.perf_counter() - t0
        text = raw.decode("utf-8", "replace")
        try:
            parsed: Any = json.loads(text) if text.strip() else None
        except ValueError:
            parsed = text
        with self._lock:
            self.statuses[status] += 1
            if status >= 500:
                self.server_errors.append(f"{status} {method} {path}: {_short(text, 300)}")
            if status == 429:
                self.rate_limited.append(f"{method} {path}")
        return HTTPResult(status, parsed, {k.lower(): v for k, v in rh.items()}, elapsed)

    def get(self, path: str, **kw: Any) -> HTTPResult:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> HTTPResult:
        return self.request("POST", path, body if body is not None else {}, **kw)

    def put(self, path: str, body: Any = None, **kw: Any) -> HTTPResult:
        return self.request("PUT", path, body, **kw)

    def patch(self, path: str, body: Any = None, **kw: Any) -> HTTPResult:
        return self.request("PATCH", path, body, **kw)

    def ok_json(self, method: str, path: str, body: Any = None, **kw: Any) -> Any:
        """Request that must succeed; raises GateError otherwise."""
        r = self.request(method, path, body, **kw)
        if not r.ok:
            raise GateError(f"{method} {path} -> {r}")
        return r.body


class GateError(Exception):
    pass


# ── JWT (HS256, stdlib) ───────────────────────────────────────────────────────

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(secret: str, sub: str, role: str = "platform_admin",
             audience: str = "modus", ttl_s: int = 6 * 3600) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    claims = {"sub": sub, "aud": audience, "iat": now, "exp": now + ttl_s, "cs_role": role}
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    sig = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(sig)}"


def wait_until(fn, timeout_s: float, interval_s: float = 1.0) -> Any:
    """Poll fn() until it returns a truthy value or the timeout passes."""
    deadline = time.monotonic() + timeout_s
    last = None
    while True:
        last = fn()
        if last:
            return last
        if time.monotonic() >= deadline:
            return last
        time.sleep(interval_s)
