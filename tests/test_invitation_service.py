"""
Tests — Invitation Service
Covers: InvitationMessage, provider registry, is_configured, get_provider,
        get_configured_channels.
"""
from __future__ import annotations

import os


def test_invitation_message_creation():
    from orchestrator.core.invitation_service import InvitationMessage
    msg = InvitationMessage(
        recipient_email="alice@example.com",
        recipient_name="Alice",
        inviter_name="Bob",
        role_name="Team Admin",
        team_name="Platform",
        invite_url="https://modus.example.com/invite/abc123",
        expires_in_days=7,
    )
    assert msg.recipient_email == "alice@example.com"
    assert msg.expires_in_days == 7


def test_invitation_message_defaults():
    from orchestrator.core.invitation_service import InvitationMessage
    msg = InvitationMessage(
        recipient_email="x@x.com",
        recipient_name="X",
        inviter_name="Y",
        role_name="Member",
        team_name=None,
        invite_url="https://example.com",
    )
    assert msg.expires_in_days == 7
    assert msg.team_name is None


def test_email_provider_not_configured():
    from orchestrator.core.invitation_service import EmailProvider
    # Without env var, should not be configured
    old = os.environ.pop("MODUS_INVITE_EMAIL_SMTP_HOST", None)
    try:
        p = EmailProvider()
        assert p.is_configured() is False
    finally:
        if old is not None:
            os.environ["MODUS_INVITE_EMAIL_SMTP_HOST"] = old


def test_email_provider_configured():
    from orchestrator.core.invitation_service import EmailProvider
    os.environ["MODUS_INVITE_EMAIL_SMTP_HOST"] = "smtp.test.com"
    try:
        p = EmailProvider()
        assert p.is_configured() is True
    finally:
        del os.environ["MODUS_INVITE_EMAIL_SMTP_HOST"]


def test_slack_provider_not_configured():
    from orchestrator.core.invitation_service import SlackProvider
    old = os.environ.pop("MODUS_INVITE_SLACK_WEBHOOK_URL", None)
    try:
        p = SlackProvider()
        assert p.is_configured() is False
    finally:
        if old is not None:
            os.environ["MODUS_INVITE_SLACK_WEBHOOK_URL"] = old


def test_slack_provider_configured():
    from orchestrator.core.invitation_service import SlackProvider
    os.environ["MODUS_INVITE_SLACK_WEBHOOK_URL"] = "https://hooks.slack.com/test"
    try:
        p = SlackProvider()
        assert p.is_configured() is True
    finally:
        del os.environ["MODUS_INVITE_SLACK_WEBHOOK_URL"]


def test_teams_provider_not_configured():
    from orchestrator.core.invitation_service import TeamsProvider
    old = os.environ.pop("MODUS_INVITE_TEAMS_WEBHOOK_URL", None)
    try:
        p = TeamsProvider()
        assert p.is_configured() is False
    finally:
        if old is not None:
            os.environ["MODUS_INVITE_TEAMS_WEBHOOK_URL"] = old


def test_teams_provider_configured():
    from orchestrator.core.invitation_service import TeamsProvider
    os.environ["MODUS_INVITE_TEAMS_WEBHOOK_URL"] = "https://teams.webhook.test"
    try:
        p = TeamsProvider()
        assert p.is_configured() is True
    finally:
        del os.environ["MODUS_INVITE_TEAMS_WEBHOOK_URL"]


def test_get_provider_valid():
    from orchestrator.core.invitation_service import get_provider, EmailProvider
    p = get_provider("email")
    assert isinstance(p, EmailProvider)


def test_get_provider_invalid():
    from orchestrator.core.invitation_service import get_provider
    import pytest
    with pytest.raises(ValueError, match="Unknown invitation channel"):
        get_provider("carrier_pigeon")


def test_get_configured_channels_none():
    from orchestrator.core.invitation_service import get_configured_channels
    # Remove all env vars
    saved = {}
    for key in ["MODUS_INVITE_EMAIL_SMTP_HOST", "MODUS_INVITE_SLACK_WEBHOOK_URL",
                "MODUS_INVITE_TEAMS_WEBHOOK_URL"]:
        saved[key] = os.environ.pop(key, None)
    try:
        channels = get_configured_channels()
        assert isinstance(channels, list)
        assert len(channels) == 0
    finally:
        for key, val in saved.items():
            if val is not None:
                os.environ[key] = val


def test_get_configured_channels_some():
    from orchestrator.core.invitation_service import get_configured_channels
    saved = {}
    for key in ["MODUS_INVITE_EMAIL_SMTP_HOST", "MODUS_INVITE_SLACK_WEBHOOK_URL",
                "MODUS_INVITE_TEAMS_WEBHOOK_URL"]:
        saved[key] = os.environ.pop(key, None)

    os.environ["MODUS_INVITE_SLACK_WEBHOOK_URL"] = "https://hooks.slack.com/test"
    try:
        channels = get_configured_channels()
        assert "slack" in channels
        assert "email" not in channels
    finally:
        del os.environ["MODUS_INVITE_SLACK_WEBHOOK_URL"]
        for key, val in saved.items():
            if val is not None:
                os.environ[key] = val


def test_providers_registry():
    from orchestrator.core.invitation_service import PROVIDERS
    assert "email" in PROVIDERS
    assert "slack" in PROVIDERS
    assert "teams" in PROVIDERS
    assert len(PROVIDERS) == 3
