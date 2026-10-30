"""Call facts: reference details the voice agent is told, chosen by rule.

Revision ID: 0022_call_facts
Revises: 0021_voice_number_requests

Docs/17. One additive table, `call_fact_sets`: a named group of facts (batch
dates, mentors, fees) with a filter rule saying which leads it applies to and a
priority. Nothing existing changes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0022_call_facts"
down_revision = "0021_voice_number_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "call_fact_sets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("conditions", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "facts", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("workspace_id", "name", name="call_fact_sets_name_uq"),
    )
    op.create_index("ix_call_fact_sets_workspace_id", "call_fact_sets", ["workspace_id"])
    op.create_index("ix_call_fact_sets_order", "call_fact_sets", ["workspace_id", "priority", "id"])


def downgrade() -> None:
    op.drop_index("ix_call_fact_sets_order", table_name="call_fact_sets")
    op.drop_index("ix_call_fact_sets_workspace_id", table_name="call_fact_sets")
    op.drop_table("call_fact_sets")
