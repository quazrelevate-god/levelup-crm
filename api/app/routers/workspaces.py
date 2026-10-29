"""Workspace endpoints.

`GET /workspaces` and `POST /workspaces` are unscoped — they are how a caller
finds or creates the workspaces they can then address. Everything under
`/workspaces/{workspace_id}` goes through `require_workspace`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser, get_current_user
from app.auth.operator import is_platform_operator, require_platform_operator
from app.dependencies import get_session
from app.errors import forbidden
from app.models import Membership, Workspace
from app.schemas.auth import WorkspaceSummary
from app.schemas.workspace import WorkspaceCreate, WorkspaceDetail, WorkspaceUpdate
from app.services.provisioning import WorkspaceProvisioner
from app.tenancy.scoping import WorkspaceScope, require_workspace, require_workspace_admin

router = APIRouter(tags=["workspaces"])


async def _seats_used(session: AsyncSession, workspace_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(Membership)
        .where(
            Membership.workspace_id == workspace_id,
            Membership.has_license.is_(True),
        )
    )
    return int(result.scalar_one())


def _detail(workspace: Workspace, seats_used: int) -> WorkspaceDetail:
    return WorkspaceDetail(
        id=workspace.id,
        name=workspace.name,
        slug=workspace.slug,
        default_country_code=workspace.default_country_code,
        timezone=workspace.timezone,
        currency=workspace.currency,
        connected_call_min_seconds=workspace.connected_call_min_seconds,
        session_timeout_minutes=workspace.session_timeout_minutes,
        leaderboard_metrics=workspace.leaderboard_metrics,
        features=workspace.features,
        seat_limit=workspace.seat_limit,
        seats_used=seats_used,
        is_active=workspace.is_active,
        identity_field_id=workspace.identity_field_id,
        primary_field_1_id=workspace.primary_field_1_id,
        primary_field_2_id=workspace.primary_field_2_id,
    )


@router.get(
    "/workspaces",
    response_model=list[WorkspaceSummary],
    summary="Workspaces the caller belongs to",
)
async def list_workspaces(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[WorkspaceSummary]:
    """Only the caller's own workspaces.

    This is the one list endpoint that legitimately queries across tenants, and
    it is bounded by `Membership.user_id`, not by a workspace scope.
    """
    result = await session.execute(
        select(Workspace)
        .join(Membership, Membership.workspace_id == Workspace.id)
        .where(
            Membership.user_id == current_user.id,
            Membership.is_active.is_(True),
            Workspace.is_active.is_(True),
        )
        .order_by(Workspace.name)
    )
    return [WorkspaceSummary.model_validate(workspace) for workspace in result.scalars().all()]


@router.post(
    "/workspaces",
    response_model=WorkspaceDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Provision a workspace (structure only)",
)
async def create_workspace(
    payload: WorkspaceCreate,
    current_user: Annotated[CurrentUser, Depends(require_platform_operator)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WorkspaceDetail:
    """Create a workspace and make the caller its Root member.

    Operator only (docs/16): a workspace is a sold account, so a signed-in
    client cannot mint another one, with seats of their choosing, for free.

    Provisioning creates structure and no taxonomy — see
    `app.services.provisioning` for exactly what that means and why the list
    lives in a registry.
    """
    provisioner = WorkspaceProvisioner(session)
    workspace, _membership = await provisioner.provision(
        name=payload.name,
        owner=current_user,
        slug=payload.slug,
        default_country_code=payload.default_country_code,
        timezone=payload.timezone,
        currency=payload.currency,
        seat_limit=payload.seat_limit,
    )
    await session.commit()
    await session.refresh(workspace)
    return _detail(workspace, seats_used=1)


@router.get(
    "/workspaces/{workspace_id}",
    response_model=WorkspaceDetail,
    summary="Workspace detail",
)
async def get_workspace(
    scope: Annotated[WorkspaceScope, Depends(require_workspace)],
) -> WorkspaceDetail:
    used, _limit = await _seat_usage_from_scope(scope)
    return _detail(scope.workspace, seats_used=used)


#: Workspace settings that are part of what the client pays for.
OPERATOR_ONLY_SETTINGS: frozenset[str] = frozenset({"seat_limit", "features"})


@router.patch(
    "/workspaces/{workspace_id}",
    response_model=WorkspaceDetail,
    summary="Update workspace name, preferences and feature flags",
)
async def update_workspace(
    payload: WorkspaceUpdate,
    request: Request,
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> WorkspaceDetail:
    workspace = scope.workspace
    updates = payload.model_dump(exclude_unset=True)

    # What the client bought — seats and paid modules — is the operator's to
    # change (docs/16). A workspace admin keeps everything else here.
    sold = sorted(OPERATOR_ONLY_SETTINGS & updates.keys())
    if sold and not is_platform_operator(request.app.state.settings, current_user):
        raise forbidden(
            "operator_only",
            "Seats and paid modules are changed by your account manager.",
            fields=sold,
        )

    if "seat_limit" in updates:
        used, _ = await _seat_usage_from_scope(scope)
        if updates["seat_limit"] < used:
            raise forbidden(
                "seat_limit_below_usage",
                f"{used} seats are in use; revoke licences before lowering the limit",
                seats_used=used,
            )

    for attribute, value in updates.items():
        setattr(workspace, attribute, value)

    await scope.session.commit()
    used, _ = await _seat_usage_from_scope(scope)
    return _detail(workspace, seats_used=used)


async def _seat_usage_from_scope(scope: WorkspaceScope) -> tuple[int, int]:
    result = await scope.session.execute(
        select(func.count())
        .select_from(Membership)
        .where(
            Membership.workspace_id == scope.workspace_id,
            Membership.has_license.is_(True),
        )
    )
    return int(result.scalar_one()), scope.workspace.seat_limit
