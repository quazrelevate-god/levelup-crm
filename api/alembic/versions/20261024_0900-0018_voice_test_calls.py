"""Test calls: mark an AI call placed to a tester's phone instead of the lead's.

Revision ID: 0018_voice_test_calls
Revises: 0017_wallets

One additive column on `voice_call_executions` (docs/15). Nothing existing is
altered and every existing row is a real call, hence the `false` default.

A test call dials the tester's own number but carries the chosen lead's full
context, so an operator can hear exactly what that lead would hear before
anyone real is called. It is recorded like any call — summary, memory,
extractions, wallet charge — but it never moves the lead's stage.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_voice_test_calls"
down_revision = "0017_wallets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "voice_call_executions",
        sa.Column("is_test", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("voice_call_executions", "is_test")
