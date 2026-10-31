"""Connected apps, workflows and the never-AI-call rule (docs/18).

Workspace admins only: a workflow changes leads with the workspace's own
authority, and a connected app decides what an outside system may set off.
Gated on the `automations` feature like any other switchable module.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.errors import not_found
from app.models.automation import AppConnection, AppEvent, Workflow, WorkflowRun
from app.models.lead import Lead
from app.schemas.automation import (
    AppEventRead,
    AppRead,
    AppUpdate,
    AutomationCatalogue,
    GraphProblemRead,
    NeverCallRead,
    NeverCallWrite,
    OperatorRead,
    TriggerRead,
    VariableRead,
    WorkflowCreate,
    WorkflowRead,
    WorkflowRunRead,
    WorkflowTestRequest,
    WorkflowTestResult,
    WorkflowUpdate,
)
from app.schemas.common import Page, PageParams, page_params
from app.services.apps import APPS, AppService
from app.services.system_filters import parse_rule
from app.services.voice_settings import read_never_call, write_never_call
from app.services.workflows import (
    OPERATORS,
    TRIGGERS,
    WorkflowEngine,
    WorkflowService,
    WorkflowStats,
)
from app.tenancy.features import require_feature
from app.tenancy.scoping import WorkspaceScope, require_workspace_admin

router = APIRouter(tags=["automation"], dependencies=[Depends(require_feature("automations"))])
#: Not behind the feature: the never-call rule protects AI calling itself.
voice_rules_router = APIRouter(tags=["voice"])

#: Where a connected app posts, under the API's own base URL.
HOOK_PATH = "/api/v1/hooks/{provider}/{token}"

Admin = Annotated[WorkspaceScope, Depends(require_workspace_admin)]


def lead_name(lead: Lead | None) -> str | None:
    if lead is None:
        return None
    name = (lead.values or {}).get("name")
    return str(name) if name else lead.identity_value


# --- catalogue ---------------------------------------------------------------


@router.get(
    "/automation/catalogue",
    response_model=AutomationCatalogue,
    summary="Triggers and comparisons a workflow can use",
)
async def automation_catalogue(scope: Admin) -> AutomationCatalogue:
    return AutomationCatalogue(
        triggers=[
            TriggerRead(
                key=t.key,
                label=t.label,
                app=t.app,
                description=t.description,
                variables=[
                    VariableRead(key=v.key, label=v.label, kind=v.kind) for v in t.variables
                ],
            )
            for t in TRIGGERS.values()
        ],
        operators=[
            OperatorRead(key=k, label=v, needs_value=k not in ("is_empty", "is_not_empty"))
            for k, v in OPERATORS.items()
        ],
    )


# --- connected apps ---------------------------------------------------------------


def _app_read(provider: str, row: AppConnection | None) -> AppRead:
    spec = APPS[provider]
    return AppRead(
        provider=provider,
        label=spec.label,
        description=spec.description,
        connected=row is not None,
        is_active=bool(row and row.is_active),
        webhook_path=HOOK_PATH.format(provider=provider, token=row.hook_token) if row else None,
        secret_set=bool(row and row.secret),
        settings={**spec.settings, **((row.settings or {}) if row else {})},
        last_event_at=row.last_event_at if row else None,
    )


@router.get("/apps", response_model=list[AppRead], summary="Apps, connected or not")
async def list_apps(scope: Admin) -> list[AppRead]:
    connected = await AppService(scope.session, workspace=scope.workspace).list_connections()
    return [_app_read(key, connected.get(key)) for key in APPS]


@router.post(
    "/apps/{provider}/connect",
    response_model=AppRead,
    status_code=status.HTTP_201_CREATED,
    summary="Connect an app and get its webhook URL",
)
async def connect_app(provider: str, scope: Admin) -> AppRead:
    row = await AppService(scope.session, workspace=scope.workspace).connect(
        provider, actor_id=scope.membership_id
    )
    await scope.session.commit()
    await scope.session.refresh(row)
    return _app_read(provider, row)


@router.patch("/apps/{provider}", response_model=AppRead, summary="Configure a connected app")
async def update_app(provider: str, payload: AppUpdate, scope: Admin) -> AppRead:
    row = await AppService(scope.session, workspace=scope.workspace).update(
        provider, is_active=payload.is_active, secret=payload.secret, settings=payload.settings
    )
    await scope.session.commit()
    await scope.session.refresh(row)
    return _app_read(provider, row)


@router.post(
    "/apps/{provider}/rotate-url",
    response_model=AppRead,
    summary="Issue a new webhook URL (the old one stops working)",
)
async def rotate_app_url(provider: str, scope: Admin) -> AppRead:
    row = await AppService(scope.session, workspace=scope.workspace).rotate_url(provider)
    await scope.session.commit()
    await scope.session.refresh(row)
    return _app_read(provider, row)


def _event_read(event: AppEvent, lead: Lead | None) -> AppEventRead:
    return AppEventRead(
        id=event.id,
        event_type=event.event_type,
        trigger=event.trigger,
        outcome=event.outcome,
        data=event.data or {},
        lead_id=event.lead_id,
        lead_name=lead_name(lead),
        workflows_run=event.workflows_run,
        error=event.error,
        created_at=event.created_at,
    )


@router.get(
    "/apps/{provider}/events",
    response_model=Page[AppEventRead],
    summary="What the app has sent, newest first",
)
async def list_app_events(
    provider: str,
    scope: Admin,
    page: Annotated[PageParams, Depends(page_params)],
) -> Page[AppEventRead]:
    found, total = await AppService(scope.session, workspace=scope.workspace).events(
        provider, limit=page.limit, offset=page.offset
    )
    return Page(
        items=[_event_read(e, lead) for e, lead in found],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


# --- workflows --------------------------------------------------------------------


def _service(scope: WorkspaceScope) -> WorkflowService:
    return WorkflowService(scope.session, workspace=scope.workspace, actor_id=scope.membership_id)


async def _workflow_read(
    service: WorkflowService, row: Workflow, stats: WorkflowStats | None = None
) -> WorkflowRead:
    numbers = stats or WorkflowStats()
    return WorkflowRead(
        id=row.id,
        name=row.name,
        trigger=row.trigger,
        graph=row.graph or {},
        is_active=row.is_active,
        total_runs=numbers.total_runs,
        runs_24h=numbers.runs_24h,
        failed_24h=numbers.failed_24h,
        last_run_at=numbers.last_run_at,
        problems=[
            GraphProblemRead(node_id=p.node_id, message=p.message)
            for p in await service.problems(row)
        ],
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/workflows", response_model=list[WorkflowRead], summary="Workflows")
async def list_workflows(scope: Admin) -> list[WorkflowRead]:
    service = _service(scope)
    stats = await service.stats()
    return [
        await _workflow_read(service, row, stats.get(row.id))
        for row in await service.list_workflows()
    ]


@router.post(
    "/workflows",
    response_model=WorkflowRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a workflow (it starts switched off)",
)
async def create_workflow(payload: WorkflowCreate, scope: Admin) -> WorkflowRead:
    service = _service(scope)
    row = await service.create(name=payload.name, graph=payload.graph, trigger=payload.trigger)
    await scope.session.commit()
    await scope.session.refresh(row)
    return await _workflow_read(service, row)


@router.get("/workflows/{workflow_id}", response_model=WorkflowRead, summary="One workflow")
async def get_workflow(workflow_id: uuid.UUID, scope: Admin) -> WorkflowRead:
    service = _service(scope)
    row = await service.get(workflow_id)
    return await _workflow_read(service, row, (await service.stats()).get(row.id))


@router.patch(
    "/workflows/{workflow_id}",
    response_model=WorkflowRead,
    summary="Save the drawing, rename, or switch on/off",
)
async def update_workflow(
    workflow_id: uuid.UUID, payload: WorkflowUpdate, scope: Admin
) -> WorkflowRead:
    service = _service(scope)
    row = await service.update(
        workflow_id, name=payload.name, graph=payload.graph, is_active=payload.is_active
    )
    await scope.session.commit()
    await scope.session.refresh(row)
    return await _workflow_read(service, row, (await service.stats()).get(row.id))


@router.delete(
    "/workflows/{workflow_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a workflow (archived; its run history is kept)",
)
async def archive_workflow(workflow_id: uuid.UUID, scope: Admin) -> None:
    await _service(scope).archive(workflow_id)
    await scope.session.commit()


@router.post(
    "/workflows/{workflow_id}/test",
    response_model=WorkflowTestResult,
    summary="What the workflow would do to one lead. Nothing is saved.",
)
async def test_workflow(
    workflow_id: uuid.UUID, payload: WorkflowTestRequest, scope: Admin
) -> WorkflowTestResult:
    service = _service(scope)
    row = await service.get(workflow_id)
    graph: dict[str, Any] = payload.graph if payload.graph is not None else row.graph
    # A broken drawing is refused here exactly as a save would refuse it.
    await service.validate(graph, complete=False)
    lead = await scope.session.get(Lead, payload.lead_id)
    if lead is None or lead.deleted_at is not None:
        raise not_found("Lead")
    result = await WorkflowEngine(scope.session, workspace=scope.workspace).test(
        graph, row.name, lead, payload.data
    )
    return WorkflowTestResult(status=result.status, steps=result.steps, error=result.error)


def _run_read(run: WorkflowRun, lead: Lead | None) -> WorkflowRunRead:
    return WorkflowRunRead(
        id=run.id,
        lead_id=run.lead_id,
        lead_name=lead_name(lead),
        status=run.status,
        steps=run.steps or [],
        data=run.data or {},
        duration_ms=run.duration_ms,
        error=run.error,
        created_at=run.created_at,
    )


@router.get(
    "/workflows/{workflow_id}/runs",
    response_model=Page[WorkflowRunRead],
    summary="Every time this workflow ran, newest first",
)
async def list_workflow_runs(
    workflow_id: uuid.UUID,
    scope: Admin,
    page: Annotated[PageParams, Depends(page_params)],
    run_status: Annotated[str | None, Query(alias="status", max_length=20)] = None,
) -> Page[WorkflowRunRead]:
    found, total = await _service(scope).runs(
        workflow_id, limit=page.limit, offset=page.offset, status=run_status
    )
    return Page(
        items=[_run_read(run, lead) for run, lead in found],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


# --- never AI-call ------------------------------------------------------------------


@voice_rules_router.get(
    "/settings/voice-never-call",
    response_model=NeverCallRead,
    summary="Leads the AI must never call",
)
async def get_never_call(scope: Admin) -> NeverCallRead:
    return NeverCallRead(conditions=read_never_call(scope.workspace.settings))


@voice_rules_router.put(
    "/settings/voice-never-call",
    response_model=NeverCallRead,
    summary="Set which leads the AI must never call",
)
async def put_never_call(payload: NeverCallWrite, scope: Admin) -> NeverCallRead:
    parse_rule(payload.conditions, code="invalid_never_call_rule")
    settings = write_never_call(scope.workspace.settings, payload.conditions)
    scope.workspace.settings = settings
    await scope.session.commit()
    return NeverCallRead(conditions=read_never_call(settings))
