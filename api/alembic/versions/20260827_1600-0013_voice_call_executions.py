"""Voice call executions — the CRM↔Bolna join table.

Revision ID: 0013_voice_executions
Revises: 0012_voice_context

`docs/06-voice-integration-contract.md` §4, §5 and §7.

One row per Bolna call attempt, where `voice_call_contexts` (0012) is one row
per lead. `external_id` is Bolna's `execution_id` — the join key the contract
names as both the correlation id for the webhook and its idempotency key.

`completed_at` is what makes a repeated webhook a no-op. It is deliberately a
timestamp rather than a boolean or a status check: the vendored `setup-webhook`
skill is explicit that one execution produces several deliveries as its status
transitions, so the CRM has to distinguish "we have seen this execution" (not
enough — later deliveries carry more) from "we have already written this
execution back" (the actual gate).

The unique index on `external_id` is partial because a row legitimately has no
external id between being written and Bolna answering — the window §7 calls
"webhook arrives before the trigger commits".

0012 is untouched by this revision.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013_voice_executions"
down_revision = "0012_voice_context"
branch_labels = None
depends_on = None

# `create_type=False` matters: without it SQLAlchemy emits a second CREATE TYPE
# from inside `create_table`, and the migration fails on its own enum. 0009 does
# the same thing for `outbox_status` — this follows it exactly.
voice_call_status = postgresql.ENUM(
    "QUEUED",
    "DISPATCHED",
    "COMPLETED",
    "FAILED",
    name="voice_call_status",
    create_type=False,
)


def upgrade() -> None:
    voice_call_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "voice_call_executions",
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
        sa.Column("external_id", sa.String(length=120)),
        sa.Column("agent_id", sa.String(length=120)),
        sa.Column("recipient_phone", sa.String(length=32), nullable=False),
        sa.Column(
            "idempotency_key",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "status",
            voice_call_status,
            nullable=False,
            server_default=sa.text("'QUEUED'"),
        ),
        sa.Column("bolna_status", sa.String(length=60)),
        sa.Column(
            "context_sent",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "attempts", sa.SmallInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("last_error", sa.Text()),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_index(
        "ix_voice_call_executions_workspace_id",
        "voice_call_executions",
        ["workspace_id"],
    )
    op.create_index(
        "voice_call_executions_external_uq",
        "voice_call_executions",
        ["workspace_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )
    op.create_unique_constraint(
        "voice_call_executions_idem_uq",
        "voice_call_executions",
        ["workspace_id", "idempotency_key"],
    )
    op.create_index(
        "ix_voice_call_executions_lead",
        "voice_call_executions",
        ["workspace_id", "lead_id", "created_at"],
    )
    op.create_index(
        "ix_voice_call_executions_pending",
        "voice_call_executions",
        ["status", "created_at"],
        postgresql_where=sa.text("status IN ('QUEUED', 'FAILED')"),
    )


def downgrade() -> None:
    op.drop_index("ix_voice_call_executions_pending", table_name="voice_call_executions")
    op.drop_index("ix_voice_call_executions_lead", table_name="voice_call_executions")
    op.drop_constraint(
        "voice_call_executions_idem_uq", "voice_call_executions", type_="unique"
    )
    op.drop_index("voice_call_executions_external_uq", table_name="voice_call_executions")
    op.drop_index(
        "ix_voice_call_executions_workspace_id", table_name="voice_call_executions"
    )
    op.drop_table("voice_call_executions")
    postgresql.ENUM(name="voice_call_status").drop(op.get_bind(), checkfirst=True)
