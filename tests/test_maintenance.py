"""
Tests for orchestrator.core.maintenance — Maintenance tasks.
"""
from __future__ import annotations

import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

import pytest

from orchestrator.core.maintenance import (
    BATCH_DELETE_SIZE,
    notify_app_pause,
    create_incident_ticket,
)


# ── notify_app_pause ─────────────────────────────────────────────────────────


class TestNotifyAppPause:
    @pytest.mark.asyncio
    async def test_successful_pause(self):
        """Test notify_app_pause succeeds with a real HTTP server."""
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                received.append(json.loads(body))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = Thread(target=server.handle_request, daemon=True)
        thread.start()

        result = await notify_app_pause(
            app_id="app-1",
            app_name="Test App",
            pause_url=f"http://127.0.0.1:{port}/pause",
            reason="Budget exceeded",
        )

        thread.join(timeout=5)
        server.server_close()

        assert result is True
        assert len(received) == 1
        assert received[0]["action"] == "pause"
        assert received[0]["app_id"] == "app-1"
        assert received[0]["reason"] == "Budget exceeded"

    @pytest.mark.asyncio
    async def test_failed_pause(self):
        """Test notify_app_pause returns False on connection failure."""
        result = await notify_app_pause(
            app_id="app-1",
            app_name="Test App",
            pause_url="http://127.0.0.1:1/pause",
            reason="test",
        )
        assert result is False


# ── create_incident_ticket ───────────────────────────────────────────────────


class TestCreateIncidentTicket:
    @pytest.mark.asyncio
    async def test_successful_ticket(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                received.append(json.loads(body))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = Thread(target=server.handle_request, daemon=True)
        thread.start()

        result = await create_incident_ticket(
            webhook_url=f"http://127.0.0.1:{port}/webhook",
            alert_data={
                "severity": "critical",
                "app_id": "app-1",
                "app_name": "Test App",
                "team_id": "team-1",
                "metric": "total_cost",
                "actual_value": "150.00",
                "threshold_value": "100.00",
            },
        )

        thread.join(timeout=5)
        server.server_close()

        assert result is True
        assert len(received) == 1
        assert received[0]["source"] == "modus"
        assert received[0]["event_type"] == "threshold_breach"

    @pytest.mark.asyncio
    async def test_failed_ticket(self):
        result = await create_incident_ticket(
            webhook_url="http://127.0.0.1:1/webhook",
            alert_data={"severity": "critical"},
        )
        assert result is False


# ── BATCH_DELETE_SIZE ────────────────────────────────────────────────────────


class TestBatchDeleteSize:
    def test_batch_size_is_positive(self):
        assert BATCH_DELETE_SIZE > 0
        assert BATCH_DELETE_SIZE == 5000
