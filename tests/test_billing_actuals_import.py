"""
Tests -- importing provider invoice totals and reconciling them against usage.

POST /api/v1/finance/reconciliation/import       (JSON)
POST /api/v1/finance/reconciliation/import/csv   (multipart upload)
GET  /api/v1/finance/reconciliation
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from orchestrator.db.models import App, AuditLog, BillingActual, Team

IMPORT = "/api/v1/finance/reconciliation/import"
IMPORT_CSV = "/api/v1/finance/reconciliation/import/csv"
RECON = "/api/v1/finance/reconciliation"


def _row(**over):
    row = {
        "provider": "openai",
        "service": "API",
        "period_start": "2026-09-01",
        "period_end": "2026-10-01",
        "actual_cost_usd": "100.00",
    }
    row.update(over)
    return row


@pytest.fixture
async def usage(db_session):
    """Tracked usage: two daily aggregates for openai in September ($60 + $30 = $90,
    one of them exactly on the period boundary), one in October and one for
    another provider; the last two must not be counted."""
    from orchestrator.db.models import UsageAggregate

    team = Team(slug="recon-team", name="Recon Team")
    db_session.add(team)
    await db_session.flush()
    app = App(team_id=team.id, app_id="recon-app", app_name="Recon App",
              api_key_hash="x", api_key_prefix="mds_x")
    db_session.add(app)
    await db_session.flush()

    def agg(provider, day, cost, model="m"):
        ps = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
        db_session.add(UsageAggregate(
            app_id=app.id, team_id=team.id, provider=provider, model=model,
            resource_type="llm_call", granularity="daily", period_start=ps,
            period_end=ps, total_cost=Decimal(cost),
        ))

    agg("openai", "2026-09-01", "60.00000000")    # exactly on period_start
    agg("openai", "2026-09-20", "30.00000000")
    agg("openai", "2026-10-02", "500.00000000")   # next period
    agg("anthropic", "2026-09-05", "7.00000000")
    await db_session.commit()


async def test_import_then_reconciliation_shows_real_variance(client, usage):
    resp = await client.post(IMPORT, json={"rows": [_row()], "source": "openai-sep"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["imported"], body["created"], body["updated"], body["unchanged"]) == (1, 1, 0, 0)

    rows = (await client.get(RECON)).json()
    assert len(rows) == 1
    r = rows[0]
    assert r["provider"] == "openai" and r["service"] == "API"
    assert r["actual_cost_usd"] == "100.00000000"
    assert r["inferred_cost_usd"] == "90.00000000"
    assert r["delta_usd"] == "10.00000000"
    assert r["delta_pct"] == 10.0
    assert r["period_start"].startswith("2026-09-01") and r["period_end"].startswith("2026-10-01")
    assert r["reconciled_at"] is not None


async def test_reconciliation_recomputes_tracked_cost_from_current_usage(client, db_session, usage):
    await client.post(IMPORT, json={"rows": [_row()]})
    from orchestrator.db.models import UsageAggregate

    app = (await db_session.execute(select(App))).scalars().first()
    ps = datetime(2026, 9, 25, tzinfo=timezone.utc)
    db_session.add(UsageAggregate(
        app_id=app.id, team_id=app.team_id, provider="openai", model="m2",
        resource_type="llm_call", granularity="daily", period_start=ps, period_end=ps,
        total_cost=Decimal("10"),
    ))
    await db_session.commit()

    r = (await client.get(RECON)).json()[0]
    assert r["inferred_cost_usd"] == "100.00000000"
    assert r["delta_usd"] == "0.00000000"


async def test_reimport_same_rows_does_not_duplicate(client, db_session, usage):
    await client.post(IMPORT, json={"rows": [_row()]})
    again = await client.post(IMPORT, json={"rows": [_row()]})
    assert again.status_code == 200
    assert (again.json()["created"], again.json()["updated"], again.json()["unchanged"]) == (0, 0, 1)

    changed = await client.post(IMPORT, json={"rows": [_row(actual_cost_usd="120.50")]})
    assert (changed.json()["created"], changed.json()["updated"]) == (0, 1)

    count = (await db_session.execute(select(func.count()).select_from(BillingActual))).scalar_one()
    assert count == 1
    r = (await client.get(RECON)).json()[0]
    assert r["actual_cost_usd"] == "120.50000000"


async def test_rows_with_different_service_or_period_are_distinct(client, db_session):
    rows = [_row(), _row(service="Batch"), _row(service=None), _row(period_start="2026-10-01", period_end="2026-11-01")]
    resp = await client.post(IMPORT, json={"rows": rows})
    assert resp.status_code == 200 and resp.json()["created"] == 4
    assert (await db_session.execute(select(func.count()).select_from(BillingActual))).scalar_one() == 4
    # service omitted is stored as "" and reported as null
    services = {r["service"] for r in (await client.get(RECON)).json()}
    assert services == {"API", "Batch", None}


async def test_money_is_stored_as_exact_decimal(client, db_session):
    resp = await client.post(IMPORT, json={"rows": [_row(actual_cost_usd="0.12345678")]})
    assert resp.status_code == 200
    db_session.expire_all()
    ba = (await db_session.execute(select(BillingActual))).scalars().one()
    assert ba.actual_cost_usd == Decimal("0.12345678")


async def test_import_writes_audit_log_entry(client, db_session):
    await client.post(IMPORT, json={"rows": [_row(), _row(provider="anthropic")], "source": "q3"})
    db_session.expire_all()
    log = (await db_session.execute(
        select(AuditLog).where(AuditLog.resource_type == "billing_actual")
    )).scalars().one()
    assert log.action == "imported" and log.actor_id == "platform-admin"
    assert log.after["rows"] == 2 and log.after["created"] == 2
    assert log.after["providers"] == ["anthropic", "openai"] and log.after["source"] == "q3"


@pytest.mark.parametrize(
    "patch,fragment",
    [
        ({"actual_cost_usd": "abc"}, "decimal amount"),
        ({"actual_cost_usd": 12.5}, "string"),
        ({"actual_cost_usd": "-1"}, "negative"),
        ({"actual_cost_usd": "1.123456789"}, "8 decimal places"),
        ({"actual_cost_usd": "NaN"}, "finite"),
        ({"actual_cost_usd": "99999999999"}, "NUMERIC"),
        ({"provider": "Open AI!"}, "provider"),
        ({"provider": ""}, "provider"),
        ({"currency": "EUR"}, "not supported"),
        ({"period_start": "not-a-date"}, "period_start"),
        ({"period_end": "2026-08-01"}, "after period_start"),
        ({"period_end": "2028-01-01"}, "longer than"),
    ],
)
async def test_bad_rows_are_rejected_with_422_and_nothing_is_imported(client, db_session, patch, fragment):
    good = _row(provider="anthropic")
    resp = await client.post(IMPORT, json={"rows": [good, _row(**patch)]})
    assert resp.status_code == 422, resp.text
    errors = resp.json()["detail"]["errors"]
    assert errors and errors[0]["row"] == 2
    assert fragment in (errors[0].get("field", "") + " " + errors[0]["message"])
    assert (await db_session.execute(select(func.count()).select_from(BillingActual))).scalar_one() == 0


async def test_missing_fields_and_duplicate_rows_are_rejected(client):
    resp = await client.post(IMPORT, json={"rows": [{"provider": "openai"}]})
    assert resp.status_code == 422
    fields = {e["field"] for e in resp.json()["detail"]["errors"]}
    assert {"period_start", "period_end", "actual_cost_usd"} <= fields

    dup = await client.post(IMPORT, json={"rows": [_row(), _row()]})
    assert dup.status_code == 422
    assert "duplicate of row 1" in dup.text

    empty = await client.post(IMPORT, json={"rows": []})
    assert empty.status_code == 422


async def test_import_requires_platform_admin(client, monkeypatch):
    from orchestrator.core import auth

    reader = auth.Identity(actor_id="bob", role="read_only", team_ids=["t"])
    monkeypatch.setattr(auth, "_STUB_IDENTITY", reader)
    resp = await client.post(IMPORT, json={"rows": [_row()]})
    assert resp.status_code == 403


# ── CSV ──────────────────────────────────────────────────────────────────────

CSV_OK = (
    "provider,service,period_start,period_end,actual_cost_usd,currency\n"
    "openai,API,2026-09-01,2026-10-01,100.00,USD\n"
    "anthropic,,2026-09-01,2026-10-01,\"7.00\",usd\n"
)


async def _post_csv(client, content: str, name="invoice.csv"):
    return await client.post(IMPORT_CSV, files={"file": (name, content.encode("utf-8"), "text/csv")})


async def test_csv_import_creates_rows_and_variance_is_visible(client, usage):
    resp = await _post_csv(client, CSV_OK)
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] == 2

    by_provider = {r["provider"]: r for r in (await client.get(RECON)).json()}
    assert by_provider["openai"]["delta_usd"] == "10.00000000"
    assert by_provider["anthropic"]["inferred_cost_usd"] == "7.00000000"
    assert by_provider["anthropic"]["delta_usd"] == "0.00000000"


async def test_csv_reimport_is_idempotent(client, db_session):
    await _post_csv(client, CSV_OK)
    again = await _post_csv(client, CSV_OK)
    assert again.status_code == 200 and again.json()["unchanged"] == 2
    assert (await db_session.execute(select(func.count()).select_from(BillingActual))).scalar_one() == 2


async def test_csv_with_bom_and_extra_columns_is_accepted(client):
    content = "﻿Provider,Period_Start,Period_End,Actual_Cost_USD,notes\nopenai,2026-09-01,2026-10-01,5,ignored\n"
    resp = await _post_csv(client, content)
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    "content,fragment,row",
    [
        ("provider,period_start,period_end\nopenai,2026-09-01,2026-10-01\n", "missing column", 1),
        ("", "empty", 0),
        ("provider,period_start,period_end,actual_cost_usd\n", "no data rows", 0),
        ("provider,period_start,period_end,actual_cost_usd\nopenai,2026-09-01,2026-10-01,12.3x\n", "decimal amount", 2),
        ("provider,period_start,period_end,actual_cost_usd\nopenai,2026-09-01,2026-10-01,1\nopenai,bad,2026-10-01,2\n", "period_start", 3),
    ],
)
async def test_csv_errors_are_422_with_line_numbers(client, db_session, content, fragment, row):
    resp = await _post_csv(client, content)
    assert resp.status_code == 422, resp.text
    errors = resp.json()["detail"]["errors"]
    assert errors[0]["row"] == row
    assert fragment in (errors[0].get("field", "") + " " + errors[0]["message"])
    assert (await db_session.execute(select(func.count()).select_from(BillingActual))).scalar_one() == 0


async def test_csv_that_is_not_utf8_is_rejected(client):
    resp = await client.post(IMPORT_CSV, files={"file": ("x.csv", b"\xff\xfe\x00bad", "text/csv")})
    assert resp.status_code == 422 and "UTF-8" in resp.text


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_variance_math_and_zero_actual():
    from orchestrator.core.billing_import import variance

    assert variance(Decimal("100"), Decimal("90")) == (Decimal("10.00000000"), 10.0)
    assert variance(Decimal("90"), Decimal("100")) == (Decimal("-10.00000000"), -11.1111)
    assert variance(Decimal("0"), Decimal("5")) == (Decimal("-5.00000000"), None)


def test_aws_invoice_maps_to_bedrock_usage():
    from orchestrator.core.billing_import import usage_providers_for

    assert "bedrock" in usage_providers_for("aws")
    assert usage_providers_for("openai") == ("openai",)


async def test_existing_row_inserted_directly_still_lists(client, db_session):
    """Rows created outside the import (older data) must keep working."""
    db_session.add(BillingActual(
        provider="azure", period_start=datetime(2026, 9, 1, tzinfo=timezone.utc),
        period_end=datetime(2026, 10, 1, tzinfo=timezone.utc), actual_cost_usd=Decimal("0"),
    ))
    await db_session.commit()
    rows = (await client.get(RECON)).json()
    assert rows[0]["provider"] == "azure" and rows[0]["delta_pct"] is None
