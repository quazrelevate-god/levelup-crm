"""The operator console: its action history, and support access.

Revision ID: 0024_operator_console
Revises: 0023_automations

Docs/19. One global table, `operator_actions` — everything the platform
operator changed, in which company, when — and `memberships.is_support`, which
marks the operator's own membership in a company they were let into to help,
so it holds no seat the client paid for.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0024_operator_console"
down_revision = "0023_automations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memberships",
        sa.Column("is_support", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_table(
        "operator_actions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("actor_email", sa.String(length=320), nullable=False),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
        ),
        sa.Column("action", sa.String(length=60), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_operator_actions_recent", "operator_actions", ["created_at"])
    op.create_index(
        "ix_operator_actions_workspace", "operator_actions", ["workspace_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("operator_actions")
    op.drop_column("memberships", "is_support")
