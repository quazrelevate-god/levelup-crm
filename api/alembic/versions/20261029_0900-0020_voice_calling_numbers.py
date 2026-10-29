"""Calling numbers: which of the operator's numbers a workspace's AI calls come from.

Revision ID: 0020_voice_calling_numbers
Revises: 0019_voice_approval

Two additive columns (docs/16).

- `workspaces.voice_calling_numbers` — the numbers, in rotation order. Empty
  means "let Bolna use its default number", which is what every workspace did
  before this revision, hence the `'[]'` default.
- `voice_call_executions.caller_phone` — the number a call actually went out
  from, so a number that starts being flagged as spam can be seen and swapped.
  NULL for calls placed before this revision and for calls on the default.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0020_voice_calling_numbers"
down_revision = "0019_voice_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "voice_calling_numbers",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column("voice_call_executions", sa.Column("caller_phone", sa.String(32)))


def downgrade() -> None:
    op.drop_column("voice_call_executions", "caller_phone")
    op.drop_column("workspaces", "voice_calling_numbers")
