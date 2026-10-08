"""
Modus — First-Start Policy Wizard API
==========================================
Guides new users through initial org setup and policy creation.

GET  /api/v1/onboarding/status    — check if onboarding is needed
POST /api/v1/onboarding/complete  — mark onboarding as complete
POST /api/v1/onboarding/apply     — apply wizard-generated config
POST /api/v1/onboarding/upload    — upload YAML policy file

"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, _bearer, get_identity
from orchestrator.db.models import (
    App, GovernancePolicy, SystemSetting, Team, Threshold,
)
from orchestrator.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter()


async def _onboarding_auth(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> Optional[Identity]:
    """
    Conditional auth gate for onboarding endpoints.

    Fresh installs (no teams and onboarding not yet completed) allow
    unauthenticated access so the first administrator can complete initial
    setup. Once onboarding has been completed or any teams exist, the caller
    must authenticate like any other API request (get_identity raises 401
    otherwise).
    """
    team_count = (await db.execute(
        select(func.count(Team.id)).where(Team.deleted_at.is_(None))
    )).scalar_one()

    completed = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == "onboarding.completed_at")
    )).scalar_one_or_none()

    if team_count == 0 and completed is None:
        # Fresh install — allow unauthenticated access
        return None

    # System is already set up — require authentication.
    return await get_identity(request, credentials)

# ── Settings key used to persist onboarding completion ────────────────────────
_ONBOARDING_KEY = "onboarding.completed_at"


# ── Response / request schemas ────────────────────────────────────────────────

class OnboardingStatus(BaseModel):
    needs_onboarding: bool
    has_teams: bool
    has_apps: bool
    has_policies: bool
    has_thresholds: bool
    completed_at: Optional[str] = None


class TeamInput(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    slug: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9\-_]*$")


class PolicyInput(BaseModel):
    name: str
    type: str = Field(..., alias="type")
    scope: str = "team"
    effect: str = "deny"
    config: dict = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class ApplyRequest(BaseModel):
    org_name: str = Field(..., min_length=1, max_length=256)
    first_team: TeamInput
    budget_daily: Decimal = Field(default=Decimal("200"), ge=0)
    budget_monthly: Decimal = Field(default=Decimal("5000"), ge=0)
    policy_mode: str = "managed"  # "managed" | "manual"
    policies: list[PolicyInput] = Field(default_factory=list)


class UploadedPolicy(BaseModel):
    name: str
    type: str
    scope: str
    effect: str
    config: dict


class UploadResponse(BaseModel):
    policies: list[UploadedPolicy]
    count: int


class ApplyResponse(BaseModel):
    team_id: str
    policies_created: int
    thresholds_created: int
    message: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_yaml_simple(raw: str) -> dict:
    """
    Minimal YAML-subset parser for policy upload files.

    Handles the specific structure expected by the onboarding wizard:
    a top-level ``policies:`` key containing a list of mappings with
    scalar and one-level nested dict values.

    Falls back to PyYAML if available; otherwise uses a basic
    line-by-line parser that covers the documented policy schema.
    """
    try:
        import yaml  # type: ignore[import-untyped]
        return yaml.safe_load(raw) or {}
    except ImportError:
        pass

    # ── stdlib-only fallback: covers the documented policy YAML shape ─────
    result: dict = {}
    lines = raw.splitlines()
    current_list: list[dict] | None = None
    current_item: dict | None = None
    current_nested: dict | None = None

    for line in lines:
        stripped = line.rstrip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(line.lstrip())

        # Top-level key (e.g. "policies:")
        if indent == 0 and stripped.endswith(":"):
            key = stripped[:-1].strip()
            current_list = []
            result[key] = current_list
            current_item = None
            current_nested = None
            continue

        # List item start (e.g. "  - name: ...")
        list_match = re.match(r"^\s+-\s+(\w+):\s*(.*)", stripped)
        if list_match and current_list is not None:
            current_item = {}
            current_list.append(current_item)
            current_nested = None
            k, v = list_match.group(1), list_match.group(2).strip()
            current_item[k] = _yaml_scalar(v) if v else None
            continue

        # Nested dict key under a list item (e.g. "    config:")
        kv_match = re.match(r"^\s+(\w+):\s*(.*)", stripped)
        if kv_match and current_item is not None:
            k, v = kv_match.group(1), kv_match.group(2).strip()
            if not v:
                # Start of a nested dict
                current_nested = {}
                current_item[k] = current_nested
            elif current_nested is not None and indent >= 6:
                # Value inside nested dict
                current_nested[k] = _yaml_scalar(v)
            else:
                current_nested = None
                current_item[k] = _yaml_scalar(v)

    return result


def _yaml_scalar(v: str):
    """Convert a YAML scalar string to a Python primitive."""
    if not v:
        return None
    # Strip quotes
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    low = v.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low == "null" or low == "~":
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/onboarding/status", response_model=OnboardingStatus, tags=["onboarding"])
async def onboarding_status(
    _auth: Optional[Identity] = Depends(_onboarding_auth),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> OnboardingStatus:
    """Check whether the first-start onboarding wizard should be shown."""

    team_count = (await db.execute(
        select(func.count(Team.id)).where(Team.deleted_at.is_(None))
    )).scalar_one()

    app_count = (await db.execute(
        select(func.count(App.id)).where(App.deleted_at.is_(None))
    )).scalar_one()

    policy_count = (await db.execute(
        select(func.count(GovernancePolicy.id))
    )).scalar_one()

    threshold_count = (await db.execute(
        select(func.count(Threshold.id))
    )).scalar_one()

    # Check for completion flag in system_settings
    completed_at: str | None = None
    try:
        setting = (await db.execute(
            select(SystemSetting).where(SystemSetting.key == _ONBOARDING_KEY)
        )).scalar_one_or_none()
        if setting is not None:
            completed_at = setting.value
    except Exception:
        pass

    has_teams = team_count > 0
    has_apps = app_count > 0
    has_policies = policy_count > 0
    has_thresholds = threshold_count > 0

    needs_onboarding = (
        not has_teams
        and not has_apps
        and not has_policies
        and not has_thresholds
        and completed_at is None
    )

    return OnboardingStatus(
        needs_onboarding=needs_onboarding,
        has_teams=has_teams,
        has_apps=has_apps,
        has_policies=has_policies,
        has_thresholds=has_thresholds,
        completed_at=completed_at,
    )


@router.post("/onboarding/apply", response_model=ApplyResponse, tags=["onboarding"])
async def onboarding_apply(
    body: ApplyRequest,
    _auth: Optional[Identity] = Depends(_onboarding_auth),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> ApplyResponse:
    """
    Apply the wizard's output: create team, default policies, and thresholds.
    """

    # ── 1. Create team ────────────────────────────────────────────────────────
    existing = (await db.execute(
        select(Team).where(Team.slug == body.first_team.slug, Team.deleted_at.is_(None))
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail=f"Team slug '{body.first_team.slug}' already exists.")

    team = Team(
        slug=body.first_team.slug,
        name=body.first_team.name,
        description=f"Created by onboarding wizard for {body.org_name}",
        max_budget_usd=body.budget_daily,
        budget_duration="daily",
        budget_monthly_usd=body.budget_monthly,
    )
    db.add(team)
    await db.flush()  # populate team.id

    # ── 2. Build policy list ──────────────────────────────────────────────────
    policies_to_create: list[dict] = []

    if body.policy_mode == "managed":
        # Auto-generated sensible defaults
        policies_to_create = [
            {
                "name": "Daily Budget Cap",
                "policy_type": "budget_cap",
                "scope": "team",
                "effect": "deny",
                "config": {"cap_usd": str(body.budget_daily), "period": "daily"},
                "priority": 10,
            },
            {
                "name": "Monthly Budget Cap",
                "policy_type": "budget_cap",
                "scope": "team",
                "effect": "deny",
                "config": {"cap_usd": str(body.budget_monthly), "period": "monthly"},
                "priority": 20,
            },
            {
                "name": "Rate Limit — 1000/hr",
                "policy_type": "rate_limit",
                "scope": "team",
                "effect": "throttle",
                "config": {"max_calls": 1000, "window_seconds": 3600},
                "priority": 30,
            },
        ]
    elif body.policies:
        # User-provided or YAML-uploaded policies
        for i, p in enumerate(body.policies):
            policies_to_create.append({
                "name": p.name,
                "policy_type": p.type,
                "scope": p.scope,
                "effect": p.effect,
                "config": p.config,
                "priority": (i + 1) * 10,
            })

    policy_count = 0
    for pdata in policies_to_create:
        policy = GovernancePolicy(
            team_id=str(team.id),
            name=pdata["name"],
            scope=pdata["scope"],
            policy_type=pdata["policy_type"],
            effect=pdata["effect"],
            config=pdata["config"],
            priority=pdata["priority"],
            is_active=True,
            created_by="onboarding-wizard",
        )
        db.add(policy)
        policy_count += 1

    # ── 3. Create default thresholds ──────────────────────────────────────────
    # Threshold model uses warning_value (optional) and critical_value (required).
    threshold = Threshold(
        team_id=str(team.id),
        name=f"Daily Cost — warn ${body.budget_daily * Decimal('0.8'):.0f} / critical ${body.budget_daily:.0f}",
        metric="total_cost",
        scope="team",
        period="daily",
        warning_value=body.budget_daily * Decimal("0.8"),
        critical_value=body.budget_daily,
        is_active=True,
    )
    db.add(threshold)
    threshold_count = 1

    await db.commit()

    return ApplyResponse(
        team_id=str(team.id),
        policies_created=policy_count,
        thresholds_created=threshold_count,
        message=f"Onboarding complete. Created team '{body.first_team.name}' with {policy_count} policies and {threshold_count} thresholds.",
    )


@router.post("/onboarding/upload", response_model=UploadResponse, tags=["onboarding"])
async def onboarding_upload(
    file: UploadFile = File(...),
    _auth: Optional[Identity] = Depends(_onboarding_auth),
) -> UploadResponse:
    """
    Parse and validate a YAML policy file. Returns the list of policies
    that would be created (does NOT persist them — the client sends them
    back via POST /apply).
    """
    if file.size and file.size > 256 * 1024:
        raise HTTPException(status_code=413, detail="File too large. Max 256 KB.")

    raw = (await file.read()).decode("utf-8", errors="replace")

    try:
        data = _parse_yaml_simple(raw)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid YAML: {exc}")

    if not isinstance(data, dict) or "policies" not in data:
        raise HTTPException(status_code=422, detail="YAML must have a top-level 'policies' key.")

    policies_raw = data["policies"]
    if not isinstance(policies_raw, list):
        raise HTTPException(status_code=422, detail="'policies' must be a list.")

    valid_types = {
        "budget_cap", "rate_limit", "model_allowlist", "model_denylist",
        "provider_block", "environment_block", "token_cap", "degradation_ladder",
    }
    valid_effects = {"deny", "throttle", "warn"}

    policies: list[UploadedPolicy] = []
    for i, p in enumerate(policies_raw):
        if not isinstance(p, dict):
            raise HTTPException(status_code=422, detail=f"Policy #{i + 1} must be a mapping.")
        name = p.get("name")
        ptype = p.get("type")
        scope = p.get("scope", "team")
        effect = p.get("effect", "deny")
        config = p.get("config", {})

        if not name:
            raise HTTPException(status_code=422, detail=f"Policy #{i + 1} missing 'name'.")
        if ptype not in valid_types:
            raise HTTPException(
                status_code=422,
                detail=f"Policy #{i + 1} has invalid type '{ptype}'. Valid: {sorted(valid_types)}",
            )
        if effect not in valid_effects:
            raise HTTPException(
                status_code=422,
                detail=f"Policy #{i + 1} has invalid effect '{effect}'. Valid: {sorted(valid_effects)}",
            )

        policies.append(UploadedPolicy(
            name=name,
            type=ptype,
            scope=scope,
            effect=effect,
            config=config if isinstance(config, dict) else {},
        ))

    return UploadResponse(policies=policies, count=len(policies))


@router.post("/onboarding/complete", tags=["onboarding"])
async def onboarding_complete(
    _auth: Optional[Identity] = Depends(_onboarding_auth),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> dict:
    """Mark onboarding as complete so the wizard does not show again."""
    now = datetime.now(timezone.utc).isoformat()

    existing = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == _ONBOARDING_KEY)
    )).scalar_one_or_none()

    if existing:
        existing.value = now
        existing.updated_by = "onboarding-wizard"
    else:
        db.add(SystemSetting(
            key=_ONBOARDING_KEY,
            value=now,
            description="Timestamp when the first-start onboarding wizard was completed.",
            updated_by="onboarding-wizard",
        ))

    await db.commit()
    return {"status": "ok", "completed_at": now}
