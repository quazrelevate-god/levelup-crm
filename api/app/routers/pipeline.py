"""Pipeline, taxonomy and preferences endpoints (M3).

`docs/02-api-contract.md` §Settings/Pipeline, §Call dispositions,
§Custom actions, plus the workspace preferences from §5 of the configuration
model.

Custom actions sit behind the `custom_actions` feature flag: a workspace with
the module off gets `403 feature_disabled` from the API, not merely a hidden
menu item.
"""

from __future__ import annotations

import uuid
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Query, status

from app.errors import api_error
from app.models.enums import ActionFieldType, StageKind
from app.schemas.field import ActionFieldRead
from app.schemas.pipeline import (
    ActionFieldCreateRequest,
    CustomActionCreate,
    CustomActionRead,
    CustomActionUpdate,
    DispositionCreate,
    DispositionRead,
    DispositionUpdate,
    LostReasonCreate,
    LostReasonRead,
    LostReasonUpdate,
    StageCreate,
    StagePipelineRead,
    StageRead,
    StageReorder,
    StageUpdate,
    VoiceOutcomeStagesRead,
    VoiceOutcomeStagesUpdate,
    WorkspacePreferencesRead,
    WorkspacePreferencesUpdate,
)
from app.services.pipeline import CustomActionService, DispositionService, PipelineService
from app.tenancy.features import FEATURE_FLAGS, require_feature
from app.tenancy.scoping import WorkspaceScope, require_workspace, require_workspace_admin

router = APIRouter(prefix="/settings", tags=["settings: pipeline"])


def _pipeline(scope: Annotated[WorkspaceScope, Depends(require_workspace)]) -> PipelineService:
    return PipelineService(scope.session)


def _pipeline_admin(
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
) -> PipelineService:
    return PipelineService(scope.session)


def _dispositions(
    scope: Annotated[WorkspaceScope, Depends(require_workspace)],
) -> DispositionService:
    return DispositionService(scope.session)


def _dispositions_admin(
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
) -> DispositionService:
    return DispositionService(scope.session)


# --- stages ------------------------------------------------------------------


@router.get("/stages", response_model=StagePipelineRead, summary="The pipeline, grouped by kind")
async def list_stages(
    service: Annotated[PipelineService, Depends(_pipeline)],
) -> StagePipelineRead:
    """Grouped, not flat — the settings screen is a three-column pipeline."""
    live = await service.list_stages()
    archived = [s for s in await service.list_stages(include_archived=True) if s.is_archived]

    def one(kind: StageKind) -> StageRead | None:
        found = next((s for s in live if s.kind is kind), None)
        return StageRead.model_validate(found) if found else None

    return StagePipelineRead(
        initial=one(StageKind.INITIAL),
        active=[StageRead.model_validate(s) for s in live if s.kind is StageKind.ACTIVE],
        won=one(StageKind.WON),
        lost=one(StageKind.LOST),
        archived=[StageRead.model_validate(s) for s in archived],
    )


@router.post(
    "/stages",
    response_model=StageRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add an active stage",
)
async def create_stage(
    payload: StageCreate,
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
    service: Annotated[PipelineService, Depends(_pipeline_admin)],
) -> StageRead:
    stage = await service.create_stage(label=payload.label, color=payload.color)
    await scope.session.commit()
    return StageRead.model_validate(stage)


@router.get(
    "/voice-outcome-stages",
    response_model=VoiceOutcomeStagesRead,
    summary="Which stages a finished AI call moves a lead to",
)
async def read_voice_outcome_stages(
    scope: Annotated[WorkspaceScope, Depends(require_workspace)],
    service: Annotated[PipelineService, Depends(_pipeline)],
) -> VoiceOutcomeStagesRead:
    """Null for both when nothing is configured, which is the default."""
    answered, unanswered = await service.voice_outcome_stages(scope.workspace)
    return VoiceOutcomeStagesRead(
        answered=StageRead.model_validate(answered) if answered else None,
        unanswered=StageRead.model_validate(unanswered) if unanswered else None,
    )


@router.put(
    "/voice-outcome-stages",
    response_model=VoiceOutcomeStagesRead,
    summary="Set the two stages a finished AI call moves a lead to",
)
async def set_voice_outcome_stages(
    payload: VoiceOutcomeStagesUpdate,
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
    service: Annotated[PipelineService, Depends(_pipeline_admin)],
) -> VoiceOutcomeStagesRead:
    """Admin only, like every other pipeline write.

    A deliberately narrow door onto `workspace.settings`: two stage ids and
    nothing else, both validated against this workspace's live active stages,
    with every other setting preserved. The alternative — exposing the whole
    settings object — would accept anything and validate none of it.
    """
    answered, unanswered = await service.set_voice_outcome_stages(
        scope.workspace, answered=payload.answered, unanswered=payload.unanswered
    )
    await scope.session.commit()
    return VoiceOutcomeStagesRead(
        answered=StageRead.model_validate(answered),
        unanswered=StageRead.model_validate(unanswered),
    )


@router.delete(
    "/voice-outcome-stages",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop moving leads on an AI call outcome",
)
async def clear_voice_outcome_stages(
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
    service: Annotated[PipelineService, Depends(_pipeline_admin)],
) -> None:
    await service.clear_voice_outcome_stages(scope.workspace)
    await scope.session.commit()


@router.patch("/stages/reorder", response_model=list[StageRead], summary="Reorder active stages")
async def reorder_stages(
    payload: StageReorder,
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
    service: Annotated[PipelineService, Depends(_pipeline_admin)],
) -> list[StageRead]:
    # Declared before `/stages/{stage_id}`: a literal segment must win over a
    # uuid placeholder, or "reorder" is parsed as a stage id.
    stages = await service.reorder_stages(ordered_ids=payload.ordered_ids)
    await scope.session.commit()
    return [StageRead.model_validate(s) for s in stages]


@router.patch("/stages/{stage_id}", response_model=StageRead, summary="Rename or recolour")
async def update_stage(
    stage_id: uuid.UUID,
    payload: StageUpdate,
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
    service: Annotated[PipelineService, Depends(_pipeline_admin)],
) -> StageRead:
    stage = await service.update_stage(stage_id, label=payload.label, color=payload.color)
    await scope.session.commit()
    return StageRead.model_validate(stage)


@router.delete("/stages/{stage_id}", response_model=StageRead, summary="Archive an active stage")
async def archive_stage(
    stage_id: uuid.UUID,
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
    service: Annotated[PipelineService, Depends(_pipeline_admin)],
) -> StageRead:
    stage = await service.archive_stage(stage_id)
    await scope.session.commit()
    return StageRead.model_validate(stage)


# --- lost reasons ------------------------------------------------------------


@router.get("/lost-reasons", response_model=list[LostReasonRead], summary="List lost reasons")
async def list_lost_reasons(
    service: Annotated[PipelineService, Depends(_pipeline)],
    include_archived: Annotated[bool, Query()] = False,
) -> list[LostReasonRead]:
    reasons = await service.list_lost_reasons(include_archived=include_archived)
    return [LostReasonRead.model_validate(r) for r in reasons]


@router.post(
    "/lost-reasons",
    response_model=LostReasonRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a lost reason (max 25)",
)
async def create_lost_reason(
    payload: LostReasonCreate,
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
    service: Annotated[PipelineService, Depends(_pipeline_admin)],
) -> LostReasonRead:
    reason = await service.create_lost_reason(label=payload.label)
    await scope.session.commit()
    return LostReasonRead.model_validate(reason)


@router.patch(
    "/lost-reasons/{reason_id}", response_model=LostReasonRead, summary="Rename a lost reason"
)
async def update_lost_reason(
    reason_id: uuid.UUID,
    payload: LostReasonUpdate,
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
    service: Annotated[PipelineService, Depends(_pipeline_admin)],
) -> LostReasonRead:
    reason = await service.update_lost_reason(
        reason_id, label=payload.label, sort_order=payload.sort_order
    )
    await scope.session.commit()
    return LostReasonRead.model_validate(reason)


@router.delete(
    "/lost-reasons/{reason_id}", response_model=LostReasonRead, summary="Archive a lost reason"
)
async def archive_lost_reason(
    reason_id: uuid.UUID,
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
    service: Annotated[PipelineService, Depends(_pipeline_admin)],
) -> LostReasonRead:
    reason = await service.archive_lost_reason(reason_id)
    await scope.session.commit()
    return LostReasonRead.model_validate(reason)


# --- call dispositions -------------------------------------------------------


@router.get(
    "/call-dispositions", response_model=list[DispositionRead], summary="List call dispositions"
)
async def list_dispositions(
    service: Annotated[DispositionService, Depends(_dispositions)],
    include_archived: Annotated[bool, Query()] = False,
) -> list[DispositionRead]:
    items = await service.list_dispositions(include_archived=include_archived)
    return [DispositionRead.model_validate(d) for d in items]


@router.post(
    "/call-dispositions",
    response_model=DispositionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a custom disposition",
)
async def create_disposition(
    payload: DispositionCreate,
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
    service: Annotated[DispositionService, Depends(_dispositions_admin)],
) -> DispositionRead:
    disposition = await service.create_disposition(label=payload.label)
    await scope.session.commit()
    return DispositionRead.model_validate(disposition)


@router.patch(
    "/call-dispositions/{disposition_id}",
    response_model=DispositionRead,
    summary="Rename a custom disposition (403 on system entries)",
)
async def update_disposition(
    disposition_id: uuid.UUID,
    payload: DispositionUpdate,
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
    service: Annotated[DispositionService, Depends(_dispositions_admin)],
) -> DispositionRead:
    disposition = await service.update_disposition(
        disposition_id, label=payload.label, sort_order=payload.sort_order
    )
    await scope.session.commit()
    return DispositionRead.model_validate(disposition)


@router.post(
    "/call-dispositions/{disposition_id}/set-default",
    response_model=DispositionRead,
    summary="Make this the default, clearing the previous one",
)
async def set_default_disposition(
    disposition_id: uuid.UUID,
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
    service: Annotated[DispositionService, Depends(_dispositions_admin)],
) -> DispositionRead:
    disposition = await service.set_default(disposition_id)
    await scope.session.commit()
    return DispositionRead.model_validate(disposition)


@router.post(
    "/call-dispositions/{disposition_id}/archive",
    response_model=DispositionRead,
    summary="Archive a disposition (allowed on system entries)",
)
async def archive_disposition(
    disposition_id: uuid.UUID,
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
    service: Annotated[DispositionService, Depends(_dispositions_admin)],
) -> DispositionRead:
    disposition = await service.archive_disposition(disposition_id)
    await scope.session.commit()
    return DispositionRead.model_validate(disposition)


# --- custom actions ----------------------------------------------------------
#
# Behind the `custom_actions` flag. The dependency refuses with 403 before the
# handler runs, so turning the module off closes the API and not just the menu.

custom_actions_router = APIRouter(
    prefix="/settings/custom-actions",
    tags=["settings: custom actions"],
    dependencies=[Depends(require_feature("custom_actions"))],
)


def _actions(scope: Annotated[WorkspaceScope, Depends(require_workspace)]) -> CustomActionService:
    return CustomActionService(scope.session)


def _actions_admin(
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
) -> CustomActionService:
    return CustomActionService(scope.session)


@custom_actions_router.get("", response_model=list[CustomActionRead], summary="List custom actions")
async def list_custom_actions(
    service: Annotated[CustomActionService, Depends(_actions)],
    status_filter: Annotated[str, Query(alias="status", pattern="^(active|archived)$")] = "active",
    search: Annotated[str | None, Query(max_length=80)] = None,
) -> list[CustomActionRead]:
    types = await service.list_types(include_archived=status_filter == "archived", search=search)
    if status_filter == "archived":
        types = [t for t in types if t.is_archived]
    return [CustomActionRead.model_validate(t) for t in types]


@custom_actions_router.post(
    "",
    response_model=CustomActionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a custom action (code assigned from 1001)",
)
async def create_custom_action(
    payload: CustomActionCreate,
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
    service: Annotated[CustomActionService, Depends(_actions_admin)],
) -> CustomActionRead:
    action_type = await service.create_type(**payload.model_dump())
    await scope.session.commit()
    return CustomActionRead.model_validate(await service.get_type(action_type.id))


@custom_actions_router.get(
    "/{type_id}", response_model=CustomActionRead, summary="One custom action"
)
async def get_custom_action(
    type_id: uuid.UUID,
    service: Annotated[CustomActionService, Depends(_actions)],
) -> CustomActionRead:
    return CustomActionRead.model_validate(await service.get_type(type_id))


@custom_actions_router.patch(
    "/{type_id}", response_model=CustomActionRead, summary="Update a custom action"
)
async def update_custom_action(
    type_id: uuid.UUID,
    payload: CustomActionUpdate,
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
    service: Annotated[CustomActionService, Depends(_actions_admin)],
) -> CustomActionRead:
    await service.update_type(type_id, **payload.model_dump(exclude_unset=True))
    await scope.session.commit()
    return CustomActionRead.model_validate(await service.get_type(type_id))


@custom_actions_router.post(
    "/{type_id}/archive", response_model=CustomActionRead, summary="Archive a custom action"
)
async def archive_custom_action(
    type_id: uuid.UUID,
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
    service: Annotated[CustomActionService, Depends(_actions_admin)],
) -> CustomActionRead:
    await service.archive_type(type_id)
    await scope.session.commit()
    return CustomActionRead.model_validate(await service.get_type(type_id))


@custom_actions_router.get(
    "/{type_id}/fields", response_model=list[ActionFieldRead], summary="An action's form fields"
)
async def list_action_fields(
    type_id: uuid.UUID,
    service: Annotated[CustomActionService, Depends(_actions)],
) -> list[ActionFieldRead]:
    action_type = await service.get_type(type_id)
    return [ActionFieldRead.model_validate(f) for f in action_type.fields]


@custom_actions_router.post(
    "/{type_id}/fields",
    response_model=ActionFieldRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a field to an action's form",
)
async def add_action_field(
    type_id: uuid.UUID,
    payload: ActionFieldCreateRequest,
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
    service: Annotated[CustomActionService, Depends(_actions_admin)],
) -> ActionFieldRead:
    field = await service.add_field(
        type_id,
        label=payload.label,
        field_type=ActionFieldType(payload.field_type),
        description=payload.description,
        is_required=payload.is_required,
        options=[(label, None) for label in payload.options],
    )
    await scope.session.commit()
    action_type = await service.get_type(type_id)
    stored = next(f for f in action_type.fields if f.id == field.id)
    return ActionFieldRead.model_validate(stored)


@custom_actions_router.delete(
    "/{type_id}/fields/{field_id}",
    response_model=ActionFieldRead,
    summary="Hide a form field (never deletes)",
)
async def hide_action_field(
    type_id: uuid.UUID,
    field_id: uuid.UUID,
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
    service: Annotated[CustomActionService, Depends(_actions_admin)],
) -> ActionFieldRead:
    field = await service.hide_field(type_id, field_id)
    await scope.session.commit()
    return ActionFieldRead.model_validate(field)


# --- workspace preferences ---------------------------------------------------


@router.get(
    "/preferences", response_model=WorkspacePreferencesRead, summary="Workspace preferences"
)
async def get_preferences(
    scope: Annotated[WorkspaceScope, Depends(require_workspace)],
) -> WorkspacePreferencesRead:
    return WorkspacePreferencesRead.model_validate(scope.workspace)


@router.patch("/preferences", response_model=WorkspacePreferencesRead, summary="Update preferences")
async def update_preferences(
    payload: WorkspacePreferencesUpdate,
    scope: Annotated[WorkspaceScope, Depends(require_workspace_admin)],
) -> WorkspacePreferencesRead:
    """The localisation seam (§5).

    Changing the country code does not retro-normalise stored phone numbers:
    they were normalised with the code in force when they were written, and
    rewriting them would silently change customer data.
    """
    workspace = scope.workspace

    if payload.timezone is not None:
        try:
            ZoneInfo(payload.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise api_error(
                422, "unknown_timezone", f"{payload.timezone!r} is not an IANA timezone"
            ) from exc
        workspace.timezone = payload.timezone

    if payload.default_country_code is not None:
        code = payload.default_country_code.lstrip("+")
        if not code.isdigit():
            raise api_error(
                422, "invalid_country_code", "A country code is digits, optionally led by +"
            )
        workspace.default_country_code = code

    if payload.currency is not None:
        if not payload.currency.isalpha():
            raise api_error(422, "invalid_currency", "A currency is a 3-letter ISO 4217 code")
        workspace.currency = payload.currency.upper()

    if payload.connected_call_min_seconds is not None:
        workspace.connected_call_min_seconds = payload.connected_call_min_seconds
    if payload.session_timeout_minutes is not None:
        workspace.session_timeout_minutes = payload.session_timeout_minutes
    if payload.leaderboard_metrics is not None:
        workspace.leaderboard_metrics = dict(payload.leaderboard_metrics)

    if payload.features is not None:
        unknown = set(payload.features) - FEATURE_FLAGS
        if unknown:
            raise api_error(
                422,
                "unknown_feature",
                f"Not a feature of this product: {', '.join(sorted(unknown))}",
                unknown=sorted(unknown),
            )
        workspace.features = {**(workspace.features or {}), **payload.features}

    await scope.session.commit()
    return WorkspacePreferencesRead.model_validate(workspace)
