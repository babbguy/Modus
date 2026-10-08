# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0
"""
Regression tests for the alerting / notification / connection / enforcement bugs.

  1. SDK policy sync hits POST /api/v1/policies/sync and enforces what it syncs.
  2. Alert rows are COMMITTED before any delivery starts; notification_sent and
     NotificationDelivery rows reflect the real outcome.
  3. The delivery result shape written == the shape the Notifications view reads.
  4. Connections test the REAL stored secret URL (never the masked one), expose
     only masked endpoints, and produce a "degraded" state.
  5. A degradation ladder never bypasses deny/throttle from other policies.
  6. The Alerts "New Rule" form payload is accepted by the thresholds API.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.api import notifications as notif_mod
from orchestrator.core import connection_checker as cc
from orchestrator.core import threshold_evaluator as te
from orchestrator.core.policy_engine import EvaluateRequest, evaluate
from orchestrator.db.models import (
    Alert, App, GovernancePolicy, NotificationDelivery, RealTimeSpend, Team, Threshold,
    UsageAggregate,
)

REPO = Path(__file__).resolve().parent.parent


# ── 1. SDK policy sync + local enforcement ────────────────────────────────────

def _agent(**kw):
    from modus.agent import ModusAgent
    defaults = dict(
        orchestrator_url="http://localhost:9999", team_token="t", app_id="a",
        fail_open=False, flush_interval=999999, auto_optimize=False,
        response_cache_enabled=False, aggregation_enabled=False,
    )
    defaults.update(kw)
    agent = ModusAgent(**defaults)
    agent._api_key = "mds_testkey"
    agent._started = True
    return agent


def test_sdk_sync_calls_sync_endpoint():
    agent = _agent()
    seen = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"policies": [{"name": "p"}], "app_id": "x"}).encode()

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        return _Resp()

    with patch("modus.agent._urlopen_tls", side_effect=fake_urlopen):
        agent._sync_policies()
    assert seen["url"] == "http://localhost:9999/api/v1/policies/sync"
    assert seen["method"] == "POST"
    assert agent._local_policies == [{"name": "p"}]


def test_sdk_sync_failure_keeps_previous_policies():
    agent = _agent()
    agent._local_policies = [{"name": "keep"}]
    with patch("modus.agent._urlopen_tls", side_effect=ConnectionError("down")):
        agent._sync_policies()
    assert agent._local_policies == [{"name": "keep"}]


def test_sdk_enforces_synced_denylist_locally_without_gateway():
    from modus.agent import PolicyViolationError
    agent = _agent()
    agent._local_policies = [{
        "id": "p1", "name": "no-opus", "policy_type": "model_denylist", "effect": "deny",
        "scope": "platform", "priority": 1, "config": {"models": ["opus"]},
        "conditions": None, "action": {"message": "nope"},
    }]
    with patch.object(agent, "_call_evaluate", side_effect=AssertionError("gateway must not be hit")):
        with pytest.raises(PolicyViolationError) as ei:
            agent.enforce(provider="anthropic", model="opus")
    assert ei.value.policy_name == "no-opus"


def test_sdk_local_policy_honours_conditions():
    agent = _agent(environment="production")
    agent._local_policies = [{
        "name": "dev-only-deny", "policy_type": "model_denylist", "effect": "deny",
        "scope": "app", "priority": 1, "config": {"models": ["m"]},
        "conditions": {"environments": ["development"]},
    }]
    assert agent._evaluate_local("openai", "m", None, None) is None
    agent._local_policies[0]["conditions"] = {"providers": ["openai"], "model_pattern": "m*"}
    assert agent._evaluate_local("openai", "m", None, None)["decision"] == "deny"
    assert agent._evaluate_local("anthropic", "m", None, None) is None


def test_sdk_local_warn_policy_does_not_block_or_skip_gateway():
    agent = _agent()
    agent._local_policies = [{
        "name": "warn-only", "policy_type": "model_denylist", "effect": "warn",
        "scope": "app", "priority": 1, "config": {"models": ["m"]},
    }]
    assert agent._evaluate_local("openai", "m", None, None) is None
    with patch.object(agent, "_call_evaluate", return_value={"decision": "allow"}) as ce:
        agent.enforce(provider="openai", model="m")
    ce.assert_called_once()


# ── shared DB helpers ─────────────────────────────────────────────────────────

async def _team_app(db: AsyncSession, slug="t1", app_id="app1"):
    team = Team(slug=slug, name=slug)
    db.add(team)
    await db.flush()
    app = App(team_id=str(team.id), app_id=app_id, app_name=app_id,
              api_key_hash="x", api_key_prefix="mds_x", environment="production")
    db.add(app)
    await db.flush()
    return team, app


def _policy(team, app, **kw):
    base = dict(
        team_id=None, app_id=None, name="p", scope="platform", policy_type="model_denylist",
        effect="deny", priority=100, conditions=None, config={"models": []}, action=None,
        is_active=True, created_by="test",
    )
    base.update(kw)
    return GovernancePolicy(**base)


# ── 5. Degradation ladder must not short-circuit other policies ───────────────

async def _spend(db, app, team, cost):
    from orchestrator.core.policy_engine import _window_key
    now = datetime.now(timezone.utc)
    for period in ("hourly", "daily", "monthly"):
        wkey, ws, we = _window_key(period, now)
        db.add(RealTimeSpend(
            app_id=str(app.id), team_id=str(team.id), period=period, window_key=wkey,
            window_start=ws, window_end=we, total_cost=Decimal(cost), call_count=5,
            input_tokens=10, output_tokens=10, total_duration_ms=10,
        ))
    await db.flush()


LADDER_CFG = {"budget_usd": "100", "period": "monthly",
              "tiers": [{"pct": 70, "model": "cheap-model"}, {"pct": 100, "action": "deny"}]}


async def _req(app, team, model):
    return EvaluateRequest(app_id=str(app.id), team_id=str(team.id), provider="openai",
                           model=model, environment="production")


async def test_ladder_at_80pct_still_applies_platform_denylist(db_session):
    team, app = await _team_app(db_session)
    await _spend(db_session, app, team, "80")
    db_session.add(_policy(team, app, name="ladder", scope="app", app_id=str(app.id),
                           team_id=str(team.id), policy_type="degradation_ladder",
                           effect="deny", priority=1, config=LADDER_CFG))
    db_session.add(_policy(team, app, name="deny-big", scope="platform",
                           config={"models": ["big-model"]}))
    await db_session.flush()
    res = await evaluate(await _req(app, team, "big-model"), db_session)
    assert res.decision == "deny", res
    assert res.policy_name == "deny-big"


async def test_ladder_at_80pct_still_applies_budget_cap_and_rate_limit(db_session):
    team, app = await _team_app(db_session)
    await _spend(db_session, app, team, "80")
    db_session.add(_policy(team, app, name="ladder", scope="app", app_id=str(app.id),
                           team_id=str(team.id), policy_type="degradation_ladder",
                           priority=1, config=LADDER_CFG))
    db_session.add(_policy(team, app, name="cap", scope="platform", policy_type="budget_cap",
                           config={"cap_usd": "50", "period": "monthly"}))
    await db_session.flush()
    res = await evaluate(await _req(app, team, "any"), db_session)
    assert res.decision == "deny" and res.policy_name == "cap"

    db_session.add(_policy(team, app, name="rl", scope="platform", policy_type="rate_limit",
                           effect="throttle", priority=0, config={"max_calls": 1, "window_seconds": 3600}))
    await db_session.flush()
    res = await evaluate(await _req(app, team, "any"), db_session)
    # deny (budget cap) is more restrictive than throttle (rate limit)
    assert res.decision == "deny" and res.policy_name == "cap"


async def test_ladder_alone_downshifts_but_allows(db_session):
    team, app = await _team_app(db_session)
    await _spend(db_session, app, team, "80")
    db_session.add(_policy(team, app, name="ladder", scope="app", app_id=str(app.id),
                           team_id=str(team.id), policy_type="degradation_ladder",
                           priority=1, config=LADDER_CFG))
    await db_session.flush()
    res = await evaluate(await _req(app, team, "big-model"), db_session)
    assert res.decision == "allow"
    assert res.suggested_model == "cheap-model"


async def test_throttle_applies_when_ladder_downshifts(db_session):
    team, app = await _team_app(db_session)
    await _spend(db_session, app, team, "80")
    db_session.add(_policy(team, app, name="ladder", scope="app", app_id=str(app.id),
                           team_id=str(team.id), policy_type="degradation_ladder",
                           priority=1, config=LADDER_CFG))
    db_session.add(_policy(team, app, name="rl", scope="platform", policy_type="rate_limit",
                           effect="throttle", config={"max_calls": 1, "window_seconds": 3600}))
    await db_session.flush()
    res = await evaluate(await _req(app, team, "x"), db_session)
    assert res.decision == "throttle" and res.policy_name == "rl"


async def test_warn_policy_never_blocks_or_hides_ladder(db_session):
    team, app = await _team_app(db_session)
    await _spend(db_session, app, team, "80")
    db_session.add(_policy(team, app, name="warn", scope="platform", effect="warn",
                           config={"models": ["big-model"]}, priority=1))
    db_session.add(_policy(team, app, name="ladder", scope="app", app_id=str(app.id),
                           team_id=str(team.id), policy_type="degradation_ladder",
                           priority=2, config=LADDER_CFG))
    await db_session.flush()
    res = await evaluate(await _req(app, team, "big-model"), db_session)
    assert res.decision == "allow" and res.suggested_model == "cheap-model"


# ── 2/3. Alert delivery after commit; result shape ────────────────────────────

async def _seed_threshold(factory, critical="10"):
    async with factory() as db:
        team, app = await _team_app(db, slug=f"t-{uuid.uuid4().hex[:6]}", app_id=f"a-{uuid.uuid4().hex[:6]}")
        thr = Threshold(team_id=str(team.id), name="cost", metric="total_cost", period="daily",
                        scope="team", critical_value=Decimal(critical), is_active=True)
        db.add(thr)
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        db.add(UsageAggregate(
            app_id=str(app.id), team_id=str(team.id), provider="openai", model="m",
            resource_type="chat", granularity="daily", period_start=start,
            period_end=start + timedelta(days=1), call_count=1, total_tokens=2,
            input_tokens=1, output_tokens=1, total_cost=Decimal("12"),
        ))
        await db.commit()
        return str(thr.id)


@pytest.fixture
def notif_cfg(engine, monkeypatch):
    # threshold_evaluator binds the session factory at import time; point it at
    # the per-test engine.
    monkeypatch.setattr(te, "_session_factory",
                        async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False))
    prev = (notif_mod._config_cache, notif_mod._cache_loaded)
    notif_mod._config_cache = {"webhook": {"enabled": True, "url": "http://hook.invalid/x",
                                           "min_severity": "warning"}}
    notif_mod._cache_loaded = True
    te._fired_tiers.clear()
    yield
    notif_mod._config_cache, notif_mod._cache_loaded = prev
    te._fired_tiers.clear()


async def _drain():
    while te._delivery_tasks:
        await asyncio.gather(*list(te._delivery_tasks), return_exceptions=True)


async def test_delivery_runs_only_after_alert_is_committed(client, engine, notif_cfg):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await _seed_threshold(factory)
    observed = []

    async def fake_post(url, *, json, headers=None, **kw):
        # A *different* session can only see the alert if it was committed.
        async with factory() as db:
            ids = (await db.execute(select(Alert.id))).scalars().all()
        observed.append((json.get("alert_id"), set(map(str, ids))))

    with patch.object(te, "_http_post_retry", side_effect=fake_post):
        fired = await te.evaluate_thresholds()
        await _drain()

    assert fired >= 2
    assert observed, "webhook must have been delivered"
    for alert_id, visible in observed:
        assert alert_id and alert_id != "None"
        assert alert_id in visible, "delivery raced ahead of the alert commit"


async def test_successful_delivery_marks_alert_and_records_rows(client, engine, notif_cfg):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await _seed_threshold(factory)

    async def ok_post(*a, **k):
        return None

    with patch.object(te, "_http_post_retry", side_effect=ok_post):
        await te.evaluate_thresholds()
        await _drain()

    async with factory() as db:
        alerts = (await db.execute(select(Alert))).scalars().all()
        deliveries = (await db.execute(select(NotificationDelivery))).scalars().all()
    delivered_alerts = [a for a in alerts if a.notification_sent]
    assert delivered_alerts, "at least the >= warning alerts must be marked sent"
    for a in delivered_alerts:
        assert a.notification_result["webhook"] == {"status": "delivered", "success": True, "error": None}
    assert deliveries and all(d.status == "delivered" for d in deliveries)
    assert {d.alert_id for d in deliveries} == {a.id for a in delivered_alerts}


async def test_failed_delivery_is_dead_lettered_not_marked_sent(client, engine, notif_cfg):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await _seed_threshold(factory)

    async def bad_post(*a, **k):
        raise RuntimeError("receiver down")

    with patch.object(te, "_http_post_retry", side_effect=bad_post):
        await te.evaluate_thresholds()
        await _drain()

    async with factory() as db:
        alerts = (await db.execute(select(Alert))).scalars().all()
        deliveries = (await db.execute(select(NotificationDelivery))).scalars().all()
    attempted = [a for a in alerts if a.notification_result]
    assert attempted and all(a.notification_sent is False for a in attempted)
    for a in attempted:
        r = a.notification_result["webhook"]
        assert r["success"] is False and r["status"] == "dead_letter" and "receiver down" in r["error"]
    assert deliveries and all(d.status == "dead_letter" and d.payload for d in deliveries)


def test_channel_result_shape_matches_dashboard_reader():
    ok = te.channel_result(te.DELIVERY_DELIVERED)
    bad = te.channel_result(te.DELIVERY_DEAD_LETTER, "boom")
    assert ok["success"] is True and bad["success"] is False and bad["error"] == "boom"
    js = (REPO / "dashboard/js/views/notifications.js").read_text(encoding="utf-8")
    # the reader keys off exactly the fields the writer sets
    assert "r.success === true" in js and "r.status === 'delivered'" in js
    assert te.DELIVERY_DELIVERED == "delivered"


async def test_unsupported_threshold_scopes_do_not_fire_team_wide(client, engine, notif_cfg):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        team, app = await _team_app(db, slug="us", app_id="us-app")
        db.add(Threshold(team_id=str(team.id), name="u", metric="total_cost", period="daily",
                         scope="user", critical_value=Decimal("1"), is_active=True))
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        db.add(UsageAggregate(
            app_id=str(app.id), team_id=str(team.id), provider="openai", model="m",
            resource_type="chat", granularity="daily", period_start=start,
            period_end=start + timedelta(days=1), call_count=1, total_tokens=2,
            input_tokens=1, output_tokens=1, total_cost=Decimal("50"),
        ))
        await db.commit()
    assert await te.evaluate_thresholds() == 0


# ── 4. Connections ────────────────────────────────────────────────────────────

class _Hook(BaseHTTPRequestHandler):
    delay = 0.0
    status = 200
    hits: list = []

    def _do(self):
        _Hook.hits.append((self.command, self.path))
        if _Hook.delay:
            time.sleep(_Hook.delay)
        self.send_response(_Hook.status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    do_GET = do_POST = _do

    def log_message(self, *a):
        pass


@pytest.fixture
def hook_server():
    _Hook.delay, _Hook.status, _Hook.hits = 0.0, 200, []
    srv = HTTPServer(("127.0.0.1", 0), _Hook)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def conn_cfg(client, hook_server):
    port = hook_server.server_address[1]
    secret_path = "/services/T000/B000/SECRETSECRETSECRET"
    prev = (notif_mod._config_cache, notif_mod._cache_loaded)
    notif_mod._config_cache = {
        "slack": {"enabled": True, "webhook_url": f"http://127.0.0.1:{port}{secret_path}/slack"},
        "teams": {"enabled": True, "webhook_url": f"http://127.0.0.1:{port}{secret_path}/teams"},
        "webhook": {"enabled": True, "url": f"http://127.0.0.1:{port}{secret_path}/hook"},
    }
    notif_mod._cache_loaded = True
    cc.clear_cache()
    yield secret_path
    notif_mod._config_cache, notif_mod._cache_loaded = prev
    cc.clear_cache()


async def test_connection_test_uses_real_url_and_ui_sees_only_masked(conn_cfg, hook_server):
    secret = conn_cfg
    for cid, suffix in (("notify_slack", "/slack"), ("notify_teams", "/teams"), ("notify_custom", "/hook")):
        _Hook.hits.clear()
        res = await cc.test_connection(cid)
        assert res["status"] == "connected", res
        assert (_Hook.hits and _Hook.hits[-1][1] == secret + suffix), _Hook.hits
        safe = cc.sanitize_connection(res)
        blob = json.dumps(safe)
        assert "SECRETSECRET" not in blob and "/services/" not in blob
        assert safe["endpoint"].endswith("***")
        assert "latency_ms" in safe["metadata"]


async def test_list_connections_never_leaks_secret_path(conn_cfg):
    conns = await cc.gather_all_connections()
    blob = json.dumps([cc.sanitize_connection(c) for c in conns])
    assert "SECRETSECRET" not in blob


async def test_connection_error_when_receiver_stopped(conn_cfg, hook_server):
    hook_server.shutdown()
    hook_server.server_close()
    res = await cc.test_connection("notify_slack")
    assert res["status"] == "error" and res["last_error"]


async def test_connection_degraded_when_slow(conn_cfg, monkeypatch):
    monkeypatch.setattr(cc, "_DEGRADED_LATENCY_MS", 150)
    _Hook.delay = 0.4
    res = await cc.test_connection("notify_slack")
    assert res["status"] == "degraded"
    assert "Slow response" in res["last_error"]
    assert res["metadata"]["latency_ms"] >= 150


async def test_connection_degraded_on_5xx_and_on_flapping(conn_cfg):
    _Hook.status = 503
    res = await cc.test_connection("notify_teams")
    assert res["status"] == "degraded" and "503" in res["last_error"]

    _Hook.status = 200
    cc.clear_cache()
    # two failures then a success -> degraded (elevated error rate)
    cc._record_outcome("notify_custom", False)
    cc._record_outcome("notify_custom", False)
    res = await cc.test_connection("notify_custom")
    assert res["status"] == "degraded" and "Elevated error rate" in res["last_error"]
    # one more clean check later it is healthy again once failures roll off
    for _ in range(cc._HISTORY_LEN):
        cc._record_outcome("notify_custom", True)
    res = await cc.test_connection("notify_custom")
    assert res["status"] == "connected"


def test_classify_ok_thresholds():
    assert cc._classify_ok(10, 200, [])[0] == "connected"
    assert cc._classify_ok(cc._DEGRADED_LATENCY_MS, 200, [])[0] == "degraded"
    assert cc._classify_ok(10, 500, [])[0] == "degraded"
    assert cc._classify_ok(10, 404, [True, True])[0] == "connected"
    assert cc._classify_ok(10, 200, [False, True, False, True])[0] == "degraded"


async def test_connections_api_summary_counts_degraded(client, conn_cfg, monkeypatch):
    monkeypatch.setattr(cc, "_DEGRADED_LATENCY_MS", 100)
    _Hook.delay = 0.3
    resp = await client.post("/api/v1/admin/connections/notify_slack/test")
    assert resp.status_code == 200 and resp.json()["status"] == "degraded"
    listing = (await client.get("/api/v1/admin/connections")).json()
    assert listing["summary"]["degraded"] >= 1
    assert "SECRETSECRET" not in json.dumps(listing)


# ── notification test endpoints load config from DB after restart ─────────────

async def test_notification_test_endpoint_loads_config_when_cache_cold(client, engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    from orchestrator.db.models import NotificationChannelConfig
    async with factory() as db:
        db.add(NotificationChannelConfig(scope="global", config_json=json.dumps(
            {"webhook": {"enabled": True, "url": "http://127.0.0.1:9/never"}})))
        await db.commit()
    prev = (notif_mod._config_cache, notif_mod._cache_loaded)
    notif_mod._config_cache, notif_mod._cache_loaded = {}, False
    try:
        resp = await client.post("/api/v1/notifications/test")
        assert resp.status_code == 200
        channels = [r["channel"] for r in resp.json()["results"]]
        assert channels == ["webhook"], channels  # not the "none configured" placeholder
    finally:
        notif_mod._config_cache, notif_mod._cache_loaded = prev


# ── 6. Alerts form <-> thresholds API ─────────────────────────────────────────

async def _mk_team_app_via_db(db_session):
    team, app = await _team_app(db_session, slug="ui-team", app_id="ui-app")
    await db_session.commit()
    return str(team.id), str(app.id)


async def test_new_rule_form_payload_create_edit_delete(client, db_session):
    team_id, app_id = await _mk_team_app_via_db(db_session)
    # exactly what dashboard/js/views/alerts.js _buildThresholdPayload sends
    payload = {"name": "Daily cost", "team_id": team_id, "scope": "app", "metric": "total_cost",
               "period": "daily", "critical_value": "10", "warning_value": "5", "app_id": app_id}
    r = await client.post("/api/v1/thresholds", json=payload)
    assert r.status_code == 201, r.text
    tid = r.json()["id"]

    r = await client.patch(f"/api/v1/thresholds/{tid}",
                           json={"name": "Renamed", "warning_value": None, "critical_value": "20", "is_active": True})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Renamed" and Decimal(r.json()["critical_value"]) == 20

    r = await client.delete(f"/api/v1/thresholds/{tid}")
    assert r.status_code == 204
    listed = (await client.get("/api/v1/thresholds")).json()
    assert tid not in [t["id"] for t in listed]


async def test_threshold_api_rejects_inconsistent_rules(client, db_session):
    team_id, app_id = await _mk_team_app_via_db(db_session)
    base = {"name": "n", "team_id": team_id, "metric": "total_cost", "period": "daily", "critical_value": "10"}
    cases = [
        ({"scope": "app"}, "app_id"),                              # app scope w/o app
        ({"scope": "provider"}, "provider"),                       # provider scope w/o provider
        ({"scope": "team", "critical_value": "0"}, "greater than 0"),
        ({"scope": "team", "warning_value": "10"}, "lower than critical"),
    ]
    for extra, needle in cases:
        r = await client.post("/api/v1/thresholds", json={**base, **extra})
        assert r.status_code == 422, (extra, r.text)
        assert needle in r.text, (extra, r.text)
    other_team = Team(slug="other", name="other")
    db_session.add(other_team)
    await db_session.commit()
    r = await client.post("/api/v1/thresholds", json={**base, "scope": "app", "app_id": app_id,
                                                      "team_id": str(other_team.id)})
    assert r.status_code == 422 and "does not belong" in r.text
    r = await client.post("/api/v1/thresholds", json={**base, "scope": "team"})
    t = r.json()["id"]
    r = await client.patch(f"/api/v1/thresholds/{t}", json={"warning_value": "11"})
    assert r.status_code == 422


def test_alerts_form_options_are_accepted_by_the_api():
    js = (REPO / "dashboard/js/views/alerts.js").read_text(encoding="utf-8")
    from orchestrator.api.thresholds import ThresholdCreate
    def pattern(name):
        for m in ThresholdCreate.model_fields[name].metadata:
            if hasattr(m, "pattern"):
                return m.pattern
    def options(select_id):
        block = re.search(rf'id="{select_id}".*?</select>', js, re.S).group(0)
        return re.findall(r'<option value="([^"]+)"', block)
    for sel, field in (("thr-scope", "scope"), ("thr-metric", "metric"), ("thr-period", "period")):
        opts = options(sel)
        assert opts, sel
        for o in opts:
            assert re.match(pattern(field), o), f"{sel} option {o!r} rejected by API pattern"
    assert "team_id" in js and "formatApiError" in js
