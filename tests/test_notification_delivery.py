"""
Notification delivery guarantees.

  1. Transient failures are retried with backoff, then succeed.
  2. A permanent 4xx is NOT retried (bad webhook/key fails fast).
  3. All retries exhausted -> the error propagates so the channel logs a failure.
  4. PagerDuty uses a stable dedup_key so repeated tier crossings correlate into
     one incident; resolve uses the same key.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from orchestrator.core import threshold_evaluator as te


def _resp(status: int):
    r = MagicMock()
    r.status_code = status
    r.request = MagicMock()
    return r


class _ClientCtx:
    """Async context manager yielding a client whose post() is scripted."""

    def __init__(self, post):
        self._post = post

    async def __aenter__(self):
        client = MagicMock()
        client.post = self._post
        return client

    async def __aexit__(self, *a):
        return False


async def test_retries_transient_then_succeeds(monkeypatch):
    calls = {"n": 0}
    slept = []

    def post(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom")
        return _resp(200)

    monkeypatch.setattr("orchestrator.core.tls.get_httpx_client", lambda **k: _ClientCtx(AsyncMock(side_effect=post)))
    monkeypatch.setattr(te.asyncio, "sleep", AsyncMock(side_effect=lambda s: slept.append(s)))

    await te._http_post_retry("http://x", json={"a": 1})
    assert calls["n"] == 3
    assert slept == [0.5, 1.0]  # exponential backoff


async def test_4xx_not_retried(monkeypatch):
    calls = {"n": 0}

    def post(*a, **k):
        calls["n"] += 1
        return _resp(404)

    monkeypatch.setattr("orchestrator.core.tls.get_httpx_client", lambda **k: _ClientCtx(AsyncMock(side_effect=post)))
    monkeypatch.setattr(te.asyncio, "sleep", AsyncMock())

    with pytest.raises(httpx.HTTPStatusError):
        await te._http_post_retry("http://x", json={"a": 1})
    assert calls["n"] == 1, "a 4xx is permanent and must not be retried"


async def test_exhausted_retries_raise(monkeypatch):
    def post(*a, **k):
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr("orchestrator.core.tls.get_httpx_client", lambda **k: _ClientCtx(AsyncMock(side_effect=post)))
    monkeypatch.setattr(te.asyncio, "sleep", AsyncMock())

    with pytest.raises(httpx.TimeoutException):
        await te._http_post_retry("http://x", json={"a": 1}, retries=3)


def test_pagerduty_dedup_key_stable_and_scoped():
    a1 = {"threshold_id": "t1", "metric": "cost", "severity": "critical"}
    a2 = {"threshold_id": "t1", "metric": "cost", "severity": "warning"}  # later tier
    a3 = {"threshold_id": "t2", "metric": "cost"}

    # Same threshold+metric -> same key (correlates crossings into one incident)
    assert te._pagerduty_dedup_key(a1) == te._pagerduty_dedup_key(a2)
    # Different threshold -> different key
    assert te._pagerduty_dedup_key(a1) != te._pagerduty_dedup_key(a3)
    assert te._pagerduty_dedup_key(a1).startswith("modus-")


async def test_pagerduty_trigger_and_resolve_share_key(monkeypatch):
    sent = []

    async def fake_post(url, *, json, headers=None, **k):
        sent.append(json)

    monkeypatch.setattr(te, "_http_post_retry", fake_post)

    config = MagicMock()
    config.integration_key = "pd-key"
    config.service_name = "svc"
    alert = {"threshold_id": "t9", "metric": "cost", "severity": "critical",
             "threshold_name": "Cap", "actual_value": "100"}

    await te._notify_pagerduty(config, alert)
    await te.resolve_pagerduty(config, alert)

    assert sent[0]["event_action"] == "trigger"
    assert sent[1]["event_action"] == "resolve"
    assert sent[0]["dedup_key"] == sent[1]["dedup_key"], (
        "trigger and resolve must share a dedup_key so PD resolves the incident"
    )


# ── Durability + dead-letter (durability) ──────────────────────────────────────────
# A notification whose bounded retries are exhausted must be persisted to the
# dead-letter table (with its payload, for replay) — never dropped into a log
# line — and the parent alert's delivery state must be back-filled so the
# dashboard shows what actually happened.


async def _make_session_factory(monkeypatch):
    from sqlalchemy.ext.asyncio import (
        AsyncSession, async_sessionmaker, create_async_engine,
    )
    from orchestrator.db.models import Base

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(te, "_session_factory", factory)
    return eng, factory


async def test_persist_deliveries_records_dead_letter_and_delivered(monkeypatch):
    import uuid
    from datetime import datetime, timezone
    from decimal import Decimal
    from sqlalchemy import select
    from orchestrator.db.models import Alert, NotificationDelivery

    eng, factory = await _make_session_factory(monkeypatch)
    try:
        alert_id = str(uuid.uuid4())
        async with factory() as db:
            db.add(Alert(
                id=alert_id, threshold_id=str(uuid.uuid4()),
                team_id=str(uuid.uuid4()), severity="critical", metric="total_cost",
                threshold_value=Decimal("50"), actual_value=Decimal("67"),
                period_start=datetime.now(timezone.utc),
                period_end=datetime.now(timezone.utc),
            ))
            await db.commit()

        alert_data = {"alert_id": alert_id, "severity": "critical",
                      "metric": "total_cost", "actual_value": "67"}
        deliveries = [("slack", "delivered", None),
                      ("webhook", "dead_letter", "HTTP 500")]
        await te._persist_deliveries(alert_data, "critical", deliveries)

        async with factory() as db:
            rows = (await db.execute(select(NotificationDelivery))).scalars().all()
            by_ch = {r.channel: r for r in rows}
            assert by_ch["slack"].status == "delivered"
            assert by_ch["slack"].payload is None
            dl = by_ch["webhook"]
            assert dl.status == "dead_letter"
            assert dl.last_error == "HTTP 500"
            assert dl.payload["alert_id"] == alert_id, "dead-letter must be replayable"

            alert = (await db.execute(
                select(Alert).where(Alert.id == alert_id))).scalar_one()
            assert alert.notification_sent is True  # at least one channel delivered
            assert alert.notification_result["webhook"]["status"] == "dead_letter"
            assert alert.notification_result["slack"]["status"] == "delivered"
    finally:
        await eng.dispose()


async def test_send_notifications_dead_letters_failed_channel(monkeypatch):
    from sqlalchemy import select
    from orchestrator.api import notifications as notif_mod
    from orchestrator.db.models import NotificationDelivery

    eng, factory = await _make_session_factory(monkeypatch)
    try:
        monkeypatch.setattr(notif_mod, "_cache_loaded", True)
        monkeypatch.setattr(notif_mod, "_config_cache", {
            "webhook": {"enabled": True, "url": "http://x",
                        "min_severity": "warning"},
        })

        async def boom(*a, **k):
            raise httpx.ConnectError("down")

        monkeypatch.setattr(te, "_notify_webhook", boom)

        alert_data = {"alert_id": None, "severity": "critical",
                      "metric": "total_cost", "threshold_name": "Cap",
                      "actual_value": "9", "threshold_value": "5",
                      "period": "monthly"}
        await te._send_notifications(alert_data)

        async with factory() as db:
            rows = (await db.execute(select(NotificationDelivery))).scalars().all()
            assert len(rows) == 1
            assert rows[0].channel == "webhook"
            assert rows[0].status == "dead_letter"
            assert rows[0].payload["metric"] == "total_cost", "must be replayable"
    finally:
        await eng.dispose()
