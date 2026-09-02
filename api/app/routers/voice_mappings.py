"""CRUD for `voice_extraction_mappings` (Settings → Voice extraction).

Five endpoints, mounted at
`/api/v1/workspaces/{workspace_id}/voice/extraction-mappings`. Member-
authenticated, gated on the `automations.manage_webhooks` capability — the
same one that governs the Integrations settings surface, since a voice
extraction mapping is exactly the same kind of admin-managed integration
config: it changes what the CRM does with a machine caller's payload, and it
is not a per-user preference.

Every write path validates twice:

1. **Field key must resolve** to a non-hidden `LeadField` in this workspace.
2. **Disposition name must not equal** the deployment's `BOLNA_SUMMARY_DISPOSITION`
   setting. The service enforces the same rule at runtime (a deployment can
   change the setting after a mapping was already saved), but returning a
   422 at create/update time is what makes the mistake visible to the
   operator making it.

Delete is a hard delete. Nothing references a mapping historically — a
written extraction is recorded on the timeline with the field name, not the
mapping id — so removing a row leaves no dangling reference. To pause a
mapping without losing its configuration, PATCH `is_enabled=false` instead.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import delete, func, select

from app.errors import api_error, not_found
from app.models.field import LeadField
from app.models.voice_mapping import VoiceExtractionMapping
from app.schemas.voice_mapping import (
    VoiceExtractionMappingCreate,
    VoiceExtractionMappingRead,
    VoiceExtractionMappingUpdate,
)
from app.services.voice_extraction import summary_disposition_conflict
from app.tenancy.scoping import WorkspaceScope, require_workspace

router = APIRouter(tags=["voice"])


def _require(scope: WorkspaceScope, group: str, capability: str) -> None:
    if not scope.capability(group, capability):
        raise api_error(
            403,
            "insufficient_permissions",
            f"This permission template does not allow: {capability.replace('_', ' ')}",
        )


async def _assert_field(scope: WorkspaceScope, key: str) -> LeadField:
    """The target key must resolve to a live LeadField in this workspace."""
    rows = await scope.session.execute(
        select(LeadField).where(LeadField.key == key).limit(1)
    )
    field: LeadField | None = rows.scalar_one_or_none()
    if field is None:
        raise api_error(
            422,
            "unknown_field",
            f"No lead field with key {key!r} in this workspace",
        )
    if field.is_hidden:
        raise api_error(
            422,
            "hidden_field",
            f"Lead field {key!r} is hidden and cannot receive automatic writes",
        )
    return field


def _refuse_summary_disposition(request: Request, disposition_name: str) -> None:
    """Refuse to map the disposition that carries the call summary.

    A mapping named after the summary disposition would silently drop the
    whole call recap paragraph into whatever lead field the mapping targeted.
    The service catches this again at write-time, but the router-level 422 is
    the earlier and clearer error message.
    """
    configured = request.app.state.settings.bolna_summary_disposition
    if summary_disposition_conflict(disposition_name, summary_disposition=configured):
        raise api_error(
            422,
            "summary_disposition_conflict",
            (
                f"{disposition_name!r} is the deployment's configured call summary "
                "disposition and cannot also be mapped to a lead field."
            ),
        )


async def _assert_unique(
    scope: WorkspaceScope,
    disposition_name: str,
    *,
    excluding: uuid.UUID | None = None,
) -> None:
    """Enforce the (workspace_id, disposition_name) uniqueness — humanely.

    The DB will raise `IntegrityError` on the same insert; the router-level
    check exists so an operator sees a 409 with the colliding row's identity
    rather than a generic 500.
    """
    statement = select(VoiceExtractionMapping).where(
        func.lower(VoiceExtractionMapping.disposition_name) == disposition_name.lower()
    )
    if excluding is not None:
        statement = statement.where(VoiceExtractionMapping.id != excluding)
    rows = await scope.session.execute(statement)
    existing: VoiceExtractionMapping | None = rows.scalars().first()
    if existing is not None:
        raise api_error(
            409,
            "duplicate_disposition",
            f"A mapping for disposition {disposition_name!r} already exists",
        )


@router.get(
    "/voice/extraction-mappings",
    response_model=list[VoiceExtractionMappingRead],
    summary="List voice extraction mappings",
)
async def list_mappings(
    scope: Annotated[WorkspaceScope, Depends(require_workspace)],
) -> list[VoiceExtractionMappingRead]:
    _require(scope, "automations", "manage_webhooks")
    rows = await scope.session.execute(
        select(VoiceExtractionMapping).order_by(VoiceExtractionMapping.disposition_name)
    )
    return [VoiceExtractionMappingRead.model_validate(row) for row in rows.scalars().all()]


@router.post(
    "/voice/extraction-mappings",
    response_model=VoiceExtractionMappingRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a voice extraction mapping",
)
async def create_mapping(
    body: VoiceExtractionMappingCreate,
    request: Request,
    scope: Annotated[WorkspaceScope, Depends(require_workspace)],
) -> VoiceExtractionMappingRead:
    _require(scope, "automations", "manage_webhooks")
    _refuse_summary_disposition(request, body.disposition_name)
    await _assert_field(scope, body.target_field_key)
    await _assert_unique(scope, body.disposition_name)

    mapping = VoiceExtractionMapping(
        disposition_name=body.disposition_name,
        target_field_key=body.target_field_key,
        min_confidence=body.min_confidence,
        is_enabled=body.is_enabled,
    )
    scope.session.add(mapping)
    await scope.session.commit()
    await scope.session.refresh(mapping)
    return VoiceExtractionMappingRead.model_validate(mapping)


async def _load(
    scope: WorkspaceScope, mapping_id: uuid.UUID
) -> VoiceExtractionMapping:
    row = await scope.session.get(VoiceExtractionMapping, mapping_id)
    # `get` on a `ScopedSession` already filters by workspace via loader
    # criteria (see `app/tenancy/session.py`) — a mapping from another
    # workspace returns None, which becomes a 404 here. That is why this is
    # not a 403: a 403 would confirm the mapping exists.
    if row is None:
        raise not_found("Voice extraction mapping")
    return row


@router.get(
    "/voice/extraction-mappings/{mapping_id}",
    response_model=VoiceExtractionMappingRead,
    summary="Read one voice extraction mapping",
)
async def read_mapping(
    mapping_id: uuid.UUID,
    scope: Annotated[WorkspaceScope, Depends(require_workspace)],
) -> VoiceExtractionMappingRead:
    _require(scope, "automations", "manage_webhooks")
    mapping = await _load(scope, mapping_id)
    return VoiceExtractionMappingRead.model_validate(mapping)


@router.patch(
    "/voice/extraction-mappings/{mapping_id}",
    response_model=VoiceExtractionMappingRead,
    summary="Update a voice extraction mapping",
)
async def update_mapping(
    mapping_id: uuid.UUID,
    body: VoiceExtractionMappingUpdate,
    request: Request,
    scope: Annotated[WorkspaceScope, Depends(require_workspace)],
) -> VoiceExtractionMappingRead:
    _require(scope, "automations", "manage_webhooks")
    mapping = await _load(scope, mapping_id)

    if body.disposition_name is not None and body.disposition_name != mapping.disposition_name:
        _refuse_summary_disposition(request, body.disposition_name)
        await _assert_unique(scope, body.disposition_name, excluding=mapping.id)
        mapping.disposition_name = body.disposition_name

    if body.target_field_key is not None and body.target_field_key != mapping.target_field_key:
        await _assert_field(scope, body.target_field_key)
        mapping.target_field_key = body.target_field_key

    if body.min_confidence is not None:
        mapping.min_confidence = body.min_confidence  # type: ignore[assignment]

    if body.is_enabled is not None:
        mapping.is_enabled = body.is_enabled

    await scope.session.commit()
    await scope.session.refresh(mapping)
    return VoiceExtractionMappingRead.model_validate(mapping)


@router.delete(
    "/voice/extraction-mappings/{mapping_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a voice extraction mapping",
)
async def delete_mapping(
    mapping_id: uuid.UUID,
    scope: Annotated[WorkspaceScope, Depends(require_workspace)],
) -> None:
    """Hard delete. Use PATCH `is_enabled=false` to pause without losing config."""
    _require(scope, "automations", "manage_webhooks")
    mapping = await _load(scope, mapping_id)
    await scope.session.execute(
        delete(VoiceExtractionMapping).where(VoiceExtractionMapping.id == mapping.id)
    )
    await scope.session.commit()
