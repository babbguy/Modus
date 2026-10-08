"""
Modus — Pricing API Router
====================================
Full CRUD for global pricing models and per-app / per-team overrides.

GET    /api/v1/pricing                          — list all pricing models (global table)
GET    /api/v1/pricing/overrides                — list active overrides (team-scoped)
POST   /api/v1/pricing/overrides                — create an override
PUT    /api/v1/pricing/overrides/{id}           — update an override
DELETE /api/v1/pricing/overrides/{id}           — delete an override
POST   /api/v1/pricing/overrides/bulk-import    — import CSV/JSON override set
GET    /api/v1/pricing/effective/{provider}/{model} — resolved price (override wins)
"""
from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity
from orchestrator.db.models import AuditLog, PricingModel, PricingOverride
from orchestrator.db.session import get_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pricing", tags=["pricing"])


# ── Response / Request schemas ─────────────────────────────────────────────────

class GlobalPricingEntry(BaseModel):
    """One row from the global pricing_models table."""
    id: str
    provider: str
    model: str
    resource_type: str
    input_cost_per_1k: Optional[Decimal]
    output_cost_per_1k: Optional[Decimal]
    flat_cost_per_call: Optional[Decimal]
    effective_from: datetime
    source: Optional[str]


class PricingOverrideResponse(BaseModel):
    id: str
    team_id: Optional[str]
    app_id: Optional[str]
    provider: str
    model: str
    resource_type: str
    input_cost_per_1k: Optional[Decimal]
    output_cost_per_1k: Optional[Decimal]
    per_unit_cost: Optional[Decimal]
    unit_label: Optional[str]
    override_reason: Optional[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime


class PricingOverrideCreate(BaseModel):
    """Create a pricing override for a team or specific app."""
    team_id: Optional[str] = Field(
        None,
        description="Team UUID. Leave null to apply to all teams (platform admin only).",
    )
    app_id: Optional[str] = Field(
        None,
        description="App UUID. Overrides at app level take precedence over team level.",
    )
    provider: str = Field(..., min_length=1, max_length=64)
    model: str = Field(..., min_length=1, max_length=256)
    resource_type: str = Field("llm_call", max_length=64)
    input_cost_per_1k: Optional[Decimal] = Field(
        None,
        ge=0,
        description="Cost per 1,000 input tokens in USD.",
    )
    output_cost_per_1k: Optional[Decimal] = Field(
        None,
        ge=0,
        description="Cost per 1,000 output tokens in USD.",
    )
    per_unit_cost: Optional[Decimal] = Field(
        None,
        ge=0,
        description="Cost per unit for non-token resources (queries, API calls, etc).",
    )
    unit_label: Optional[str] = Field(None, max_length=64)
    override_reason: Optional[str] = Field(
        None,
        max_length=512,
        description="Why this override exists (e.g. 'Enterprise discount Q1 2025').",
    )


class PricingOverrideUpdate(BaseModel):
    input_cost_per_1k: Optional[Decimal] = Field(None, ge=0)
    output_cost_per_1k: Optional[Decimal] = Field(None, ge=0)
    per_unit_cost: Optional[Decimal] = Field(None, ge=0)
    unit_label: Optional[str] = Field(None, max_length=64)
    override_reason: Optional[str] = Field(None, max_length=512)
    is_active: Optional[bool] = None


class EffectivePrice(BaseModel):
    provider: str
    model: str
    resource_type: str
    input_cost_per_1k: Optional[Decimal]
    output_cost_per_1k: Optional[Decimal]
    per_unit_cost: Optional[Decimal]
    source: str  # "override_app" | "override_team" | "global" | "not_found"
    override_id: Optional[str]


class BulkImportEntry(BaseModel):
    provider: str
    model: str
    resource_type: str = "llm_call"
    input_cost_per_1k: Optional[Decimal] = None
    output_cost_per_1k: Optional[Decimal] = None
    per_unit_cost: Optional[Decimal] = None
    unit_label: Optional[str] = None
    override_reason: Optional[str] = None


class BulkImportRequest(BaseModel):
    team_id: Optional[str] = None
    app_id: Optional[str] = None
    entries: list[BulkImportEntry] = Field(..., min_length=1, max_length=500)
    replace_existing: bool = Field(
        False,
        description="If true, deactivate existing overrides for this scope before importing.",
    )


class BulkImportResponse(BaseModel):
    created: int
    skipped: int
    errors: list[str]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _override_to_response(o: PricingOverride) -> PricingOverrideResponse:
    return PricingOverrideResponse(
        id=str(o.id),
        team_id=str(o.team_id) if o.team_id else None,
        app_id=str(o.app_id) if o.app_id else None,
        provider=o.provider,
        model=o.model,
        resource_type=o.resource_type,
        input_cost_per_1k=o.input_cost_per_1k,
        output_cost_per_1k=o.output_cost_per_1k,
        per_unit_cost=o.per_unit_cost,
        unit_label=o.unit_label,
        override_reason=o.override_reason,
        is_active=o.is_active,
        created_at=o.created_at,
        updated_at=o.updated_at,
    )


# ── Global pricing table ───────────────────────────────────────────────────────

@router.get("", response_model=list[GlobalPricingEntry], summary="List global pricing models")
async def list_pricing_models(
    provider: Optional[str] = None,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> list[GlobalPricingEntry]:
    """
    Return the global pricing table (bundled + synced prices).
    Any authenticated user can read this — it's not team-scoped.
    """
    q = select(PricingModel)
    if provider:
        q = q.where(PricingModel.provider == provider)
    q = q.order_by(PricingModel.provider, PricingModel.model)
    rows = (await db.execute(q)).scalars().all()

    return [
        GlobalPricingEntry(
            id=str(p.id),
            provider=p.provider,
            model=p.model,
            resource_type=p.resource_type,
            input_cost_per_1k=p.input_cost_per_1k,
            output_cost_per_1k=p.output_cost_per_1k,
            flat_cost_per_call=p.flat_cost_per_call,
            effective_from=p.effective_from,
            source=p.source,
        )
        for p in rows
    ]


# ── Overrides ──────────────────────────────────────────────────────────────────

@router.get(
    "/overrides",
    response_model=list[PricingOverrideResponse],
    summary="List pricing overrides",
)
async def list_overrides(
    team_id: Optional[str] = None,
    app_id: Optional[str] = None,
    active_only: bool = True,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> list[PricingOverrideResponse]:
    """
    List pricing overrides visible to the caller.
    Team members see their team's overrides. Platform admins see all.
    """
    q = select(PricingOverride)

    if active_only:
        q = q.where(PricingOverride.is_active == True)

    if team_id:
        identity.assert_team_access(team_id)
        q = q.where(PricingOverride.team_id == team_id)
    elif not identity.is_platform_admin and identity.team_ids:
        q = q.where(PricingOverride.team_id.in_(identity.team_ids))

    if app_id:
        q = q.where(PricingOverride.app_id == app_id)

    q = q.order_by(PricingOverride.provider, PricingOverride.model)
    rows = (await db.execute(q)).scalars().all()
    return [_override_to_response(o) for o in rows]


@router.post(
    "/overrides",
    response_model=PricingOverrideResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a pricing override",
)
async def create_override(
    body: PricingOverrideCreate,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> PricingOverrideResponse:
    """
    Create a negotiated-rate override for a specific provider/model.

    Override resolution order (highest wins):
      1. App-level override (app_id set)
      2. Team-level override (team_id set, no app_id)
      3. Global pricing table
    """
    identity.assert_permission("pricing:write")

    if body.team_id:
        identity.assert_team_access(body.team_id)

    # Validate at least one pricing field is set
    if all(v is None for v in [body.input_cost_per_1k, body.output_cost_per_1k, body.per_unit_cost]):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one of input_cost_per_1k, output_cost_per_1k, or per_unit_cost must be provided.",
        )

    override = PricingOverride(
        team_id=body.team_id,
        app_id=body.app_id,
        provider=body.provider,
        model=body.model,
        resource_type=body.resource_type,
        input_cost_per_1k=body.input_cost_per_1k,
        output_cost_per_1k=body.output_cost_per_1k,
        per_unit_cost=body.per_unit_cost,
        unit_label=body.unit_label,
        override_reason=body.override_reason,
        is_active=True,
    )
    db.add(override)
    await db.flush()

    db.add(AuditLog(
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        team_id=body.team_id,
        resource_type="pricing_override",
        resource_id=str(override.id),
        action="created",
        after={
            "provider": body.provider,
            "model": body.model,
            "input_cost_per_1k": str(body.input_cost_per_1k),
            "output_cost_per_1k": str(body.output_cost_per_1k),
        },
    ))

    logger.info(
        "Pricing override created",
        extra={
            "provider": body.provider,
            "model": body.model,
            "team_id": body.team_id,
            "app_id": body.app_id,
        },
    )
    return _override_to_response(override)


@router.put(
    "/overrides/{override_id}",
    response_model=PricingOverrideResponse,
    summary="Update a pricing override",
)
async def update_override(
    override_id: str,
    body: PricingOverrideUpdate,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> PricingOverrideResponse:
    identity.assert_permission("pricing:write")

    override = await db.get(PricingOverride, override_id)
    if not override or not override.is_active:
        raise HTTPException(status_code=404, detail="Pricing override not found.")

    if override.team_id:
        identity.assert_team_access(str(override.team_id))

    before = _audit_snapshot(override)

    if body.input_cost_per_1k is not None:
        override.input_cost_per_1k = body.input_cost_per_1k
    if body.output_cost_per_1k is not None:
        override.output_cost_per_1k = body.output_cost_per_1k
    if body.per_unit_cost is not None:
        override.per_unit_cost = body.per_unit_cost
    if body.unit_label is not None:
        override.unit_label = body.unit_label
    if body.override_reason is not None:
        override.override_reason = body.override_reason
    if body.is_active is not None:
        override.is_active = body.is_active

    db.add(AuditLog(
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        team_id=str(override.team_id) if override.team_id else None,
        resource_type="pricing_override",
        resource_id=override_id,
        action="updated",
        before=before,
        after=_audit_snapshot(override),
    ))

    return _override_to_response(override)


def _audit_snapshot(override: PricingOverride) -> dict:
    """Full identifying state of an override for the audit trail (before/after)."""
    return {
        "provider": override.provider,
        "model": override.model,
        "input_cost_per_1k": str(override.input_cost_per_1k),
        "output_cost_per_1k": str(override.output_cost_per_1k),
    }


@router.delete(
    "/overrides/{override_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Delete a pricing override",
)
async def delete_override(
    override_id: str,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> None:
    identity.assert_permission("pricing:write")

    override = await db.get(PricingOverride, override_id)
    if not override or not override.is_active:
        raise HTTPException(status_code=404, detail="Pricing override not found.")

    if override.team_id:
        identity.assert_team_access(str(override.team_id))

    override.is_active = False

    db.add(AuditLog(
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        team_id=str(override.team_id) if override.team_id else None,
        resource_type="pricing_override",
        resource_id=override_id,
        action="deleted",
        before=_audit_snapshot(override),
    ))


@router.get(
    "/effective/{provider}/{model}",
    response_model=EffectivePrice,
    summary="Get resolved effective price",
)
async def effective_price(
    provider: str,
    model: str,
    app_id: Optional[str] = None,
    team_id: Optional[str] = None,
    resource_type: str = "llm_call",
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> EffectivePrice:
    """
    Return the price that would actually be used for a given provider/model,
    considering overrides in resolution order: app > team > global.
    Useful for the dashboard 'what will this cost?' preview.
    """
    # 1. App-level override
    if app_id:
        q = select(PricingOverride).where(
            PricingOverride.app_id == app_id,
            PricingOverride.provider == provider,
            PricingOverride.model == model,
            PricingOverride.resource_type == resource_type,
            PricingOverride.is_active == True,
        )
        o = (await db.execute(q)).scalar_one_or_none()
        if o:
            return EffectivePrice(
                provider=provider, model=model, resource_type=resource_type,
                input_cost_per_1k=o.input_cost_per_1k,
                output_cost_per_1k=o.output_cost_per_1k,
                per_unit_cost=o.per_unit_cost,
                source="override_app",
                override_id=str(o.id),
            )

    # 2. Team-level override
    if team_id:
        q = select(PricingOverride).where(
            PricingOverride.team_id == team_id,
            PricingOverride.app_id.is_(None),
            PricingOverride.provider == provider,
            PricingOverride.model == model,
            PricingOverride.resource_type == resource_type,
            PricingOverride.is_active == True,
        )
        o = (await db.execute(q)).scalar_one_or_none()
        if o:
            return EffectivePrice(
                provider=provider, model=model, resource_type=resource_type,
                input_cost_per_1k=o.input_cost_per_1k,
                output_cost_per_1k=o.output_cost_per_1k,
                per_unit_cost=o.per_unit_cost,
                source="override_team",
                override_id=str(o.id),
            )

    # 3. Global pricing table
    q = select(PricingModel).where(
        PricingModel.provider == provider,
        PricingModel.model == model,
        PricingModel.resource_type == resource_type,
    ).order_by(PricingModel.effective_from.desc()).limit(1)
    gp = (await db.execute(q)).scalar_one_or_none()
    if gp:
        return EffectivePrice(
            provider=provider, model=model, resource_type=resource_type,
            input_cost_per_1k=gp.input_cost_per_1k,
            output_cost_per_1k=gp.output_cost_per_1k,
            per_unit_cost=gp.per_unit_cost,
            source="global",
            override_id=None,
        )

    # 4. Not found
    return EffectivePrice(
        provider=provider, model=model, resource_type=resource_type,
        input_cost_per_1k=None, output_cost_per_1k=None, per_unit_cost=None,
        source="not_found", override_id=None,
    )


@router.post(
    "/overrides/bulk-import",
    response_model=BulkImportResponse,
    status_code=status.HTTP_200_OK,
    summary="Bulk import pricing overrides",
)
async def bulk_import_overrides(
    body: BulkImportRequest,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> BulkImportResponse:
    """
    Import multiple overrides at once. Useful for loading a rate card from a
    vendor contract without clicking through the UI for each model.
    """
    identity.assert_permission("pricing:write")

    if body.team_id:
        identity.assert_team_access(body.team_id)

    if body.replace_existing:
        # Deactivate all existing overrides for this scope
        existing_q = select(PricingOverride).where(
            PricingOverride.is_active == True,
        )
        if body.app_id:
            existing_q = existing_q.where(PricingOverride.app_id == body.app_id)
        elif body.team_id:
            existing_q = existing_q.where(
                PricingOverride.team_id == body.team_id,
                PricingOverride.app_id.is_(None),
            )
        existing = (await db.execute(existing_q)).scalars().all()
        for o in existing:
            o.is_active = False

    created = 0
    skipped = 0
    errors: list[str] = []

    for entry in body.entries:
        try:
            if all(v is None for v in [entry.input_cost_per_1k, entry.output_cost_per_1k, entry.per_unit_cost]):
                errors.append(f"{entry.provider}/{entry.model}: no pricing values provided, skipped.")
                skipped += 1
                continue

            override = PricingOverride(
                team_id=body.team_id,
                app_id=body.app_id,
                provider=entry.provider,
                model=entry.model,
                resource_type=entry.resource_type,
                input_cost_per_1k=entry.input_cost_per_1k,
                output_cost_per_1k=entry.output_cost_per_1k,
                per_unit_cost=entry.per_unit_cost,
                unit_label=entry.unit_label,
                override_reason=entry.override_reason or "Bulk import",
                is_active=True,
            )
            db.add(override)
            created += 1
        except Exception as exc:
            errors.append(f"{entry.provider}/{entry.model}: {exc}")
            skipped += 1

    await db.flush()

    db.add(AuditLog(
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        team_id=body.team_id,
        resource_type="pricing_override",
        resource_id="bulk-import",
        action="bulk_imported",
        after={"created": created, "skipped": skipped},
    ))

    logger.info("Bulk pricing import", extra={"created": created, "skipped": skipped})
    return BulkImportResponse(created=created, skipped=skipped, errors=errors)
