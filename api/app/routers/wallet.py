"""The workspace's view of its AI-call wallet (docs/14).

Two reads and no writes. A member with `billings.view_billing` — Root by
default, Admin only if a workspace grants it — sees the balance, the minutes
used and the statement. Nobody inside a workspace can move money: top-ups,
discounts and adjustments are the operator's, through `python -m app.wallet`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.errors import api_error
from app.models.enums import WalletTransactionKind
from app.schemas.common import Page, PageParams, page_params
from app.schemas.wallet import WalletRead, WalletTransactionRead, WalletUsageRead
from app.services.wallet import WalletService, WalletUsage, price_of
from app.tenancy.scoping import WorkspaceScope, require_workspace

router = APIRouter(tags=["wallet"])


def _require_view(scope: WorkspaceScope) -> None:
    if not scope.capability("billings", "view_billing"):
        raise api_error(
            403,
            "insufficient_permissions",
            "This permission template does not allow: view billing",
        )


def _usage(usage: WalletUsage) -> WalletUsageRead:
    return WalletUsageRead(
        minutes=usage.minutes, connected_calls=usage.connected_calls, spent=usage.spent
    )


@router.get("/wallet", response_model=WalletRead, summary="AI-call balance and minutes used")
async def get_wallet(
    scope: Annotated[WorkspaceScope, Depends(require_workspace)],
) -> WalletRead:
    """404 `wallet_not_configured` when this workspace is not metered."""
    _require_view(scope)
    service = WalletService(scope.session, workspace=scope.workspace)
    wallet = await service.require()
    today, month = service.period_starts()
    minimum = price_of(1, wallet.rate_per_minute)
    return WalletRead(
        currency=wallet.currency,
        balance=wallet.balance,
        rate_per_minute=wallet.rate_per_minute,
        can_call=minimum <= 0 or wallet.balance >= minimum,
        minimum_to_call=minimum,
        today=_usage(await service.usage(today)),
        this_month=_usage(await service.usage(month)),
        updated_at=wallet.updated_at,
    )


@router.get(
    "/wallet/transactions",
    response_model=Page[WalletTransactionRead],
    summary="The wallet statement, newest first",
)
async def list_wallet_transactions(
    scope: Annotated[WorkspaceScope, Depends(require_workspace)],
    page: Annotated[PageParams, Depends(page_params)],
    kind: Annotated[WalletTransactionKind | None, Query()] = None,
) -> Page[WalletTransactionRead]:
    _require_view(scope)
    service = WalletService(scope.session, workspace=scope.workspace)
    await service.require()
    rows = await service.statement(page, kind=kind)
    return Page(
        items=[WalletTransactionRead.model_validate(row) for row in rows.items],
        total=rows.total,
        limit=rows.limit,
        offset=rows.offset,
    )
