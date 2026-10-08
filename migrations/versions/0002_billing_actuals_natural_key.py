# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0
"""Unique natural key on billing_actuals.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-08 00:00:00

The billing-actuals import endpoints upsert on
(provider, service, period_start, period_end). ``service`` is normalised to ""
instead of NULL so the constraint is enforceable. Rows are only ever written by
the import endpoints, but any pre-existing duplicates are collapsed (newest row
kept) before the constraint is added so the migration cannot fail on them.
SQLite deployments do not use Alembic: init_db() creates tables with create_all.
"""
from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

CONSTRAINT = "uq_billing_actuals_natural_key"


def upgrade() -> None:
    op.execute("UPDATE billing_actuals SET service = '' WHERE service IS NULL")
    op.execute(
        """
        DELETE FROM billing_actuals a
        USING billing_actuals b
        WHERE a.provider = b.provider
          AND a.service = b.service
          AND a.period_start = b.period_start
          AND a.period_end = b.period_end
          AND (a.created_at < b.created_at
               OR (a.created_at = b.created_at AND a.id < b.id))
        """
    )
    op.create_unique_constraint(
        CONSTRAINT, "billing_actuals",
        ["provider", "service", "period_start", "period_end"],
    )


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT, "billing_actuals", type_="unique")
