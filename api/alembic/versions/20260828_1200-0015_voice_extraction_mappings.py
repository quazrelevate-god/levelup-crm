"""Voice extraction mappings — the configurable layer above Bolna's extracted_data.

Revision ID: 0015_voice_extraction_mappings
Revises: 0014_voice_raw_payload

`docs/06-voice-integration-contract.md` §6.1 and
`docs/12-voice-extraction-mappings.md`.

One row per (workspace, Bolna disposition name), naming which of the workspace's
own lead fields the extracted value should be folded into and at what confidence
threshold. Additive: nothing on `voice_call_executions`, `voice_call_contexts`
or `leads` is touched; a workspace that never creates a mapping keeps the exact
behaviour that shipped in 0014 — extracted values ride along in `raw_payload`
and no lead field is rewritten.

Deliberately narrower than the frozen contract text for now: only `LEAD_FIELD`
targets, no `value_map` column. Both are additive follow-ons if and when the
STAGE / ACTION_FIELD kinds are wired up — adding a column or an enum value
later is a smaller migration than deleting one, and rolling out with fewer
knobs means the failure modes the confidence gate is designed to catch are
easier to reason about while the feature is new.

The unique index is `(workspace_id, disposition_name)` — one mapping per
disposition per workspace, matching contract §6.1. A second mapping for the
same disposition would race the write-back and put an operator in the position
of having to reason about which of two mappings won.

The CHECK constraint bounds `min_confidence` to [0, 1] at the schema level as
well as in the Pydantic model, so a rogue direct SQL write cannot land a
threshold outside the range every extracted value is compared against.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015_voice_extraction_mappings"
down_revision = "0014_voice_raw_payload"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "voice_extraction_mappings",
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
        # Bolna's disposition name, exactly as it emits it (contract §5). This
        # is the customer's vocabulary — never a product enum.
        sa.Column("disposition_name", sa.String(length=120), nullable=False),
        # The workspace's own LeadField.key. String rather than a foreign key
        # because fields are hidden not deleted (docs/01-data-model.md §1.1),
        # and a mapping should point at a key by name — the key is immutable
        # by construction, so it cannot be renamed out from under a mapping.
        # The router validates the key exists at create/update time.
        sa.Column("target_field_key", sa.String(length=64), nullable=False),
        sa.Column(
            "min_confidence",
            sa.Numeric(precision=3, scale=2),
            nullable=False,
            server_default=sa.text("0.70"),
        ),
        sa.Column(
            "is_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
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
        sa.CheckConstraint(
            "min_confidence >= 0 AND min_confidence <= 1",
            name="voice_extraction_mappings_confidence_ck",
        ),
    )
    op.create_index(
        "ix_voice_extraction_mappings_workspace_id",
        "voice_extraction_mappings",
        ["workspace_id"],
    )
    # One mapping per disposition per workspace — contract §6.1.
    op.create_unique_constraint(
        "voice_extraction_mappings_disposition_uq",
        "voice_extraction_mappings",
        ["workspace_id", "disposition_name"],
    )
    # The write-back's lookup: "enabled mappings for this workspace." Small
    # table, but the read runs on every completed call.
    op.create_index(
        "ix_voice_extraction_mappings_enabled",
        "voice_extraction_mappings",
        ["workspace_id"],
        postgresql_where=sa.text("is_enabled"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_voice_extraction_mappings_enabled",
        table_name="voice_extraction_mappings",
    )
    op.drop_constraint(
        "voice_extraction_mappings_disposition_uq",
        "voice_extraction_mappings",
        type_="unique",
    )
    op.drop_index(
        "ix_voice_extraction_mappings_workspace_id",
        table_name="voice_extraction_mappings",
    )
    op.drop_table("voice_extraction_mappings")
