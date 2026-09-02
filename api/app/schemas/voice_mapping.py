"""Request/response models for the voice extraction mapping CRUD.

Every shape here validates at the boundary. The service and the router both
run again downstream — the schema level rejection is defence in depth, not the
whole check — but for a single writer of a single mapping row it is where the
clearest error message can be produced.

`min_confidence` is bounded to [0, 1] here **and** by the CHECK constraint on
the column. Both are load-bearing: the schema check is what returns a 422 with
a field-level message to the settings UI, and the CHECK is what stops a rogue
direct SQL write from landing a value the write-back service will never accept
against any real confidence score.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "VoiceExtractionMappingCreate",
    "VoiceExtractionMappingRead",
    "VoiceExtractionMappingUpdate",
]


class VoiceExtractionMappingRead(BaseModel):
    """One mapping as the settings UI reads it back."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    disposition_name: str
    target_field_key: str
    #: Serialised as a float so the settings UI can drive a slider without
    #: hand-parsing Decimal. Bounded at the schema level and by a CHECK.
    min_confidence: float = Field(ge=0.0, le=1.0)
    is_enabled: bool
    created_at: dt.datetime
    updated_at: dt.datetime


class VoiceExtractionMappingCreate(BaseModel):
    """The body an admin submits from the settings page.

    Cross-field validation (does `target_field_key` name a live LeadField in
    this workspace? does `disposition_name` collide with the deployment's
    configured summary disposition?) is done in the router, where the workspace
    and the settings are in scope — the schema is unaware of both by design.
    """

    model_config = ConfigDict(extra="forbid")

    disposition_name: str = Field(min_length=1, max_length=120)
    target_field_key: str = Field(min_length=1, max_length=64)
    min_confidence: float = Field(default=0.70, ge=0.0, le=1.0)
    is_enabled: bool = True


class VoiceExtractionMappingUpdate(BaseModel):
    """Partial update — any subset of the four writable fields.

    `disposition_name` is included because a Bolna disposition can be renamed
    on their side, and forcing an operator to delete and re-create a mapping
    they still want would drop the audit trail of who created it and when.
    Uniqueness `(workspace_id, disposition_name)` still bites, so a rename to
    a colliding name is a 409 — not a silent overwrite.
    """

    model_config = ConfigDict(extra="forbid")

    disposition_name: str | None = Field(default=None, min_length=1, max_length=120)
    target_field_key: str | None = Field(default=None, min_length=1, max_length=64)
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    is_enabled: bool | None = None
