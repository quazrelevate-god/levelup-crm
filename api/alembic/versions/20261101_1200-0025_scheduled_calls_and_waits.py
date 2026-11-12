"""Scheduled AI calls, workflow waits, and app access tokens.

Revision ID: 0025_scheduled_calls_and_waits
Revises: 0024_operator_console

Docs/22. `voice_scheduled_calls` — AI calls the worker places later (a lead's
callback, a workflow's call, a person's schedule). `workflow_runs.pending` and
`resume_at` — where a run paused at a Wait block picks up. `app_connections.
credential` — the access token for Calendly or WhatsApp, write-only.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0025_scheduled_calls_and_waits"
down_revision = "0024_operator_console"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_connections", sa.Column("credential", sa.Text()))
    op.add_column(
        "workflow_runs",
        sa.Column(
            "pending", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
    )
    op.add_column("workflow_runs", sa.Column("resume_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_workflow_runs_waiting",
        "workflow_runs",
        ["resume_at"],
        postgresql_where=sa.text("status = 'WAITING'"),
    )

    op.create_table(
        "voice_scheduled_calls",
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
        sa.Column(
            "lead_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("purpose", sa.String(length=40)),
        sa.Column("reason", sa.Text()),
        sa.Column(
            "workflow_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workflow_runs.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memberships.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "call_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("voice_call_executions.id", ondelete="SET NULL"),
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text()),
        sa.Column("placed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_voice_scheduled_calls_workspace_id", "voice_scheduled_calls", ["workspace_id"]
    )
    op.create_index(
        "ix_voice_scheduled_due",
        "voice_scheduled_calls",
        ["due_at"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_index(
        "voice_scheduled_one_pending_uq",
        "voice_scheduled_calls",
        ["workspace_id", "lead_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )


def downgrade() -> None:
    op.drop_table("voice_scheduled_calls")
    op.drop_index("ix_workflow_runs_waiting", table_name="workflow_runs")
    op.drop_column("workflow_runs", "resume_at")
    op.drop_column("workflow_runs", "pending")
    op.drop_column("app_connections", "credential")
