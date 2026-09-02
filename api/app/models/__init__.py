"""SQLAlchemy models.

Importing this package registers every mapper, which is what lets SQLAlchemy
resolve the string forward-references between modules and what lets Alembic
autogenerate see the full metadata. `alembic/env.py` imports it for that reason.

Tables holding customer data inherit `TenantModel`, which declares
`workspace_id` and its cascade. The only tables that legitimately skip it are
`users`, `workspaces`, and `refresh_tokens`.
"""

from __future__ import annotations

from app.models.assignment import (
    AssignmentCursor,
    AssignmentRule,
    SalesGroup,
    SalesGroupMember,
    ScheduledReport,
)
from app.models.dashboard import Dashboard
from app.models.enums import (
    ActionDirection,
    ActionFieldType,
    AssignmentStrategy,
    AvailabilityStatus,
    ChangesetSource,
    DedupeMode,
    ImportJobKind,
    ImportJobStatus,
    IndexedFieldStatus,
    IntakeOutcome,
    LeadFieldType,
    OutboxStatus,
    PermissionGrant,
    SavedFilterVisibility,
    ScheduledReportFormat,
    StageKind,
    SystemActionKind,
    TemplateChannel,
    VoiceCallStatus,
)
from app.models.field import (
    ActionField,
    ActionFieldOption,
    CustomActionType,
    FieldOption,
    IndexedField,
    LeadField,
)
from app.models.integration import (
    ApiKey,
    IntakeLogEntry,
    OutboxEvent,
    WebhookEndpoint,
)
from app.models.lead import (
    Action,
    ActionAttachment,
    Changeset,
    Lead,
    MessageTemplate,
)
from app.models.mixins import Base, TenantModel, TimestampMixin
from app.models.permission import TemplateFieldGrant, TemplateLeadView
from app.models.pipeline import CallDisposition, LostReason, Stage
from app.models.user import PasswordResetToken, RefreshToken, User
from app.models.view import SavedFilter, TableLayout
from app.models.voice import VoiceCallContext, VoiceCallExecution
from app.models.voice_mapping import VoiceExtractionMapping
from app.models.work import ImportJob, Label, LeadLabel, Task
from app.models.workspace import (
    AvailabilityLog,
    Membership,
    PermissionTemplate,
    Workspace,
)

__all__ = [
    "Action",
    "ActionAttachment",
    "ActionDirection",
    "ActionField",
    "ActionFieldOption",
    "ActionFieldType",
    "ApiKey",
    "AssignmentCursor",
    "AssignmentRule",
    "AssignmentStrategy",
    "AvailabilityLog",
    "AvailabilityStatus",
    "Base",
    "CallDisposition",
    "Changeset",
    "ChangesetSource",
    "CustomActionType",
    "Dashboard",
    "DedupeMode",
    "FieldOption",
    "ImportJob",
    "ImportJobKind",
    "ImportJobStatus",
    "IndexedField",
    "IndexedFieldStatus",
    "IntakeLogEntry",
    "IntakeOutcome",
    "Label",
    "Lead",
    "LeadField",
    "LeadFieldType",
    "LeadLabel",
    "LostReason",
    "Membership",
    "MessageTemplate",
    "OutboxEvent",
    "OutboxStatus",
    "PasswordResetToken",
    "PermissionGrant",
    "PermissionTemplate",
    "RefreshToken",
    "SalesGroup",
    "SalesGroupMember",
    "SavedFilter",
    "SavedFilterVisibility",
    "ScheduledReport",
    "ScheduledReportFormat",
    "Stage",
    "StageKind",
    "SystemActionKind",
    "TableLayout",
    "Task",
    "TemplateChannel",
    "TemplateFieldGrant",
    "TemplateLeadView",
    "TenantModel",
    "TimestampMixin",
    "User",
    "VoiceCallContext",
    "VoiceCallExecution",
    "VoiceCallStatus",
    "VoiceExtractionMapping",
    "WebhookEndpoint",
    "Workspace",
]
