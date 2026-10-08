"""
Tests — Diagnostics API and helper functions.
Covers: format_uptime, collect_configuration, generate_recommendations,
        scan endpoint, download endpoint, safe helper, memory/sqlite helpers.
"""
from __future__ import annotations

# ── Unit tests for helpers ───────────────────────────────────────────────────

def test_format_uptime_minutes_only():
    from orchestrator.api.diagnostics import _format_uptime
    assert _format_uptime(120) == "2m"


def test_format_uptime_hours():
    from orchestrator.api.diagnostics import _format_uptime
    assert _format_uptime(7260) == "2h 1m"


def test_format_uptime_days():
    from orchestrator.api.diagnostics import _format_uptime
    result = _format_uptime(90061)
    assert "1d" in result
    assert "1h" in result


def test_format_uptime_zero():
    from orchestrator.api.diagnostics import _format_uptime
    assert _format_uptime(0) == "0m"


def test_safe_success():
    from orchestrator.api.diagnostics import _safe
    assert _safe(lambda: 42) == 42


def test_safe_exception():
    from orchestrator.api.diagnostics import _safe
    assert _safe(lambda: 1 / 0, "fallback") == "fallback"


def test_safe_default_none():
    from orchestrator.api.diagnostics import _safe
    assert _safe(lambda: 1 / 0) is None


def test_collect_configuration():
    from orchestrator.api.diagnostics import _collect_configuration
    config = _collect_configuration()
    assert "auth_mode" in config
    assert "environment" in config
    assert "debug" in config
    assert "database_type" in config
    assert isinstance(config["master_api_key_set"], bool)


def test_collect_recent_errors_no_crash():
    from orchestrator.api.diagnostics import _collect_recent_errors
    errors = _collect_recent_errors()
    assert isinstance(errors, list)


def test_get_memory_mb_no_crash():
    from orchestrator.api.diagnostics import _get_memory_mb
    result = _get_memory_mb()
    # Returns float or None — either is fine
    assert result is None or isinstance(result, float)


def test_get_sqlite_size_no_crash():
    from orchestrator.api.diagnostics import _get_sqlite_size
    result = _get_sqlite_size()
    # In test mode with in-memory sqlite, likely None
    assert result is None or isinstance(result, float)


# ── Recommendations engine ───────────────────────────────────────────────────

def test_recommendations_all_clear():
    from orchestrator.api.diagnostics import _generate_recommendations
    recs = _generate_recommendations(
        system={"memory_mb": 100},
        database={"status": "ok", "db_file_size_mb": 50},
        tasks={"write_queue": {"depth": 0, "over_pressure": False}},
        api_health={"db_status": "ok", "db_ping_ms": 5},
        connections=[{"status": "ok"}],
        errors=[],
    )
    assert len(recs) == 1
    assert recs[0]["severity"] == "info"
    assert "normally" in recs[0]["message"]


def test_recommendations_write_queue_pressure():
    from orchestrator.api.diagnostics import _generate_recommendations
    recs = _generate_recommendations(
        system={"memory_mb": 100},
        database={"status": "ok"},
        tasks={"write_queue": {"depth": 5000, "over_pressure": True}},
        api_health={"db_status": "ok"},
        connections=[],
        errors=[],
    )
    sev = [r["severity"] for r in recs]
    assert "warning" in sev


def test_recommendations_write_queue_elevated():
    from orchestrator.api.diagnostics import _generate_recommendations
    recs = _generate_recommendations(
        system={"memory_mb": 100},
        database={"status": "ok"},
        tasks={"write_queue": {"depth": 2000, "over_pressure": False}},
        api_health={"db_status": "ok"},
        connections=[],
        errors=[],
    )
    msgs = " ".join(r["message"] for r in recs)
    assert "elevated" in msgs


def test_recommendations_db_error():
    from orchestrator.api.diagnostics import _generate_recommendations
    recs = _generate_recommendations(
        system={"memory_mb": 100},
        database={"status": "error"},
        tasks={"write_queue": {"depth": 0}},
        api_health={"db_status": "ok"},
        connections=[],
        errors=[],
    )
    sev = [r["severity"] for r in recs]
    assert "error" in sev


def test_recommendations_large_db():
    from orchestrator.api.diagnostics import _generate_recommendations
    recs = _generate_recommendations(
        system={"memory_mb": 100},
        database={"status": "ok", "db_file_size_mb": 2000},
        tasks={"write_queue": {"depth": 0}},
        api_health={"db_status": "ok"},
        connections=[],
        errors=[],
    )
    msgs = " ".join(r["message"] for r in recs)
    assert "compaction" in msgs or "2000" in msgs


def test_recommendations_api_error():
    from orchestrator.api.diagnostics import _generate_recommendations
    recs = _generate_recommendations(
        system={"memory_mb": 100},
        database={"status": "ok"},
        tasks={"write_queue": {"depth": 0}},
        api_health={"db_status": "error"},
        connections=[],
        errors=[],
    )
    sev = [r["severity"] for r in recs]
    assert "error" in sev


def test_recommendations_slow_db_ping():
    from orchestrator.api.diagnostics import _generate_recommendations
    recs = _generate_recommendations(
        system={"memory_mb": 100},
        database={"status": "ok"},
        tasks={"write_queue": {"depth": 0}},
        api_health={"db_status": "ok", "db_ping_ms": 200},
        connections=[],
        errors=[],
    )
    msgs = " ".join(r["message"] for r in recs)
    assert "200" in msgs


def test_recommendations_high_memory():
    from orchestrator.api.diagnostics import _generate_recommendations
    recs = _generate_recommendations(
        system={"memory_mb": 800},
        database={"status": "ok"},
        tasks={"write_queue": {"depth": 0}},
        api_health={"db_status": "ok"},
        connections=[],
        errors=[],
    )
    msgs = " ".join(r["message"] for r in recs)
    assert "800" in msgs


def test_recommendations_connection_errors():
    from orchestrator.api.diagnostics import _generate_recommendations
    recs = _generate_recommendations(
        system={"memory_mb": 100},
        database={"status": "ok"},
        tasks={"write_queue": {"depth": 0}},
        api_health={"db_status": "ok"},
        connections=[{"status": "error"}, {"status": "ok"}],
        errors=[],
    )
    msgs = " ".join(r["message"] for r in recs)
    assert "1 connection" in msgs


def test_recommendations_log_errors():
    from orchestrator.api.diagnostics import _generate_recommendations
    recs = _generate_recommendations(
        system={"memory_mb": 100},
        database={"status": "ok"},
        tasks={"write_queue": {"depth": 0}},
        api_health={"db_status": "ok"},
        connections=[],
        errors=[{"level": "ERROR", "message": "test"}],
    )
    msgs = " ".join(r["message"] for r in recs)
    assert "ERROR" in msgs


# ── Async collector tests ────────────────────────────────────────────────────

async def test_collect_system_info():
    from orchestrator.api.diagnostics import _collect_system_info
    info = await _collect_system_info()
    assert "python_version" in info
    assert "platform" in info
    assert "hostname" in info
    assert "uptime_seconds" in info
    assert "uptime_human" in info


async def test_collect_background_tasks():
    from orchestrator.api.diagnostics import _collect_background_tasks
    result = await _collect_background_tasks()
    assert "write_queue" in result
    assert "aggregation" in result
    assert "governance" in result


# ── Endpoint tests ───────────────────────────────────────────────────────────

async def test_download_no_scan(client):
    resp = await client.get("/api/v1/admin/diagnostics/download")
    assert resp.status_code == 404
