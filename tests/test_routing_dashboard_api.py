"""
Tests for Routing Intelligence dashboard API endpoints:
  GET  /routing/summary
  GET  /routing/savings-over-time
  PATCH /routing/fingerprints/{hash}
"""

import pytest

from orchestrator.db.models import RoutingFingerprint, RoutingOutcome

# App UUIDs (routing rows reference apps.id).
TEST_APP = "aaaaaaaa-0000-4000-8000-000000000001"
APP_A = "aaaaaaaa-0000-4000-8000-00000000000a"
APP_B = "aaaaaaaa-0000-4000-8000-00000000000b"
APP_X = "aaaaaaaa-0000-4000-8000-0000000000aa"
APP_Y = "aaaaaaaa-0000-4000-8000-0000000000bb"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_fingerprint(db_session, **overrides):
    """Create a RoutingFingerprint with sensible defaults."""
    defaults = dict(
        app_id=TEST_APP,
        fingerprint_hash="abcdef1234567890",
        system_prompt_hash="sys_hash_001",
        phase="routing",
        observe_call_count=100,
        observe_threshold=100,
        routing_confidence=0.92,
        cheap_model="claude-haiku-4-5-20251001",
        expensive_model="claude-sonnet-4-6",
        cheap_model_agreement_rate=0.94,
        calibration_sample_count=80,
        drift_score=0.5,
        drift_threshold=2.5,
        confidence_decay_factor=1.0,
        allow_routing=True,
        max_misroute_rate=0.05,
        total_routed_calls=500,
        total_escalations=15,
        total_validator_failures=2,
    )
    defaults.update(overrides)
    fp = RoutingFingerprint(**defaults)
    db_session.add(fp)
    return fp


def _make_outcome(db_session, **overrides):
    """Create a RoutingOutcome with sensible defaults."""
    defaults = dict(
        fingerprint_hash="abcdef1234567890",
        app_id=TEST_APP,
        routed_to="cheap",
        cost_saved=0.003,
    )
    defaults.update(overrides)
    outcome = RoutingOutcome(**defaults)
    db_session.add(outcome)
    return outcome


# ── GET /routing/summary ─────────────────────────────────────────────────────


class TestRoutingSummary:
    @pytest.mark.asyncio
    async def test_summary_empty_db(self, client):
        resp = await client.get("/api/v1/routing/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_fingerprints"] == 0
        assert data["routing_active"] == 0
        assert data["total_cost_saved_usd"] == 0.0
        assert data["escalation_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_summary_with_fingerprints(self, client, db_session):
        _make_fingerprint(db_session, fingerprint_hash="fp001", phase="routing")
        _make_fingerprint(db_session, fingerprint_hash="fp002", phase="observe",
                          routing_confidence=0.0, total_routed_calls=0, total_escalations=0)
        _make_fingerprint(db_session, fingerprint_hash="fp003", phase="excluded",
                          routing_confidence=0.4, allow_routing=False,
                          total_routed_calls=50, total_escalations=10)
        _make_fingerprint(db_session, fingerprint_hash="fp004", phase="drift_flagged",
                          routing_confidence=0.6, total_routed_calls=200, total_escalations=8)
        await db_session.commit()

        resp = await client.get("/api/v1/routing/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_fingerprints"] == 4
        assert data["routing_active"] == 1
        assert data["routing_observe"] == 1
        assert data["routing_excluded"] == 1
        assert data["drift_flagged"] == 1

    @pytest.mark.asyncio
    async def test_summary_filter_by_app_id(self, client, db_session):
        _make_fingerprint(db_session, fingerprint_hash="fp_a1", app_id=APP_A)
        _make_fingerprint(db_session, fingerprint_hash="fp_b1", app_id=APP_B)
        await db_session.commit()

        resp = await client.get(f"/api/v1/routing/summary?app_id={APP_A}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_fingerprints"] == 1

    @pytest.mark.asyncio
    async def test_summary_cost_saved(self, client, db_session):
        _make_fingerprint(db_session)
        _make_outcome(db_session, cost_saved=1.50)
        _make_outcome(db_session, cost_saved=2.25)
        await db_session.commit()

        resp = await client.get("/api/v1/routing/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_cost_saved_usd"] == pytest.approx(3.75, abs=0.01)

    @pytest.mark.asyncio
    async def test_summary_escalation_rate(self, client, db_session):
        _make_fingerprint(db_session, total_routed_calls=100, total_escalations=5)
        await db_session.commit()

        resp = await client.get("/api/v1/routing/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["escalation_rate"] == pytest.approx(0.05, abs=0.001)


# ── GET /routing/savings-over-time ───────────────────────────────────────────


class TestSavingsOverTime:
    @pytest.mark.asyncio
    async def test_savings_empty(self, client):
        resp = await client.get("/api/v1/routing/savings-over-time")
        assert resp.status_code == 200
        data = resp.json()
        assert data == []

    @pytest.mark.asyncio
    async def test_savings_returns_daily_rows(self, client, db_session):
        _make_outcome(db_session, routed_to="cheap", cost_saved=1.0)
        _make_outcome(db_session, routed_to="cheap", cost_saved=2.0)
        _make_outcome(db_session, routed_to="escalated", cost_saved=0.0)
        await db_session.commit()

        resp = await client.get("/api/v1/routing/savings-over-time?days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        row = data[0]
        assert "date" in row
        assert "cost_saved_usd" in row
        assert "routed_calls" in row
        assert "escalations" in row
        assert row["cost_saved_usd"] == pytest.approx(3.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_savings_filter_by_app_id(self, client, db_session):
        _make_outcome(db_session, app_id=APP_X, cost_saved=5.0)
        _make_outcome(db_session, app_id=APP_Y, cost_saved=3.0)
        await db_session.commit()

        resp = await client.get(f"/api/v1/routing/savings-over-time?app_id={APP_X}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["cost_saved_usd"] == pytest.approx(5.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_savings_days_param(self, client):
        resp = await client.get("/api/v1/routing/savings-over-time?days=1")
        assert resp.status_code == 200

        resp = await client.get("/api/v1/routing/savings-over-time?days=0")
        assert resp.status_code == 422  # Validation error, ge=1


# ── PATCH /routing/fingerprints/{hash} ───────────────────────────────────────


class TestPatchFingerprint:
    @pytest.mark.asyncio
    async def test_patch_not_found(self, client):
        resp = await client.patch(
            "/api/v1/routing/fingerprints/nonexistent",
            json={"allow_routing": False},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_patch_allow_routing(self, client, db_session):
        _make_fingerprint(db_session, allow_routing=True)
        await db_session.commit()

        resp = await client.patch(
            "/api/v1/routing/fingerprints/abcdef1234567890",
            json={"allow_routing": False},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["allow_routing"] is False

    @pytest.mark.asyncio
    async def test_patch_force_model(self, client, db_session):
        _make_fingerprint(db_session, force_model=None)
        await db_session.commit()

        resp = await client.patch(
            "/api/v1/routing/fingerprints/abcdef1234567890",
            json={"force_model": "claude-sonnet-4-6"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["force_model"] == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_patch_clear_force_model(self, client, db_session):
        _make_fingerprint(db_session, force_model="claude-sonnet-4-6")
        await db_session.commit()

        resp = await client.patch(
            "/api/v1/routing/fingerprints/abcdef1234567890",
            json={"force_model": ""},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["force_model"] is None

    @pytest.mark.asyncio
    async def test_patch_preserves_other_fields(self, client, db_session):
        _make_fingerprint(db_session, phase="routing", routing_confidence=0.92)
        await db_session.commit()

        resp = await client.patch(
            "/api/v1/routing/fingerprints/abcdef1234567890",
            json={"allow_routing": False},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["phase"] == "routing"
        assert data["routing_confidence"] == pytest.approx(0.92, abs=0.01)

    @pytest.mark.asyncio
    async def test_patch_empty_body(self, client, db_session):
        _make_fingerprint(db_session)
        await db_session.commit()

        resp = await client.patch(
            "/api/v1/routing/fingerprints/abcdef1234567890",
            json={},
        )
        assert resp.status_code == 200


# ── Existing endpoint regression ─────────────────────────────────────────────


class TestExistingEndpoints:
    @pytest.mark.asyncio
    async def test_list_fingerprints(self, client, db_session):
        _make_fingerprint(db_session, fingerprint_hash="fp_list_1")
        _make_fingerprint(db_session, fingerprint_hash="fp_list_2")
        await db_session.commit()

        resp = await client.get("/api/v1/routing/fingerprints")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

    @pytest.mark.asyncio
    async def test_get_fingerprint(self, client, db_session):
        _make_fingerprint(db_session, fingerprint_hash="fp_detail_1")
        await db_session.commit()

        resp = await client.get("/api/v1/routing/fingerprints/fp_detail_1")
        assert resp.status_code == 200
        assert resp.json()["fingerprint_hash"] == "fp_detail_1"

    @pytest.mark.asyncio
    async def test_reset_fingerprint(self, client, db_session):
        _make_fingerprint(db_session, phase="routing")
        await db_session.commit()

        resp = await client.post("/api/v1/routing/fingerprints/abcdef1234567890/reset")
        assert resp.status_code == 200
        assert resp.json()["status"] == "reset_to_observe"

    @pytest.mark.asyncio
    async def test_exclude_fingerprint(self, client, db_session):
        _make_fingerprint(db_session)
        await db_session.commit()

        resp = await client.post("/api/v1/routing/fingerprints/abcdef1234567890/exclude")
        assert resp.status_code == 200
        assert resp.json()["status"] == "excluded"
