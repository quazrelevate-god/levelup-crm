"""Prepaid wallets for AI call minutes (docs/14).

`docs/01-data-model.md` §9 originally listed billing as an anti-requirement for
v1. That changed when AI calling became something clients pay for by the
minute: a client recharges a wallet in advance, every connected call draws it
down, and a wallet that has run out stops new calls until it is topped up.

Two tables, deliberately:

- **`wallets`** — one row per metered workspace: the current balance and the
  per-minute rate. The balance is a cached total,
  updated in the same transaction as the ledger row that moves it, under a row
  lock, so it can never drift from the ledger.
- **`wallet_transactions`** — the ledger. Append-only: a row is never edited or
  deleted, and a mistake is corrected by a further `ADJUSTMENT` row, exactly
  as an accountant would. A statement is the ledger read in order.

**A workspace without a wallet is not metered.** Internal, demo and test
workspaces have no wallet and call freely; the operator creates one when a
client is onboarded, and from then on calls need balance. See
`WalletService.ensure_can_call`.

**Money is `numeric(12,2)` plus a currency code** (architecture rule 11). The
currency is copied from the workspace when the wallet is created and stamped
on every ledger row, so a statement still reads correctly if a workspace's
currency setting ever changes.

**Rates exclude tax.** The wallet holds the client's net credit: GST is charged
on the recharge invoice, outside this ledger, so a recharge of 5,000 credits
5,000 and every minute deducts the plain rate.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.enums import WalletTransactionKind
from app.models.mixins import TenantModel

__all__ = ["VoiceNumberRequest", "Wallet", "WalletTransaction"]

wallet_transaction_kind_enum = SAEnum(
    WalletTransactionKind,
    name="wallet_transaction_kind",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
)

#: `numeric(12,2)`, rule 11. Never a float.
MONEY = Numeric(12, 2)


class Wallet(TenantModel):
    """One metered workspace's prepaid balance and its price."""

    __tablename__ = "wallets"
    __table_args__ = (
        UniqueConstraint("workspace_id", name="wallets_workspace_uq"),
        CheckConstraint("rate_per_minute >= 0", name="rate_not_negative"),
    )

    #: ISO 4217, copied from the workspace at creation.
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    #: May go below zero: a call already in progress is never cut off when the
    #: balance runs out. It is the *next* call that is refused.
    balance: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default=text("0"))
    #: What one minute of connected call time costs, before tax (GST is
    #: invoiced on the recharge, not deducted here).
    #: Set per wallet, so one client can be on a different price from another.
    #: Calls are billed per started minute: a 61-second call is two minutes.
    rate_per_minute: Mapped[Decimal] = mapped_column(MONEY, nullable=False)


class WalletTransaction(TenantModel):
    """One movement of a wallet's balance. Append-only."""

    __tablename__ = "wallet_transactions"
    __table_args__ = (
        # The sign of each kind is a fact about the kind, so the database holds
        # it rather than trusting every writer to remember.
        CheckConstraint(
            "(kind IN ('TOPUP', 'DISCOUNT') AND amount > 0)"
            " OR (kind IN ('CALL_CHARGE', 'NUMBER_RENTAL') AND amount < 0)"
            " OR (kind = 'ADJUSTMENT' AND amount <> 0)",
            name="amount_sign_matches_kind",
        ),
        # A statement, newest first — the only list this table serves.
        Index("ix_wallet_transactions_statement", "workspace_id", "created_at"),
        # "What has this call already been charged?" — asked on every webhook.
        Index(
            "ix_wallet_transactions_call",
            "workspace_id",
            "voice_call_id",
            postgresql_where=text("voice_call_id IS NOT NULL"),
        ),
    )

    wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wallets.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[WalletTransactionKind] = mapped_column(
        wallet_transaction_kind_enum, nullable=False
    )
    #: Signed: credits positive, debits negative.
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    #: The wallet's balance immediately after this row. Lets a statement be
    #: read without re-adding the ledger from the beginning.
    balance_after: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    # --- call charges --------------------------------------------------------
    #: The call this charge is for. `SET NULL`, not `CASCADE`: a ledger row
    #: outlives anything it points at.
    voice_call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("voice_call_executions.id", ondelete="SET NULL")
    )
    #: Connected seconds as Bolna reported them, and the minutes billed for
    #: them — every started minute counts as a whole one.
    billed_seconds: Mapped[int | None] = mapped_column(Integer)
    billed_minutes: Mapped[int | None] = mapped_column(Integer)
    #: The rate this charge was priced at, kept so a later rate change never
    #: rewrites what an old statement says.
    rate_per_minute: Mapped[Decimal | None] = mapped_column(MONEY)

    # --- operator entries ----------------------------------------------------
    #: Free text: a payment reference, a discount reason, an explanation.
    note: Mapped[str | None] = mapped_column(Text())
    #: Who recorded an operator entry. Operators act from the command line,
    #: outside any workspace, so this is a label rather than a membership id.
    recorded_by: Mapped[str | None] = mapped_column(String(200))


class VoiceNumberRequest(TenantModel):
    """One calling number a client asked for and pays rent on (docs/16).

    PENDING — requested, first period charged, waiting for the operator to buy
    it. ACTIVE — the operator fulfilled it; it is in the workspace's calling
    numbers and paid until `paid_until`. PAST_DUE — a period fell due and the
    wallet could not cover it. CANCELLED — refunded if it was never fulfilled,
    or released if it was.
    """

    __tablename__ = "voice_number_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'ACTIVE', 'PAST_DUE', 'CANCELLED')", name="status_known"
        ),
        CheckConstraint("monthly_price >= 0", name="price_not_negative"),
    )

    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memberships.id", ondelete="SET NULL")
    )
    #: Where the client would like the number from, as they said it. A wish,
    #: not a promise: it depends on what the provider has in stock.
    region: Mapped[str | None] = mapped_column(String(80))
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="PENDING", server_default=text("'PENDING'")
    )
    #: The price per period when it was requested; later price changes do not
    #: reach numbers already rented.
    monthly_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    phone_number: Mapped[str | None] = mapped_column(String(32))
    paid_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    fulfilled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
