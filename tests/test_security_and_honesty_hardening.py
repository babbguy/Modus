"""
Security & honesty quick batch.

Each test here is the *proof criterion* for a specific fix:
1. Notification delivery survives a process restart (DB fallback, no UI needed).
2. GET /notifications/config never returns secrets; sentinel round-trip keeps them.
3. /evaluate authorization: foreign-team access → 403, never a fail-open allow.
4. Assistant provider calls do not block the event loop.
5. PQC status reports the runtime algorithm, never the config aspiration.
6. Bounded prover results are "sampled", with a truthful statement.
"""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException


# ── 1. Notification restart resilience ────────────────────────────────────────

async def test_notifications_delivered_after_restart_without_ui_touch(monkeypatch):
    """After a restart (empty cache), _send_notifications loads config from
    the DB itself — alert delivery must not depend on someone opening the
    dashboard config page."""
    from orchestrator.api import notifications as notif_mod
    from orchestrator.core import threshold_evaluator as te

    # Simulate freshly-restarted process: cache empty and not loaded
    monkeypatch.setattr(notif_mod, "_config_cache", {})
    monkeypatch.setattr(notif_mod, "_cache_loaded", False)

    stored_config = {
        "slack": {"enabled": True, "webhook_url": "https://hooks.slack.example/T/x",
                  "channel": None, "min_severity": "warning"},
    }

    # DB session whose query returns the stored config row
    row = MagicMock()
    row.config_json = json.dumps(stored_config)
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    session = AsyncMock()
    session.execute.return_value = result

    class _FakeFactory:
        def __call__(self):
            return self
        async def __aenter__(self):
            return session
        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(te, "_session_factory", _FakeFactory())

    sent = []

    async def fake_slack(cfg, alert_data):
        sent.append(("slack", alert_data["severity"]))

    monkeypatch.setattr(te, "_notify_slack", fake_slack)

    await te._send_notifications({"severity": "critical", "alert_id": "a1",
                                  "threshold_name": "t", "metric": "cost",
                                  "actual_value": "10", "threshold_value": "5"})

    assert sent == [("slack", "critical")], (
        "Alert was not delivered from a cold cache — restart resilience broken"
    )


# ── 2. Secret masking ─────────────────────────────────────────────────────────

async def test_notification_secrets_never_returned(client, monkeypatch):
    from orchestrator.api import notifications as notif_mod

    monkeypatch.setattr(notif_mod, "_config_cache", {})
    monkeypatch.setattr(notif_mod, "_cache_loaded", False)

    config = {
        "email": {"enabled": True, "smtp_host": "mail.example.com",
                  "smtp_password": "hunter2-real-password",
                  "recipients": ["ops@example.com"]},
        "pagerduty": {"enabled": True, "integration_key": "pd-key-123456"},
    }
    put = await client.put("/api/v1/notifications/config", json=config)
    assert put.status_code == 200
    body = put.json()
    assert body["email"]["smtp_password"] == "********"
    assert body["pagerduty"]["integration_key"] == "********"

    got = await client.get("/api/v1/notifications/config")
    assert got.status_code == 200
    body = got.json()
    assert body["email"]["smtp_password"] == "********"
    assert body["pagerduty"]["integration_key"] == "********"
    assert "hunter2" not in got.text and "pd-key-123456" not in got.text

    # Round-trip: PUT the masked body back — stored secrets must be preserved
    put2 = await client.put("/api/v1/notifications/config", json=body)
    assert put2.status_code == 200
    assert notif_mod._config_cache["email"]["smtp_password"] == "hunter2-real-password"
    assert notif_mod._config_cache["pagerduty"]["integration_key"] == "pd-key-123456"


# ── 3. Evaluate authorization ─────────────────────────────────────────────────

async def test_evaluate_foreign_team_gets_403_not_fail_open(monkeypatch):
    """An authz failure inside evaluation must surface as 403 — the blanket
    fail-open error handler must never convert it into an allow."""
    from orchestrator.api import evaluate as ev

    async def raise_403(req, identity, db):
        raise HTTPException(status_code=403, detail="Access to team 'x' is not permitted.")

    monkeypatch.setattr(ev, "_evaluate_inner", raise_403)

    req = MagicMock()
    req.app_id, req.provider, req.model = "app-1", "openai", "gpt-4o"
    req.input_tokens, req.output_tokens = 10, 10

    with pytest.raises(HTTPException) as exc_info:
        await ev.evaluate_request(req, identity=MagicMock(), db=AsyncMock())
    assert exc_info.value.status_code == 403


def test_evaluate_inner_asserts_team_access_before_spend_disclosure():
    """The team-access assertion must exist in the evaluation path."""
    import inspect
    from orchestrator.api import evaluate as ev

    src = inspect.getsource(ev._evaluate_inner)
    assert "assert_team_access" in src, "IDOR guard missing from _evaluate_inner"
    # The guard must run before the spend-hierarchy queries
    assert src.index("assert_team_access") < src.index("RealTimeSpend"), (
        "team-access check must precede spend reads"
    )


# ── 4. Assistant event-loop liveness ─────────────────────────────────────────

async def test_assistant_does_not_block_event_loop(monkeypatch):
    from orchestrator.api import assistant as asst

    def slow_blocking_provider(api_key, model, system, message):
        time.sleep(0.5)  # deliberately blocking, like urllib
        return "answer"

    monkeypatch.setitem(asst._PROVIDERS, "anthropic", slow_blocking_provider)

    cfg = MagicMock()
    cfg.assistant_enabled = True
    cfg.assistant_provider = "anthropic"
    cfg.assistant_api_key = "k"
    cfg.assistant_model = "m"
    monkeypatch.setattr(asst, "get_settings", lambda: cfg)

    req = MagicMock()
    req.context_view, req.context_data, req.message = "overview", {}, "hi"

    chat_task = asyncio.create_task(asst.assistant_chat(req, identity=MagicMock()))

    # While the provider call sleeps in its worker thread, the loop must
    # still run other coroutines promptly.
    loop_alive_at = None
    t0 = time.perf_counter()
    await asyncio.sleep(0.05)
    loop_alive_at = time.perf_counter() - t0

    resp = await chat_task
    assert resp.response == "answer"
    assert loop_alive_at < 0.3, (
        f"Event loop was blocked for {loop_alive_at:.2f}s during the provider call"
    )


# ── 5. PQC status honesty ─────────────────────────────────────────────────────

def test_pqc_resolve_algorithm_reports_runtime_truth():
    from orchestrator.core import pqc_signer as pq

    if pq._HAS_PQCRYPTO:
        assert pq.resolve_algorithm("real") == "ml-dsa-65"
        assert pq.resolve_algorithm("auto") == "ml-dsa-65"
    else:
        # tier=real without pqcrypto must NEVER be reported as ml-dsa-65
        assert pq.resolve_algorithm("real") == "unavailable:pqcrypto-not-installed"
        assert pq.resolve_algorithm("auto") == pq._ALGO_SHIM
    assert pq.resolve_algorithm("simulated") == pq._ALGO_SHIM
    assert pq.resolve_algorithm("tier1") == pq._ALGO_SHIM


def test_pqc_signer_accepts_config_vocabulary():
    from orchestrator.core import pqc_signer as pq

    signer = pq.PQCAttestationSigner(attestation_key="k" * 32, tier="simulated")
    assert signer.algorithm == pq._ALGO_SHIM


@pytest.mark.skipif(
    __import__("orchestrator.core.pqc_signer", fromlist=["_HAS_PQCRYPTO"])._HAS_PQCRYPTO,
    reason="pqcrypto installed — loud-failure path not reachable",
)
def test_pqc_real_tier_fails_loudly_without_pqcrypto():
    from orchestrator.core import pqc_signer as pq

    with pytest.raises(RuntimeError, match="pqcrypto"):
        pq.PQCAttestationSigner(attestation_key="k" * 32, tier="real")


# ── 6. Prover honesty ─────────────────────────────────────────────────────────

def test_bounded_result_is_sampled_with_truthful_statement():
    from orchestrator.core.policy_prover import bounded_prove, generate_proof_certificate

    policy = json.dumps({
        "type": "budget_cap",
        "name": "cap",
        "effect": "deny",
        "config": {"cap_usd": "100", "period": "daily"},
    })
    result = bounded_prove(policy, policy_id="p1")
    assert result.status == "sampled"
    assert "not a universal proof" in result.statement
    assert "All" not in result.statement.split(".")[0], (
        "statement must not claim all points passed when cap violations were sampled"
    )

    cert = generate_proof_certificate(result)
    assert cert["verified"] is False
    assert cert["sampled_only"] is True


def test_prover_parses_json_without_yaml():
    """JSON policy input must parse even if PyYAML were absent (Law 1)."""
    from orchestrator.core import policy_prover as pp

    with patch.object(pp, "_yaml", None):
        policies = pp._parse_policies(json.dumps({"type": "budget_cap", "config": {}}))
        assert len(policies) == 1
        assert policies[0]["type"] == "budget_cap"
