"""Number requests: a client asks for a calling number and pays from the wallet.

Revision ID: 0021_voice_number_requests
Revises: 0020_voice_calling_numbers

Docs/16. A workspace admin requests a number; the first period is charged to
the wallet at once; the operator buys it in Bolna and fulfils the request,
which puts the number into the workspace's calling numbers. Each later period
is charged when due.

- A new ledger kind, `NUMBER_RENTAL`, always a debit. Added outside the
  migration's transaction: Postgres will not let a new enum value be used in
  the same transaction that created it, and the sign constraint below uses it.
- The sign constraint on `wallet_transactions` is rebuilt to include it.
- `voice_number_requests`, one row per requested number.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0021_voice_number_requests"
down_revision = "0020_voice_calling_numbers"
branch_labels = None
depends_on = None

_MONEY = sa.Numeric(12, 2)
_OLD_SIGN = (
    "(kind IN ('TOPUP', 'DISCOUNT') AND amount > 0)"
    " OR (kind = 'CALL_CHARGE' AND amount < 0)"
    " OR (kind = 'ADJUSTMENT' AND amount <> 0)"
)
_NEW_SIGN = (
    "(kind IN ('TOPUP', 'DISCOUNT') AND amount > 0)"
    " OR (kind IN ('CALL_CHARGE', 'NUMBER_RENTAL') AND amount < 0)"
    " OR (kind = 'ADJUSTMENT' AND amount <> 0)"
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE wallet_transaction_kind ADD VALUE IF NOT EXISTS 'NUMBER_RENTAL'")

    op.drop_constraint("amount_sign_matches_kind", "wallet_transactions", type_="check")
    op.create_check_constraint("amount_sign_matches_kind", "wallet_transactions", _NEW_SIGN)

    op.create_table(
        "voice_number_requests",
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
            "requested_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memberships.id", ondelete="SET NULL"),
        ),
        sa.Column("region", sa.String(length=80)),
        sa.Column("note", sa.Text()),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("monthly_price", _MONEY, nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("phone_number", sa.String(length=32)),
        sa.Column("paid_until", sa.DateTime(timezone=True)),
        sa.Column("fulfilled_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'ACTIVE', 'PAST_DUE', 'CANCELLED')", name="status_known"
        ),
        sa.CheckConstraint("monthly_price >= 0", name="price_not_negative"),
    )
    op.create_index(
        "ix_voice_number_requests_workspace_id", "voice_number_requests", ["workspace_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_voice_number_requests_workspace_id", table_name="voice_number_requests")
    op.drop_table("voice_number_requests")
    rented = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM wallet_transactions WHERE kind = 'NUMBER_RENTAL'"))
        .scalar()
    )
    if rented:
        # The ledger is append-only: rolling back must not erase charges.
        raise RuntimeError(f"{rented} number-rental charges exist; refusing to downgrade")
    op.drop_constraint("amount_sign_matches_kind", "wallet_transactions", type_="check")
    op.create_check_constraint("amount_sign_matches_kind", "wallet_transactions", _OLD_SIGN)
    # Postgres cannot drop an enum value; NUMBER_RENTAL stays in the type, unused.
