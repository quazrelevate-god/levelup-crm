"""Voice-call context — the CRM-side half of the Bolna integration.

Revision ID: 0012_voice_context
Revises: 0011_m11_credentials

`docs/06-voice-integration-contract.md`,
`docs/09-context-continuity-and-bolna-integration.md`.

One row per lead, holding the digest a voice agent needs to avoid treating a
repeat caller as a brand-new conversation: the most recent call's summary,
when it happened, how many calls there have been, and the idempotency key
that keeps a retried write from double-recording. The full call-by-call
*history* still lives on the existing `actions` timeline — this table exists
so the next call's context lookup is one indexed row, not a timeline scan.

Deliberately not a `lead_fields` entry. This is a structural product concept
the voice integration needs to function, not a piece of a customer's own
taxonomy (CLAUDE.md, "known traps") — the same reasoning already behind
`api_keys` and the planned `voice_extraction_mappings` living in dedicated
tables rather than in `values`.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_voice_context"
down_revision = "0011_m11_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "voice_call_contexts",
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
        sa.Column("last_call_summary", sa.Text()),
        sa.Column("last_call_at", sa.DateTime(timezone=True)),
        sa.Column("last_call_external_id", sa.String(length=120)),
        sa.Column("call_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
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
    op.create_index("ix_voice_call_contexts_workspace_id", "voice_call_contexts", ["workspace_id"])
    op.create_unique_constraint(
        "voice_call_contexts_lead_uq", "voice_call_contexts", ["workspace_id", "lead_id"]
    )


def downgrade() -> None:
    op.drop_constraint("voice_call_contexts_lead_uq", "voice_call_contexts", type_="unique")
    op.drop_index("ix_voice_call_contexts_workspace_id", table_name="voice_call_contexts")
    op.drop_table("voice_call_contexts")
