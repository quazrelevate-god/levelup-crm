"""Call facts: reference details the voice agent is told, chosen by rule (docs/17)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CallFactRead",
    "CallFactSetCreate",
    "CallFactSetRead",
    "CallFactSetUpdate",
    "CallFactWrite",
]


class CallFactWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: As the team says it, e.g. "Batch start date". Becomes the variable name.
    label: str = Field(min_length=1, max_length=60)
    value: str = Field(default="", max_length=500)


class CallFactRead(BaseModel):
    #: What the agent's script can use: `{{batch_start_date}}`.
    key: str
    label: str
    value: str


class CallFactSetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    #: Lowest number is checked first.
    priority: int = Field(default=0, ge=0, le=10_000)
    #: The filter DSL. Empty: applies to every lead.
    conditions: dict[str, Any] = Field(default_factory=dict)
    facts: list[CallFactWrite] = Field(default_factory=list, max_length=30)


class CallFactSetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    priority: int | None = Field(default=None, ge=0, le=10_000)
    conditions: dict[str, Any] | None = None
    facts: list[CallFactWrite] | None = Field(default=None, max_length=30)
    is_active: bool | None = None


class CallFactSetRead(BaseModel):
    id: uuid.UUID
    name: str
    priority: int
    conditions: dict[str, Any]
    #: The rule in words, e.g. `program is "VE"`; "Every lead" when empty.
    rule: str
    facts: list[CallFactRead]
    is_active: bool
    created_at: dt.datetime
    updated_at: dt.datetime
