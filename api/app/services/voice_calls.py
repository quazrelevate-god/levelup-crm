"""The CRM ↔ Bolna call integration (Phase 2).

Phase 1 built the durable per-lead context. This module is the transport around
it: it takes an existing CRM lead, hands Bolna everything the agent needs to
carry on the previous conversation, and folds the result of that call back onto
the *same* lead when Bolna reports it finished.

    trigger()            lead ──▶ context ──▶ user_data ──▶ POST /call
                                                              │ execution_id
                                                              ▼
    handle_execution()   lead ◀── summary ◀── webhook ◀───────┘

Everything load-bearing here is a consequence of a rule that already existed:

**No second customer system.** The lead is resolved through `LeadService` and
`VoiceContextService` — the same two objects the rest of the product uses — and
a phone number that matches no lead is a 404. Nothing in this module creates a
lead, so a repeat call to the same number cannot produce a second record of the
same person. The `leads_identity_uq` index guarantees the rest.

**The projection chokepoint is upstream, not bypassed.** `user_data` is built
from `VoiceContextService.get_context`, whose `values` have already been through
`FieldProjectionService` (architecture rule 3). A field the caller's permission
template cannot View is absent from the payload Bolna receives — not redacted,
absent, exactly as contract §3 requires. This module never touches
`lead.values` directly.

**The webhook identifies the lead by execution id first.** `voice_call_executions`
is the authoritative mapping; then `crm_lead_id` resolved inside the
authenticated workspace; then, if enabled, an *unambiguous* match on the
workspace's phone field. Never by name, and an unmatched delivery never creates
a lead unless `BOLNA_CREATE_MISSING_LEADS` is explicitly on (see
`_lead_for_payload`).

**Post-call automation** (docs/13): the terminal delivery is normalised
(`voice_postcall.normalise_execution`), summarised by Bolna's own LLM summary
with a safe fallback, and written as exactly one `CALL_LOGGED` action marked
`source: AI_CALL` whose body is that summary.

**Write-back is idempotent at two levels.** `voice_call_executions.completed_at`
gates the whole operation, and `VoiceContextService.update_last_call_summary` is
independently idempotent on `external_id`. Either alone would be enough for the
common retry; together they also survive out-of-order delivery, which the single
`last_call_external_id` column could not.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import uuid
from typing import Any

from app.errors import api_error, not_found
from app.fields.rendering import render_for_voice
from app.integrations.bolna import BolnaCallRequest, BolnaClient, BolnaSettings
from app.models.enums import ChangesetSource, VoiceCallStatus
from app.models.field import FieldOption, LeadField
from app.models.lead import Lead
from app.models.pipeline import CallDisposition
from app.models.voice import VoiceCallExecution
from app.models.workspace import Workspace
from app.schemas.voice import VoiceExecutionWebhook
from app.services.actions import ActionWriter
from app.services.leads import LeadService
from app.services.voice_context import VoiceContext, VoiceContextService
from app.services.voice_extraction import ExtractionOutcome, VoiceExtractionService
from app.services.voice_postcall import (
    CALLER_PHONE_PATHS,
    DIRECTION_PATHS,
    DURATION_PATHS,
    PHONE_PATHS,
    SUMMARY_SOURCE_AI,
    TERMINAL_FAILURE_STATUSES,
    TERMINAL_SUCCESS_STATUSES,
    CallSummarizer,
    NormalizedCallResult,
    VendorCallSummarizer,
    dig,
    first_present,
    normalise_execution,
    summarize_call,
)
from app.tenancy.session import ScopedSession

logger = logging.getLogger(__name__)

__all__ = [
    "PHONE_PATHS",
    "RESERVED_PREFIX",
    "STATUS_DISPOSITION_LABELS",
    "TERMINAL_FAILURE_STATUSES",
    "TERMINAL_SUCCESS_STATUSES",
    "TriggerOutcome",
    "VoiceCallService",
    "WebhookOutcome",
]

#: Contract §4: the `crm_*` namespace is reserved so CRM-supplied context can
#: never collide with a customer's own field keys.
RESERVED_PREFIX = "crm_"

#: The payload readers and status vocabulary live in `voice_postcall`, where
#: the normaliser uses them; re-exported here because existing callers and
#: tests import them from this module.
__all__ += [
    "CALLER_PHONE_PATHS",
    "DIRECTION_PATHS",
    "DURATION_PATHS",
    "dig",
    "first_present",
]

#: `CallLogCreate.direction` — the CRM's own three values.
DIRECTION_OUTGOING = "OUTGOING"
DIRECTION_INCOMING = "INCOMING"

#: `CALL_LOGGED.payload.source` for a call the Bolna agent made. The timeline
#: renders these as "AI Call"; a human-logged call has no `source` key.
AI_CALL_SOURCE = "AI_CALL"

#: Bolna's terminal status -> the label of one of the **product's own** system
#: call dispositions (`app/services/provisioning.py::_SYSTEM_DISPOSITIONS`).
#:
#: These are not a customer's vocabulary — they are the seven entries this
#: product provisions into every workspace as `is_system`, which an admin may
#: archive but never rename. Referencing them by label is the same kind of
#: reference `voice_context.py` already makes to the built-in `name` and `email`
#: field keys. A workspace that has archived one simply falls through to its
#: configured default, so this can shape the outcome but never fail a call.
STATUS_DISPOSITION_LABELS: dict[str, str] = {
    "busy": "Number Busy",
    "no-answer": "No Answer",
    "no_answer": "No Answer",
    "call-disconnected": "No Answer",
    "failed": "No Answer",
    "error": "No Answer",
    "canceled": "No Answer",
    "cancelled": "No Answer",
    "stopped": "No Answer",
    "balance-low": "No Answer",
}


#: How many previous timeline entries ride along in the outbound payload. A
#: voice prompt that has to be *spoken* cannot absorb a full timeline, and
#: `last_call_summary` is already the digest — these are for colour.
OUTBOUND_INTERACTION_LIMIT = 3


@dataclasses.dataclass(slots=True)
class TriggerOutcome:
    """The result of asking Bolna to place a call."""

    execution: VoiceCallExecution
    context: VoiceContext
    user_data: dict[str, Any]
    error: str | None = None


@dataclasses.dataclass(slots=True)
class WebhookOutcome:
    """The result of processing one Bolna delivery.

    `extraction` is an additive per-delivery report — a workspace with no
    voice-extraction mappings sees an all-empty `ExtractionOutcome`, so the
    response shape is unchanged for callers that have never configured one.
    """

    status: str
    execution_id: str | None
    lead_id: uuid.UUID | None
    written: bool
    call_count: int
    last_call_summary: str | None
    extraction: ExtractionOutcome = dataclasses.field(default_factory=ExtractionOutcome)
    #: Post-call automation: what this call's timeline entry says, where the
    #: text came from, and which rows it produced.
    call_summary: str | None = None
    summary_source: str | None = None
    call_log_id: uuid.UUID | None = None
    call_id: uuid.UUID | None = None


class VoiceCallService:
    """Built per request, like every other service in this codebase.

    Composes `VoiceContextService` rather than reaching past it: the Phase 1
    context system is the single source of a lead's voice state, and this class
    adds transport around it without owning any of it.
    """

    def __init__(
        self,
        session: ScopedSession,
        *,
        workspace: Workspace,
        leads: LeadService,
        context: VoiceContextService,
        client: BolnaClient | None,
        config: BolnaSettings | None,
        actor_id: uuid.UUID | None,
        match_by_phone: bool = True,
        create_missing_leads: bool = False,
        summarizer: CallSummarizer | None = None,
    ) -> None:
        self._session = session
        self._workspace = workspace
        self._leads = leads
        self._context = context
        self._client = client
        self._config = config
        self._actor_id = actor_id
        # Phone matching defaults on (an inbound call has nothing else to go
        # on); creating a lead for an unmatched call defaults *off* — a webhook
        # that cannot be matched must never invent a customer record.
        self._match_by_phone = match_by_phone
        self._create_missing_leads = create_missing_leads
        # Bolna's own LLM summary unless a test (or a later CRM-side model)
        # supplies another. See `app.services.voice_postcall`.
        self._summarizer: CallSummarizer = summarizer or VendorCallSummarizer()

    # --- configuration ------------------------------------------------------

    def _require_configured(self) -> tuple[BolnaClient, BolnaSettings]:
        """A deployment with no Bolna credentials fails loudly and usefully.

        422, not 500: "you have not configured this yet" is the caller's
        problem to fix, and a stack trace would suggest it is ours.
        """
        if self._client is None or self._config is None:
            raise api_error(
                422,
                "voice_not_configured",
                "This deployment has no Bolna credentials configured",
            )
        return self._client, self._config

    # --- outbound -----------------------------------------------------------

    async def _fields_and_options(
        self,
    ) -> tuple[list[LeadField], dict[uuid.UUID, list[FieldOption]]]:
        """Field definitions and their options, for rendering (contract §4)."""
        rows = await self._session.execute(
            self._session.select(LeadField).order_by(LeadField.sort_order)
        )
        fields: list[LeadField] = list(rows.scalars().all())

        option_rows = await self._session.execute(self._session.select(FieldOption))
        options: dict[uuid.UUID, list[FieldOption]] = {}
        for option in option_rows.scalars().all():
            options.setdefault(option.field_id, []).append(option)
        return fields, options

    async def build_user_data(
        self, context: VoiceContext, *, idempotency_key: uuid.UUID
    ) -> dict[str, Any]:
        """Assemble the `user_data` Bolna substitutes into the agent's prompt.

        Two namespaces, deliberately separated:

        - **Bare keys** are the workspace's own lead-field keys, rendered for
          speech. Contract §4: "Every key here is a workspace lead-field key.
          There is no fixed list." Only keys the caller's template grants View
          are present, because that is what `get_context` already returned.
        - **`crm_*` keys** are the reserved namespace. The three the contract
          freezes (`crm_lead_id`, `crm_workspace_id`, `crm_idempotency`) plus the
          continuity block this milestone exists for: the previous call's
          summary, when it happened, how many calls there have been, and a few
          recent timeline entries.

        The continuity block has to live in the reserved namespace rather than
        as lead fields, because `voice_call_contexts` is a dedicated table (see
        `app.models.voice`) — it is product structure, not a customer's
        taxonomy, and inventing lead fields for it would put product concepts
        into the customer's own vocabulary.

        Nothing secret can reach this dict: every bare key comes from projected
        `values`, and every reserved key is an id, a timestamp, a count, or text
        a caller previously wrote as a call summary.
        """
        fields, options = await self._fields_and_options()
        rendered = render_for_voice(
            context.values,
            fields,
            timezone_name=self._workspace.timezone,
            currency=self._workspace.currency,
            options_by_field=options,
        )

        user_data: dict[str, Any] = dict(rendered)
        user_data.update(
            {
                # Frozen by contract §4.
                "crm_lead_id": str(context.lead_id),
                "crm_workspace_id": str(self._workspace.id),
                "crm_idempotency": str(idempotency_key),
                # The continuity block — the whole point of the milestone.
                "crm_call_count": str(context.call_count),
                "crm_is_repeat_caller": "yes" if context.call_count else "no",
            }
        )
        if context.last_call_summary:
            user_data["crm_last_call_summary"] = context.last_call_summary
        if context.last_call_at is not None:
            user_data["crm_last_call_at"] = context.last_call_at.isoformat()
        if context.stage_name:
            user_data["crm_stage"] = context.stage_name
        if context.assignee_name:
            user_data["crm_owner"] = context.assignee_name
        recent = [
            entry.get("body")
            for entry in context.recent_interactions[:OUTBOUND_INTERACTION_LIMIT]
            if entry.get("body")
        ]
        if recent:
            user_data["crm_recent_notes"] = " | ".join(str(item) for item in recent)
        return user_data

    async def trigger(self, lead: Lead, *, agent_id: str | None = None) -> TriggerOutcome:
        """Place a Bolna call for an existing lead.

        The shape is `app/events/dispatcher.py`'s, deliberately: **claim, send,
        record**. The `voice_call_executions` row is written and committed
        *before* the HTTP call, so a process that dies mid-request leaves a
        visible QUEUED row the retry worker can pick up, rather than a call that
        may or may not have been placed and no trace either way.

        This does make the outbound request inside a request handler, which is
        the one place this milestone departs from architecture rule 8. Rule 8
        governs the *event* bus — fan-out to endpoints a workspace registered,
        where nobody is waiting on the answer. A call trigger is a command whose
        `execution_id` the caller needs synchronously to correlate the webhook,
        and returning it is an explicit requirement of this milestone. The
        durability rule 8 exists to protect is kept by the committed QUEUED row
        and the retry worker; only the latency is different.
        """
        client, config = self._require_configured()
        resolved_agent = agent_id or config.agent_id
        if not resolved_agent:
            raise api_error(
                422,
                "voice_agent_not_configured",
                "No Bolna agent id configured for this deployment",
            )

        context = await self._context.get_context(lead)
        # Only the phone field, with no fallback to `identity_value`. That
        # fallback used to exist and was actively harmful: a workspace whose
        # identity is Name would hand Bolna a person's name to dial. Refusing
        # is the honest answer — "this lead has no number" is actionable,
        # where a call placed to "Perumal" is a vendor error message nobody
        # can trace back to a CRM configuration choice.
        recipient = context.phone
        if not recipient:
            raise api_error(422, "lead_has_no_phone", "That lead has no phone number")

        idempotency_key = uuid.uuid4()
        user_data = await self.build_user_data(context, idempotency_key=idempotency_key)

        row = VoiceCallExecution(
            lead_id=lead.id,
            recipient_phone=recipient,
            agent_id=resolved_agent,
            idempotency_key=idempotency_key,
            status=VoiceCallStatus.QUEUED,
            context_sent=user_data,
        )
        self._session.add(row)
        await self._session.flush()
        # Committed before the call: see the docstring. A crash after this point
        # leaves a row somebody can act on.
        await self._session.commit()

        result = await client.place_call(
            BolnaCallRequest(
                agent_id=resolved_agent,
                recipient_phone_number=recipient,
                user_data=user_data,
            )
        )

        row.attempts = (row.attempts or 0) + 1
        if result.ok:
            row.external_id = result.execution_id
            row.bolna_status = result.status
            row.status = VoiceCallStatus.DISPATCHED
            row.dispatched_at = dt.datetime.now(dt.UTC)
            row.last_error = None
        else:
            row.status = VoiceCallStatus.FAILED
            row.last_error = result.error
        await self._session.commit()

        return TriggerOutcome(
            execution=row,
            context=context,
            user_data=user_data,
            error=result.error,
        )

    # --- inbound ------------------------------------------------------------

    async def _execution_by_external_id(
        self, external_id: str, *, lock: bool = False
    ) -> VoiceCallExecution | None:
        """The execution row for a Bolna id, optionally row-locked.

        The webhook locks it (`SELECT … FOR UPDATE`). Bolna retries, and two
        deliveries of the same terminal status can arrive together; without the
        lock both would read `completed_at IS NULL` and both would write a call
        log. With it, the second waits, then sees the first one's
        `completed_at` and becomes a duplicate.
        """
        statement = (
            self._session.select(VoiceCallExecution)
            .where(VoiceCallExecution.external_id == external_id)
            .limit(1)
        )
        if lock:
            statement = statement.with_for_update()
        rows = await self._session.execute(statement)
        found: VoiceCallExecution | None = rows.scalar_one_or_none()
        return found

    async def _note(self, lead: Lead, *, body: str) -> None:
        """One AUTOMATION changeset, one timeline entry (rules 5 and 5a)."""
        writer = ActionWriter(self._session, actor_id=self._actor_id)
        await writer.open_changeset(
            source=ChangesetSource.AUTOMATION,
            summary=f"Voice call outcome recorded for {lead.identity_value}",
        )
        writer.record_note(lead, body=body)

    async def _lead_by_phone(self, call: NormalizedCallResult) -> tuple[Lead | None, str | None]:
        """Find the lead this call was with, by its phone number.

        Matches on the workspace's **phone field**, not its identity field. A
        workspace may identify leads by Name, and comparing a phone number
        against names never matches — which, with auto-create on, used to
        create a lead *named* "+91…".

        The number is normalised by the same validator the write path uses, so
        `9876543210` typed by a person and `+919876543210` from Bolna are one
        number under the workspace's own country code (rule 12).

        **Exactly one match or nothing.** Two leads sharing a number is a real
        state for a non-identity field, and picking one would be attaching a
        call to a person by guesswork. Ambiguity is reported as unmatched.

        Both numbers are tried, the likelier one first: the called number on
        an outbound call, the calling number on an inbound one. The other is
        the agent's own line, which matches no lead.

        Returns `(lead, normalised_number)`; either may be `None`.
        """
        phone_key = await self._context.phone_field_key()
        if phone_key is None:
            return None, None

        candidates = [call.recipient_phone, call.caller_phone]
        if call.direction == DIRECTION_INCOMING:
            candidates.reverse()

        first_normalised: str | None = None
        for raw in candidates:
            if not raw:
                continue
            normalised = await self._leads.normalise_field_value(phone_key, raw)
            if normalised is None:
                continue
            first_normalised = first_normalised or normalised
            rows = await self._session.execute(
                self._session.select(Lead)
                .where(Lead.values[phone_key].astext == normalised, Lead.deleted_at.is_(None))
                .limit(2)
            )
            matches: list[Lead] = list(rows.scalars().all())
            if len(matches) == 1:
                return matches[0], normalised
            if len(matches) > 1:
                # Ambiguous: refuse rather than guess, and do not try the
                # other number — that would be picking by elimination.
                return None, None
        return None, first_normalised

    async def _create_lead_for_call(self, phone: str, call: NormalizedCallResult) -> Lead | None:
        """Create the customer this call was with — only when explicitly enabled.

        Off by default (`BOLNA_CREATE_MISSING_LEADS=false`): an unmatched
        webhook must not invent a customer record. When a deployment does turn
        it on, it goes through `LeadService.create_lead`, the one create path.

        Only possible when the phone field *is* the identity. A workspace that
        identifies leads by name cannot have one created from a number alone,
        and writing the number into the name is the bug this replaces.
        """
        phone_key = await self._context.phone_field_key()
        identity_key = await self._leads.identity_key()
        if phone_key is None or phone_key != identity_key:
            return None

        values: dict[str, Any] = {identity_key: phone}
        # A name only if Bolna actually supplied one. Never invented.
        candidate = call.user_data.get("customer_name")
        if candidate:
            values["name"] = str(candidate)[:200]

        lead, _ = await self._leads.create_lead(values=values)
        await self._session.flush()
        return lead

    def _unmatched(self, call: NormalizedCallResult, *, external_id: str) -> Exception:
        """Refuse a delivery that belongs to no lead — loudly, with a reference.

        Never attached to a best guess. The log line carries a reference the
        operator can quote, the execution id, the status and the last four
        digits of the number: enough to find it in Bolna's dashboard, and
        nothing that makes the log itself sensitive.
        """
        reference = uuid.uuid4().hex[:12]
        number = call.recipient_phone or call.caller_phone or ""
        logger.warning(
            "voice.webhook unmatched reference=%s workspace=%s execution=%s status=%s phone=%s",
            reference,
            self._workspace.id,
            external_id,
            call.status or "-",
            f"…{number[-4:]}" if number else "-",
        )
        return api_error(
            422,
            "unknown_execution",
            "No CRM record of that execution, and the payload carries neither a "
            f"crm_lead_id nor a phone number matching exactly one lead (reference {reference})",
            reference=reference,
        )

    async def _lead_for_payload(
        self, call: NormalizedCallResult, *, external_id: str
    ) -> tuple[Lead, VoiceCallExecution]:
        """Resolve the lead this delivery belongs to, and its execution row.

        In descending order of certainty:

        1. **The execution row this CRM created** when it triggered the call —
           the CRM itself recorded which lead this execution is for.
        2. **`crm_lead_id`**, from `user_data` or from Bolna's echo of it in
           `context_details.recipient_data`. Resolved through `LeadService`, so
           only inside the authenticated workspace (contract §5). Covers §7's
           "webhook arrives before the trigger commits".
        3. **The phone field**, when `BOLNA_MATCH_BY_PHONE` is on, and only on
           an unambiguous match. Safe because every request here has already
           presented a valid, revocable workspace API key — a holder of that
           key can already update leads by phone through `/intake/leads`.
        4. **A new lead**, only when `BOLNA_CREATE_MISSING_LEADS` is on.

        Anything else is refused with a logged reference. Never matched by name.
        """
        existing = await self._execution_by_external_id(external_id, lock=True)
        if existing is not None:
            lead = await self._leads.get_lead(existing.lead_id)
            return lead, existing

        lead_from_payload: Lead | None = None
        raw_lead_id = call.crm_lead_id
        if raw_lead_id:
            try:
                lead_id = uuid.UUID(raw_lead_id)
            except ValueError:
                raise api_error(422, "invalid_lead_id", "crm_lead_id is not a valid id") from None
            # `get_lead` is workspace-scoped and 404s for anything outside it,
            # which is what makes a cross-workspace crm_lead_id unusable here.
            lead_from_payload = await self._leads.get_lead(lead_id)

        phone: str | None = None
        if lead_from_payload is None and self._match_by_phone:
            lead_from_payload, phone = await self._lead_by_phone(call)

        if lead_from_payload is None and phone is not None and self._create_missing_leads:
            lead_from_payload = await self._create_lead_for_call(phone, call)

        if lead_from_payload is None:
            raise self._unmatched(call, external_id=external_id)

        row = VoiceCallExecution(
            lead_id=lead_from_payload.id,
            recipient_phone=phone or call.recipient_phone or lead_from_payload.identity_value,
            agent_id=call.agent_id,
            external_id=external_id,
            status=VoiceCallStatus.DISPATCHED,
            context_sent={},
        )
        self._session.add(row)
        await self._session.flush()
        return lead_from_payload, row

    # --- turning an execution into a call log -------------------------------

    async def _disposition_for(
        self, *, status: str, duration: int, succeeded: bool
    ) -> CallDisposition | None:
        """Pick one of the *workspace's own* call dispositions for this call.

        Mirrors the rule the manual log-call form already follows
        (`app/models/pipeline.py`): the workspace's default disposition is the
        connected one, and `connected_call_min_seconds` is what "connected"
        means here. So a Bolna call that reached the customer and lasted long
        enough is dispositioned exactly as a human logging the same call would.

        Anything else looks for the matching system label, and falls through to
        the default if the workspace has archived it. Returning `None` — no live
        disposition at all — is possible and handled by the caller; it must not
        cost us the call record.
        """
        rows = await self._session.execute(
            self._session.select(CallDisposition)
            .where(CallDisposition.is_archived.is_(False))
            .order_by(CallDisposition.sort_order)
        )
        live: list[CallDisposition] = list(rows.scalars().all())
        if not live:
            return None

        default = next((d for d in live if d.is_default), live[0])
        threshold = self._workspace.connected_call_min_seconds
        if succeeded and duration >= threshold:
            return default

        label = STATUS_DISPOSITION_LABELS.get(status)
        if label:
            match = next((d for d in live if d.label == label), None)
            if match is not None:
                return match
        return default

    async def _outcome(
        self, status: str, lead: Lead, row: VoiceCallExecution, external_id: str
    ) -> WebhookOutcome:
        context = await self._context.get_context(lead)
        return WebhookOutcome(
            status=status,
            execution_id=external_id,
            lead_id=lead.id,
            written=False,
            call_count=context.call_count,
            last_call_summary=context.last_call_summary,
            call_summary=row.summary,
            summary_source=row.summary_source,
            call_log_id=row.call_action_id,
            call_id=row.id,
        )

    async def handle_execution(self, payload: VoiceExecutionWebhook) -> WebhookOutcome:
        """Process one Bolna delivery. Safe to call any number of times.

        Three outcomes, and only one of them writes:

        - **`duplicate`** — this execution already completed. Nothing is
          touched; a 200, because a non-2xx would make Bolna retry a delivery
          that has already been fully processed.
        - **`pending`** — a non-terminal status (`queued`, `ringing`,
          `in-progress`). The status and body are recorded; nothing else.
        - **`accepted`** — the first terminal delivery. In one transaction:

          1. the call's transcript, duration and summary are stored on its
             execution row;
          2. an AI summary is produced (Bolna's own, via `CallSummarizer`), or
             the safe fallback if there is none or summarising failed — never
             failing the webhook;
          3. **one** `CALL_LOGGED` action is written, marked `source: AI_CALL`,
             with the summary as its body — the timeline's "AI Call" entry;
          4. the lead's continuity summary (`last_call_summary`) is updated,
             only with a real AI summary — a placeholder would pollute the next
             call's prompt;
          5. extraction write-back runs on a successful call (docs/12), which
             only writes meaningful, changed values.

        The execution row is locked for the duration, so concurrent retries
        serialise and exactly one of them writes.
        """
        call = normalise_execution(
            payload.model_dump(mode="json"),
            summary_disposition=self._config.summary_disposition if self._config else None,
        )
        external_id = call.execution_id
        if not external_id:
            raise api_error(422, "missing_execution_id", "The payload carries no execution_id")

        lead, row = await self._lead_for_payload(call, external_id=external_id)
        now = dt.datetime.now(dt.UTC)

        # The idempotency gate. Everything after the first terminal delivery for
        # this execution is a no-op, however many times Bolna retries.
        if row.completed_at is not None:
            logger.info("voice.webhook duplicate execution=%s lead=%s", external_id, lead.id)
            return await self._outcome("duplicate", lead, row, external_id)

        row.bolna_status = payload.status
        row.webhook_received_at = now
        if call.agent_id and not row.agent_id:
            row.agent_id = call.agent_id
        # Verbatim, on every delivery. The evidence that settles which keys
        # Bolna really sends; never logged, only stored.
        row.raw_payload = payload.model_dump(mode="json")

        if not call.terminal:
            await self._session.commit()
            logger.info(
                "voice.webhook pending execution=%s lead=%s status=%s",
                external_id,
                lead.id,
                call.status or "-",
            )
            return await self._outcome("pending", lead, row, external_id)

        duration = call.duration_seconds or 0
        summary = await summarize_call(call, self._summarizer)
        row.transcript = call.transcript
        row.duration_seconds = duration
        row.summary = summary.text
        row.summary_source = summary.source
        row.summary_error = summary.error

        written = False
        if summary.source == SUMMARY_SOURCE_AI:
            # `external_id` makes this independently idempotent. No NOTE: the
            # call log below carries the summary, and one call is one entry.
            _, written = await self._context.update_last_call_summary(
                lead, summary=summary.text, external_id=external_id, record_note=False
            )

        # The call itself, through `ActionWriter.record_call` — the method the
        # manual log-call form uses — so every report that counts calls counts
        # these too. `extra` marks it as an AI call and links it back.
        disposition = await self._disposition_for(
            status=call.status, duration=duration, succeeded=call.succeeded
        )
        call_action_id: uuid.UUID | None = None
        if disposition is not None:
            writer = ActionWriter(self._session, actor_id=self._actor_id)
            await writer.open_changeset(
                source=ChangesetSource.AUTOMATION,
                summary=f"AI call logged on {lead.identity_value}",
            )
            action = writer.record_call(
                lead,
                direction=call.direction,
                disposition_id=disposition.id,
                duration_seconds=duration,
                notes=summary.text,
                extra={
                    "source": AI_CALL_SOURCE,
                    "execution_id": external_id,
                    "call_id": str(row.id),
                    "call_status": call.status or None,
                    "summary_source": summary.source,
                    "has_transcript": call.transcript is not None,
                },
            )
            await self._session.flush()
            call_action_id = action.id
        else:
            # Every disposition archived. Do not lose the call: record it as a
            # note instead of failing the webhook.
            await self._note(
                lead,
                body=(
                    f"AI call ended (status: {call.status or 'unknown'}, {duration}s). "
                    f"{summary.text}"
                ),
            )
        row.call_action_id = call_action_id

        # Additive extraction write-back (docs/12), only on a terminal success:
        # a failed call may carry stale extracted data. Its own AUTOMATION
        # changeset, so an operator can undo it separately.
        extraction = ExtractionOutcome()
        if call.succeeded:
            extraction_writer = ActionWriter(self._session, actor_id=self._actor_id)
            await extraction_writer.open_changeset(
                source=ChangesetSource.AUTOMATION,
                summary=f"Voice extraction applied on {lead.identity_value}",
            )
            extractor = VoiceExtractionService(
                self._session,
                workspace=self._workspace,
                leads=self._leads,
                actor_id=self._actor_id,
                summary_disposition=(self._config.summary_disposition if self._config else None),
            )
            extraction = await extractor.apply(payload, lead, extraction_writer)

        row.completed_at = now
        row.status = VoiceCallStatus.FAILED if call.failed else VoiceCallStatus.COMPLETED
        await self._session.commit()

        logger.info(
            "voice.webhook accepted execution=%s lead=%s status=%s summary=%s%s",
            external_id,
            lead.id,
            call.status or "-",
            summary.source,
            f" summary_error={summary.error}" if summary.error else "",
        )
        outcome = await self._outcome("accepted", lead, row, external_id)
        outcome.written = written
        outcome.extraction = extraction
        return outcome

    # --- lookups shared with the router -------------------------------------

    async def resolve_lead(self, *, lead_id: uuid.UUID | None, phone: str | None) -> Lead:
        """One lead, by CRM id or by phone. Never creates one.

        Both paths go through services that already exist — `LeadService` for
        the id, `VoiceContextService` for the number — so identity, phone
        normalisation (rule 12) and visibility behave exactly as they do
        everywhere else in the product.
        """
        if lead_id is not None:
            return await self._leads.get_lead(lead_id)
        if phone is not None:
            return await self._context.find_lead_by_phone(phone)
        raise not_found("Lead")  # pragma: no cover - schema enforces one of them
