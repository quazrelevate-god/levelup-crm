"""Calling numbers a client requests and rents from the wallet (docs/16).

A client can only ask. The first 30 days are paid up front; the operator buys
the number and fulfils the request; each later period is charged when due, and
a period the wallet cannot cover takes the number out of the rotation.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import WorkspaceFixture, add_member, build_workspace, login

from app.auth.passwords import PasswordHasherService
from app.models.billing import VoiceNumberRequest, Wallet, WalletTransaction
from app.models.enums import WalletTransactionKind
from app.services.number_requests import PERIOD, NumberRequestService
from app.services.wallet import WalletService
from app.tenancy.session import ScopedSession

PRICE = Decimal("850.00")
NUMBER = "+919000000555"


@pytest.fixture
def hasher(wired_app: FastAPI) -> PasswordHasherService:
    hasher = wired_app.state.password_hasher
    assert isinstance(hasher, PasswordHasherService)
    return hasher


@pytest.fixture
async def ws(db_session: AsyncSession, hasher: PasswordHasherService) -> WorkspaceFixture:
    fixture = await build_workspace(
        db_session, hasher, name="Rent Co", owner_email="owner@rent.example"
    )
    fixture.workspace.default_country_code = "91"
    await db_session.commit()
    return fixture


async def _fund(session: AsyncSession, ws: WorkspaceFixture, balance: str) -> None:
    service = WalletService(ScopedSession(session, ws.id), workspace=ws.workspace)
    await service.create(rate_per_minute=Decimal("10"))
    if Decimal(balance) > 0:
        await service.record(
            WalletTransactionKind.TOPUP, Decimal(balance), note="opening", recorded_by="test"
        )
    await session.commit()


async def _balance(session: AsyncSession, ws: WorkspaceFixture) -> Decimal:
    row = await session.execute(
        select(Wallet.balance)
        .where(Wallet.workspace_id == ws.id)
        .execution_options(populate_existing=True)
    )
    return Decimal(row.scalar_one())


async def _rentals(session: AsyncSession, ws: WorkspaceFixture) -> list[Decimal]:
    rows = await session.execute(
        select(WalletTransaction.amount).where(
            WalletTransaction.workspace_id == ws.id,
            WalletTransaction.kind == WalletTransactionKind.NUMBER_RENTAL,
        )
    )
    return [Decimal(a) for a in rows.scalars().all()]


async def _request(api: AsyncClient, ws: WorkspaceFixture, **body: Any) -> Any:
    return await api.post(ws.path("/voice/number-requests"), headers=ws.owner.auth, json=body)


async def _reload(session: AsyncSession, ws: WorkspaceFixture) -> None:
    await session.refresh(ws.workspace)


# --- the client asks -------------------------------------------------------------


async def test_a_request_charges_the_first_30_days_and_waits_for_the_operator(
    api: AsyncClient, ws: WorkspaceFixture, db_session: AsyncSession
) -> None:
    await _fund(db_session, ws, "2000")
    await login(api, ws.owner)

    response = await _request(api, ws, region="Karnataka", note="For VE calls")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "PENDING"
    assert body["monthly_price"] == "850.00"
    assert body["region"] == "Karnataka"
    assert await _balance(db_session, ws) == Decimal("1150.00")
    assert await _rentals(db_session, ws) == [-PRICE]
    await _reload(db_session, ws)
    assert ws.workspace.voice_calling_numbers == []  # nothing is added by asking


async def test_a_wallet_that_cannot_cover_it_refuses_and_records_nothing(
    api: AsyncClient, ws: WorkspaceFixture, db_session: AsyncSession
) -> None:
    await _fund(db_session, ws, "500")
    await login(api, ws.owner)

    response = await _request(api, ws)

    assert response.status_code == 402, response.text
    assert response.json()["detail"]["code"] == "wallet_insufficient_balance"
    assert await _balance(db_session, ws) == Decimal("500.00")
    rows = await db_session.execute(select(VoiceNumberRequest))
    assert rows.scalars().all() == []


async def test_without_a_wallet_a_number_cannot_be_requested(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    await login(api, ws.owner)

    response = await _request(api, ws)

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "wallet_not_configured"


async def test_a_member_who_is_not_an_admin_cannot_request(
    api: AsyncClient,
    ws: WorkspaceFixture,
    db_session: AsyncSession,
    hasher: PasswordHasherService,
) -> None:
    await _fund(db_session, ws, "2000")
    caller = await add_member(
        db_session, hasher, ws, key="caller", email="caller@rent.example", template_name="Caller"
    )
    await login(api, caller)

    response = await api.post(ws.path("/voice/number-requests"), headers=caller.auth, json={})

    assert response.status_code == 403, response.text
    assert await _balance(db_session, ws) == Decimal("2000.00")


# --- the operator fulfils or cancels -------------------------------------------


async def test_the_operator_fulfils_and_the_number_joins_the_rotation(
    api: AsyncClient,
    ws: WorkspaceFixture,
    db_session: AsyncSession,
    make_operator: Callable[..., None],
) -> None:
    await _fund(db_session, ws, "2000")
    make_operator(ws.owner.user.email)
    await login(api, ws.owner)
    requested = (await _request(api, ws)).json()

    response = await api.post(
        ws.path(f"/voice/number-requests/{requested['id']}/fulfil"),
        headers=ws.owner.auth,
        json={"phone_number": "90000 00555"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ACTIVE"
    assert body["phone_number"] == NUMBER
    assert body["paid_until"] is not None
    await _reload(db_session, ws)
    assert ws.workspace.voice_calling_numbers == [NUMBER]


@pytest.mark.parametrize("action", ["fulfil", "cancel"])
async def test_a_client_admin_cannot_fulfil_or_cancel(
    api: AsyncClient,
    ws: WorkspaceFixture,
    db_session: AsyncSession,
    make_operator: Callable[..., None],
    action: str,
) -> None:
    """Fulfilling would let a client add a number; cancelling would refund it."""
    await _fund(db_session, ws, "2000")
    make_operator("operator@example.com")
    await login(api, ws.owner)
    requested = (await _request(api, ws)).json()

    response = await api.post(
        ws.path(f"/voice/number-requests/{requested['id']}/{action}"),
        headers=ws.owner.auth,
        json={"phone_number": NUMBER} if action == "fulfil" else None,
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "operator_only"
    await _reload(db_session, ws)
    assert ws.workspace.voice_calling_numbers == []
    assert await _balance(db_session, ws) == Decimal("1150.00")


async def test_cancelling_a_pending_request_refunds_it(
    api: AsyncClient,
    ws: WorkspaceFixture,
    db_session: AsyncSession,
    make_operator: Callable[..., None],
) -> None:
    await _fund(db_session, ws, "2000")
    make_operator(ws.owner.user.email)
    await login(api, ws.owner)
    requested = (await _request(api, ws)).json()

    response = await api.post(
        ws.path(f"/voice/number-requests/{requested['id']}/cancel"), headers=ws.owner.auth
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "CANCELLED"
    assert await _balance(db_session, ws) == Decimal("2000.00")


async def test_cancelling_a_rented_number_releases_it_without_a_refund(
    api: AsyncClient,
    ws: WorkspaceFixture,
    db_session: AsyncSession,
    make_operator: Callable[..., None],
) -> None:
    await _fund(db_session, ws, "2000")
    make_operator(ws.owner.user.email)
    await login(api, ws.owner)
    requested = (await _request(api, ws)).json()
    await api.post(
        ws.path(f"/voice/number-requests/{requested['id']}/fulfil"),
        headers=ws.owner.auth,
        json={"phone_number": NUMBER},
    )

    await api.post(
        ws.path(f"/voice/number-requests/{requested['id']}/cancel"), headers=ws.owner.auth
    )

    await _reload(db_session, ws)
    assert ws.workspace.voice_calling_numbers == []
    assert await _balance(db_session, ws) == Decimal("1150.00")


# --- renewals ---------------------------------------------------------------------


async def _rented(
    session: AsyncSession, ws: WorkspaceFixture, balance: str
) -> NumberRequestService:
    """A fulfilled number, as the operator leaves it."""
    await _fund(session, ws, balance)
    service = NumberRequestService(ScopedSession(session, ws.id), workspace=ws.workspace)
    row = await service.request(price=PRICE, region=None, note=None, requested_by_id=None)
    await service.fulfil(row.id, NUMBER)
    await session.commit()
    return service


async def test_a_period_that_falls_due_is_charged(
    ws: WorkspaceFixture, db_session: AsyncSession
) -> None:
    service = await _rented(db_session, ws, "2000")
    [row] = await service.list()
    first_paid_until = row.paid_until
    assert first_paid_until is not None

    await service.settle_due(first_paid_until + dt.timedelta(hours=1))

    assert row.status == "ACTIVE"
    assert row.paid_until == first_paid_until + PERIOD
    assert await _rentals(db_session, ws) == [-PRICE, -PRICE]


async def test_an_unpaid_period_takes_the_number_out_until_the_wallet_pays(
    ws: WorkspaceFixture, db_session: AsyncSession
) -> None:
    service = await _rented(db_session, ws, "900")  # 50 left after the first period
    [row] = await service.list()
    due = row.paid_until
    assert due is not None

    await service.settle_due(due + dt.timedelta(hours=1))

    assert row.status == "PAST_DUE"
    assert ws.workspace.voice_calling_numbers == []

    wallet = WalletService(ScopedSession(db_session, ws.id), workspace=ws.workspace)
    await wallet.record(WalletTransactionKind.TOPUP, Decimal("1000"), note="t", recorded_by="t")
    later = due + dt.timedelta(days=2)
    await service.settle_due(later)

    assert row.status == "ACTIVE"
    assert row.paid_until == later + PERIOD
    assert ws.workspace.voice_calling_numbers == [NUMBER]


async def test_nothing_is_charged_before_it_is_due(
    ws: WorkspaceFixture, db_session: AsyncSession
) -> None:
    service = await _rented(db_session, ws, "2000")

    await service.settle_due()

    assert await _rentals(db_session, ws) == [-PRICE]


# --- the list ------------------------------------------------------------------------


async def test_the_list_shows_requests_the_price_and_who_may_act(
    api: AsyncClient,
    ws: WorkspaceFixture,
    db_session: AsyncSession,
    make_operator: Callable[..., None],
) -> None:
    await _fund(db_session, ws, "2000")
    make_operator("operator@example.com")
    await login(api, ws.owner)
    await _request(api, ws, region="Maharashtra")

    response = await api.get(ws.path("/voice/number-requests"), headers=ws.owner.auth)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["monthly_price"] == "850.00"
    assert body["currency"] == "INR"
    assert body["is_operator"] is False
    assert [item["region"] for item in body["items"]] == ["Maharashtra"]
