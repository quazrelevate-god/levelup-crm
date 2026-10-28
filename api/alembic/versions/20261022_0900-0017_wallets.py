"""Prepaid wallets for AI call minutes.

Revision ID: 0017_wallets
Revises: 0016_voice_post_call

Two new tables and one enum (docs/14). Nothing existing is altered and no
earlier revision is touched.

- `wallets` — one per metered workspace: balance and per-minute rate. A
  workspace without one is not metered.
- `wallet_transactions` — the append-only ledger behind the balance. Each
  kind's sign is a check constraint, so a top-up can never take money away and
  a call charge can never add it.

Check constraints carry their short names only. Alembic applies the metadata
naming convention (`ck_%(table_name)s_%(constraint_name)s`) to a revision just
as it does to the models, so a name written out in full here would come out
prefixed twice and no longer match.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0017_wallets"
down_revision = "0016_voice_post_call"
branch_labels = None
depends_on = None

# `create_type=False`: see 0013. Without it `create_table` emits a second
# CREATE TYPE and the migration fails on its own enum.
wallet_transaction_kind = postgresql.ENUM(
    "TOPUP",
    "DISCOUNT",
    "ADJUSTMENT",
    "CALL_CHARGE",
    name="wallet_transaction_kind",
    create_type=False,
)

_MONEY = sa.Numeric(12, 2)


def _timestamps() -> list[sa.Column[object]]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade() -> None:
    wallet_transaction_kind.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "wallets",
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
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("balance", _MONEY, nullable=False, server_default=sa.text("0")),
        sa.Column("rate_per_minute", _MONEY, nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("workspace_id", name="wallets_workspace_uq"),
        sa.CheckConstraint("rate_per_minute >= 0", name="rate_not_negative"),
    )
    op.create_index("ix_wallets_workspace_id", "wallets", ["workspace_id"])

    op.create_table(
        "wallet_transactions",
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
            "wallet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("wallets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", wallet_transaction_kind, nullable=False),
        sa.Column("amount", _MONEY, nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("balance_after", _MONEY, nullable=False),
        sa.Column(
            "voice_call_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("voice_call_executions.id", ondelete="SET NULL"),
        ),
        sa.Column("billed_seconds", sa.Integer()),
        sa.Column("billed_minutes", sa.Integer()),
        sa.Column("rate_per_minute", _MONEY),
        sa.Column("note", sa.Text()),
        sa.Column("recorded_by", sa.String(length=200)),
        *_timestamps(),
        sa.CheckConstraint(
            "(kind IN ('TOPUP', 'DISCOUNT') AND amount > 0)"
            " OR (kind = 'CALL_CHARGE' AND amount < 0)"
            " OR (kind = 'ADJUSTMENT' AND amount <> 0)",
            name="amount_sign_matches_kind",
        ),
    )
    op.create_index("ix_wallet_transactions_workspace_id", "wallet_transactions", ["workspace_id"])
    op.create_index(
        "ix_wallet_transactions_statement",
        "wallet_transactions",
        ["workspace_id", "created_at"],
    )
    op.create_index(
        "ix_wallet_transactions_call",
        "wallet_transactions",
        ["workspace_id", "voice_call_id"],
        postgresql_where=sa.text("voice_call_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_wallet_transactions_call", table_name="wallet_transactions")
    op.drop_index("ix_wallet_transactions_statement", table_name="wallet_transactions")
    op.drop_index("ix_wallet_transactions_workspace_id", table_name="wallet_transactions")
    op.drop_table("wallet_transactions")
    op.drop_index("ix_wallets_workspace_id", table_name="wallets")
    op.drop_table("wallets")
    postgresql.ENUM(name="wallet_transaction_kind").drop(op.get_bind(), checkfirst=True)
