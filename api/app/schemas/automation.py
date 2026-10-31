"""Connected apps, workflows and the never-AI-call rule (docs/18)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AppEventRead",
    "AppRead",
    "AppUpdate",
    "AutomationCatalogue",
    "GraphProblemRead",
    "NeverCallRead",
    "NeverCallWrite",
    "OperatorRead",
    "TriggerRead",
    "VariableRead",
    "WorkflowCreate",
    "WorkflowRead",
    "WorkflowRunRead",
    "WorkflowTestRequest",
    "WorkflowTestResult",
    "WorkflowUpdate",
]


class VariableRead(BaseModel):
    key: str
    label: str
    kind: str


class TriggerRead(BaseModel):
    key: str
    label: str
    app: str | None
    description: str
    variables: list[VariableRead]


class OperatorRead(BaseModel):
    key: str
    label: str
    needs_value: bool


class AutomationCatalogue(BaseModel):
    triggers: list[TriggerRead]
    operators: list[OperatorRead]


class AppRead(BaseModel):
    provider: str
    label: str
    description: str
    connected: bool
    is_active: bool
    #: Joined to the API's base URL by the screen. Present once connected.
    webhook_path: str | None
    #: Whether the app's signing secret has been pasted in. Never the secret.
    secret_set: bool
    settings: dict[str, Any]
    last_event_at: dt.datetime | None


class AppUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_active: bool | None = None
    #: Write-only. An empty string clears it.
    secret: str | None = Field(default=None, max_length=200)
    settings: dict[str, Any] | None = None


class AppEventRead(BaseModel):
    id: uuid.UUID
    event_type: str
    trigger: str | None
    #: MATCHED, CREATED_LEAD, NO_LEAD, DUPLICATE, IGNORED, REJECTED.
    outcome: str
    data: dict[str, Any]
    lead_id: uuid.UUID | None
    lead_name: str | None
    workflows_run: int
    error: str | None
    created_at: dt.datetime


class GraphProblemRead(BaseModel):
    node_id: str | None
    message: str


class WorkflowRead(BaseModel):
    id: uuid.UUID
    name: str
    trigger: str | None
    graph: dict[str, Any]
    is_active: bool
    total_runs: int
    runs_24h: int
    failed_24h: int
    last_run_at: dt.datetime | None
    #: What must still be filled in before it can be switched on.
    problems: list[GraphProblemRead]
    created_at: dt.datetime
    updated_at: dt.datetime


class WorkflowCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    trigger: str | None = Field(default=None, max_length=80)
    graph: dict[str, Any] | None = None


class WorkflowUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    graph: dict[str, Any] | None = None
    is_active: bool | None = None


class WorkflowTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lead_id: uuid.UUID
    #: The trigger's data to pretend with, e.g. `{"amount": "400"}`.
    data: dict[str, Any] = Field(default_factory=dict)
    #: Test the graph as drawn on screen, before it is saved.
    graph: dict[str, Any] | None = None


class WorkflowTestResult(BaseModel):
    status: str
    steps: list[dict[str, Any]]
    error: str | None


class WorkflowRunRead(BaseModel):
    id: uuid.UUID
    lead_id: uuid.UUID | None
    lead_name: str | None
    status: str
    steps: list[dict[str, Any]]
    data: dict[str, Any]
    duration_ms: int
    error: str | None
    created_at: dt.datetime


class NeverCallRead(BaseModel):
    #: The filter DSL; `{}` means no lead is excluded.
    conditions: dict[str, Any]


class NeverCallWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conditions: dict[str, Any] = Field(default_factory=dict)
