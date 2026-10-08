"""
Modus — Webhook Receivers
==============================
Accepts inbound webhook events from the Nomus regulatory engine
(POST /api/v1/webhooks/nomus). Requests are verified via HMAC-SHA256 using
MODUS_NOMUS_WEBHOOK_SECRET (falling back to MODUS_NOMUS_API_KEY).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging

from fastapi import APIRouter, Request, Response, status

from orchestrator.core.config import settings

logger = logging.getLogger(__name__)

webhook_router = APIRouter()

# Track background tasks to prevent garbage collection of fire-and-forget coroutines
# and ensure exceptions are logged rather than silently swallowed.
_background_tasks: set[asyncio.Task] = set()


# ─── Nomus Webhook Receiver ─────────────────────────────────────────────


def _verify_nomus_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Verify HMAC-SHA256 signature from Nomus."""
    if not secret or not signature:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected.lower(), signature.lower())


@webhook_router.post(
    "/webhooks/nomus",
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
async def receive_nomus_webhook(request: Request) -> Response:
    """Receive webhook events from Nomus Regulatory Engine.

    Supported events:
      - rules.updated   -> Triggers immediate policy re-sync
      - rules.error     -> Logs the error (no action needed)
      - scout.signal_promoted -> Triggers re-sync for new regulatory signal

    Verifies HMAC-SHA256 signature using MODUS_NOMUS_API_KEY.
    """
    secret = settings.nomus_webhook_secret or settings.nomus_api_key
    if not secret:
        logger.warning("Nomus webhook received but neither MODUS_NOMUS_WEBHOOK_SECRET nor MODUS_NOMUS_API_KEY configured")
        return Response(
            content='{"error": "nomus webhook secret not configured"}',
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            media_type="application/json",
        )

    # Verify signature
    raw_body = await request.body()
    signature = request.headers.get("X-Nomus-Signature", "")

    if not _verify_nomus_signature(raw_body, signature, secret):
        logger.warning("Nomus webhook signature verification failed")
        return Response(
            content='{"error": "invalid signature"}',
            status_code=status.HTTP_401_UNAUTHORIZED,
            media_type="application/json",
        )

    # Parse payload from raw_body (already read above for signature verification)
    import json as _json
    try:
        payload = _json.loads(raw_body)
    except Exception:
        return Response(
            content='{"error": "invalid JSON payload"}',
            status_code=status.HTTP_400_BAD_REQUEST,
            media_type="application/json",
        )

    event = payload.get("event", "")
    data = payload.get("data", {})
    nomus_event = request.headers.get("X-Nomus-Event", event)

    logger.info(
        "Nomus webhook received",
        extra={"event": nomus_event, "source": data.get("sourceName", "unknown")},
    )

    # Handle events
    if nomus_event in ("rules.updated", "scout.signal_promoted"):
        # Trigger an immediate re-sync of Nomus policies

        task = asyncio.create_task(_background_sync(nomus_event, data))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

        return Response(
            content='{"status": "ok", "action": "sync_triggered"}',
            status_code=status.HTTP_200_OK,
            media_type="application/json",
        )

    if nomus_event == "rules.error":
        logger.warning(
            "Nomus pipeline error reported",
            extra={
                "source": data.get("sourceName"),
                "error": data.get("error", "")[:200],
                "step": data.get("stepReached"),
            },
        )
        return Response(
            content='{"status": "ok", "action": "logged"}',
            status_code=status.HTTP_200_OK,
            media_type="application/json",
        )

    # Unknown event — acknowledge to prevent retries
    logger.debug("Unknown Nomus webhook event: %s", nomus_event)
    return Response(
        content='{"status": "ok", "action": "ignored"}',
        status_code=status.HTTP_200_OK,
        media_type="application/json",
    )


async def _background_sync(event: str, data: dict) -> None:
    """Run Nomus policy sync in the background after webhook receipt."""
    from orchestrator.core.nomus_client import sync_ruleset

    try:
        result = await sync_ruleset()
        logger.info(
            "Nomus policy sync completed (triggered by %s webhook): %s",
            event, "success" if result.get("success") else result.get("error", "unknown"),
        )
    except Exception as exc:
        logger.warning("Background Nomus sync failed: %s", exc)
