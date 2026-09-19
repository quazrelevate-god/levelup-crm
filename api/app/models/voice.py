"""Persistent voice-call context, per lead (CRM-side milestone).

`docs/06-voice-integration-contract.md` and
`docs/09-context-continuity-and-bolna-integration.md`. This table exists so a
repeat call to the same lead is not treated as a new conversation: it holds
the digest of the most recent call — a short summary, when it happened, how
many calls there have been — keyed to the lead it belongs to.

Deliberately a dedicated table, not a `lead_fields` entry. `last_call_summary`
is a structural product concept the voice integration needs to function, not
a piece of a customer's own taxonomy an admin configures at runtime — the same
reasoning that already put `api_keys`, `webhook_endpoints` and the planned
`voice_extraction_mappings` in dedicated tables instead of `values`.

Not a history table. The full call-by-call *history* already lives on the
existing timeline (`actions`, written in the same transaction as this row —
see `app.services.voice_context.VoiceContextService.update_last_call_summary`).
This table is a fast, single-row-per-lead digest: exactly what a voice agent's
next-call prompt needs, without walking the timeline first.

Phase 2 adds `VoiceCallExecution` below: one row per *call attempt*, where
`VoiceCallContext` is one row per *lead*. The two are different questions —
"what does the agent need to know about this person" versus "what happened on
this particular call" — and conflating them is what would make the webhook
non-idempotent.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.enums import VoiceCallStatus
from app.models.mixins import TenantModel

__all__ = ["VoiceCallContext", "VoiceCallExecution"]

voice_call_status_enum = SAEnum(
    VoiceCallStatus,
    name="voice_call_status",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
)


class VoiceCallContext(TenantModel):
    """One row per lead: the digest a voice agent needs to not start cold."""

    __tablename__ = "voice_call_contexts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "lead_id", name="voice_call_contexts_lead_uq"),
    )

    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    #: Short, human (or agent-)written recap of the most recent call. Always
    #: overwritten, never appended — the append-only record is the timeline.
    last_call_summary: Mapped[str | None] = mapped_column(Text())
    last_call_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Idempotency key for the write that produced `last_call_summary` — a
    #: caller-supplied id today, and `execution_id` once Bolna is wired up
    #: (contract §5, which names `execution_id` as exactly this key). A repeat
    #: update carrying the same id is a no-op: this row is not rewritten and
    #: no duplicate timeline action is written.
    last_call_external_id: Mapped[str | None] = mapped_column(String(120))
    call_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    lead = relationship("Lead")


class VoiceCallExecution(TenantModel):
    """One row per Bolna call attempt — the join between a CRM lead and Bolna.

    Contract §4 says "always save `execution_id`; it is the join key for
    webhooks and execution lookups", and §5 makes it the idempotency key for the
    inbound webhook. This table is where that key lives.

    **Why not just `voice_call_contexts.last_call_external_id`?** Because that
    column holds the *most recent* write, and a webhook can arrive late. Two
    calls complete out of order, or a retry of call A lands after call B
    finished, and a single "last id" column reads the stale delivery as new and
    rewrites the summary with an older one. A row per execution makes the
    question "have we already finished processing *this* call?" answerable
    directly, which is what §7 requires.

    **`completed_at` is the idempotency gate, not `status`.** The vendored
    `setup-webhook` skill is explicit that one execution produces several
    deliveries as its status transitions (`queued` → `in-progress` →
    `completed`) and that deduping on the execution id alone throws away the
    later, more useful ones. So non-terminal deliveries update `bolna_status`
    and nothing else, and only the first terminal delivery sets `completed_at`
    and writes back. Everything after that is a no-op.

    **`context_sent` is an audit record, and is safe to store.** It is the exact
    `user_data` that went to Bolna: already View-projected (rule 3) and already
    rendered for speech. By construction it contains lead field values and the
    reserved `crm_*` ids — never a credential, which lives only in
    `app.integrations.bolna` and never enters this layer.
    """

    __tablename__ = "voice_call_executions"

    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    #: Bolna's `execution_id`. Null between writing this row and Bolna
    #: answering — the window §7 calls "webhook arrives before the trigger
    #: commits" — which is why the unique index below is partial.
    external_id: Mapped[str | None] = mapped_column(String(120))
    #: Which Bolna agent placed it, recorded per call: a workspace may change
    #: its configured agent, and history should say what actually happened.
    agent_id: Mapped[str | None] = mapped_column(String(120))
    #: E.164, normalised through the workspace's default country code (rule 12).
    recipient_phone: Mapped[str] = mapped_column(String(32), nullable=False)
    #: `crm_idempotency` from contract §4. Generated once, here, per trigger —
    #: the value you want the first time two triggers race.
    idempotency_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()")
    )
    status: Mapped[VoiceCallStatus] = mapped_column(
        voice_call_status_enum,
        nullable=False,
        default=VoiceCallStatus.QUEUED,
        server_default=text("'QUEUED'"),
    )
    #: Bolna's own status string, kept raw. Their vocabulary is theirs to
    #: change; mapping it into our enum lossily would lose the diagnosis.
    bolna_status: Mapped[str | None] = mapped_column(String(60))
    #: The exact `user_data` sent. See the class docstring on why this is safe.
    context_sent: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )
    #: The last webhook body Bolna delivered for this execution, verbatim.
    #:
    #: Kept because the vendored skills describe `telephony_data` in prose
    #: ("to/from numbers, call type, ring duration") but never at field level —
    #: the shared `references/execution-payload.md` they point at is not in this
    #: repo. Rather than guess the key names and silently drop a duration, the
    #: receiver reads several plausible spellings *and* stores what actually
    #: arrived, so the first real call settles it from evidence.
    #:
    #: Safe to store: it is the vendor's own description of a call. The Bolna
    #: credential travels in a request header this process sends, never in a
    #: body it receives.
    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )
    attempts: Mapped[int] = mapped_column(
        SmallInteger(), nullable=False, default=0, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text())
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Set by the first terminal webhook. Non-null means "already written back";
    #: every later delivery for this execution is a duplicate.
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- post-call outcome (migration 0016) ---------------------------------
    #: The conversation as Bolna reported it. Per call, unlike `raw_payload`
    #: (overwritten per delivery) and `last_call_summary` (overwritten per call).
    transcript: Mapped[str | None] = mapped_column(Text())
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    #: Exactly the text the timeline's AI Call entry shows.
    summary: Mapped[str | None] = mapped_column(Text())
    #: `AI` when the summariser produced it, `FALLBACK` when it is the safe
    #: placeholder. Never inferred from the text.
    summary_source: Mapped[str | None] = mapped_column(String(20))
    #: Why the summary is a fallback: `summarizer_failed: <ExceptionType>` or
    #: `summary_unavailable`. Never an exception message or payload text.
    summary_error: Mapped[str | None] = mapped_column(Text())
    #: When the most recent delivery for this execution arrived.
    webhook_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The `CALL_LOGGED` action this call produced on the lead's timeline.
    call_action_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    lead = relationship("Lead")

    __table_args__ = (
        # Partial: many rows may legitimately have no external id at once (every
        # call still in flight), but a given Bolna execution belongs to exactly
        # one CRM row.
        Index(
            "voice_call_executions_external_uq",
            "workspace_id",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
        ),
        UniqueConstraint("workspace_id", "idempotency_key", name="voice_call_executions_idem_uq"),
        # The lead's call history, newest first — the receiver's lookup and the
        # only list this table serves.
        Index("ix_voice_call_executions_lead", "workspace_id", "lead_id", "created_at"),
        # The retry worker's query: rows that never reached Bolna.
        Index(
            "ix_voice_call_executions_pending",
            "status",
            "created_at",
            postgresql_where=text("status IN ('QUEUED', 'FAILED')"),
        ),
    )
