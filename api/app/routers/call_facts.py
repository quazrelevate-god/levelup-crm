"""Call facts endpoints (docs/17). Workspace admins configure them.

Reference details are the workspace's own business data — which batch starts
when, who mentors it — so they are managed by the client's admins, like fields
and stages, not by the platform operator.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.models.call_facts import CallFactSet
from app.schemas.call_facts import (
    CallFactRead,
    CallFactSetCreate,
    CallFactSetRead,
    CallFactSetUpdate,
)
from app.services.call_facts import CallFactService, FactInput
from app.tenancy.scoping import WorkspaceScope, require_workspace_admin

router = APIRouter(tags=["call facts"])


def _service(scope: WorkspaceScope) -> CallFactService:
    return CallFactService(scope.session, workspace=scope.workspace)


def _read(service: CallFactService, row: CallFactSet) -> CallFactSetRead:
    return CallFactSetRead(
        id=row.id,
        name=row.name,
        priority=row.priority,
        conditions=row.conditions,
        rule=service.describe_rule(row),
        facts=[CallFactRead(**fact) for fact in row.facts],
        is_active=row.is_active,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get(
    "/settings/call-facts",
    response_model=list[CallFactSetRead],
    summary="Call fact sets, in the order they are checked",
)
async def list_call_facts(
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
) -> list[CallFactSetRead]:
    service = _service(scope)
    return [_read(service, row) for row in await service.list_sets()]


@router.post(
    "/settings/call-facts",
    response_model=CallFactSetRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a call fact set",
)
async def create_call_facts(
    payload: CallFactSetCreate,
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
) -> CallFactSetRead:
    service = _service(scope)
    row = await service.create(
        name=payload.name,
        priority=payload.priority,
        conditions=payload.conditions,
        facts=[FactInput(label=f.label, value=f.value) for f in payload.facts],
    )
    await scope.session.commit()
    # The database sets `updated_at`; read it back before answering.
    await scope.session.refresh(row)
    return _read(service, row)


@router.patch(
    "/settings/call-facts/{fact_set_id}",
    response_model=CallFactSetRead,
    summary="Edit a call fact set",
)
async def update_call_facts(
    fact_set_id: uuid.UUID,
    payload: CallFactSetUpdate,
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
) -> CallFactSetRead:
    service = _service(scope)
    row = await service.update(
        fact_set_id,
        name=payload.name,
        priority=payload.priority,
        conditions=payload.conditions,
        facts=(
            [FactInput(label=f.label, value=f.value) for f in payload.facts]
            if payload.facts is not None
            else None
        ),
        is_active=payload.is_active,
    )
    await scope.session.commit()
    # The database sets `updated_at`; read it back before answering.
    await scope.session.refresh(row)
    return _read(service, row)


@router.delete(
    "/settings/call-facts/{fact_set_id}",
    response_model=CallFactSetRead,
    summary="Archive a call fact set (soft delete)",
)
async def archive_call_facts(
    fact_set_id: uuid.UUID,
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
) -> CallFactSetRead:
    service = _service(scope)
    row = await service.update(fact_set_id, is_active=False)
    await scope.session.commit()
    # The database sets `updated_at`; read it back before answering.
    await scope.session.refresh(row)
    return _read(service, row)
