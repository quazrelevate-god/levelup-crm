"""Post-call automation: keep each call's outcome on its execution row.

Revision ID: 0016_voice_post_call
Revises: 0015_voice_extraction_mappings

Additive, nullable columns on `voice_call_executions`. Nothing existing is
altered and no earlier revision is touched.

Until now the only record of what a call *said* was `raw_payload`, which is
overwritten on every delivery, and `voice_call_contexts.last_call_summary`,
which is per lead and overwritten on every call. Neither answers "what happened
on this particular call" once a second call has happened. These columns do:

- `transcript`, `duration_seconds` — the normalised facts of the call.
- `summary`, `summary_source`, `summary_error` — what the CRM timeline shows,
  whether it came from the AI summariser or is the safe fallback, and why the
  summariser did not produce one (type name only; never a payload).
- `webhook_received_at` — the last delivery, for "did Bolna ever call us?".
- `call_action_id` — the `CALL_LOGGED` timeline action this call produced.
  Not a foreign key: actions are append-only audit rows and this is a pointer
  for traceability, not an ownership relation.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016_voice_post_call"
down_revision = "0015_voice_extraction_mappings"
branch_labels = None
depends_on = None

_COLUMNS = (
    "transcript",
    "duration_seconds",
    "summary",
    "summary_source",
    "summary_error",
    "webhook_received_at",
    "call_action_id",
)


def upgrade() -> None:
    op.add_column("voice_call_executions", sa.Column("transcript", sa.Text(), nullable=True))
    op.add_column(
        "voice_call_executions", sa.Column("duration_seconds", sa.Integer(), nullable=True)
    )
    op.add_column("voice_call_executions", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column(
        "voice_call_executions", sa.Column("summary_source", sa.String(length=20), nullable=True)
    )
    op.add_column("voice_call_executions", sa.Column("summary_error", sa.Text(), nullable=True))
    op.add_column(
        "voice_call_executions",
        sa.Column("webhook_received_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "voice_call_executions",
        sa.Column("call_action_id", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.drop_column("voice_call_executions", column)
