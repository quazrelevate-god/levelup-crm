"""Calling numbers a client asks for and rents from the wallet (docs/16).

The client never adds a number. They request one; the first period's rent is
taken from the wallet at once; the operator buys a number in Bolna and fulfils
the request, which puts it into the workspace's calling numbers. Each later
period is charged when it falls due.

Production runs no background worker, so renewals are charged when the
workspace next uses calling — listing its numbers or placing a call — via
`settle_due`. A number whose period cannot be paid becomes PAST_DUE and leaves
the rotation until the wallet covers it again.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from fastapi import HTTPException

from app.errors import api_error, not_found
from app.fields.registry import FieldValueError, ValidationContext, normalise_phone
from app.models.billing import VoiceNumberRequest
from app.models.enums import WalletTransactionKind
from app.models.workspace import Workspace
from app.services.wallet import WalletService
from app.tenancy.session import ScopedSession

__all__ = ["PERIOD", "NumberRequestService"]

#: One rental period. Thirty days rather than a calendar month, so a period
#: always costs the same and never needs month-length arithmetic.
PERIOD = dt.timedelta(days=30)


class NumberRequestService:
    def __init__(self, session: ScopedSession, *, workspace: Workspace) -> None:
        self._session = session
        self._workspace = workspace
        self._wallet = WalletService(session, workspace=workspace)

    async def list(self) -> list[VoiceNumberRequest]:
        rows = await self._session.execute(
            self._session.select(VoiceNumberRequest).order_by(VoiceNumberRequest.created_at.desc())
        )
        return list(rows.scalars().all())

    async def get(self, request_id: uuid.UUID) -> VoiceNumberRequest:
        row = await self._session.get(VoiceNumberRequest, request_id)
        if row is None:
            raise not_found("Number request")
        return row

    # --- the client ----------------------------------------------------------

    async def request(
        self,
        *,
        price: Decimal,
        region: str | None,
        note: str | None,
        requested_by_id: uuid.UUID | None,
    ) -> VoiceNumberRequest:
        """Charge the first period, then record the request. Both or neither."""
        wallet = await self._wallet.require()
        row = VoiceNumberRequest(
            requested_by_id=requested_by_id,
            region=(region or "").strip() or None,
            note=(note or "").strip() or None,
            status="PENDING",
            monthly_price=price,
            currency=wallet.currency,
        )
        await self._wallet.charge_number_rental(
            price, note="Calling number: first 30 days (requested)"
        )
        self._session.add(row)
        await self._session.flush()
        return row

    # --- the operator ----------------------------------------------------------

    async def fulfil(self, request_id: uuid.UUID, phone_number: str) -> VoiceNumberRequest:
        row = await self.get(request_id)
        if row.status != "PENDING":
            raise api_error(
                409, "number_request_not_pending", "Only a pending request can be fulfilled"
            )
        number = self._normalise(phone_number)
        now = dt.datetime.now(dt.UTC)
        row.phone_number = number
        row.status = "ACTIVE"
        row.fulfilled_at = now
        row.paid_until = now + PERIOD
        self._add_to_rotation(number)
        await self._session.flush()
        return row

    async def cancel(self, request_id: uuid.UUID) -> VoiceNumberRequest:
        """Pending: refund the first period. Fulfilled: release the number."""
        row = await self.get(request_id)
        if row.status == "CANCELLED":
            return row
        if row.status == "PENDING":
            await self._wallet.record(
                WalletTransactionKind.ADJUSTMENT,
                row.monthly_price,
                note="Refund: calling number request cancelled",
                recorded_by="system",
            )
        elif row.phone_number:
            self._remove_from_rotation(row.phone_number)
        row.status = "CANCELLED"
        row.cancelled_at = dt.datetime.now(dt.UTC)
        await self._session.flush()
        return row

    # --- renewals ------------------------------------------------------------

    async def settle_due(self, now: dt.datetime | None = None) -> None:
        """Charge every period that has fallen due, oldest first.

        A period the wallet cannot cover makes the number PAST_DUE and takes it
        out of the rotation; a PAST_DUE number is charged again on the next
        settle and comes back once the wallet covers it.
        """
        now = now or dt.datetime.now(dt.UTC)
        rows = await self._session.execute(
            self._session.select(VoiceNumberRequest).where(
                VoiceNumberRequest.status.in_(("ACTIVE", "PAST_DUE")),
                VoiceNumberRequest.paid_until <= now,
            )
        )
        for row in rows.scalars().all():
            while row.paid_until is not None and row.paid_until <= now:
                try:
                    await self._wallet.charge_number_rental(
                        row.monthly_price, note=f"Calling number {row.phone_number}: next 30 days"
                    )
                except HTTPException:
                    if row.status != "PAST_DUE" and row.phone_number:
                        self._remove_from_rotation(row.phone_number)
                    row.status = "PAST_DUE"
                    break
                if row.status == "PAST_DUE":
                    # Lapsed and now paid: a fresh period from today, not a
                    # back-payment for the days it was out of the rotation.
                    row.paid_until = now + PERIOD
                    row.status = "ACTIVE"
                    if row.phone_number:
                        self._add_to_rotation(row.phone_number)
                else:
                    row.paid_until = row.paid_until + PERIOD
        await self._session.flush()

    # --- helpers ---------------------------------------------------------------

    def _normalise(self, raw: str) -> str:
        context = ValidationContext(
            default_country_code=self._workspace.default_country_code,
            currency=self._workspace.currency,
            timezone=self._workspace.timezone,
        )
        try:
            number = normalise_phone(raw, context)
        except FieldValueError as exc:
            raise api_error(422, "invalid_calling_number", exc.message) from exc
        if not number:
            raise api_error(422, "invalid_calling_number", "A phone number is required")
        return number

    def _add_to_rotation(self, number: str) -> None:
        numbers = list(self._workspace.voice_calling_numbers or [])
        if number not in numbers:
            # A new list, not an in-place append: JSONB changes are only seen
            # by SQLAlchemy when the attribute itself is reassigned.
            self._workspace.voice_calling_numbers = [*numbers, number]

    def _remove_from_rotation(self, number: str) -> None:
        numbers = list(self._workspace.voice_calling_numbers or [])
        self._workspace.voice_calling_numbers = [n for n in numbers if n != number]
