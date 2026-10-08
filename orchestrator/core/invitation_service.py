"""
Modus — Invitation Service
====================================
Pluggable notification backend for sending user invitations.

Providers:
  - EmailProvider:  SMTP-based (configurable: SendGrid, SES, Mailgun, etc.)
  - SlackProvider:  Slack incoming webhook
  - TeamsProvider:  Microsoft Teams incoming webhook

Configuration via environment variables:
  MODUS_INVITE_EMAIL_SMTP_HOST, _PORT, _USER, _PASS, _FROM
  MODUS_INVITE_SLACK_WEBHOOK_URL
  MODUS_INVITE_TEAMS_WEBHOOK_URL

Admins select the channel per-invitation. The service routes to the
appropriate provider. New providers can be added by implementing
InvitationProvider and registering in the PROVIDERS dict.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class InvitationMessage:
    """Data needed to send an invitation."""
    recipient_email: str
    recipient_name: str
    inviter_name: str
    role_name: str
    team_name: str | None
    invite_url: str
    expires_in_days: int = 7


class InvitationProvider(ABC):
    """Base class for invitation delivery providers."""

    @abstractmethod
    async def send(self, message: InvitationMessage) -> bool:
        """Send the invitation. Returns True on success."""
        ...

    @abstractmethod
    def is_configured(self) -> bool:
        """Check if this provider has the required configuration."""
        ...


class EmailProvider(InvitationProvider):
    """
    SMTP email provider. Supports any SMTP server.

    Configure via env vars:
      MODUS_INVITE_EMAIL_SMTP_HOST
      MODUS_INVITE_EMAIL_SMTP_PORT (default: 587)
      MODUS_INVITE_EMAIL_SMTP_USER
      MODUS_INVITE_EMAIL_SMTP_PASS
      MODUS_INVITE_EMAIL_FROM (default: noreply@localhost)
    """

    def is_configured(self) -> bool:
        return bool(os.environ.get("MODUS_INVITE_EMAIL_SMTP_HOST"))

    async def send(self, message: InvitationMessage) -> bool:
        import aiosmtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        host = os.environ.get("MODUS_INVITE_EMAIL_SMTP_HOST", "")
        port = int(os.environ.get("MODUS_INVITE_EMAIL_SMTP_PORT", "587"))
        user = os.environ.get("MODUS_INVITE_EMAIL_SMTP_USER", "")
        password = os.environ.get("MODUS_INVITE_EMAIL_SMTP_PASS", "")
        from_addr = os.environ.get("MODUS_INVITE_EMAIL_FROM", "noreply@localhost")

        team_context = f" to the {message.team_name} team" if message.team_name else ""

        subject = f"You've been invited to Modus{team_context}"

        html_body = f"""
        <div style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
            <h2>You're invited to Modus</h2>
            <p>{message.inviter_name} has invited you{team_context}
               as <strong>{message.role_name}</strong>.</p>
            <p>
                <a href="{message.invite_url}"
                   style="display: inline-block; padding: 12px 24px;
                          background: #2563eb; color: white; text-decoration: none;
                          border-radius: 6px; font-weight: 600;">
                    Accept Invitation
                </a>
            </p>
            <p style="color: #6b7280; font-size: 14px;">
                This invitation expires in {message.expires_in_days} days.
            </p>
        </div>
        """

        text_body = (
            f"{message.inviter_name} has invited you{team_context} "
            f"as {message.role_name}.\n\n"
            f"Accept: {message.invite_url}\n\n"
            f"Expires in {message.expires_in_days} days."
        )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = message.recipient_email
        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        try:
            await aiosmtplib.send(
                msg,
                hostname=host,
                port=port,
                username=user or None,
                password=password or None,
                use_tls=port == 465,
                start_tls=port == 587,
            )
            logger.info("Invitation email sent to %s", message.recipient_email)
            return True
        except Exception as exc:
            logger.error("Failed to send invitation email to %s: %s",
                         message.recipient_email, exc)
            return False


class SlackProvider(InvitationProvider):
    """
    Slack incoming webhook provider.

    Configure via env var:
      MODUS_INVITE_SLACK_WEBHOOK_URL
    """

    def is_configured(self) -> bool:
        return bool(os.environ.get("MODUS_INVITE_SLACK_WEBHOOK_URL"))

    async def send(self, message: InvitationMessage) -> bool:
        import httpx

        webhook_url = os.environ.get("MODUS_INVITE_SLACK_WEBHOOK_URL", "")
        team_context = f" to *{message.team_name}*" if message.team_name else ""

        payload = {
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f":envelope: *Modus Invitation*\n\n"
                            f"{message.inviter_name} has invited "
                            f"*{message.recipient_name}* ({message.recipient_email})"
                            f"{team_context} as *{message.role_name}*."
                        ),
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Accept Invitation"},
                            "url": message.invite_url,
                            "style": "primary",
                        }
                    ],
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"Expires in {message.expires_in_days} days",
                        }
                    ],
                },
            ]
        }

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(webhook_url, json=payload, timeout=10)
                resp.raise_for_status()
            logger.info("Invitation sent via Slack for %s", message.recipient_email)
            return True
        except Exception as exc:
            logger.error("Failed to send Slack invitation for %s: %s",
                         message.recipient_email, exc)
            return False


class TeamsProvider(InvitationProvider):
    """
    Microsoft Teams incoming webhook provider.

    Configure via env var:
      MODUS_INVITE_TEAMS_WEBHOOK_URL
    """

    def is_configured(self) -> bool:
        return bool(os.environ.get("MODUS_INVITE_TEAMS_WEBHOOK_URL"))

    async def send(self, message: InvitationMessage) -> bool:
        import httpx

        webhook_url = os.environ.get("MODUS_INVITE_TEAMS_WEBHOOK_URL", "")
        team_context = f" to **{message.team_name}**" if message.team_name else ""

        payload = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "summary": "Modus Invitation",
            "themeColor": "2563eb",
            "title": "Modus Invitation",
            "sections": [
                {
                    "activityTitle": f"Invitation from {message.inviter_name}",
                    "text": (
                        f"{message.inviter_name} has invited "
                        f"**{message.recipient_name}** ({message.recipient_email})"
                        f"{team_context} as **{message.role_name}**.\n\n"
                        f"Expires in {message.expires_in_days} days."
                    ),
                }
            ],
            "potentialAction": [
                {
                    "@type": "OpenUri",
                    "name": "Accept Invitation",
                    "targets": [{"os": "default", "uri": message.invite_url}],
                }
            ],
        }

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(webhook_url, json=payload, timeout=10)
                resp.raise_for_status()
            logger.info("Invitation sent via Teams for %s", message.recipient_email)
            return True
        except Exception as exc:
            logger.error("Failed to send Teams invitation for %s: %s",
                         message.recipient_email, exc)
            return False


# ── Provider registry ─────────────────────────────────────────────────────────

PROVIDERS: dict[str, InvitationProvider] = {
    "email": EmailProvider(),
    "slack": SlackProvider(),
    "teams": TeamsProvider(),
}


def get_provider(channel: str) -> InvitationProvider:
    """Get the provider for a delivery channel."""
    provider = PROVIDERS.get(channel)
    if provider is None:
        raise ValueError(f"Unknown invitation channel: {channel!r}. "
                         f"Available: {list(PROVIDERS.keys())}")
    return provider


def get_configured_channels() -> list[str]:
    """Return list of channels that are properly configured."""
    return [name for name, provider in PROVIDERS.items() if provider.is_configured()]
