"""Prepaid AI-call wallets: the balance, the gate, and the ledger (docs/14).

Every rule about money lives here, so the call integration, the API and the
operator's command line all apply the same ones:

- **Only connected time costs anything.** A call is billed when Bolna reports
  it completed with a positive duration. Unanswered, busy, failed and
  zero-second calls are free.
- **Per started minute.** A 61-second call is two minutes. The rate is the
  wallet's own and excludes tax: GST is invoiced on the recharge, so the
  wallet holds net credit.
- **A call in progress is never cut off.** The final call may take the balance
  below zero. What stops is the *next* call: `ensure_can_call` refuses to dial
  unless the wallet holds at least one minute's price.
- **Only the operator adds money.** Top-ups, discounts and adjustments are
  recorded through `python -m app.wallet`, never through a workspace endpoint —
  a client's own admin cannot credit their own wallet.
- **A workspace without a wallet is not metered.** It calls freely; see
  `app.models.billing`.

The ledger is append-only. The cached `wallets.balance` moves only together
with a new `wallet_transactions` row, in one transaction, under a row lock.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.errors import api_error
from app.models.billing import Wallet, WalletTransaction
from app.models.enums import VoiceCallStatus, WalletTransactionKind
from app.models.voice import VoiceCallExecution
from app.models.workspace import Workspace
from app.schemas.common import Page, PageParams
from app.tenancy.session import ScopedSession

logger = logging.getLogger(__name__)

__all__ = [
    "OPERATOR_KINDS",
    "WalletService",
    "WalletUsage",
    "billable_minutes",
    "price_of",
]

CENT = Decimal("0.01")

#: What the operator may record by hand. `CALL_CHARGE` is the system's alone.
OPERATOR_KINDS = frozenset(
    {WalletTransactionKind.TOPUP, WalletTransactionKind.DISCOUNT, WalletTransactionKind.ADJUSTMENT}
)


def billable_minutes(seconds: int) -> int:
    """Started minutes in `seconds` of connected time. 0 for no connection."""
    return -(-seconds // 60) if seconds > 0 else 0


def price_of(minutes: int, rate_per_minute: Decimal) -> Decimal:
    """`minutes` at `rate_per_minute`, to the cent."""
    return (rate_per_minute * minutes).quantize(CENT, rounding=ROUND_HALF_UP)


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclasses.dataclass(frozen=True, slots=True)
class WalletUsage:
    """Connected AI-call time billed since a moment."""

    minutes: int
    connected_calls: int
    spent: Decimal


class WalletService:
    """Built per request (or per command), like every service in this codebase."""

    def __init__(self, session: ScopedSession, *, workspace: Workspace) -> None:
        self._session = session
        self._workspace = workspace

    # --- reading ------------------------------------------------------------

    async def get(self, *, for_update: bool = False) -> Wallet | None:
        stmt = self._session.select(Wallet)
        if for_update:
            stmt = stmt.with_for_update()
        result = await self._session.execute(stmt)
        wallet: Wallet | None = result.scalar_one_or_none()
        return wallet

    async def require(self, *, for_update: bool = False) -> Wallet:
        wallet = await self.get(for_update=for_update)
        if wallet is None:
            raise api_error(
                404, "wallet_not_configured", "Billing has not been set up for this workspace"
            )
        return wallet

    async def usage(self, since: dt.datetime) -> WalletUsage:
        """Billed minutes, connected calls and spend from `since` onwards."""
        result = await self._session.execute(
            select(
                func.coalesce(func.sum(WalletTransaction.billed_minutes), 0),
                func.count(func.distinct(WalletTransaction.voice_call_id)),
                func.coalesce(func.sum(WalletTransaction.amount), 0),
            ).where(
                WalletTransaction.workspace_id == self._session.workspace_id,
                WalletTransaction.kind == WalletTransactionKind.CALL_CHARGE,
                WalletTransaction.created_at >= since,
            )
        )
        minutes, calls, amount = result.one()
        return WalletUsage(minutes=int(minutes), connected_calls=int(calls), spent=_money(-amount))

    def period_starts(self, now: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime]:
        """Start of today and of this month, in the *workspace's* timezone.

        Rule 10: a client in Kolkata asking "how many minutes today" means their
        today, not the server's and not the browser's.
        """
        zone = ZoneInfo(self._workspace.timezone)
        local = (now or dt.datetime.now(dt.UTC)).astimezone(zone)
        day = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return day, day.replace(day=1)

    async def statement(
        self, page: PageParams, *, kind: WalletTransactionKind | None = None
    ) -> Page[WalletTransaction]:
        """The ledger, newest first."""
        stmt = self._session.select(WalletTransaction).order_by(
            WalletTransaction.created_at.desc(), WalletTransaction.id.desc()
        )
        count = (
            select(func.count())
            .select_from(WalletTransaction)
            .where(WalletTransaction.workspace_id == self._session.workspace_id)
        )
        if kind is not None:
            stmt = stmt.where(WalletTransaction.kind == kind)
            count = count.where(WalletTransaction.kind == kind)
        total = int((await self._session.execute(count)).scalar() or 0)
        rows = await self._session.execute(stmt.limit(page.limit).offset(page.offset))
        return Page(
            items=list(rows.scalars().all()), total=total, limit=page.limit, offset=page.offset
        )

    # --- the gate -----------------------------------------------------------

    async def ensure_can_call(self) -> None:
        """Refuse to dial unless the wallet covers at least one minute.

        402 Payment Required, with the balance and what is needed, so the
        screen can say exactly how short the client is. Unmetered workspaces
        (no wallet) are never refused.
        """
        wallet = await self.get()
        if wallet is None:
            return
        needed = price_of(1, wallet.rate_per_minute)
        # A zero-rate wallet is a free plan: nothing to run out of.
        if needed > 0 and wallet.balance < needed:
            raise api_error(
                402,
                "wallet_insufficient_balance",
                "Your call balance is too low to place a call. Please recharge your wallet.",
                balance=str(_money(wallet.balance)),
                required=str(needed),
                currency=wallet.currency,
            )

    # --- charging -----------------------------------------------------------

    async def charge_number_rental(self, amount: Decimal, *, note: str) -> WalletTransaction:
        """One period of a calling number's rent (docs/16), paid up front.

        Unlike a call, rent is never allowed to take the balance below zero:
        402 when the wallet cannot cover it, and nothing is written.
        """
        price = _money(amount)
        wallet = await self.require(for_update=True)
        if wallet.balance < price:
            raise api_error(
                402,
                "wallet_insufficient_balance",
                "Your wallet balance is too low for this number. Please recharge your wallet.",
                balance=str(_money(wallet.balance)),
                required=str(price),
                currency=wallet.currency,
            )
        wallet.balance = _money(wallet.balance - price)
        row = WalletTransaction(
            wallet_id=wallet.id,
            kind=WalletTransactionKind.NUMBER_RENTAL,
            amount=-price,
            currency=wallet.currency,
            balance_after=wallet.balance,
            note=note,
            recorded_by=None,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def settle_call(self, call: VoiceCallExecution) -> WalletTransaction | None:
        """Charge a finished call what it still owes. Safe to run repeatedly.

        A call's result can arrive in parts — the first terminal delivery may
        carry no duration and a later one the real figure (see
        `VoiceCallService._upgrade`). So this does not charge "the call"; it
        works out what the call should cost in total, subtracts what the
        ledger already holds for it, and charges the difference. Run twice, the
        second run finds nothing owed and writes nothing.

        Each row records the minutes and seconds *it* added, so totals are a
        plain sum. A later delivery reporting *less* time never refunds
        automatically — that is the operator's call, as an `ADJUSTMENT`.

        The caller holds the execution row lock; this takes the wallet's, so
        two calls finishing at once cannot both read the same balance.
        """
        wallet = await self.get(for_update=True)
        if wallet is None:
            return None

        connected = call.status == VoiceCallStatus.COMPLETED and (call.duration_seconds or 0) > 0
        seconds = (call.duration_seconds or 0) if connected else 0
        minutes = billable_minutes(seconds)

        result = await self._session.execute(
            select(
                func.coalesce(func.sum(WalletTransaction.billed_minutes), 0),
                func.coalesce(func.sum(WalletTransaction.billed_seconds), 0),
                func.min(WalletTransaction.rate_per_minute),
            ).where(
                WalletTransaction.workspace_id == self._session.workspace_id,
                WalletTransaction.kind == WalletTransactionKind.CALL_CHARGE,
                WalletTransaction.voice_call_id == call.id,
            )
        )
        billed_minutes, billed_seconds, first_rate = result.one()
        owed_minutes = minutes - int(billed_minutes)
        if owed_minutes < 0:
            logger.warning(
                "wallet.settle call=%s reported %ss after %s minutes were billed; no refund",
                call.id,
                seconds,
                billed_minutes,
            )
        if owed_minutes <= 0:
            return None

        # The price the call was first billed at, so a rate change between two
        # deliveries of one call cannot price its halves differently.
        rate = Decimal(first_rate) if first_rate is not None else wallet.rate_per_minute
        amount = price_of(owed_minutes, rate)
        if amount <= 0:  # a zero-rate wallet records nothing
            return None

        wallet.balance = _money(wallet.balance - amount)
        row = WalletTransaction(
            wallet_id=wallet.id,
            kind=WalletTransactionKind.CALL_CHARGE,
            amount=-amount,
            currency=wallet.currency,
            balance_after=wallet.balance,
            voice_call_id=call.id,
            billed_seconds=seconds - int(billed_seconds),
            billed_minutes=owed_minutes,
            rate_per_minute=rate,
        )
        self._session.add(row)
        await self._session.flush()
        logger.info(
            "wallet.charge call=%s minutes=%s amount=%s balance=%s",
            call.id,
            owed_minutes,
            amount,
            wallet.balance,
        )
        return row

    # --- operator actions (python -m app.wallet) ----------------------------

    async def create(self, *, rate_per_minute: Decimal) -> Wallet:
        """Start metering this workspace, at an opening balance of zero."""
        if await self.get() is not None:
            raise api_error(409, "wallet_exists", "This workspace already has a wallet")
        rate = _money(rate_per_minute)
        if rate < 0:
            raise api_error(422, "invalid_rate", "The rate cannot be negative")
        wallet = Wallet(
            currency=self._workspace.currency, balance=Decimal("0.00"), rate_per_minute=rate
        )
        self._session.add(wallet)
        await self._session.flush()
        return wallet

    async def set_rate(self, rate_per_minute: Decimal) -> Wallet:
        """Change the price of future minutes. Past charges keep their rate."""
        rate = _money(rate_per_minute)
        if rate < 0:
            raise api_error(422, "invalid_rate", "The rate cannot be negative")
        wallet = await self.require(for_update=True)
        wallet.rate_per_minute = rate
        await self._session.flush()
        return wallet

    async def record(
        self,
        kind: WalletTransactionKind,
        amount: Decimal,
        *,
        note: str | None,
        recorded_by: str | None,
    ) -> WalletTransaction:
        """A top-up, discount or adjustment, recorded by the operator."""
        if kind not in OPERATOR_KINDS:
            raise api_error(422, "invalid_kind", f"{kind.value} cannot be recorded by hand")
        value = _money(amount)
        note = (note or "").strip() or None
        if kind in (WalletTransactionKind.TOPUP, WalletTransactionKind.DISCOUNT) and value <= 0:
            raise api_error(422, "invalid_amount", f"A {kind.value.lower()} must be positive")
        if kind == WalletTransactionKind.ADJUSTMENT:
            if value == 0:
                raise api_error(422, "invalid_amount", "An adjustment cannot be zero")
            if note is None:
                raise api_error(422, "note_required", "An adjustment needs a note saying why")

        wallet = await self.require(for_update=True)
        wallet.balance = _money(wallet.balance + value)
        row = WalletTransaction(
            wallet_id=wallet.id,
            kind=kind,
            amount=value,
            currency=wallet.currency,
            balance_after=wallet.balance,
            note=note,
            recorded_by=recorded_by,
        )
        self._session.add(row)
        await self._session.flush()
        return row
