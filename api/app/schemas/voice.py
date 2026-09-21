"""Request/response models for the voice endpoints.

Nothing here talks to Bolna. `VoiceContextRead` is deliberately built only
from fields that already pass through `FieldProjectionService` (the `values`
map) or that are structural, non-secret lead metadata (stage, assignee,
timeline). There is no path by which a credential, password hash, or API key
can end up in this shape.

That property extends to the Phase 2 shapes below. `VoiceCallTriggerResult`
echoes the `user_data` that was sent to Bolna precisely *because* it contains
nothing secret — it is the projected, rendered lead context and the reserved
`crm_*` ids, and echoing it is what lets an operator (and the test suite) prove
what the agent was actually told. The Bolna credential lives in
`app.integrations.bolna` and is never passed into, or out of, this layer.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "ExtractionRead",
    "VoiceCallDetailRead",
    "VoiceCallLeadRef",
    "VoiceCallSummaryRead",
    "VoiceCallTrigger",
    "VoiceCallTriggerResult",
    "VoiceContextRead",
    "VoiceExecutionResult",
    "VoiceExecutionWebhook",
    "VoiceInteraction",
    "VoiceSummaryUpdate",
    "VoiceSummaryUpdateResult",
]


class VoiceInteraction(BaseModel):
    """One entry from the lead's existing timeline (`actions`), trimmed."""

    kind: str
    body: str | None = None
    performed_at: dt.datetime


class VoiceContextRead(BaseModel):
    """Everything a voice agent needs to avoid starting cold on a repeat call."""

    lead_id: uuid.UUID
    identity_value: str
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    stage_id: uuid.UUID | None = None
    stage_name: str | None = None
    assignee_id: uuid.UUID | None = None
    assignee_name: str | None = None
    #: The caller's View-projected subset of the lead's own fields — never
    #: the raw stored blob. See `FieldProjectionService.project_values`.
    values: dict[str, Any] = Field(default_factory=dict)
    last_call_summary: str | None = None
    last_call_at: dt.datetime | None = None
    call_count: int = 0
    recent_interactions: list[VoiceInteraction] = Field(default_factory=list)
    created_at: dt.datetime
    last_action_at: dt.datetime | None = None


class VoiceSummaryUpdate(BaseModel):
    summary: str = Field(min_length=1, max_length=4_000)
    #: Idempotency key. Send the same value on a retried request — or, once
    #: Bolna is wired up, that call's `execution_id` — and the second
    #: delivery is a no-op: no field rewrite, no duplicate timeline entry.
    external_id: str | None = Field(default=None, max_length=120)


class VoiceSummaryUpdateResult(BaseModel):
    context: VoiceContextRead
    #: False when this request was a duplicate of the last recorded write
    #: (same `external_id`) and nothing changed.
    written: bool


# --- Phase 2: the Bolna integration ---------------------------------------


class VoiceCallTrigger(BaseModel):
    """Ask the CRM to place a Bolna call to an existing lead.

    Either identifier works, and exactly one is required. `phone` is the one a
    future inbound flow will have on hand before it knows any CRM id; `lead_id`
    is what the app's own UI will send. Neither ever *creates* a lead — an
    unknown number is a 404, because a voice trigger inventing a customer record
    is how a CRM ends up with two of everybody.
    """

    model_config = ConfigDict(extra="forbid")

    lead_id: uuid.UUID | None = None
    phone: str | None = Field(default=None, min_length=1, max_length=32)
    #: Override the deployment's configured agent for this one call.
    agent_id: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _exactly_one_identifier(self) -> VoiceCallTrigger:
        if (self.lead_id is None) == (self.phone is None):
            raise ValueError("Provide exactly one of lead_id or phone")
        return self


class VoiceCallTriggerResult(BaseModel):
    """What the CRM caller gets back once Bolna has accepted the call."""

    #: This CRM's own row id for the attempt.
    call_id: uuid.UUID
    lead_id: uuid.UUID
    #: Bolna's `execution_id` — the join key for the completion webhook.
    execution_id: str | None = None
    #: The CRM's view of the attempt: QUEUED, DISPATCHED, COMPLETED, FAILED.
    status: str
    #: Bolna's own status string, passed through unmapped.
    bolna_status: str | None = None
    agent_id: str | None = None
    recipient_phone: str
    #: The exact `user_data` sent to Bolna (contract §4). Safe to echo: it is
    #: the projected, rendered lead context plus the reserved `crm_*` ids.
    user_data: dict[str, Any] = Field(default_factory=dict)
    #: Convenience for the caller and the proof this milestone exists for: the
    #: summary carried into *this* call, i.e. what the previous call left behind.
    previous_call_summary: str | None = None
    call_count: int = 0
    error: str | None = None


class VoiceExecutionWebhook(BaseModel):
    """Bolna's execution payload, forwarded as-is (contract §5).

    Permissive on purpose. The vendored `get-executions` skill lists a dozen
    fields that may or may not be present depending on how a call ended, and
    Bolna adds more over time; rejecting an unknown key would turn a vendor
    release into a dropped call summary. Only the parts the CRM actually reads
    are named — everything else is kept and ignored.

    Bolna spells the execution id `execution_id` on the webhook and `id` on
    `GET /executions/{id}`; both are accepted so the same receiver works for a
    replayed reconciliation fetch.
    """

    model_config = ConfigDict(extra="allow")

    execution_id: str | None = Field(default=None, max_length=120)
    id: str | None = Field(default=None, max_length=120)
    agent_id: str | None = Field(default=None, max_length=120)
    status: str | None = None
    #: A summary supplied directly by the caller, or Bolna's own LLM summary
    #: when summarisation is enabled on the agent. Unbounded here and capped by
    #: the summariser: a long vendor summary must not 422 the whole call.
    summary: str | None = None
    #: Usually a string; tolerated as a list of turns (see `voice_postcall`).
    transcript: str | list[Any] | None = None
    #: Keyed by disposition name, exactly as Bolna emits it (contract §5).
    extracted_data: dict[str, Any] = Field(default_factory=dict)
    #: Carries the reserved `crm_*` keys the trigger put there.
    user_data: dict[str, Any] = Field(default_factory=dict)
    context_details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("extracted_data", "user_data", "context_details", mode="before")
    @classmethod
    def _null_is_empty(cls, value: Any) -> Any:
        """Bolna sends `null` for these until a call completes.

        Rejecting that would 422 every in-progress status delivery, so an
        absent or non-object value is read as "nothing yet".
        """
        return value if isinstance(value, dict) else {}

    @field_validator("status", "summary", mode="before")
    @classmethod
    def _text_or_none(cls, value: Any) -> Any:
        if value is None or isinstance(value, str):
            return value
        return None

    @field_validator("status", mode="after")
    @classmethod
    def _bounded_status(cls, value: str | None) -> str | None:
        return value[:60] if value else value

    def resolved_execution_id(self) -> str | None:
        return self.execution_id or self.id


class VoiceExecutionResult(BaseModel):
    """The receiver's answer.

    `duplicate` is a 200, not an error: Bolna retries, and a non-2xx would make
    it retry harder for a delivery the CRM has already fully processed.
    """

    #: `accepted` (written back), `duplicate` (already processed, no-op),
    #: `pending` (a non-terminal status update, recorded but nothing written),
    #: `upgraded` (an already-recorded call completed by a richer delivery —
    #: same row, same call log; see `VoiceCallService._upgrade_reasons`).
    status: str
    execution_id: str | None = None
    lead_id: uuid.UUID | None = None
    #: Whether this delivery actually changed the lead's stored context.
    written: bool = False
    call_count: int = 0
    last_call_summary: str | None = None
    #: Additive — mapped disposition names whose extracted value was written
    #: back to a lead field on this delivery. Empty when no mappings are
    #: configured, or when nothing survived the confidence/validation gates.
    extraction_written: list[str] = Field(default_factory=list)
    #: Additive — mapped disposition names whose extracted value produced a
    #: timeline note but no field write (below confidence, invalid, hidden
    #: field, or a summary/identity-disposition conflict).
    extraction_noted: list[str] = Field(default_factory=list)
    #: Additive — dispositions present in the payload for which no enabled
    #: mapping exists. Useful for the operator setting mappings up: what
    #: extractions Bolna is producing that nothing is listening for.
    extraction_unmapped: list[str] = Field(default_factory=list)
    #: Post-call automation. The text the timeline's AI Call entry shows, and
    #: whether it is the AI summary (`AI`) or the safe placeholder (`FALLBACK`).
    #: Null on `pending` deliveries.
    call_summary: str | None = None
    summary_source: str | None = None
    #: The `CALL_LOGGED` action this call produced, and the CRM's own row id.
    call_log_id: uuid.UUID | None = None
    call_id: uuid.UUID | None = None


# --- reading past calls back (docs/13 §6) ----------------------------------


class VoiceCallLeadRef(BaseModel):
    """Who a call was with, in the workspace's own vocabulary.

    The two headline values are the workspace's configured primary fields
    (H1/H2) — for one customer that is Name and Course, for another something
    else entirely. Both arrive already View-projected, so a caller who cannot
    view a field gets `None` rather than its value, and the labels come from
    the field definitions rather than from anything hardcoded.
    """

    lead_id: uuid.UUID
    identity_value: str
    primary_h1: Any = None
    primary_h2: Any = None
    primary_h1_label: str | None = None
    primary_h2_label: str | None = None


class VoiceCallSummaryRead(BaseModel):
    """One AI call, at the level the lead card and the call list need.

    Deliberately without transcript, extracted data or raw payload: those are
    the detail read. A list of fifty calls must not ship fifty transcripts.
    """

    id: uuid.UUID
    lead: VoiceCallLeadRef
    execution_id: str | None = None
    #: The CRM's own view: QUEUED, DISPATCHED, COMPLETED, FAILED.
    status: str
    #: Bolna's own status string, unmapped.
    bolna_status: str | None = None
    duration_seconds: int | None = None
    summary: str | None = None
    #: `AI` when the summariser wrote it, `FALLBACK` for the safe placeholder.
    summary_source: str | None = None
    agent_id: str | None = None
    created_at: dt.datetime
    dispatched_at: dt.datetime | None = None
    completed_at: dt.datetime | None = None


class ExtractionRead(BaseModel):
    """One extracted item, flattened out of Bolna's grouping for display.

    Bolna nests: `{"General": {"Call Summary": {"subjective": …}}}`. Reading
    the top level as the extraction's name makes "General" look like the
    extraction and hides the only thing anybody wanted to see, so the group
    and the name are carried separately here.
    """

    #: `General`, when the payload grouped its extractions; else null.
    group: str | None = None
    #: The extraction's own name, e.g. `Call Summary`.
    name: str
    #: `Group / Name`, or just the name when ungrouped.
    path: str
    #: The value itself, already unwrapped from `value` / `subjective` / … .
    value: Any = None
    confidence: float | None = None


class VoiceCallDetailRead(VoiceCallSummaryRead):
    """One AI call, in full. The dedicated call page's read."""

    recipient_phone: str
    transcript: str | None = None
    extracted_data: dict[str, Any] = Field(default_factory=dict)
    #: The same extractions, flattened for display: one entry per extracted
    #: item, whether or not any mapping writes it to a lead field.
    extractions: list[ExtractionRead] = Field(default_factory=list)
    #: The vendor's own body, **sanitised**: anything credential-shaped is
    #: redacted on the way out (`services.voice_history.redact_payload`).
    #: Read-only — there is no endpoint that writes it.
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    webhook_received_at: dt.datetime | None = None
    #: The `CALL_LOGGED` timeline action this call produced, if it got that far.
    call_log_id: uuid.UUID | None = None
    summary_error: str | None = None
    last_error: str | None = None
