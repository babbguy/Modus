"""Finance summary must attribute month-to-date spend to teams (SQLite)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal


async def test_finance_summary_reports_team_spend(client, db_session, registered_app):
    from sqlalchemy import update

    from orchestrator.db.models import Team, UsageAggregate

    await db_session.execute(
        update(Team).where(Team.id == registered_app["team_id"]).values(budget_monthly_usd=Decimal("1000"))
    )
    day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    db_session.add(UsageAggregate(
        app_id=registered_app["app_uuid"], team_id=registered_app["team_id"],
        provider="anthropic", model="claude-sonnet-5-5", resource_type="llm_call",
        granularity="daily", period_start=day, period_end=day + timedelta(days=1),
        call_count=10, input_tokens=1000, output_tokens=500, total_tokens=1500,
        input_cost=Decimal("12.00"), output_cost=Decimal("13.00"), total_cost=Decimal("25.00"),
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/finance/summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert Decimal(body["total_current_spend_usd"]) == Decimal("25.00")
