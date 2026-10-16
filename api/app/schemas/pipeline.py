"""Request/response models for pipeline and taxonomy settings (M3)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ActionDirection, ActionFieldType, StageKind
from app.models.pipeline import MAX_STAGE_LABEL
from app.schemas.field import ActionFieldRead

__all__ = [
    "CustomActionCreate",
    "CustomActionRead",
    "CustomActionUpdate",
    "DispositionCreate",
    "DispositionRead",
    "DispositionUpdate",
    "LostReasonCreate",
    "LostReasonRead",
    "LostReasonUpdate",
    "StageCreate",
    "StagePipelineRead",
    "StageRead",
    "StageReorder",
    "StageUpdate",
    "VoiceOutcomeStagesRead",
    "VoiceOutcomeStagesUpdate",
    "WorkspacePreferencesRead",
    "WorkspacePreferencesUpdate",
]


class StageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: StageKind
    label: str
    color: str
    sort_order: int
    is_archived: bool


class VoiceOutcomeStagesUpdate(BaseModel):
    """Which two stages a finished AI call moves a lead to.

    Exactly two stage ids, both required: a half-configured mapping would move
    one outcome and silently drop the other, which reads as a bug rather than
    as a choice. To stop the behaviour, archive a stage or clear the mapping.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    answered: uuid.UUID
    unanswered: uuid.UUID


class VoiceOutcomeStagesRead(BaseModel):
    """The configured stages in full, so a caller can see what it just set.

    Null when nothing is configured, which is every workspace by default.
    """

    model_config = ConfigDict(frozen=True)

    answered: StageRead | None
    unanswered: StageRead | None


class StagePipelineRead(BaseModel):
    """The pipeline as the three-column settings screen draws it (§2.1).

    Grouped rather than flat, because the screen is not a list — it is an
    initial stage, a reorderable band, and a closed pair.
    """

    initial: StageRead | None
    active: list[StageRead]
    won: StageRead | None
    lost: StageRead | None
    archived: list[StageRead]


class StageCreate(BaseModel):
    """`kind` is absent by design: only ACTIVE stages can be created.

    The three singletons arrive with provisioning and are renamed, never added.
    """

    label: str = Field(min_length=1, max_length=MAX_STAGE_LABEL)
    color: str | None = Field(default=None, max_length=9)


class StageUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=MAX_STAGE_LABEL)
    color: str | None = Field(default=None, max_length=9)


class StageReorder(BaseModel):
    ordered_ids: list[uuid.UUID]


class LostReasonRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    label: str
    sort_order: int
    is_default: bool
    is_archived: bool


class LostReasonCreate(BaseModel):
    label: str = Field(min_length=1, max_length=80)


class LostReasonUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=80)
    sort_order: int | None = None


class DispositionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    label: str
    is_default: bool
    is_system: bool
    is_archived: bool
    sort_order: int


class DispositionCreate(BaseModel):
    label: str = Field(min_length=1, max_length=80)


class DispositionUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=80)
    sort_order: int | None = None


class CustomActionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: int
    name: str
    icon: str | None
    score: int
    direction: ActionDirection
    description: str | None
    allow_predated: bool
    is_archived: bool
    fields: list[ActionFieldRead] = Field(default_factory=list)
    created_at: datetime


class CustomActionCreate(BaseModel):
    """`code` is absent: it is assigned sequentially from 1001 per workspace."""

    name: str = Field(min_length=1, max_length=80)
    icon: str | None = Field(default=None, max_length=64)
    score: int = Field(default=0, ge=-1000, le=1000)
    direction: ActionDirection = ActionDirection.INFORMATION
    description: str | None = None
    allow_predated: bool = False


class CustomActionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    icon: str | None = Field(default=None, max_length=64)
    score: int | None = Field(default=None, ge=-1000, le=1000)
    direction: ActionDirection | None = None
    description: str | None = None
    allow_predated: bool | None = None


class ActionFieldCreateRequest(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    field_type: ActionFieldType
    description: str | None = None
    is_required: bool = False
    options: list[str] = Field(default_factory=list)


class WorkspacePreferencesRead(BaseModel):
    """§5 — the localisation seam.

    "A US customer must work without a code change": phone normalisation reads
    `default_country_code`, timestamps render in `timezone`, and MONEY fields
    render in `currency`.
    """

    model_config = ConfigDict(from_attributes=True)

    default_country_code: str
    timezone: str
    currency: str
    connected_call_min_seconds: int
    session_timeout_minutes: int | None
    leaderboard_metrics: dict[str, Any]
    features: dict[str, Any]


class WorkspacePreferencesUpdate(BaseModel):
    default_country_code: str | None = Field(default=None, max_length=6)
    timezone: str | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    connected_call_min_seconds: int | None = Field(default=None, ge=0, le=3600)
    session_timeout_minutes: int | None = Field(default=None, ge=1)
    leaderboard_metrics: dict[str, bool] | None = None
    features: dict[str, bool] | None = None
