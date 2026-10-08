<%!
import re
%>\
<%
# Determine branch from revision ID prefix
is_enterprise = up_revision and up_revision.startswith("e_")
is_community = up_revision and up_revision.startswith("c_")
%>\
% if is_enterprise:
# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0
% else:
# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0
% endif
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from __future__ import annotations
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
