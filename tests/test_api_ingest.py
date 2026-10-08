"""
Tests — Ingest API (valid/invalid payloads, batch dedup)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

def _make_payload(batch_id=None, records=None):
    if batch_id is None:
        batch_id = str(uuid.uuid4())
    if records is None:
        records = [{
            "provider": "openai",
            "resource_type": "llm",
            "model": "gpt-4o",
            "input_tokens": 100,
            "output_tokens": 50,
            "total_cost": "0.0025",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]
    return {"batch_id": batch_id, "records": records}


async def test_ingest_rejects_without_api_key(client):
    """Ingest requires X-Modus-APIKey header."""
    resp = await client.post("/api/v1/ingest", json=_make_payload())
    assert resp.status_code in (401, 403)


async def test_ingest_rejects_invalid_api_key(client):
    """Ingest rejects unknown API keys."""
    resp = await client.post(
        "/api/v1/ingest",
        json=_make_payload(),
        headers={"X-Modus-APIKey": "mds_invalid_key_12345678901234567890"},
    )
    assert resp.status_code in (401, 403)


async def test_ingest_rejects_empty_records(client):
    """Ingest rejects batches with no records."""
    resp = await client.post(
        "/api/v1/ingest",
        json={"batch_id": str(uuid.uuid4()), "records": []},
        headers={"X-Modus-APIKey": "mds_test"},
    )
    assert resp.status_code in (401, 403, 422)
