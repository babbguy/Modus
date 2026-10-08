"""
Modus — Three-Tier Forecast Engine
========================================
Multi-horizon spend forecasting with confidence intervals and budget breach
prediction.

Architecture
------------
Three tiers, evaluated in order of preference:

Tier 1 — Built-in (always available, zero deps)
    OLS linear regression, double exponential smoothing (Holt method),
    seasonal decomposition (weekday patterns), and confidence intervals.
    Pure Python — no numpy, no scipy, no sklearn.

Tier 2 — Customer ML Endpoint (BYOML)
    Customer configures a URL to their own ML service (SageMaker, Vertex AI,
    internal model server). Modus POSTs historical spend data and receives
    structured predictions. Data never leaves customer infrastructure.

Tier 3 — AI-Powered Forecasting
    Uses the customer's own configured AI provider (OpenAI, Anthropic, Google,
    Ollama, DeepSeek) to analyze spend patterns and generate forecasts. The
    customer is already paying for these providers — we use their existing key.
    Ollama means this works fully air-gapped.

All three tiers comply with the Four Laws:
    - Zero external dependencies (stdlib only)
    - Zero data leaving customer infra (customer's own ML / customer's own AI)
    - Works on a $5/mo VPS (no GPU required for Tier 1)
    - Non-blocking (all async, background only)
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib import request as urllib_request

from sqlalchemy import text

from orchestrator.db.session import _session_factory, sqlite_dt

logger = logging.getLogger(__name__)


# ── Tier 1: Built-In Forecasting (Pure Python) ──────────────────────────────


def _ols(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """OLS linear regression → (slope, intercept, r²). Zero deps."""
    n = len(xs)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0, 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    ss_xy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    ss_xx = sum((x - mx) ** 2 for x in xs)
    if ss_xx < 1e-10:
        return 0.0, my, 0.0
    slope = ss_xy / ss_xx
    intercept = my - slope * mx
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    r_sq = 1.0 - (ss_res / ss_tot) if ss_tot > 1e-10 else 0.0
    return slope, intercept, max(0.0, min(1.0, r_sq))


def _prediction_std_error(
    xs: list[float], ys: list[float], slope: float, intercept: float
) -> float:
    """Standard error of prediction — used for confidence intervals."""
    n = len(xs)
    if n < 3:
        return 0.0
    residuals = [y - (slope * x + intercept) for x, y in zip(xs, ys)]
    mse = sum(r * r for r in residuals) / (n - 2)
    return math.sqrt(mse)


def _holt_double_exponential(
    ys: list[float], alpha: float = 0.3, beta: float = 0.1
) -> tuple[float, float]:
    """
    Holt's double exponential smoothing.
    Returns (level, trend) — forecast at horizon h = level + h * trend.
    Captures acceleration/deceleration that OLS misses.
    """
    if len(ys) < 2:
        return ys[0] if ys else 0.0, 0.0
    level = ys[0]
    trend = ys[1] - ys[0]
    for y in ys[1:]:
        prev_level = level
        level = alpha * y + (1 - alpha) * (level + trend)
        trend = beta * (level - prev_level) + (1 - beta) * trend
    return level, trend


def _seasonal_factors(ys: list[float], period: int = 7) -> list[float]:
    """
    Compute multiplicative seasonal factors for a weekly cycle (7 days).
    Returns list of 7 factors (Mon=0 … Sun=6). Factor > 1.0 means above-average.
    """
    if len(ys) < period * 2:
        return [1.0] * period
    # Average by day-of-week position
    sums = [0.0] * period
    counts = [0] * period
    for i, y in enumerate(ys):
        idx = i % period
        sums[idx] += y
        counts[idx] += 1
    avgs = [sums[i] / counts[i] if counts[i] > 0 else 1.0 for i in range(period)]
    grand_avg = sum(avgs) / period if period > 0 else 1.0
    if grand_avg < 1e-10:
        return [1.0] * period
    return [a / grand_avg for a in avgs]


def builtin_forecast(
    daily_costs: list[float],
    days_remaining_month: int,
    days_remaining_quarter: int,
    days_remaining_year: int,
    mtd_actual: float,
    weekday_offset: int = 0,
) -> dict[str, Any]:
    """
    Full built-in forecast using OLS + Holt + seasonal adjustment.

    Parameters
    ----------
    daily_costs : list[float]
        Historical daily costs (most recent last), at least 7 days ideally.
    days_remaining_month/quarter/year : int
        Calendar days remaining for each horizon.
    mtd_actual : float
        Month-to-date actual spend.
    weekday_offset : int
        Day-of-week index for the FIRST element of daily_costs (0=Mon).

    Returns
    -------
    dict with keys:
        forecast_eom, forecast_eoq, forecast_eoy : float
        confidence_low_eom, confidence_high_eom : float (80% interval)
        trend_daily : float (daily spend acceleration)
        method : str ("ols+holt+seasonal")
        r_squared : float
        seasonal_factors : list[float] (7 values, Mon–Sun)
    """
    n = len(daily_costs)
    if n < 3:
        # Not enough data — naive projection
        avg = sum(daily_costs) / max(n, 1)
        return {
            "forecast_eom": round(mtd_actual + avg * days_remaining_month, 2),
            "forecast_eoq": round(mtd_actual + avg * days_remaining_quarter, 2),
            "forecast_eoy": round(mtd_actual + avg * days_remaining_year, 2),
            "confidence_low_eom": round(mtd_actual + avg * days_remaining_month * 0.7, 2),
            "confidence_high_eom": round(mtd_actual + avg * days_remaining_month * 1.3, 2),
            "trend_daily": 0.0,
            "method": "naive_average",
            "r_squared": 0.0,
            "seasonal_factors": [1.0] * 7,
        }

    xs = list(range(n))

    # OLS for trend + R²
    slope, intercept, r_sq = _ols([float(x) for x in xs], daily_costs)
    std_err = _prediction_std_error(
        [float(x) for x in xs], daily_costs, slope, intercept
    )

    # Holt for exponential trend
    level, trend = _holt_double_exponential(daily_costs)

    # Seasonal factors
    seasonal = _seasonal_factors(daily_costs)

    # Blend: weight OLS and Holt based on R²
    # High R² → OLS is reliable. Low R² → lean on Holt (adapts faster).
    ols_weight = r_sq
    holt_weight = 1.0 - r_sq

    def _project(horizon: int) -> tuple[float, float, float]:
        """Project spend for `horizon` future days. Returns (forecast, low, high)."""
        total = 0.0
        for h in range(1, horizon + 1):
            ols_pred = max(0.0, slope * (n - 1 + h) + intercept)
            holt_pred = max(0.0, level + h * trend)
            blended = ols_weight * ols_pred + holt_weight * holt_pred
            # Apply seasonal factor
            dow = (weekday_offset + n - 1 + h) % 7
            blended *= seasonal[dow]
            total += blended

        # Confidence interval (80%): ±1.28 * std_err * sqrt(horizon)
        margin = 1.28 * std_err * math.sqrt(max(horizon, 1))
        ci_total = margin * horizon  # cumulative margin over the projection
        return total, max(0.0, total - ci_total), total + ci_total

    eom_proj, eom_low, eom_high = _project(days_remaining_month)
    eoq_proj, _, _ = _project(days_remaining_quarter)
    eoy_proj, _, _ = _project(days_remaining_year)

    return {
        "forecast_eom": round(mtd_actual + eom_proj, 2),
        "forecast_eoq": round(mtd_actual + eoq_proj, 2),
        "forecast_eoy": round(mtd_actual + eoy_proj, 2),
        "confidence_low_eom": round(mtd_actual + eom_low, 2),
        "confidence_high_eom": round(mtd_actual + eom_high, 2),
        "trend_daily": round(slope, 4),
        "method": "ols+holt+seasonal",
        "r_squared": round(r_sq, 4),
        "seasonal_factors": [round(s, 3) for s in seasonal],
    }


def predict_budget_breach(
    daily_costs: list[float],
    budget_monthly: float,
    mtd_actual: float,
    day_of_month: int,
) -> Optional[dict[str, Any]]:
    """
    Predict if and when a team will breach their monthly budget.

    Returns None if no breach predicted, or:
        breach_date : str (ISO-8601 date)
        breach_day : int (day of month)
        projected_overage : float (USD over budget)
        confidence : str ("high", "medium", "low")
    """
    if budget_monthly <= 0:
        return None
    n = len(daily_costs)
    if n < 3:
        return None

    # Already over budget?
    if mtd_actual >= budget_monthly:
        return {
            "breach_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "breach_day": day_of_month,
            "projected_overage": round(mtd_actual - budget_monthly, 2),
            "confidence": "high",
            "already_breached": True,
        }

    # Project day-by-day until EOM or breach
    level, trend = _holt_double_exponential(daily_costs)
    remaining_budget = budget_monthly - mtd_actual
    cumulative = 0.0
    now = datetime.now(timezone.utc)

    for h in range(1, 32):  # max 31 more days
        projected_day = max(0.0, level + h * trend)
        cumulative += projected_day
        if cumulative >= remaining_budget:
            breach_date = now + timedelta(days=h)
            # R² as confidence proxy
            xs = [float(x) for x in range(n)]
            _, _, r_sq = _ols(xs, daily_costs)
            confidence = "high" if r_sq > 0.7 else "medium" if r_sq > 0.4 else "low"
            return {
                "breach_date": breach_date.strftime("%Y-%m-%d"),
                "breach_day": breach_date.day,
                "projected_overage": round(cumulative - remaining_budget, 2),
                "confidence": confidence,
                "already_breached": False,
            }

    return None  # No breach predicted within the month


# ── Tier 2: Customer ML Endpoint (BYOML) ────────────────────────────────────


async def forecast_custom_ml(
    endpoint_url: str,
    auth_header: Optional[str],
    team_id: str,
    daily_costs: list[float],
    daily_labels: list[str],
    metadata: dict[str, Any],
    timeout_seconds: int = 30,
) -> Optional[dict[str, Any]]:
    """
    POST historical spend data to customer's own ML endpoint.
    Returns the parsed prediction response, or None on failure.

    Request schema (sent to customer's endpoint):
    {
        "team_id": "uuid",
        "daily_costs": [12.50, 14.20, ...],
        "daily_labels": ["2026-03-01", "2026-03-02", ...],
        "metadata": {
            "mtd_actual": 450.00,
            "budget_monthly": 2000.00,
            "days_remaining_month": 19,
            "provider_breakdown": {"openai": 300, "anthropic": 150}
        }
    }

    Expected response schema:
    {
        "forecast_eom": 1850.00,
        "forecast_eoq": 5500.00,
        "confidence_low_eom": 1600.00,
        "confidence_high_eom": 2100.00,
        "breach_predicted": false,
        "breach_date": null,
        "recommendations": ["Consider switching team-alpha to gpt-4o-mini"],
        "method": "xgboost_v2"
    }
    """
    try:
        payload = json.dumps({
            "team_id": team_id,
            "daily_costs": daily_costs,
            "daily_labels": daily_labels,
            "metadata": metadata,
        }).encode()

        headers = {"Content-Type": "application/json"}
        if auth_header:
            headers["Authorization"] = auth_header

        req = urllib_request.Request(
            endpoint_url,
            data=payload,
            headers=headers,
            method="POST",
        )

        def _call():
            with urllib_request.urlopen(req, timeout=timeout_seconds) as resp:
                if resp.status != 200:
                    logger.warning(
                        "Custom ML endpoint returned %d", resp.status,
                        extra={"team_id": team_id, "url": endpoint_url},
                    )
                    return None
                return json.loads(resp.read().decode())

        result = await asyncio.to_thread(_call)
        if result and isinstance(result, dict):
            result["method"] = result.get("method", "custom_ml")
            return result
        return None

    except Exception as exc:
        logger.warning(
            "Custom ML endpoint call failed: %s", exc,
            extra={"team_id": team_id, "url": endpoint_url},
        )
        return None


# ── Tier 3: AI-Powered Forecasting (Customer's Own Provider) ────────────────


_AI_FORECAST_SYSTEM_PROMPT = """\
You are a financial forecasting analyst specializing in AI/ML infrastructure costs.
You will receive structured spend data for a team and must return a JSON forecast.

Rules:
- Analyze daily spend patterns, trends, seasonality (weekday vs weekend).
- Predict end-of-month (EOM), end-of-quarter (EOQ), and end-of-year (EOY) spend.
- Provide confidence intervals (80% prediction interval).
- Identify anomalies or concerning patterns.
- Flag budget breach risk if budget is provided.
- Return ONLY valid JSON — no markdown, no explanation outside the JSON.

Response format (strict JSON):
{
    "forecast_eom": <float>,
    "forecast_eoq": <float>,
    "forecast_eoy": <float>,
    "confidence_low_eom": <float>,
    "confidence_high_eom": <float>,
    "trend": "<increasing|stable|decreasing|volatile>",
    "trend_pct_monthly": <float>,
    "patterns_detected": ["<pattern description>", ...],
    "breach_predicted": <bool>,
    "breach_date": "<YYYY-MM-DD or null>",
    "risk_level": "<low|medium|high|critical>",
    "narrative": "<2-3 sentence plain-English summary for finance stakeholders>"
}"""


async def forecast_ai_powered(
    daily_costs: list[float],
    daily_labels: list[str],
    team_name: str,
    mtd_actual: float,
    budget_monthly: Optional[float],
    days_remaining_month: int,
    days_remaining_quarter: int,
    days_remaining_year: int,
    provider_breakdown: Optional[dict[str, float]] = None,
) -> Optional[dict[str, Any]]:
    """
    Use the customer's configured AI provider to generate a spend forecast.
    Falls back to None if AI is unavailable or returns unparseable output.
    """
    from orchestrator.core.config import settings as _cfg

    api_key = _cfg.summary_api_key.get_secret_value() if _cfg.summary_api_key else ""
    if not api_key:
        logger.debug("AI forecast skipped: no summary_api_key configured")
        return None

    # Build the user prompt with structured data
    user_prompt = json.dumps({
        "team": team_name,
        "period": daily_labels[0] if daily_labels else "unknown",
        "daily_spend_usd": [round(c, 2) for c in daily_costs[-60:]],  # Last 60 days max
        "daily_labels": daily_labels[-60:],
        "mtd_actual_usd": round(mtd_actual, 2),
        "budget_monthly_usd": round(budget_monthly, 2) if budget_monthly else None,
        "days_remaining_month": days_remaining_month,
        "days_remaining_quarter": days_remaining_quarter,
        "days_remaining_year": days_remaining_year,
        "provider_breakdown_usd": provider_breakdown,
    }, indent=2)

    try:
        from orchestrator.core.ai_engine.factory import get_provider

        provider = get_provider(
            agent=_cfg.summary_agent,
            api_key=api_key,
            model=_cfg.summary_model_id,
            base_url=_cfg.summary_base_url,
        )

        raw = await provider.summarize(
            f"{_AI_FORECAST_SYSTEM_PROMPT}\n\n--- DATA ---\n{user_prompt}"
        )

        # Parse JSON from response (handle markdown code blocks)
        text_clean = raw.strip()
        if text_clean.startswith("```"):
            lines = text_clean.split("\n")
            text_clean = "\n".join(
                line for line in lines if not line.strip().startswith("```")
            )

        result = json.loads(text_clean)
        if isinstance(result, dict):
            result["method"] = f"ai_{_cfg.summary_agent}"
            return result

    except json.JSONDecodeError:
        logger.warning("AI forecast returned non-JSON output")
    except Exception as exc:
        logger.warning("AI-powered forecast failed: %s", exc)

    return None


# ── Orchestrator: Three-Tier Dispatch ────────────────────────────────────────


async def get_forecast(
    team_id: str,
    team_name: str,
    daily_costs: list[float],
    daily_labels: list[str],
    mtd_actual: float,
    budget_monthly: Optional[float],
    days_remaining_month: int,
    days_remaining_quarter: int,
    days_remaining_year: int,
    weekday_offset: int = 0,
    provider_breakdown: Optional[dict[str, float]] = None,
    forecast_method: str = "auto",
    custom_ml_url: Optional[str] = None,
    custom_ml_auth: Optional[str] = None,
) -> dict[str, Any]:
    """
    Three-tier forecast dispatch.

    forecast_method:
        "auto"      — Try Tier 3 (AI) if configured, else Tier 1 (built-in)
        "builtin"   — Force Tier 1 only
        "custom_ml" — Try Tier 2, fallback to Tier 1
        "ai"        — Try Tier 3, fallback to Tier 1

    Always returns a result — Tier 1 is the guaranteed fallback.
    """
    # Tier 1 is always computed as fallback
    builtin_result = builtin_forecast(
        daily_costs=daily_costs,
        days_remaining_month=days_remaining_month,
        days_remaining_quarter=days_remaining_quarter,
        days_remaining_year=days_remaining_year,
        mtd_actual=mtd_actual,
        weekday_offset=weekday_offset,
    )

    # Budget breach prediction (always from Tier 1 — fast, no API cost)
    now = datetime.now(timezone.utc)
    breach = predict_budget_breach(
        daily_costs=daily_costs,
        budget_monthly=budget_monthly or 0.0,
        mtd_actual=mtd_actual,
        day_of_month=now.day,
    )
    builtin_result["breach_prediction"] = breach

    if forecast_method == "builtin":
        return builtin_result

    # Tier 2: Custom ML
    if forecast_method in ("custom_ml", "auto") and custom_ml_url:
        ml_result = await forecast_custom_ml(
            endpoint_url=custom_ml_url,
            auth_header=custom_ml_auth,
            team_id=team_id,
            daily_costs=daily_costs,
            daily_labels=daily_labels,
            metadata={
                "mtd_actual": mtd_actual,
                "budget_monthly": budget_monthly,
                "days_remaining_month": days_remaining_month,
                "provider_breakdown": provider_breakdown,
            },
        )
        if ml_result:
            # Merge: ML predictions with builtin seasonal + breach
            ml_result.setdefault("seasonal_factors", builtin_result["seasonal_factors"])
            ml_result.setdefault("breach_prediction", breach)
            ml_result.setdefault("r_squared", builtin_result["r_squared"])
            return ml_result

    # Tier 3: AI-Powered
    if forecast_method in ("ai", "auto"):
        ai_result = await forecast_ai_powered(
            daily_costs=daily_costs,
            daily_labels=daily_labels,
            team_name=team_name,
            mtd_actual=mtd_actual,
            budget_monthly=budget_monthly,
            days_remaining_month=days_remaining_month,
            days_remaining_quarter=days_remaining_quarter,
            days_remaining_year=days_remaining_year,
            provider_breakdown=provider_breakdown,
        )
        if ai_result:
            ai_result.setdefault("seasonal_factors", builtin_result["seasonal_factors"])
            ai_result.setdefault("breach_prediction", breach)
            ai_result.setdefault("r_squared", builtin_result["r_squared"])
            return ai_result

    # Fallback: always Tier 1
    return builtin_result


# ── Provider Cost Change Impact ──────────────────────────────────────────────


async def simulate_price_change(
    provider: str,
    price_change_pct: float,
    days: int = 30,
) -> Optional[dict[str, Any]]:
    """
    Simulate the impact of a provider price change on spend.
    E.g., "What happens to our monthly bill if OpenAI raises prices 20%?"
    """
    if _session_factory is None:
        return None

    try:
        async with _session_factory() as db:
            now = datetime.now(timezone.utc)
            start = now - timedelta(days=days)

            q = text("""
                SELECT
                    team_id,
                    model,
                    SUM(total_cost) AS cost,
                    SUM(call_count) AS calls,
                    SUM(total_tokens) AS tokens
                FROM usage_aggregates
                WHERE granularity = 'daily'
                  AND period_start >= :start AND period_start < :end
                  AND LOWER(provider) = LOWER(:provider)
                GROUP BY team_id, model
                ORDER BY SUM(total_cost) DESC
            """)
            result = await db.execute(q, {
                "start": sqlite_dt(start), "end": sqlite_dt(now), "provider": provider,
            })
            rows = result.all()

            if not rows:
                return {
                    "provider": provider,
                    "price_change_pct": price_change_pct,
                    "current_monthly_usd": 0.0,
                    "projected_monthly_usd": 0.0,
                    "delta_monthly_usd": 0.0,
                    "impact_by_model": [],
                }

            elapsed_days = max((now - start).days, 1)
            total_current = 0.0
            impact_by_model: list[dict] = []

            for r in rows:
                period_cost = float(r.cost or 0)
                monthly_cost = period_cost * 30 / elapsed_days
                delta = monthly_cost * (price_change_pct / 100)
                total_current += monthly_cost

                impact_by_model.append({
                    "model": r.model,
                    "current_monthly_usd": round(monthly_cost, 2),
                    "projected_monthly_usd": round(monthly_cost + delta, 2),
                    "delta_monthly_usd": round(delta, 2),
                    "calls_30d": int(r.calls or 0),
                })

            total_delta = total_current * (price_change_pct / 100)
            return {
                "provider": provider,
                "price_change_pct": price_change_pct,
                "current_monthly_usd": round(total_current, 2),
                "projected_monthly_usd": round(total_current + total_delta, 2),
                "delta_monthly_usd": round(total_delta, 2),
                "impact_by_model": impact_by_model,
            }

    except Exception as exc:
        logger.error("Price change simulation failed: %s", exc)
        return None
