"""Connected apps and workflows.

Revision ID: 0023_automations
Revises: 0022_call_facts

Docs/18. Four additive tables — `app_connections`, `app_events`, `workflows`,
`workflow_runs` — and the `automations` feature switched on for every existing
workspace, as it is for new ones. Nothing existing changes shape.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023_automations"
down_revision = "0022_call_facts"
branch_labels = None
depends_on = None


def _id() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _workspace() -> sa.Column:
    return sa.Column(
        "workspace_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def _membership(name: str) -> sa.Column:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("memberships.id", ondelete="SET NULL"),
    )


def _jsonb(name: str, default: str = "'{}'::jsonb") -> sa.Column:
    return sa.Column(name, postgresql.JSONB(), nullable=False, server_default=sa.text(default))


def upgrade() -> None:
    op.create_table(
        "app_connections",
        _id(),
        _workspace(),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("hook_token", sa.String(length=64), nullable=False),
        sa.Column("secret", sa.Text()),
        _jsonb("settings"),
        _membership("created_by_id"),
        sa.Column("last_event_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.UniqueConstraint("workspace_id", "provider", name="app_connections_provider_uq"),
        sa.UniqueConstraint("hook_token", name="app_connections_hook_token_uq"),
    )
    op.create_index("ix_app_connections_workspace_id", "app_connections", ["workspace_id"])

    op.create_table(
        "app_events",
        _id(),
        _workspace(),
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("trigger", sa.String(length=80)),
        sa.Column("dedupe_key", sa.String(length=160)),
        _jsonb("data"),
        _jsonb("payload"),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column(
            "lead_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leads.id", ondelete="SET NULL"),
        ),
        sa.Column("workflows_run", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.Text()),
        *_timestamps(),
    )
    op.create_index("ix_app_events_workspace_id", "app_events", ["workspace_id"])
    op.create_index(
        "ix_app_events_recent", "app_events", ["workspace_id", "connection_id", "created_at"]
    )
    op.create_index(
        "app_events_dedupe_uq",
        "app_events",
        ["workspace_id", "connection_id", "trigger", "dedupe_key"],
        unique=True,
        postgresql_where=sa.text(
            "dedupe_key IS NOT NULL AND outcome IN ('MATCHED', 'CREATED_LEAD', 'NO_LEAD')"
        ),
    )

    op.create_table(
        "workflows",
        _id(),
        _workspace(),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("trigger", sa.String(length=80)),
        _jsonb("graph"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        _membership("created_by_id"),
        _membership("updated_by_id"),
        *_timestamps(),
    )
    op.create_index("ix_workflows_workspace_id", "workflows", ["workspace_id"])
    op.create_index("ix_workflows_trigger", "workflows", ["workspace_id", "trigger", "is_active"])
    op.create_index(
        "workflows_name_uq",
        "workflows",
        ["workspace_id", "name"],
        unique=True,
        postgresql_where=sa.text("NOT is_archived"),
    )

    op.create_table(
        "workflow_runs",
        _id(),
        _workspace(),
        sa.Column(
            "workflow_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workflows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "lead_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leads.id", ondelete="SET NULL"),
        ),
        sa.Column("trigger", sa.String(length=80), nullable=False),
        sa.Column(
            "app_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app_events.id", ondelete="SET NULL"),
        ),
        sa.Column("status", sa.String(length=20), nullable=False),
        _jsonb("steps", "'[]'::jsonb"),
        _jsonb("data"),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.Text()),
        *_timestamps(),
    )
    op.create_index("ix_workflow_runs_workspace_id", "workflow_runs", ["workspace_id"])
    op.create_index(
        "ix_workflow_runs_recent", "workflow_runs", ["workspace_id", "workflow_id", "created_at"]
    )

    # New workspaces get the module by default; existing ones get it now.
    op.execute(
        "UPDATE workspaces SET features = "
        "COALESCE(features, '{}'::jsonb) || '{\"automations\": true}'::jsonb"
    )


def downgrade() -> None:
    op.execute("UPDATE workspaces SET features = features - 'automations'")
    op.drop_table("workflow_runs")
    op.drop_table("workflows")
    op.drop_table("app_events")
    op.drop_table("app_connections")
