"""Keep the raw Bolna webhook body alongside each execution.

Revision ID: 0014_voice_raw_payload
Revises: 0013_voice_executions

One additive, nullable-with-default column. Nothing existing is altered, and
0013 is untouched.

**Why it earns a migration.** The vendored Bolna skills describe the execution
object's `telephony_data` in prose — "provider, to/from numbers, call type,
provider call ID, hangup reason/code, ring duration" — but the field-level
reference they link (`references/execution-payload.md`) was never vendored, so
the exact key names for the customer's number and the call's duration are not
knowable from this repository. The receiver therefore reads several plausible
spellings, and stores the body it actually received so the first real call
replaces guesswork with evidence. Without this column that evidence exists only
in a log line that will have rotated away by the time anyone asks.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014_voice_raw_payload"
down_revision = "0013_voice_executions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "voice_call_executions",
        sa.Column(
            "raw_payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("voice_call_executions", "raw_payload")
