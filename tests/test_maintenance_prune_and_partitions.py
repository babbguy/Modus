"""Tests for orchestrator.core.maintenance — prune, partitions, callbacks."""
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.core.maintenance import (
    BATCH_DELETE_SIZE,
    notify_app_pause,
    create_incident_ticket,
)


# ── notify_app_pause ─────────────────────────────────────────────────────────

class TestNotifyAppPause:
    @pytest.mark.asyncio
    async def test_success(self):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("orchestrator.core.maintenance.urllib_request.urlopen", return_value=mock_resp):
            result = await notify_app_pause(
                "app1", "TestApp", "http://localhost:9000/pause", "budget exceeded"
            )
            assert result is True

    @pytest.mark.asyncio
    async def test_failure(self):
        with patch(
            "orchestrator.core.maintenance.urllib_request.urlopen",
            side_effect=Exception("Connection refused"),
        ):
            result = await notify_app_pause(
                "app1", "TestApp", "http://localhost:9000/pause", "test"
            )
            assert result is False


# ── create_incident_ticket ───────────────────────────────────────────────────

class TestCreateIncidentTicket:
    @pytest.mark.asyncio
    async def test_success(self):
        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("orchestrator.core.maintenance.urllib_request.urlopen", return_value=mock_resp):
            result = await create_incident_ticket(
                "https://jira.example.com/api/issue",
                {
                    "severity": "critical",
                    "app_id": "app1",
                    "app_name": "TestApp",
                    "team_id": "team1",
                    "metric": "cost",
                    "actual_value": "150.00",
                    "threshold_value": "100.00",
                },
            )
            assert result is True

    @pytest.mark.asyncio
    async def test_failure(self):
        with patch(
            "orchestrator.core.maintenance.urllib_request.urlopen",
            side_effect=Exception("timeout"),
        ):
            result = await create_incident_ticket(
                "https://jira.example.com/api/issue",
                {"severity": "warning"},
            )
            assert result is False


# ── Constants ────────────────────────────────────────────────────────────────

class TestConstants:
    def test_batch_size(self):
        assert BATCH_DELETE_SIZE == 5000
