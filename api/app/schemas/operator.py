"""The operator console (docs/19)."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

__all__ = [
    "CompanyCreate",
    "CompanyCreated",
    "CompanyRead",
    "CompanyUpdate",
    "FeatureRead",
    "LinkRead",
    "MoneyEntry",
    "MoneyEntryRead",
    "NumberRequestRead",
    "NumberSettle",
    "OperatorAccountRead",
    "OperatorActionRead",
    "OperatorLinkRequest",
    "OverviewRead",
    "PersonAdd",
    "PersonAdded",
    "PersonRead",
    "PersonUpdate",
    "RateUpdate",
    "RoleRead",
    "WalletRead",
]


def _check_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown timezone {value!r}") from exc
    return value


class FeatureRead(BaseModel):
    key: str
    label: str
    description: str
    on_by_default: bool


class CompanyRead(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    is_active: bool
    owner_name: str | None
    owner_email: str | None
    seats_used: int
    seat_limit: int
    members: int
    currency: str
    timezone: str
    default_country_code: str
    balance: Decimal | None
    rate_per_minute: Decimal | None
    minutes_this_month: int
    low_balance: bool
    #: The operator is inside this company right now.
    support_active: bool
    features: dict[str, bool]
    created_at: dt.datetime


class CompanyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    default_country_code: str = Field(default="91", pattern=r"^\d{1,4}$")
    timezone: str = Field(default="Asia/Kolkata", max_length=64)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    seat_limit: int = Field(default=5, ge=1, le=10_000)
    features: dict[str, bool] = Field(default_factory=dict)
    opening_balance: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    rate_per_minute: Decimal = Field(default=Decimal("0"), ge=0, max_digits=8, decimal_places=2)
    owner_name: str = Field(min_length=1, max_length=200)
    owner_email: EmailStr

    _tz = field_validator("timezone")(_check_timezone)

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()


class LinkRead(BaseModel):
    #: A one-time "set your password" link to send the person. `None` when
    #: they already have an account and use their own password.
    link: str | None
    expires_at: dt.datetime | None


class CompanyCreated(BaseModel):
    company: CompanyRead
    owner: LinkRead


class CompanyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    seat_limit: int | None = Field(default=None, ge=1, le=10_000)
    features: dict[str, bool] | None = None
    is_active: bool | None = None
    timezone: str | None = Field(default=None, max_length=64)
    default_country_code: str | None = Field(default=None, pattern=r"^\d{1,4}$")

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str | None) -> str | None:
        return _check_timezone(value) if value is not None else None


class RoleRead(BaseModel):
    id: uuid.UUID
    name: str
    #: Root: cannot be edited or removed; the company's owner role.
    is_locked: bool
    #: Full control of the company's CRM.
    is_admin: bool


class PersonRead(BaseModel):
    membership_id: uuid.UUID
    user_id: uuid.UUID
    full_name: str
    email: str
    role_id: uuid.UUID
    role_name: str
    manager_id: uuid.UUID | None
    is_active: bool
    has_license: bool
    #: The operator, inside to help. Holds no seat.
    is_support: bool
    created_at: dt.datetime


class PersonAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    role_id: uuid.UUID
    #: Who they report to. Empty: nobody (top of the tree).
    manager_id: uuid.UUID | None = None


class PersonAdded(BaseModel):
    person: PersonRead
    sign_in: LinkRead


class PersonUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_id: uuid.UUID | None = None
    manager_id: uuid.UUID | None = None
    #: Move them to the top of the tree (report to nobody).
    clear_manager: bool = False
    is_active: bool | None = None
    #: When deactivating someone who holds open leads: who takes them.
    reassign_to: uuid.UUID | None = None


class MoneyEntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    amount: Decimal
    balance_after: Decimal
    note: str | None
    recorded_by: str | None
    billed_minutes: int | None = None
    created_at: dt.datetime


class WalletRead(BaseModel):
    currency: str
    balance: Decimal | None
    rate_per_minute: Decimal | None
    low_balance_at: Decimal
    entries: list[MoneyEntryRead]
    total_entries: int


class MoneyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: TOPUP — they paid; DISCOUNT — free credit; ADJUSTMENT — a correction,
    #: either sign, always with a note.
    kind: Literal["TOPUP", "DISCOUNT", "ADJUSTMENT"]
    amount: Decimal = Field(max_digits=12, decimal_places=2)
    note: str | None = Field(default=None, max_length=500)


class RateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rate_per_minute: Decimal = Field(ge=0, max_digits=8, decimal_places=2)


class OperatorActionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    actor_email: str
    workspace_id: uuid.UUID | None
    action: str
    summary: str
    created_at: dt.datetime


class NumberRequestRead(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    company: str
    region: str | None
    monthly_price: Decimal
    created_at: dt.datetime


class NumberSettle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The number given to them. Leave out to cancel the request instead.
    phone_number: str | None = Field(default=None, max_length=32)


class OperatorAccountRead(BaseModel):
    email: str
    has_account: bool


class OperatorLinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr


class OverviewRead(BaseModel):
    companies: int
    active_companies: int
    licensed_users: int
    #: `{currency: total}` across every wallet.
    wallet_totals: dict[str, Decimal]
    ai_calls_24h: int
    low_balance_at: Decimal
    low_balance: list[CompanyRead]
    pending_number_requests: int
    #: When the background worker last said it was alive; None if it is not
    #: running (docs/21).
    worker_last_seen: dt.datetime | None = None
