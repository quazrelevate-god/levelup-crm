"""Wallet read models (docs/14).

Read-only by design. Nothing a workspace member can send moves money: top-ups,
discounts and adjustments are recorded by the operator from the command line.

Amounts are `Decimal`, which serialises to a JSON *string* ("120.00"). That is
deliberate: a JSON number is a float to most clients, and rule 11 says money
is never a float.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.models.enums import WalletTransactionKind

__all__ = ["WalletRead", "WalletTransactionRead", "WalletUsageRead"]


class WalletUsageRead(BaseModel):
    """Connected AI-call time billed in one period."""

    minutes: int
    connected_calls: int
    spent: Decimal


class WalletRead(BaseModel):
    currency: str
    balance: Decimal
    #: Per started minute of connected time, before tax.
    rate_per_minute: Decimal
    #: Whether a new AI call may be placed right now.
    can_call: bool
    #: The balance a call needs before it is allowed to dial: one minute.
    minimum_to_call: Decimal
    #: Periods are the workspace's own today and month (rule 10).
    today: WalletUsageRead
    this_month: WalletUsageRead
    updated_at: dt.datetime


class WalletTransactionRead(BaseModel):
    """One statement line."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: WalletTransactionKind
    #: Signed: credits positive, charges negative.
    amount: Decimal
    currency: str
    balance_after: Decimal
    #: For a call charge: the call, what it added in seconds and minutes, and
    #: the rate it was priced at.
    voice_call_id: uuid.UUID | None
    billed_seconds: int | None
    billed_minutes: int | None
    rate_per_minute: Decimal | None
    #: The operator's note — a payment reference or a reason. Shown to the
    #: client on their statement. `recorded_by` is not: it names the
    #: operator's staff, which is nothing the client needs.
    note: str | None
    created_at: dt.datetime
