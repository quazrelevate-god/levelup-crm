"""Approval for AI calling: a lead is dialled by a batch only once a person says so.

Revision ID: 0019_voice_approval
Revises: 0018_voice_test_calls

Two additive, nullable columns on `leads` (docs/15). `voice_approved_at` is
the switch: set means approved, NULL means not. `voice_approved_by_id` records
who flipped it on. Every existing lead starts unapproved, which is the point —
nobody is called by a batch until somebody has looked at them.

The partial index serves the one question asked of it, "which leads in this
workspace are approved", without indexing the unapproved majority.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0019_voice_approval"
down_revision = "0018_voice_test_calls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("voice_approved_at", sa.DateTime(timezone=True)))
    op.add_column(
        "leads",
        sa.Column(
            "voice_approved_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memberships.id", ondelete="SET NULL"),
        ),
    )
    op.create_index(
        "ix_leads_voice_approved",
        "leads",
        ["workspace_id"],
        postgresql_where=sa.text("voice_approved_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_leads_voice_approved", table_name="leads")
    op.drop_column("leads", "voice_approved_by_id")
    op.drop_column("leads", "voice_approved_at")
