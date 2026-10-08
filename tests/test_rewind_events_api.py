"""Rewind events round-trip in the shape the SDK sends (a list of actions)."""
from __future__ import annotations


async def test_rewind_event_round_trip(client, registered_app):
    actions = [
        {"tool_name": "send_email", "success": True, "error": None},
        {"tool_name": "write_file", "success": False, "error": "permission denied"},
    ]
    created = await client.post("/api/v1/governance/rewind-event", json={
        "session_id": "sess-rewind-1",
        "app_id": registered_app["app_uuid"],
        "team_id": registered_app["team_id"],
        "trigger_reason": "circuit_breaker",
        "actions_rolled_back": actions,
        "failure_context": {"model": "claude-sonnet-5-5"},
    })
    assert created.status_code == 201, created.text

    listed = await client.get("/api/v1/governance/rewind-events?limit=20")
    assert listed.status_code == 200, listed.text
    events = [e for e in listed.json() if e["session_id"] == "sess-rewind-1"]
    assert len(events) == 1
    assert events[0]["actions_rolled_back"] == actions
