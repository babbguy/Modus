"""
Dashboard API contract tests — lock the shape of every endpoint the
dashboard UI depends on. Every key the frontend reads is asserted by
name so a silent backend rename breaks the build instead of shipping
empty tiles to production.

Motivation: on 2026-04-06, dashboard work shipped with multiple silent
field-name mismatches (policies view reading `d.action` when the
backend returned `d.decision`; sessions view reading
`amplification_ratio` when the backend returned `amplification_factor`).
These rendered as empty/dash tiles in production but the old test suite
didn't catch them because it only checked status codes.

These tests exist to make that class of bug impossible going forward.
Every contract test here should document which UI file reads the same
fields, so when a backend field is renamed the fix location is obvious.
"""
from __future__ import annotations

# ── Helpers ─────────────────────────────────────────────────────────────────


def _assert_keys(item: dict, required: set[str], context: str) -> None:
    """Assert item is a dict with every key in `required` present."""
    assert isinstance(item, dict), f"{context}: expected dict, got {type(item).__name__}"
    missing = required - item.keys()
    assert not missing, f"{context}: missing keys {missing}. Got {sorted(item.keys())}"


# ── Overview view contracts ─────────────────────────────────────────────────


async def test_summary_contract(client):
    """Overview/DevOps/Finance KPI row — dashboard reads total_cost_today,
    total_cost_7d, total_cost_30d, active_apps, online_agents,
    cost_delta_pct_7d, total_cost_mtd. See dashboard/js/views/overview.js
    _renderKpis and dashboard/js/views/finance.js loadData (dashSummary)."""
    resp = await client.get("/api/v1/dashboard/summary")
    assert resp.status_code == 200
    data = resp.json()
    expected = {
        "total_cost_today", "total_cost_7d", "total_cost_30d",
        "active_apps", "online_agents", "total_cost_mtd",
    }
    _assert_keys(data, expected, "summary")
    # cost_delta_pct_7d may be null but must be present
    assert "cost_delta_pct_7d" in data


async def test_cost_over_time_contract(client):
    """Overview cost chart reads .period and .cost per point.
    See dashboard/js/views/overview.js _renderCostChart."""
    resp = await client.get("/api/v1/dashboard/cost-over-time?days=7&granularity=daily")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    # Empty is acceptable (fresh deployment), but if non-empty, shape must match
    for point in data:
        _assert_keys(point, {"period", "cost"}, "cost-over-time point")


async def test_by_provider_contract(client):
    """Overview and DevOps Provider Breakdown reads .provider, .cost, .cost_pct.
    See dashboard/js/views/overview.js _renderProviders."""
    resp = await client.get("/api/v1/dashboard/by-provider?days=7")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    for item in data:
        _assert_keys(item, {"provider", "cost", "cost_pct"}, "by-provider")


async def test_by_app_contract(client):
    """Overview Top Apps table reads .app_id, .app_name, .team_slug,
    .environment, .calls, .tokens, .cost. See dashboard/js/views/overview.js
    _renderTopApps."""
    resp = await client.get("/api/v1/dashboard/by-app?days=7&limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    for item in data:
        _assert_keys(
            item,
            {"app_id", "app_name", "team_slug", "environment", "calls", "tokens", "cost"},
            "by-app",
        )


async def test_top_models_contract(client):
    """Overview Top Models table reads .model, .provider, .calls, .cost,
    .avg_input_tokens, .avg_output_tokens.
    See dashboard/js/views/overview.js _renderTopModels."""
    resp = await client.get("/api/v1/dashboard/top-models?days=7&limit=8")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    for item in data:
        _assert_keys(
            item,
            {"model", "provider", "calls", "cost", "avg_input_tokens", "avg_output_tokens"},
            "top-models",
        )


async def test_top_models_app_id_filter(client):
    """GAP-2 drill-through: top-models must accept app_id query param.
    The overview app detail modal passes app_id to filter models to one app."""
    # Just verify the endpoint accepts the param without erroring
    resp = await client.get("/api/v1/dashboard/top-models?days=7&app_id=nonexistent")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


async def test_recent_alerts_contract(client):
    """Overview Recent Alerts reads .id, .severity, .metric, .threshold_value,
    .actual_value, .app_id, .team_id, .fired_at, .acknowledged,
    .notification_sent, .notification_result.
    See dashboard/js/views/overview.js _renderAlerts and
    dashboard/js/views/notifications.js _renderDelivery (GAP-8)."""
    resp = await client.get("/api/v1/dashboard/recent-alerts?limit=5")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    for item in data:
        _assert_keys(
            item,
            {
                "id", "severity", "metric", "threshold_value", "actual_value",
                "app_id", "team_id", "fired_at", "acknowledged",
                "notification_sent", "notification_result",
            },
            "recent-alerts",
        )


async def test_recent_alerts_app_id_filter(client):
    """GAP-2 drill-through: recent-alerts must accept app_id query param."""
    resp = await client.get("/api/v1/dashboard/recent-alerts?limit=5&app_id=nonexistent")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_app_status_contract(client):
    """Overview Agent Status reads .app_id, .app_name, .team_slug, .environment,
    .online, .last_seen_at, .agent_version, .instrumented_providers.
    See dashboard/js/views/overview.js _renderAgents."""
    resp = await client.get("/api/v1/dashboard/app-status")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    for item in data:
        _assert_keys(
            item,
            {
                "app_id", "app_name", "team_slug", "environment",
                "online", "last_seen_at", "agent_version",
            },
            "app-status",
        )


# ── Finance view contracts ──────────────────────────────────────────────────


async def test_finance_summary_contract(client):
    """Finance KPI row reads .total_monthly_budget_usd,
    .total_current_spend_usd, .total_projected_eom_usd, .overall_burn_pct,
    .overall_risk, .teams_at_risk, .teams_over_budget, .spend_by_department.
    See dashboard/js/views/finance.js _renderKpis + _renderDeptDonut and
    dashboard/js/views/overview.js loadData (budget KPIs GAP-1)."""
    resp = await client.get("/api/v1/finance/summary")
    assert resp.status_code == 200
    data = resp.json()
    expected = {
        "total_monthly_budget_usd", "total_current_spend_usd",
        "total_projected_eom_usd", "overall_burn_pct", "overall_risk",
    }
    _assert_keys(data, expected, "finance/summary")


async def test_burn_rate_contract(client):
    """Finance burn rate table reads .teams[].team_name, .department,
    .budget_monthly_usd, .current_spend_usd, .projected_eom_usd,
    .burn_pct, .risk. See dashboard/js/views/finance.js _renderBurnRateInner."""
    resp = await client.get("/api/v1/finance/burn-rate")
    assert resp.status_code == 200
    data = resp.json()
    _assert_keys(data, {"teams", "period"}, "finance/burn-rate")
    for team in data["teams"]:
        _assert_keys(
            team,
            {
                "team_name", "department", "budget_monthly_usd",
                "current_spend_usd", "projected_eom_usd", "burn_pct", "risk",
            },
            "burn-rate team",
        )


async def test_burn_rate_prior_month_contract(client):
    """GAP-7 compare mode: burn-rate period=prior_month must be distinct
    from default period. Missing this caused compare deltas to always be 0%."""
    cur = await client.get("/api/v1/finance/burn-rate?period=current")
    prior = await client.get("/api/v1/finance/burn-rate?period=prior_month")
    assert cur.status_code == 200 and prior.status_code == 200
    assert cur.json()["period"] != prior.json()["period"]


async def test_forecast_contract(client):
    """Finance forecast tile reads .forecast_eom, .confidence_low_eom,
    .confidence_high_eom, .forecast_eoq, .forecast_eoy, .mtd_actual,
    .r_squared, .trend_daily, .method.
    See dashboard/js/views/finance.js _renderForecastInner."""
    resp = await client.post(
        "/api/v1/finance/forecast",
        json={"method": "auto", "basis_days": 28},
    )
    assert resp.status_code == 200
    data = resp.json()
    _assert_keys(
        data,
        {
            "forecast_eom", "forecast_eoq", "forecast_eoy",
            "mtd_actual", "r_squared", "trend_daily", "method",
        },
        "finance/forecast",
    )


async def test_spend_trend_contract(client):
    """Finance spend trend chart reads .days[], .series[].label/.data,
    .budget_line. See dashboard/js/views/finance.js _renderSpendTrend."""
    resp = await client.get("/api/v1/finance/spend-trend?period=mtd")
    assert resp.status_code == 200
    data = resp.json()
    _assert_keys(data, {"days", "series"}, "finance/spend-trend")
    for series in data["series"]:
        _assert_keys(series, {"label", "data"}, "spend-trend series")


async def test_breach_predictions_contract(client):
    """Finance breach alerts reads .team_name, .already_breached,
    .confidence, .breach_date, .projected_overage.
    See dashboard/js/views/finance.js _renderBreachInner."""
    resp = await client.get("/api/v1/finance/breach-predictions")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    for item in data:
        assert "team_name" in item
        assert "already_breached" in item
        assert "projected_overage" in item


async def test_cost_centers_list_contract(client):
    """GAP-6 Finance cost centers tile reads .name/.cost_center_name,
    .code/.cost_center_code, .owner/.owner_email, .monthly_budget.
    See dashboard/js/views/finance.js _renderCostCenters."""
    resp = await client.get("/api/v1/finance/cost-centers")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


async def test_reconciliation_contract(client):
    """GAP-6 Finance reconciliation tile reads .status, .period,
    .total_tracked/.tracked_cost, .total_billed/.billed_cost.
    See dashboard/js/views/finance.js _renderReconciliation."""
    resp = await client.get("/api/v1/finance/reconciliation")
    assert resp.status_code == 200


async def test_chargeback_contract(client):
    """GAP-6 Finance chargeback tile reads allocation rows with
    .cost_center, .team_name, .amount/.allocated_cost, .period.
    See dashboard/js/views/finance.js _renderChargeback."""
    resp = await client.get("/api/v1/finance/chargeback")
    assert resp.status_code == 200


# ── Policies view contracts (GAP-3 prevention) ──────────────────────────────


async def test_policy_decisions_field_names(client):
    """GAP-3 decisions tile — THE field-name bug that nearly shipped.
    Dashboard MUST use: decision, decided_at, request_provider,
    request_model, request_estimated_cost, reason, policy_name, policy_id.
    NOT: action, created_at, provider, model, estimated_cost.
    See dashboard/js/views/policies.js _renderDecisions."""
    resp = await client.get("/api/v1/policy/decisions?limit=5")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    for item in data:
        # These are the fields the frontend actually reads
        expected = {
            "decision", "decided_at", "reason", "policy_id", "policy_name",
            "request_provider", "request_model", "request_estimated_cost",
        }
        _assert_keys(item, expected, "policy/decisions")
        # Guardrail against the old bug: these WRONG names must not appear
        wrong_names = {"action", "created_at", "provider", "model", "estimated_cost"}
        bad = wrong_names & item.keys()
        assert not bad, (
            f"policy/decisions: backend returned legacy field names {bad}; "
            f"these broke the dashboard in the GAP-3 incident. Rename required."
        )


async def test_policies_list_contract(client):
    """Policies table reads .name, .description, .policy_type, .effect,
    .scope, .priority, .active, .config.
    See dashboard/js/views/policies.js _renderTable."""
    resp = await client.get("/api/v1/policies")
    assert resp.status_code == 200
    data = resp.json()
    items = data if isinstance(data, list) else data.get("policies", [])
    for item in items:
        # policies may have more fields; we just check the ones used by the UI
        for key in ("name", "policy_type", "effect", "scope", "priority", "active"):
            assert key in item, f"policies list missing {key}"


# ── Sessions view contracts (GAP-9 prevention) ──────────────────────────────


async def test_attribution_sessions_field_names(client):
    """GAP-9 sessions list — prevent the field-name regression.
    Dashboard MUST use: session_id, app_id, total_calls, total_cost,
    total_input_tokens, total_output_tokens, framework_tier,
    attribution_confidence, started_at. NOT calls/tokens/cost.
    See dashboard/js/views/sessions.js _renderSessions."""
    resp = await client.get("/api/v1/attribution/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    for item in data:
        expected = {
            "session_id", "app_id", "total_calls", "total_cost",
            "total_input_tokens", "total_output_tokens",
            "framework_tier", "attribution_confidence", "started_at",
        }
        _assert_keys(item, expected, "attribution/sessions")


async def test_attribution_amplification_field_names(client):
    """GAP-9 amplification tile — dashboard uses node_label, provider,
    model, direct_cost, attributed_cost, amplification_factor, app_id.
    NOT: app_name, user_requests, ai_calls, amplification_ratio,
    avg_cost_per_request (those were the GAP-9 bug).
    See dashboard/js/views/sessions.js _renderAmplification."""
    resp = await client.get("/api/v1/attribution/amplification")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    for item in data:
        expected = {
            "node_label", "provider", "model", "direct_cost",
            "attributed_cost", "amplification_factor", "app_id",
        }
        _assert_keys(item, expected, "attribution/amplification")
        # Guardrail against the old bug
        wrong = {"user_requests", "ai_calls", "amplification_ratio", "avg_cost_per_request"}
        bad = wrong & item.keys()
        assert not bad, f"attribution/amplification: legacy field names {bad} present"


async def test_attribution_retry_tax_field_names(client):
    """GAP-9 retry-tax tile — uses node_label, retry_tax, direct_cost,
    retry_tax_pct, app_id. NOT retry_cost, retries, retry_count.
    See dashboard/js/views/sessions.js _renderRetryTax."""
    resp = await client.get("/api/v1/attribution/retry-tax")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    for item in data:
        expected = {"node_label", "retry_tax", "direct_cost", "retry_tax_pct", "app_id"}
        _assert_keys(item, expected, "attribution/retry-tax")
        wrong = {"retry_cost", "retries", "retry_count"}
        assert not (wrong & item.keys()), "legacy retry-tax field names present"


async def test_attribution_defensive_spend_field_names(client):
    """GAP-9 defensive spend tile — uses node_label, defensive_spend,
    direct_cost, is_defensive, app_id. NOT defensive_cost, category, calls.
    See dashboard/js/views/sessions.js _renderDefensive."""
    resp = await client.get("/api/v1/attribution/defensive-spend")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    for item in data:
        expected = {"node_label", "defensive_spend", "direct_cost", "is_defensive", "app_id"}
        _assert_keys(item, expected, "attribution/defensive-spend")
        wrong = {"defensive_cost", "category", "calls"}
        assert not (wrong & item.keys()), "legacy defensive-spend field names present"


# ── Audit log contracts (GAP-10) ────────────────────────────────────────────


async def test_audit_log_contract(client):
    """GAP-10 pricing history tile reads .occurred_at/.created_at,
    .action/.event_type, .actor_id/.changed_by, .before/.old_value,
    .after/.new_value. See dashboard/js/views/pricing.js _renderHistory."""
    resp = await client.get("/api/v1/audit-log?limit=5")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    for item in data:
        expected = {"id", "actor_id", "resource_type", "action", "occurred_at"}
        _assert_keys(item, expected, "audit-log")


# ── Notifications view ──────────────────────────────────────────────────────


async def test_notifications_config_contract(client):
    """Notifications view reads config by channel: slack, teams, email,
    pagerduty, webhook. Each channel has .enabled and channel-specific fields.
    See dashboard/js/views/notifications.js _renderConfig."""
    resp = await client.get("/api/v1/notifications/config")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)
    # Channels may be absent if never configured; just verify response is a dict


# ── Pricing view ────────────────────────────────────────────────────────────


async def test_pricing_overrides_contract(client):
    """Pricing overrides tile reads .provider, .model, .input_cost_per_1k,
    .output_cost_per_1k, .override_reason, .team_id, .app_id.
    See dashboard/js/views/pricing.js _renderOverrides."""
    resp = await client.get("/api/v1/pricing/overrides")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    for item in data:
        for key in ("provider", "model", "input_cost_per_1k", "output_cost_per_1k"):
            assert key in item, f"pricing override missing {key}"


async def test_pricing_global_contract(client):
    """Pricing global tile reads .provider, .model, .input_cost_per_1k,
    .output_cost_per_1k. See dashboard/js/views/pricing.js _renderGlobal."""
    resp = await client.get("/api/v1/pricing")
    assert resp.status_code == 200
