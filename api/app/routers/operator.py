"""The operator console's API (docs/19): `/api/v1/operator/...`.

Platform operator only — named by `PLATFORM_OPERATOR_EMAILS`, which nothing in
the app can change. Not under a workspace path: the operator stands outside
every company and reaches into one at a time.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser, get_password_hasher
from app.auth.operator import require_platform_operator
from app.auth.passwords import PasswordHasherService
from app.dependencies import get_session
from app.models import Membership, PermissionTemplate, User
from app.models.enums import WalletTransactionKind
from app.schemas.common import Page, PageParams, page_params
from app.schemas.operator import (
    CompanyCreate,
    CompanyCreated,
    CompanyRead,
    CompanyUpdate,
    FeatureRead,
    LinkRead,
    MoneyEntry,
    MoneyEntryRead,
    NumberRequestRead,
    NumberSettle,
    OperatorAccountRead,
    OperatorActionRead,
    OperatorLinkRequest,
    OverviewRead,
    PersonAdd,
    PersonAdded,
    PersonRead,
    PersonUpdate,
    RateUpdate,
    RoleRead,
    WalletRead,
)
from app.services.lead_ownership import LeadOwnership, get_lead_ownership
from app.services.operator_console import FEATURE_INFO, CompanyRow, OperatorConsole
from app.tenancy.features import DEFAULT_ENABLED, FEATURE_FLAGS

router = APIRouter(prefix="/operator", tags=["operator"])

Operator = Annotated[CurrentUser, Depends(require_platform_operator)]
Session = Annotated[AsyncSession, Depends(get_session)]


def _console(
    request: Request,
    operator: Operator,
    session: Session,
    hasher: Annotated[PasswordHasherService, Depends(get_password_hasher)],
    lead_ownership: Annotated[LeadOwnership, Depends(get_lead_ownership)],
) -> OperatorConsole:
    return OperatorConsole(
        session,
        settings=request.app.state.settings,
        actor_id=operator.id,
        actor_email=operator.email,
        hasher=hasher,
        lead_ownership=lead_ownership,
    )


Console = Annotated[OperatorConsole, Depends(_console)]


def _company(row: CompanyRow) -> CompanyRead:
    ws = row.workspace
    return CompanyRead(
        id=ws.id,
        name=ws.name,
        slug=ws.slug,
        is_active=ws.is_active,
        owner_name=row.owner_name,
        owner_email=row.owner_email,
        seats_used=row.seats_used,
        seat_limit=ws.seat_limit,
        members=row.members,
        currency=ws.currency,
        timezone=ws.timezone,
        default_country_code=ws.default_country_code,
        balance=row.balance,
        rate_per_minute=row.rate_per_minute,
        minutes_this_month=row.minutes_this_month,
        low_balance=row.low_balance,
        support_active=row.support_active,
        features={key: bool((ws.features or {}).get(key, False)) for key in sorted(FEATURE_FLAGS)},
        created_at=ws.created_at,
    )


def _person(m: Membership, user: User, role: PermissionTemplate) -> PersonRead:
    return PersonRead(
        membership_id=m.id,
        user_id=user.id,
        full_name=user.full_name,
        email=user.email,
        role_id=role.id,
        role_name=role.name,
        manager_id=m.manager_id,
        is_active=m.is_active,
        has_license=m.has_license,
        is_support=m.is_support,
        created_at=m.created_at,
    )


# --- overview and catalogue -------------------------------------------------------


@router.get("/overview", response_model=OverviewRead, summary="Every company at a glance")
async def overview(console: Console) -> OverviewRead:
    rows = await console.companies()
    totals: dict[str, Decimal] = {}
    for row in rows:
        if row.balance is not None:
            currency = row.workspace.currency
            totals[currency] = totals.get(currency, Decimal("0")) + row.balance
    return OverviewRead(
        companies=len(rows),
        active_companies=sum(1 for r in rows if r.workspace.is_active),
        licensed_users=sum(r.seats_used for r in rows),
        wallet_totals=totals,
        ai_calls_24h=await console.calls_today(),
        low_balance_at=console.low_balance_at,
        low_balance=[_company(r) for r in rows if r.low_balance and r.workspace.is_active],
        pending_number_requests=len(await console.pending_numbers()),
    )


@router.get("/features", response_model=list[FeatureRead], summary="Modules a company can have")
async def features(operator: Operator) -> list[FeatureRead]:
    return [
        FeatureRead(
            key=key,
            label=FEATURE_INFO.get(key, (key, ""))[0],
            description=FEATURE_INFO.get(key, ("", ""))[1],
            on_by_default=key in DEFAULT_ENABLED,
        )
        for key in sorted(FEATURE_FLAGS)
    ]


# --- companies ------------------------------------------------------------------------


@router.get("/companies", response_model=list[CompanyRead], summary="The companies ledger")
async def list_companies(console: Console) -> list[CompanyRead]:
    return [_company(row) for row in await console.companies()]


@router.post(
    "/companies",
    response_model=CompanyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Create a company, its wallet and its owner (Root)",
)
async def create_company(
    payload: CompanyCreate, console: Console, session: Session
) -> CompanyCreated:
    workspace, invitation = await console.create_company(
        name=payload.name,
        default_country_code=payload.default_country_code,
        timezone=payload.timezone,
        currency=payload.currency,
        seat_limit=payload.seat_limit,
        features=payload.features,
        opening_balance=payload.opening_balance,
        rate_per_minute=payload.rate_per_minute,
        owner_name=payload.owner_name,
        owner_email=str(payload.owner_email),
    )
    workspace_id = workspace.id
    await session.commit()
    return CompanyCreated(
        company=_company(await console.company(workspace_id)),
        owner=LinkRead(link=invitation.link, expires_at=invitation.expires_at),
    )


@router.get("/companies/{workspace_id}", response_model=CompanyRead, summary="One company")
async def get_company(workspace_id: uuid.UUID, console: Console) -> CompanyRead:
    return _company(await console.company(workspace_id))


@router.patch(
    "/companies/{workspace_id}",
    response_model=CompanyRead,
    summary="Rename, change seats or features, suspend or reactivate",
)
async def update_company(
    workspace_id: uuid.UUID, payload: CompanyUpdate, console: Console, session: Session
) -> CompanyRead:
    await console.update_company(
        workspace_id,
        name=payload.name,
        seat_limit=payload.seat_limit,
        features=payload.features,
        is_active=payload.is_active,
        timezone=payload.timezone,
        default_country_code=payload.default_country_code,
    )
    await session.commit()
    return _company(await console.company(workspace_id))


# --- people --------------------------------------------------------------------------------


@router.get(
    "/companies/{workspace_id}/roles", response_model=list[RoleRead], summary="The company's roles"
)
async def list_roles(workspace_id: uuid.UUID, console: Console) -> list[RoleRead]:
    return [
        RoleRead(
            id=role.id,
            name=role.name,
            is_locked=role.is_readonly,
            is_admin=any(
                bool((role.capabilities or {}).get(group, {}).get("admin_access"))
                for group in ("leads", "permissions")
            ),
        )
        for role in await console.roles(workspace_id)
    ]


@router.get(
    "/companies/{workspace_id}/people",
    response_model=list[PersonRead],
    summary="Everyone in the company",
)
async def list_people(workspace_id: uuid.UUID, console: Console) -> list[PersonRead]:
    return [_person(m, m.user, m.template) for m in await console.people(workspace_id)]


async def _person_by_id(
    console: OperatorConsole, workspace_id: uuid.UUID, membership_id: uuid.UUID
) -> PersonRead:
    for m in await console.people(workspace_id):
        if m.id == membership_id:
            return _person(m, m.user, m.template)
    raise AssertionError("person vanished")  # pragma: no cover


@router.post(
    "/companies/{workspace_id}/people",
    response_model=PersonAdded,
    status_code=status.HTTP_201_CREATED,
    summary="Add a person at any role, with a sign-in link",
)
async def add_person(
    workspace_id: uuid.UUID, payload: PersonAdd, console: Console, session: Session
) -> PersonAdded:
    invitation = await console.add_person(
        workspace_id,
        full_name=payload.full_name,
        email=str(payload.email),
        template_id=payload.role_id,
        manager_id=payload.manager_id,
    )
    membership_id = invitation.membership.id
    await session.commit()
    return PersonAdded(
        person=await _person_by_id(console, workspace_id, membership_id),
        sign_in=LinkRead(link=invitation.link, expires_at=invitation.expires_at),
    )


@router.patch(
    "/companies/{workspace_id}/people/{membership_id}",
    response_model=PersonRead,
    summary="Change a person's role or manager, or switch them off",
)
async def update_person(
    workspace_id: uuid.UUID,
    membership_id: uuid.UUID,
    payload: PersonUpdate,
    console: Console,
    session: Session,
) -> PersonRead:
    await console.update_person(
        workspace_id,
        membership_id,
        template_id=payload.role_id,
        manager_id=payload.manager_id,
        clear_manager=payload.clear_manager,
        is_active=payload.is_active,
        reassign_to=payload.reassign_to,
    )
    await session.commit()
    return await _person_by_id(console, workspace_id, membership_id)


@router.post(
    "/companies/{workspace_id}/people/{membership_id}/sign-in-link",
    response_model=LinkRead,
    summary="A fresh set-your-password link for one person",
)
async def person_sign_in_link(
    workspace_id: uuid.UUID, membership_id: uuid.UUID, console: Console, session: Session
) -> LinkRead:
    invitation = await console.sign_in_link(workspace_id, membership_id)
    await session.commit()
    return LinkRead(link=invitation.link, expires_at=invitation.expires_at)


# --- money ------------------------------------------------------------------------------------


async def _wallet_read(
    console: OperatorConsole, workspace_id: uuid.UUID, page: PageParams
) -> WalletRead:
    company = await console.company(workspace_id)
    statement = await console.statement(workspace_id, page)
    return WalletRead(
        currency=company.workspace.currency,
        balance=company.balance,
        rate_per_minute=company.rate_per_minute,
        low_balance_at=console.low_balance_at,
        entries=[MoneyEntryRead.model_validate(t) for t in statement.items] if statement else [],
        total_entries=statement.total if statement else 0,
    )


@router.get(
    "/companies/{workspace_id}/wallet",
    response_model=WalletRead,
    summary="Balance, price per minute and money history",
)
async def get_wallet(
    workspace_id: uuid.UUID,
    console: Console,
    page: Annotated[PageParams, Depends(page_params)],
) -> WalletRead:
    return await _wallet_read(console, workspace_id, page)


@router.post(
    "/companies/{workspace_id}/wallet/entries",
    response_model=WalletRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add money, give free credit, or correct the balance",
)
async def add_money(
    workspace_id: uuid.UUID, payload: MoneyEntry, console: Console, session: Session
) -> WalletRead:
    await console.money(
        workspace_id,
        kind=WalletTransactionKind(payload.kind),
        amount=payload.amount,
        note=payload.note,
    )
    await session.commit()
    return await _wallet_read(console, workspace_id, PageParams())


@router.put(
    "/companies/{workspace_id}/wallet/rate",
    response_model=WalletRead,
    summary="Set the price per AI-call minute",
)
async def set_rate(
    workspace_id: uuid.UUID, payload: RateUpdate, console: Console, session: Session
) -> WalletRead:
    await console.set_rate(workspace_id, payload.rate_per_minute)
    await session.commit()
    return await _wallet_read(console, workspace_id, PageParams())


# --- support access -------------------------------------------------------------------------


@router.post(
    "/companies/{workspace_id}/support",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Go inside a company to help (as Root, holding no seat)",
)
async def start_support(workspace_id: uuid.UUID, console: Console, session: Session) -> None:
    await console.start_support(workspace_id)
    await session.commit()


@router.delete(
    "/companies/{workspace_id}/support",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Leave a company you went into to help",
)
async def end_support(workspace_id: uuid.UUID, console: Console, session: Session) -> None:
    await console.end_support(workspace_id)
    await session.commit()


# --- number requests ------------------------------------------------------------------------


@router.get(
    "/number-requests",
    response_model=list[NumberRequestRead],
    summary="Calling-number requests waiting, from every company",
)
async def pending_numbers(console: Console) -> list[NumberRequestRead]:
    return [
        NumberRequestRead(
            id=req.id,
            workspace_id=ws.id,
            company=ws.name,
            region=req.region,
            monthly_price=req.monthly_price,
            created_at=req.created_at,
        )
        for req, ws in await console.pending_numbers()
    ]


@router.post(
    "/companies/{workspace_id}/number-requests/{request_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Give the number (with phone_number) or cancel the request (without)",
)
async def settle_number(
    workspace_id: uuid.UUID,
    request_id: uuid.UUID,
    payload: NumberSettle,
    console: Console,
    session: Session,
) -> None:
    await console.settle_number(workspace_id, request_id, phone_number=payload.phone_number)
    await session.commit()


# --- history and operator accounts ------------------------------------------------------------


@router.get(
    "/history",
    response_model=Page[OperatorActionRead],
    summary="Everything done from the console, newest first",
)
async def history(
    console: Console,
    page: Annotated[PageParams, Depends(page_params)],
    workspace_id: Annotated[uuid.UUID | None, Query()] = None,
) -> Page[OperatorActionRead]:
    rows, total = await console.history(
        workspace_id=workspace_id, limit=page.limit, offset=page.offset
    )
    return Page(
        items=[OperatorActionRead.model_validate(r) for r in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get(
    "/operators",
    response_model=list[OperatorAccountRead],
    summary="The operator addresses, and whether each has an account",
)
async def operators(console: Console) -> list[OperatorAccountRead]:
    return [
        OperatorAccountRead(email=email, has_account=user is not None)
        for email, user in await console.operator_accounts()
    ]


@router.post(
    "/operators/sign-in-link",
    response_model=LinkRead,
    summary="A set-your-password link for an operator address",
)
async def operator_link(
    payload: OperatorLinkRequest, console: Console, session: Session
) -> LinkRead:
    link, expires = await console.operator_link(str(payload.email))
    await session.commit()
    return LinkRead(link=link, expires_at=expires)
