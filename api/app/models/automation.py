"""Connected apps and workflows (docs/18).

Two halves that meet in the middle:

**Connected apps** are outside systems that tell the CRM something happened —
Razorpay saying a payment was captured. A connection holds what the CRM needs
to trust that message (a URL token and the app's signing secret) and every
message received is logged in `app_events`, matched or not, so "the payment
went through and nothing moved" can always be answered.

**Workflows** decide what to do about it. A workflow is a small graph a
workspace admin draws — a trigger, if/else checks, actions — stored as JSONB.
Every run leaves a `workflow_runs` row with the path it took, step by step.

Which apps and which triggers exist is product code (like the 13 field types);
everything a workspace draws, names or chooses — stages, fields, amounts — is
its own data in these rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.mixins import TenantModel

__all__ = ["AppConnection", "AppEvent", "Workflow", "WorkflowRun"]


class AppConnection(TenantModel):
    """One outside app connected to a workspace (one per provider)."""

    __tablename__ = "app_connections"

    #: Product-defined: `razorpay`.
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=True, server_default=text("true")
    )
    #: The unguessable segment of this connection's webhook URL. Globally
    #: unique, because the URL is how an incoming request finds its workspace.
    hook_token: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The app's webhook signing secret, used to verify every delivery. Needed
    #: in plaintext to compute the HMAC, like `webhook_endpoints.secret`, and
    #: never returned by the API — the settings screen only says whether it is set.
    secret: Mapped[str | None] = mapped_column(Text())
    #: Provider options, e.g. `{"create_lead_if_missing": false}`.
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memberships.id", ondelete="SET NULL")
    )
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("workspace_id", "provider", name="app_connections_provider_uq"),
        UniqueConstraint("hook_token", name="app_connections_hook_token_uq"),
    )


class AppEvent(TenantModel):
    """One message an app sent, whatever became of it."""

    __tablename__ = "app_events"

    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_connections.id", ondelete="CASCADE"), nullable=False
    )
    #: The app's own name for it, e.g. `payment.captured`.
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    #: The CRM trigger it maps to, e.g. `razorpay.payment_captured`; null when
    #: the event is not one any workflow can start from.
    trigger: Mapped[str | None] = mapped_column(String(80))
    #: What makes two deliveries the same fact (the payment id for a payment),
    #: so Razorpay's retries and its several events per payment run once.
    dedupe_key: Mapped[str | None] = mapped_column(String(160))
    #: The normalised fields workflows read: amount, phone, email, notes, ...
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )
    #: The body as it arrived, bounded on write.
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )
    #: MATCHED, CREATED_LEAD, NO_LEAD, DUPLICATE, IGNORED, REJECTED.
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL")
    )
    workflows_run: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default=text("0")
    )
    error: Mapped[str | None] = mapped_column(Text())

    __table_args__ = (
        Index("ix_app_events_recent", "workspace_id", "connection_id", "created_at"),
        # The claim: Razorpay sends `payment.captured` and `payment_link.paid`
        # for one payment within milliseconds, and retries on any hiccup. The
        # second insert waits on the first and then fails here, so a payment
        # runs its workflows exactly once however it arrives.
        Index(
            "app_events_dedupe_uq",
            "workspace_id",
            "connection_id",
            "trigger",
            "dedupe_key",
            unique=True,
            postgresql_where=text(
                "dedupe_key IS NOT NULL AND outcome IN ('MATCHED', 'CREATED_LEAD', 'NO_LEAD')"
            ),
        ),
    )


class Workflow(TenantModel):
    """A trigger and the graph of checks and actions that follows it."""

    __tablename__ = "workflows"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Copied from the graph's trigger node on save, so "which workflows start
    #: on this event" is an indexed lookup rather than a scan of every graph.
    trigger: Mapped[str | None] = mapped_column(String(80))
    #: `{"nodes": [{id, type, position, data}], "edges": [{id, source,
    #: sourceHandle, target}]}` — the shape the canvas edits directly.
    graph: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )
    #: Off until someone switches it on: a half-drawn workflow never runs.
    is_active: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False, server_default=text("false")
    )
    #: Soft delete (rule 13): runs keep pointing at it.
    is_archived: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False, server_default=text("false")
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memberships.id", ondelete="SET NULL")
    )
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memberships.id", ondelete="SET NULL")
    )

    __table_args__ = (
        Index("ix_workflows_trigger", "workspace_id", "trigger", "is_active"),
        Index(
            "workflows_name_uq",
            "workspace_id",
            "name",
            unique=True,
            postgresql_where=text("NOT is_archived"),
        ),
    )


class WorkflowRun(TenantModel):
    """One time a workflow ran for one lead, and the path it took."""

    __tablename__ = "workflow_runs"

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL")
    )
    trigger: Mapped[str] = mapped_column(String(80), nullable=False)
    app_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_events.id", ondelete="SET NULL")
    )
    #: SUCCESS or FAILED (a step could not be done).
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    #: `[{node_id, type, label, result, detail}]` in the order they ran.
    steps: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'[]'::jsonb")
    )
    #: The trigger's data as the workflow saw it.
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )
    duration_ms: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default=text("0")
    )
    error: Mapped[str | None] = mapped_column(Text())

    __table_args__ = (
        Index("ix_workflow_runs_recent", "workspace_id", "workflow_id", "created_at"),
    )
