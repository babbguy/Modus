"""
Tests — Pre-request Evaluation Gateway
Covers: EvaluateRequest/Response schemas, session budget CRUD,
        enforcement disabled path, app not found, endpoint responses.
"""
from __future__ import annotations

# ── Schema tests ─────────────────────────────────────────────────────────────

def test_evaluate_request_defaults():
    from orchestrator.api.evaluate import EvaluateRequest
    req = EvaluateRequest(
        provider="anthropic",
        model="claude-sonnet-4-5-20250514",
        input_tokens=100,
        app_id="my-app",
    )
    assert req.output_tokens == 0
    assert req.session_id is None
    assert req.environment == "production"
    assert req.metadata is None


def test_evaluate_response_schema():
    from orchestrator.api.evaluate import EvaluateResponse
    resp = EvaluateResponse(
        decision="allow",
        reason="Request permitted.",
        estimated_cost_usd="0.001500",
    )
    assert resp.decision == "allow"
    assert resp.retry_after_seconds is None
    assert resp.suggested_model is None


def test_evaluate_response_deny():
    from orchestrator.api.evaluate import EvaluateResponse
    resp = EvaluateResponse(
        decision="deny",
        reason="Budget exceeded.",
        estimated_cost_usd="0.01",
        retry_after_seconds=60,
        suggested_model="claude-haiku-3-5-20241022",
    )
    assert resp.retry_after_seconds == 60
    assert resp.suggested_model == "claude-haiku-3-5-20241022"


def test_session_budget_create_schema():
    from orchestrator.api.evaluate import SessionBudgetCreate
    sbc = SessionBudgetCreate(
        session_id="sess-001",
        app_id="app-001",
        max_budget_usd="10.00",
    )
    assert sbc.session_id == "sess-001"


def test_session_info_schema():
    from orchestrator.api.evaluate import SessionInfo
    si = SessionInfo(
        session_id="sess-001",
        app_id="app-001",
        current_spend_usd="1.50",
        max_budget_usd="10.00",
        percent_used=15.0,
    )
    assert si.percent_used == 15.0


# ── Endpoint tests ───────────────────────────────────────────────────────────

async def test_evaluate_no_app(client):
    """Evaluate with a nonexistent app should get denied or error."""
    resp = await client.post("/api/v1/evaluate/", json={
        "provider": "anthropic",
        "model": "claude-sonnet-4-5-20250514",
        "input_tokens": 100,
        "output_tokens": 50,
        "app_id": "nonexistent-app-xyz",
        "environment": "production",
    })
    assert resp.status_code == 200
    body = resp.json()
    # Should be denied or allowed depending on fail_open + enforcement state
    assert body["decision"] in ("allow", "deny")
    assert "estimated_cost_usd" in body


async def test_evaluate_enforcement_disabled(client, engine):
    """When enforcement is disabled, should return allow."""
    from orchestrator.core.config import settings
    original = settings.enforcement_enabled
    settings.enforcement_enabled = False
    try:
        resp = await client.post("/api/v1/evaluate/", json={
            "provider": "anthropic",
            "model": "claude-sonnet-4-5-20250514",
            "input_tokens": 100,
            "output_tokens": 50,
            "app_id": "nonexistent-app",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["decision"] == "allow"
        assert "Enforcement disabled" in body["reason"]
    finally:
        settings.enforcement_enabled = original
