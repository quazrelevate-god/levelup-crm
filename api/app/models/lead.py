"""Leads, actions and changesets (M5).

`docs/01-data-model.md` §4. The three things PROMPTS.md M5 says must be designed
in now because they cannot be retrofitted:

1. **Changesets.** Every mutation batch opens one, and every action it produces
   carries its id. That is what makes M7's undo possible.
2. **`STAGE_CHANGE` / `ASSIGNMENT_CHANGE` payloads carrying old and new ids**,
   with the expression indexes M6's history filters need — created in this
   revision, not in M6.
3. **`score_applied` snapshotted on each action**, so editing a custom action
   type's score later does not silently rewrite history.

Lead values live in JSONB keyed by `lead_fields.key`. There are no per-customer
columns and no DDL at runtime (architecture rule 7).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.enums import ChangesetSource, SystemActionKind, TemplateChannel
from app.models.mixins import TenantModel

__all__ = [
    "Action",
    "ActionAttachment",
    "Changeset",
    "Lead",
    "MessageTemplate",
]


class Changeset(TenantModel):
    """One mutation batch (§4.2).

    A single PATCH, a 500-lead bulk edit, an import run, a redistribution — each
    opens exactly one changeset, and every action it produces carries the id.
    Undo replays inverse field changes for the whole set atomically.

    Designed in now because retrofitting a batch id across every mutation path
    later means touching every one of them again.
    """

    __tablename__ = "changesets"
    __table_args__ = (Index("changesets_ws_created_idx", "workspace_id", "created_at"),)

    source: Mapped[ChangesetSource] = mapped_column(
        SAEnum(ChangesetSource, name="changeset_source", native_enum=True), nullable=False
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memberships.id", ondelete="SET NULL")
    )
    #: Human summary, e.g. "Set Stage to Contacted on 312 leads". Written at
    #: open time so the edit report reads without reconstructing intent.
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    lead_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    is_undone: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    undone_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memberships.id", ondelete="SET NULL")
    )
    #: An undo is itself a changeset, pointing at what it reversed.
    undo_of_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("changesets.id", ondelete="SET NULL")
    )


class Lead(TenantModel):
    """One lead. Its customer-defined data lives in `values`.

    `identity_value` is a denormalised copy of whichever field the workspace
    designated as its identifier (§1.1) — Phone by default, but Email for a B2B
    customer and a registration number for a dealership. Changing that setting
    triggers a backfill.

    Soft delete only (architecture rule 13): `deleted_at`, never a DELETE.
    """

    __tablename__ = "leads"
    __table_args__ = (
        Index(
            "leads_identity_uq",
            "workspace_id",
            "identity_value",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "leads_ws_created_idx",
            "workspace_id",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "leads_ws_stage_idx",
            "workspace_id",
            "stage_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "leads_ws_assignee_idx",
            "workspace_id",
            "assignee_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "leads_ws_score_idx",
            "workspace_id",
            "score",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("leads_values_gin", "values", postgresql_using="gin"),
        # Free-text search over the workspace's own fields. A workspace invents
        # its fields, so there is no column to index — the vector is maintained
        # on write from the searchable ones (`app.fields.search`).
        Index("leads_search_idx", "search_vector", postgresql_using="gin"),
        # Partial matching on the identity field. A tsquery matches whole
        # lexemes, so "98765" would never find `+919876543210`; trigrams are
        # what make typing a fragment of a phone number work.
        Index(
            "leads_identity_trgm",
            "identity_value",
            postgresql_using="gin",
            postgresql_ops={"identity_value": "gin_trgm_ops"},
        ),
        CheckConstraint("rating IS NULL OR rating BETWEEN 1 AND 5", name="ck_leads_rating_range"),
        # "Which leads may a batch call" — the approved few, never the rest.
        Index(
            "ix_leads_voice_approved",
            "workspace_id",
            postgresql_where=text("voice_approved_at IS NOT NULL"),
        ),
    )

    identity_value: Mapped[str] = mapped_column(String(320), nullable=False)
    #: Maintained on write, never by a trigger: the searchable set is decided by
    #: the type registry, which is Python, and a trigger would have to duplicate
    #: that knowledge in PL/pgSQL and drift from it.
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR)
    #: Keyed by `lead_fields.key`. No per-customer columns, ever.
    values: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    stage_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stages.id", ondelete="SET NULL")
    )
    lost_reason_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lost_reasons.id", ondelete="SET NULL")
    )
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memberships.id", ondelete="SET NULL")
    )
    rating: Mapped[int | None] = mapped_column(SmallInteger)
    #: Rollup of `actions.score_applied`. Maintained by the action-writing
    #: service rather than computed on read — a lead list sorting by score
    #: cannot aggregate a timeline per row.
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))

    last_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memberships.id", ondelete="SET NULL")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Approved for AI calling (docs/15): set means a batch may dial this lead,
    #: NULL means it may not. A single "AI Call" from the lead screen does not
    #: need it — that is a person choosing this lead there and then.
    voice_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voice_approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memberships.id", ondelete="SET NULL")
    )

    actions: Mapped[list[Action]] = relationship(
        back_populates="lead",
        cascade="all, delete-orphan",
        order_by="Action.performed_at.desc()",
    )


class Action(TenantModel):
    """One timeline event (§4.1).

    The timeline is the audit trail: every lead mutation writes one of these in
    the same transaction as the change itself (architecture rule 5).

    `score_applied` is copied from the action type at write time so editing a
    type's score later does not silently rewrite history.
    """

    __tablename__ = "actions"
    __table_args__ = (
        Index("actions_lead_time_idx", "lead_id", "performed_at"),
        Index("actions_ws_kind_idx", "workspace_id", "kind", "performed_at"),
        Index("actions_changeset_idx", "changeset_id"),
        # M6's history filters depend on these two. Built here, in the
        # milestone that defines the payloads, rather than in the one that
        # needs them (docs/01-data-model.md §6.1).
        Index(
            "actions_status_change_idx",
            "workspace_id",
            text("(payload ->> 'old_stage_id')"),
            text("(payload ->> 'new_stage_id')"),
            "performed_at",
            postgresql_where=text("kind = 'STAGE_CHANGE'"),
        ),
        Index(
            "actions_assignment_idx",
            "workspace_id",
            text("(payload ->> 'old_assignee_id')"),
            text("(payload ->> 'new_assignee_id')"),
            "performed_at",
            postgresql_where=text("kind = 'ASSIGNMENT_CHANGE'"),
        ),
    )

    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    #: The batch this action belongs to. Nullable only for actions written
    #: outside any mutation (a note), which still open a changeset in practice.
    changeset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("changesets.id", ondelete="SET NULL")
    )
    kind: Mapped[SystemActionKind] = mapped_column(
        SAEnum(SystemActionKind, name="system_action_kind", native_enum=True), nullable=False
    )
    #: Set when `kind` is CUSTOM.
    action_type_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("custom_action_types.id", ondelete="SET NULL")
    )
    #: Null means the system did it — an automation, an intake payload.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memberships.id", ondelete="SET NULL")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    body: Mapped[str | None] = mapped_column(Text)
    score_applied: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    is_pinned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: Separate from `created_at`: a predated action happened before it was
    #: recorded, and the timeline orders by when it happened.
    performed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    lead: Mapped[Lead] = relationship(back_populates="actions")
    attachments: Mapped[list[ActionAttachment]] = relationship(
        back_populates="action", cascade="all, delete-orphan"
    )


class ActionAttachment(TenantModel):
    """A file uploaded against a FILE action field."""

    __tablename__ = "action_attachments"

    action_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actions.id", ondelete="CASCADE"), nullable=False
    )
    field_key: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    action: Mapped[Action] = relationship(back_populates="attachments")


class MessageTemplate(TenantModel):
    """A canned WhatsApp/SMS/email body with `{{field_key}}` substitution (§5.4).

    Visibility is one of three: personal (`owner_id` = the caller), shared
    (both null) or role-scoped (`template_id` = the caller's permission
    template).

    Rendering substitutes from a lead's values **through
    `FieldProjectionService`**, so a template cannot leak a field the sender
    lacks View on.
    """

    __tablename__ = "message_templates"
    __table_args__ = (Index("ix_message_templates_ws_channel", "workspace_id", "channel"),)

    channel: Mapped[TemplateChannel] = mapped_column(
        SAEnum(TemplateChannel, name="template_channel", native_enum=True), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: EMAIL only.
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: Null means shared with the whole workspace.
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memberships.id", ondelete="CASCADE")
    )
    #: Set to scope the template to one permission template's holders.
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("permission_templates.id", ondelete="CASCADE")
    )
    is_archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    def as_payload(self) -> dict[str, Any]:
        """Serialisable form. `owner_id`/`template_id` are reported so the UI
        can label a template personal, shared or role-scoped."""
        return {
            "id": str(self.id),
            "channel": self.channel.value,
            "name": self.name,
            "subject": self.subject,
            "body": self.body,
            "owner_id": str(self.owner_id) if self.owner_id else None,
            "template_id": str(self.template_id) if self.template_id else None,
            "visibility": (
                "personal" if self.owner_id else ("role" if self.template_id else "shared")
            ),
        }
