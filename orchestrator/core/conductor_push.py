"""
Modus Orchestrator — Conductor Push Agent
=================================================
Background task that pushes aggregated data from this Orchestrator
to the org-level Conductor service.

Push protocol:
  1. Collect current aggregates, team/app metadata, alerts, and policy decisions
  2. POST to /api/v1/conductor/push with Bearer token
  3. Conductor returns acknowledgement with receipt ID
  4. On failure, retry with circuit breaker

The same push protocol is used at every layer boundary in the Modus
architecture (Agent→Orchestrator, Orchestrator→Conductor, Conductor→Federation),
making the system pluggable by design.

Four Laws compliance:
  - Zero external dependencies (stdlib + httpx only, already a dep)
  - Zero data leaving customer infrastructure (Conductor is customer-hosted)
  - Non-blocking (async, fire-and-forget with retry)
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone

from orchestrator.core.config import settings

logger = logging.getLogger(__name__)

# Circuit breaker state
_consecutive_failures = 0
_circuit_open_until: datetime | None = None
_CIRCUIT_BREAKER_THRESHOLD = 5
_CIRCUIT_BREAKER_COOLDOWN = 60  # seconds


def _get_instance_id() -> str:
    """Get or generate a stable instance ID for this Orchestrator."""
    if settings.conductor_instance_id:
        return settings.conductor_instance_id
    # Generate a deterministic ID from app_name + database_url
    import hashlib
    seed = f"{settings.app_name}:{settings.database_url}"
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


def _get_instance_name() -> str:
    """Get the human-readable name for this Orchestrator."""
    return settings.conductor_instance_name or settings.app_name


async def register_with_conductor() -> bool:
    """
    Register this Orchestrator with the Conductor.
    Called once at startup.
    """
    if not settings.conductor_url:
        return False

    try:
        import httpx

        instance_id = _get_instance_id()
        instance_name = _get_instance_name()

        # Count apps and agents
        from orchestrator.db.session import get_session_ctx
        from orchestrator.db.models import App, AgentHeartbeat
        from sqlalchemy import select, func

        async with get_session_ctx() as db:
            app_count = (await db.execute(
                select(func.count()).select_from(App).where(App.is_active == True)  # noqa: E712
            )).scalar() or 0

            agent_count = (await db.execute(
                select(func.count(func.distinct(AgentHeartbeat.app_id)))
            )).scalar() or 0

        url = f"{settings.conductor_url.rstrip('/')}/api/v1/conductor/register"

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url,
                json={
                    "name": instance_name,
                    "instance_id": instance_id,
                    "endpoint_url": f"http://localhost:{settings.port}",
                    "version": settings.version,
                    "environment": settings.environment,
                    "app_count": app_count,
                    "agent_count": agent_count,
                    "region_id": settings.region_id,
                },
                headers={
                    "Authorization": f"Bearer {settings.conductor_secret}",
                    "X-Orchestrator-Instance-ID": instance_id,
                },
            )

        if resp.status_code in (200, 201):
            data = resp.json()
            logger.info(
                "Registered with Conductor: status=%s, node_id=%s",
                data.get("status"), data.get("orchestrator_node_id"),
            )
            return True
        else:
            logger.error(
                "Failed to register with Conductor: %d %s",
                resp.status_code, resp.text[:200],
            )
            return False

    except Exception:
        logger.exception("Failed to register with Conductor")
        return False


async def push_to_conductor() -> bool:
    """
    Push current aggregated data to the Conductor.

    Collects:
      - Daily aggregates for the current month
      - Team and app metadata
      - Recent alerts (last 7 days)
      - Recent policy decisions (last 7 days, deny/throttle only for savings)
    """
    global _consecutive_failures, _circuit_open_until

    if not settings.conductor_url:
        return False

    # Check circuit breaker
    now = datetime.now(timezone.utc)
    if _circuit_open_until and now < _circuit_open_until:
        logger.debug("Circuit breaker open, skipping push until %s", _circuit_open_until)
        return False

    try:
        import httpx
        from orchestrator.db.session import get_session_ctx
        from orchestrator.db.models import (
            App, Team, UsageAggregate, Alert, PolicyDecision,
            AgentHeartbeat, CostCenter, TeamCostCenter,
        )
        from sqlalchemy import select, func

        instance_id = _get_instance_id()
        batch_id = f"push-{instance_id}-{now.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(4)}"

        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        week_ago = now - timedelta(days=7)

        async with get_session_ctx() as db:
            # 1. Collect daily aggregates for current month
            agg_result = await db.execute(
                select(UsageAggregate).where(
                    UsageAggregate.granularity == "daily",
                    UsageAggregate.period_start >= month_start,
                )
            )
            aggregates_raw = agg_result.scalars().all()

            # Enrich with app/team names
            app_ids = list({a.app_id for a in aggregates_raw})
            team_ids = list({a.team_id for a in aggregates_raw})

            app_names = {}
            if app_ids:
                app_result = await db.execute(
                    select(App.id, App.app_name, App.app_id, App.environment).where(
                        App.id.in_(app_ids)
                    )
                )
                for r in app_result.all():
                    app_names[r.id] = (r.app_name, r.app_id, r.environment)

            team_info = {}
            if team_ids:
                team_result = await db.execute(
                    select(Team.id, Team.slug, Team.name).where(Team.id.in_(team_ids))
                )
                for r in team_result.all():
                    team_info[r.id] = (r.slug, r.name)

            aggregates = []
            for a in aggregates_raw:
                app_data = app_names.get(a.app_id, ("", "", "production"))
                team_data = team_info.get(a.team_id, ("", ""))
                aggregates.append({
                    "app_id": a.app_id,
                    "app_name": app_data[0],
                    "team_id": a.team_id,
                    "team_slug": team_data[0],
                    "team_name": team_data[1],
                    "provider": a.provider,
                    "model": a.model,
                    "resource_type": a.resource_type or "llm_call",
                    "environment": app_data[2] if len(app_data) > 2 else "production",
                    "granularity": a.granularity,
                    "period_start": a.period_start.isoformat(),
                    "period_end": a.period_end.isoformat(),
                    "call_count": a.call_count or 0,
                    "input_tokens": a.input_tokens or 0,
                    "output_tokens": a.output_tokens or 0,
                    "total_tokens": a.total_tokens or 0,
                    "input_cost": str(a.input_cost or 0),
                    "output_cost": str(a.output_cost or 0),
                    "total_cost": str(a.total_cost or 0),
                    "avg_duration_ms": a.avg_duration_ms,
                    "p95_duration_ms": a.p95_duration_ms,
                })

            # 2. Collect team metadata
            teams_result = await db.execute(
                select(Team).where(Team.deleted_at.is_(None))
            )
            teams_raw = teams_result.scalars().all()

            # Get cost center info
            cc_by_team = {}
            try:
                tcc_result = await db.execute(
                    select(TeamCostCenter.team_id, CostCenter.code, CostCenter.name)
                    .join(CostCenter, CostCenter.id == TeamCostCenter.cost_center_id)
                )
                for r in tcc_result.all():
                    cc_by_team[r.team_id] = (r.code, r.name)
            except Exception:
                pass  # cost centers may not exist yet

            teams = []
            for t in teams_raw:
                cc = cc_by_team.get(t.id, (None, None))
                teams.append({
                    "id": t.id,
                    "slug": t.slug,
                    "name": t.name,
                    "department": t.department,
                    "max_budget_usd": str(t.max_budget_usd) if t.max_budget_usd else None,
                    "budget_monthly_usd": str(t.budget_monthly_usd) if hasattr(t, "budget_monthly_usd") and t.budget_monthly_usd else None,
                    "budget_quarterly_usd": str(t.budget_quarterly_usd) if hasattr(t, "budget_quarterly_usd") and t.budget_quarterly_usd else None,
                    "cost_center_code": cc[0],
                    "cost_center_name": cc[1],
                })

            # 3. Collect app metadata
            apps_result = await db.execute(
                select(App).where(App.deleted_at.is_(None))
            )
            apps_raw = apps_result.scalars().all()

            # Get latest heartbeats for each app
            heartbeats = {}
            try:
                hb_result = await db.execute(
                    select(
                        AgentHeartbeat.app_id,
                        func.max(AgentHeartbeat.agent_version).label("version"),
                        AgentHeartbeat.instrumented_providers,
                    ).group_by(AgentHeartbeat.app_id)
                )
                for r in hb_result.all():
                    heartbeats[r.app_id] = (r.version, r.instrumented_providers)
            except Exception as exc:
                logger.warning(
                    "Conductor push: heartbeat lookup failed, app versions omitted: %s", exc
                )

            apps = []
            for a in apps_raw:
                hb = heartbeats.get(a.id, (None, None))
                apps.append({
                    "id": a.id,
                    "app_id": a.app_id,
                    "app_name": a.app_name,
                    "team_id": a.team_id,
                    "environment": a.environment or "production",
                    "is_active": a.is_active,
                    "last_seen_at": a.last_seen_at.isoformat() if a.last_seen_at else None,
                    "agent_version": hb[0],
                    "instrumented_providers": hb[1],
                })

            # 4. Collect recent alerts
            alerts_result = await db.execute(
                select(Alert).where(Alert.fired_at >= week_ago).order_by(Alert.fired_at.desc())
            )
            alerts_raw = alerts_result.scalars().all()
            alerts = [
                {
                    "id": a.id,
                    "severity": a.severity,
                    "metric": a.metric,
                    "threshold_value": str(a.threshold_value),
                    "actual_value": str(a.actual_value),
                    "app_id": a.app_id,
                    "team_id": a.team_id,
                    "fired_at": a.fired_at.isoformat(),
                    "acknowledged": a.acknowledged_at is not None,
                }
                for a in alerts_raw
            ]

            # 5. Collect recent policy decisions (deny/throttle for savings tracking)
            policy_decisions = []
            try:
                pd_result = await db.execute(
                    select(PolicyDecision).where(
                        PolicyDecision.decided_at >= week_ago,
                    ).order_by(PolicyDecision.decided_at.desc())
                )
                pd_raw = pd_result.scalars().all()
                policy_decisions = [
                    {
                        "id": p.id,
                        "decision": p.decision,
                        "estimated_cost": str(p.request_estimated_cost or 0)
                        if hasattr(p, "request_estimated_cost") else "0",
                        "decided_at": p.decided_at.isoformat(),
                        "team_id": p.team_id,
                        "app_id": p.app_id,
                    }
                    for p in pd_raw
                ]
            except Exception:
                pass  # policy decisions table may not exist

        # Send to Conductor
        url = f"{settings.conductor_url.rstrip('/')}/api/v1/conductor/push"

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                json={
                    "batch_id": batch_id,
                    "aggregates": aggregates,
                    "teams": teams,
                    "apps": apps,
                    "alerts": alerts,
                    "policy_decisions": policy_decisions,
                },
                headers={
                    "Authorization": f"Bearer {settings.conductor_secret}",
                    "X-Orchestrator-Instance-ID": instance_id,
                },
            )

        if resp.status_code in (200, 201):
            data = resp.json()
            _consecutive_failures = 0
            _circuit_open_until = None
            logger.info(
                "Push to Conductor: batch=%s status=%s aggs=%d teams=%d apps=%d alerts=%d",
                batch_id,
                data.get("status"),
                data.get("aggregates_accepted", 0),
                data.get("teams_updated", 0),
                data.get("apps_updated", 0),
                data.get("alerts_accepted", 0),
            )
            return True
        else:
            _consecutive_failures += 1
            logger.error(
                "Push to Conductor failed: %d %s (failure %d/%d)",
                resp.status_code, resp.text[:200],
                _consecutive_failures, _CIRCUIT_BREAKER_THRESHOLD,
            )
            if _consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD:
                _circuit_open_until = now + timedelta(seconds=_CIRCUIT_BREAKER_COOLDOWN)
                logger.warning("Circuit breaker tripped, pausing pushes for %ds", _CIRCUIT_BREAKER_COOLDOWN)
            return False

    except Exception:
        _consecutive_failures += 1
        logger.exception(
            "Push to Conductor error (failure %d/%d)",
            _consecutive_failures, _CIRCUIT_BREAKER_THRESHOLD,
        )
        if _consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD:
            _circuit_open_until = datetime.now(timezone.utc) + timedelta(seconds=_CIRCUIT_BREAKER_COOLDOWN)
            logger.warning("Circuit breaker tripped, pausing pushes for %ds", _CIRCUIT_BREAKER_COOLDOWN)
        return False


async def run_conductor_push_loop() -> None:
    """Background task that pushes data to the Conductor periodically."""
    if not settings.conductor_url:
        logger.info("Conductor push disabled (MODUS_CONDUCTOR_URL not set)")
        return

    interval = settings.conductor_push_interval_seconds
    logger.info(
        "Conductor push loop started (url=%s, interval=%ds)",
        settings.conductor_url, interval,
    )

    # Register first
    await register_with_conductor()

    # Wait one cycle before first push
    await asyncio.sleep(min(interval, 30))

    while True:
        try:
            await push_to_conductor()
        except asyncio.CancelledError:
            # Final push on shutdown
            logger.info("Conductor push loop cancelled, performing final push")
            await push_to_conductor()
            return
        except Exception:
            logger.exception("Conductor push loop error")

        await asyncio.sleep(interval)
