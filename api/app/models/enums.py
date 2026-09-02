"""Product enums.

Per docs/01-data-model.md §1: the only enums in this database are *product*
concepts. Every business concept is a row. If you are about to add something
like `ProductType` or `ApplicationStatus` here, it belongs in a table.

M1 owns one enum. The rest (`stage_kind`, `lead_field_type`, ...) arrive with
the milestones that need them.
"""

from __future__ import annotations

import enum

__all__ = [
    "ActionDirection",
    "ActionFieldType",
    "AssignmentStrategy",
    "AvailabilityStatus",
    "ChangesetSource",
    "DedupeMode",
    "ImportJobKind",
    "ImportJobStatus",
    "IndexedFieldStatus",
    "IntakeOutcome",
    "LeadFieldType",
    "OutboxStatus",
    "PermissionGrant",
    "SavedFilterVisibility",
    "ScheduledReportFormat",
    "StageKind",
    "SystemActionKind",
    "TemplateChannel",
    "VoiceCallStatus",
]


class AvailabilityStatus(enum.StrEnum):
    """Whether a member is eligible to receive work.

    WORKING is the only status the assignment engine (M8) will hand a lead to
    when a rule sets `skip_unavailable`. ON_LEAVE is temporary and self-service;
    INACTIVE is set by deactivation and requires a licence to reverse.
    """

    WORKING = "WORKING"
    ON_LEAVE = "ON_LEAVE"
    INACTIVE = "INACTIVE"


class LeadFieldType(enum.StrEnum):
    """The 13 lead field types (docs/03-configuration-model.md §1.3).

    This list is closed. It is the one place the product declares *kinds of
    data*; what a workspace calls a field, and which options it offers, are
    rows. Adding `COURSE` or `APPLICATION_STATUS` here would be the exact
    mistake CLAUDE.md warns about.
    """

    TEXT = "TEXT"
    DROPDOWN = "DROPDOWN"
    TAGS = "TAGS"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    CHECKBOX = "CHECKBOX"
    DATE = "DATE"
    MONEY = "MONEY"
    NUMBER = "NUMBER"
    WEBSITE = "WEBSITE"
    DEPENDENT_DROPDOWN = "DEPENDENT_DROPDOWN"
    RECURRING_DATE = "RECURRING_DATE"
    LOCATION = "LOCATION"


class ActionFieldType(enum.StrEnum):
    """The 8 action field types (docs/03-configuration-model.md §4.3).

    Deliberately a different, smaller set than `LeadFieldType`: an action can
    reference a workspace member or carry an upload, and a lead field cannot.
    """

    TEXT = "TEXT"
    NUMBER = "NUMBER"
    DATE = "DATE"
    DROPDOWN = "DROPDOWN"
    TAGS = "TAGS"
    USER = "USER"
    FILE = "FILE"
    MEDIA_LINK = "MEDIA_LINK"


class IndexedFieldStatus(enum.StrEnum):
    """Lifecycle of a workspace-declared expression index.

    Not a Postgres enum — stored as text per docs/01-data-model.md §2.4, which
    declares the column `text NOT NULL DEFAULT 'PENDING'`.
    """

    PENDING = "PENDING"
    READY = "READY"
    FAILED = "FAILED"


class StageKind(enum.StrEnum):
    """Structural pipeline kinds — NOT customer statuses.

    A workspace names and colours its stages freely; what it cannot do is have
    two live WON stages. The kind is what the product reasons about, the label
    is what the customer reasons about.
    """

    INITIAL = "INITIAL"
    ACTIVE = "ACTIVE"
    WON = "WON"
    LOST = "LOST"


class ActionDirection(enum.StrEnum):
    """Direction of a custom action (docs/03-configuration-model.md §4.2)."""

    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"
    INFORMATION = "INFORMATION"


class SystemActionKind(enum.StrEnum):
    """Timeline event kinds the product itself defines.

    Customer-defined action types live in `custom_action_types` and surface
    here as `CUSTOM`, carrying `action_type_id`.
    """

    LEAD_CREATED = "LEAD_CREATED"
    FIELD_CHANGE = "FIELD_CHANGE"
    STAGE_CHANGE = "STAGE_CHANGE"
    ASSIGNMENT_CHANGE = "ASSIGNMENT_CHANGE"
    RATING_CHANGE = "RATING_CHANGE"
    NOTE = "NOTE"
    CALL_LOGGED = "CALL_LOGGED"
    WHATSAPP_SENT = "WHATSAPP_SENT"
    EMAIL_SENT = "EMAIL_SENT"
    SMS_SENT = "SMS_SENT"
    TASK_CREATED = "TASK_CREATED"
    TASK_COMPLETED = "TASK_COMPLETED"
    CUSTOM = "CUSTOM"


class PermissionGrant(enum.StrEnum):
    """The four independent grants in the field matrix (§6.4).

    Presence of a `template_field_grants` row means granted; absence means
    denied. There is no explicit deny.
    """

    VIEW = "VIEW"
    EDIT = "EDIT"
    IMPORT = "IMPORT"
    EXPORT = "EXPORT"


class ChangesetSource(enum.StrEnum):
    """What opened a changeset (docs/01-data-model.md §4.2)."""

    SINGLE_EDIT = "SINGLE_EDIT"
    BULK_EDIT = "BULK_EDIT"
    IMPORT = "IMPORT"
    DISTRIBUTION = "DISTRIBUTION"
    AUTOMATION = "AUTOMATION"
    INTAKE = "INTAKE"


class TemplateChannel(enum.StrEnum):
    """Message template channels (docs/01-data-model.md §5.4)."""

    WHATSAPP = "WHATSAPP"
    SMS = "SMS"
    EMAIL = "EMAIL"


class SavedFilterVisibility(enum.StrEnum):
    """Who a saved filter is for (docs/01-data-model.md §6).

    PERSONAL is the author's alone. SHARED is the whole workspace. ROLE hands
    it to everyone on one permission template, which is how a manager gives
    their callers a worklist without giving it to marketing.

    Note this governs *visibility of the filter*, never of the leads it
    returns: running someone else's filter still projects through the runner's
    own field grants, so a shared filter cannot be used to read a column its
    reader lacks.
    """

    PERSONAL = "PERSONAL"
    SHARED = "SHARED"
    ROLE = "ROLE"


class ImportJobKind(enum.StrEnum):
    """What a job run is doing (M7).

    Four kinds rather than one with a flag, because the audit found four
    genuinely different flows behind "upload a spreadsheet" and treating them
    as one produced a mapping screen that could not express any of them
    properly.
    """

    #: Create-or-update leads from a sheet, keyed on the identity field.
    LEAD_IMPORT = "LEAD_IMPORT"
    #: Update existing leads only. Distinct from LEAD_IMPORT so a typo in the
    #: identity column fails the row instead of silently creating a lead.
    LEAD_UPDATE = "LEAD_UPDATE"
    #: Historical timeline migration — the flow every customer switching CRMs
    #: needs, and the one the audit called out as missing.
    ACTION_IMPORT = "ACTION_IMPORT"
    EXPORT = "EXPORT"


class ImportJobStatus(enum.StrEnum):
    """Where a run has got to.

    The operator maps, previews, then commits, and can stop after any of them —
    so the intermediate states are real rather than transient.
    """

    UPLOADED = "UPLOADED"
    #: A mapping has been stored but not yet dry-run.
    MAPPED = "MAPPED"
    #: Dry run complete; `result` holds the create/update counts.
    PREVIEWED = "PREVIEWED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class AssignmentStrategy(enum.StrEnum):
    """How a matched rule picks the member (docs/01-data-model.md §5.3).

    Six ways of answering one question. They are product concepts — *how* to
    choose — never *who*: the members, groups and field values a strategy
    reads are all rows.
    """

    #: Next member in the list, by cursor. The cursor is per rule.
    ROUND_ROBIN = "ROUND_ROBIN"
    #: Round-robin, but a member with weight 3 is dealt three times per cycle.
    WEIGHTED = "WEIGHTED"
    #: Look up `config.map[<the lead's value for config.field_key>]`.
    FIELD_VALUE = "FIELD_VALUE"
    #: Round-robin within `config.group_id`, honouring per-member weights.
    SALES_GROUP = "SALES_GROUP"
    #: Always `config.membership_id`. Useful as a catch-all last rule.
    FIXED = "FIXED"
    #: Match, and deliberately leave the lead unassigned. Lets an admin stop
    #: rule evaluation for a segment without deleting the rules below it.
    UNASSIGNED = "UNASSIGNED"


class ScheduledReportFormat(enum.StrEnum):
    """Attachment format for a scheduled report (§5.5)."""

    CSV = "CSV"
    XLSX = "XLSX"


class DedupeMode(enum.StrEnum):
    """What intake does when the identity already exists (§Intake).

    A product concept: three answers to one question, none of them anything a
    customer names.
    """

    #: Merge non-null values onto the existing lead. Never blanks what is there.
    UPDATE = "UPDATE"
    #: Leave the existing lead untouched and report it.
    SKIP = "SKIP"
    #: Deliberately create a second lead. Refused when the workspace's identity
    #: field is unique, which it always is — kept so the API can say *why*
    #: rather than silently doing something else.
    CREATE_DUPLICATE = "CREATE_DUPLICATE"


class OutboxStatus(enum.StrEnum):
    """Where one outbound delivery has got to (§Outbound)."""

    PENDING = "PENDING"
    #: Claimed by a worker. A row stuck here after a crash is retried, not lost.
    DELIVERING = "DELIVERING"
    DELIVERED = "DELIVERED"
    #: Failed, will be retried at `next_attempt_at`.
    FAILED = "FAILED"
    #: Out of attempts. Never retried automatically; an operator redrives it.
    DEAD = "DEAD"


class IntakeOutcome(enum.StrEnum):
    """What one intake request did. Rejections are logged too (§Intake)."""

    CREATED = "CREATED"
    UPDATED = "UPDATED"
    #: Matched an existing lead and `dedupe` said to leave it alone.
    SKIPPED = "SKIPPED"
    REJECTED = "REJECTED"


class VoiceCallStatus(enum.StrEnum):
    """Where one Bolna call attempt has got to, from the CRM's point of view.

    Deliberately short and *ours*. Bolna's own vocabulary is richer and is kept
    verbatim in `voice_call_executions.bolna_status` — a vendor is entitled to
    add statuses, and mapping theirs into ours lossily would lose the diagnosis
    while pretending to be authoritative.
    """

    #: Row written, not yet sent. A crash here leaves something to retry.
    QUEUED = "QUEUED"
    #: Bolna accepted it and returned an `execution_id`.
    DISPATCHED = "DISPATCHED"
    #: A terminal webhook arrived and was written back. Never reprocessed.
    COMPLETED = "COMPLETED"
    #: Bolna refused it, was unreachable, or the call ended without a summary.
    FAILED = "FAILED"
