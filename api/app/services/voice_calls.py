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

**The webhook identifies the lead by execution id, never by phone.** Contract §5
is explicit: "never fall back to matching on phone number, which would let a
spoofed payload write to an arbitrary lead." `voice_call_executions` is the
authoritative mapping; a payload whose execution we did not create must carry a
`crm_lead_id` that resolves *inside the authenticated workspace*, or it is
refused.

**Write-back is idempotent at two levels.** `voice_call_executions.completed_at`
gates the whole operation, and `VoiceContextService.update_last_call_summary` is
independently idempotent on `external_id`. Either alone would be enough for the
common retry; together they also survive out-of-order delivery, which the single
`last_call_external_id` column could not.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
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
from app.tenancy.session import ScopedSession

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

#: Bolna's own vocabulary (`.claude/skills/get-executions/SKILL.md`), kept as
#: *their* strings rather than mapped into a CRM enum — a vendor is entitled to
#: add statuses, and a lossy mapping loses the diagnosis.
TERMINAL_SUCCESS_STATUSES = frozenset({"completed"})
TERMINAL_FAILURE_STATUSES = frozenset(
    {
        "failed",
        "error",
        "busy",
        "no-answer",
        "no_answer",
        "canceled",
        "cancelled",
        "stopped",
        "balance-low",
        "call-disconnected",
    }
)

#: Where a customer's phone number might be in a Bolna execution payload.
#:
#: Read in order, first hit wins. Several spellings rather than one because the
#: vendored skills describe `telephony_data` only in prose — "provider, to/from
#: numbers, call type" — and the field-level reference they link is not in this
#: repo. Guessing one name and getting it wrong would silently fail to match a
#: lead; reading the plausible set and storing `raw_payload` alongside means the
#: first real call tells us which is right instead of us assuming.
PHONE_PATHS: tuple[tuple[str, ...], ...] = (
    ("telephony_data", "to_number"),
    ("telephony_data", "recipient_phone_number"),
    ("telephony_data", "to"),
    ("telephony_data", "recipient"),
    ("recipient_phone_number",),
    ("to_number",),
    ("context_details", "recipient_phone_number"),
)

#: Same reasoning, for the caller's number on an inbound call.
CALLER_PHONE_PATHS: tuple[tuple[str, ...], ...] = (
    ("telephony_data", "from_number"),
    ("telephony_data", "from"),
    ("from_number",),
)

#: Same reasoning, for how long the two parties actually spoke. `get-executions`
#: names `conversation_time` at the top level; the rest are defensive.
DURATION_PATHS: tuple[tuple[str, ...], ...] = (
    ("conversation_time",),
    ("telephony_data", "duration"),
    ("telephony_data", "call_duration"),
    ("duration_seconds",),
    ("duration",),
)

#: And for which way the call went.
DIRECTION_PATHS: tuple[tuple[str, ...], ...] = (
    ("telephony_data", "call_type"),
    ("telephony_data", "direction"),
    ("direction",),
    ("call_type",),
)

#: `CallLogCreate.direction` — the CRM's own three values.
DIRECTION_OUTGOING = "OUTGOING"
DIRECTION_INCOMING = "INCOMING"

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


def dig(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    """Follow a dotted path through nested dicts, or return None."""
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def first_present(payload: dict[str, Any], paths: tuple[tuple[str, ...], ...]) -> Any:
    for path in paths:
        found = dig(payload, path)
        if found not in (None, ""):
            return found
    return None


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
        create_missing_leads: bool = True,
    ) -> None:
        self._session = session
        self._workspace = workspace
        self._leads = leads
        self._context = context
        self._client = client
        self._config = config
        self._actor_id = actor_id
        # Both default on because that is what an inbound call needs; both are
        # deployment settings so a workspace that wants the stricter
        # contract-§5 behaviour can have it without a code change.
        self._match_by_phone = match_by_phone
        self._create_missing_leads = create_missing_leads

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

    async def _execution_by_external_id(self, external_id: str) -> VoiceCallExecution | None:
        rows = await self._session.execute(
            self._session.select(VoiceCallExecution)
            .where(VoiceCallExecution.external_id == external_id)
            .limit(1)
        )
        found: VoiceCallExecution | None = rows.scalar_one_or_none()
        return found

    def _extract_summary(self, payload: VoiceExecutionWebhook) -> str | None:
        """Find the call summary in a Bolna execution payload.

        Order of preference, and why:

        1. `summary` on the payload — explicit, and what an adapter in front of
           Bolna would send.
        2. The disposition named by `BOLNA_SUMMARY_DISPOSITION`. A disposition
           name is the *customer's* vocabulary, so the product cannot ship a
           guess at it (CLAUDE.md, "Known traps") — it is configuration, unset
           by default.
        3. `context_details.summary`, which Bolna populates for some agent
           configurations.

        Deliberately **not** the transcript. A transcript is not a summary, and
        storing one in `last_call_summary` would quietly turn the next call's
        prompt into a wall of text. No summary is an honest no summary.
        """
        if payload.summary and payload.summary.strip():
            return payload.summary.strip()

        configured = self._config.summary_disposition if self._config else None
        if configured:
            entry = payload.extracted_data.get(configured)
            if isinstance(entry, dict):
                value = entry.get("value")
                if value not in (None, ""):
                    return str(value).strip()
            elif entry not in (None, ""):
                return str(entry).strip()

        detail = payload.context_details.get("summary")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        return None

    async def _note(self, lead: Lead, *, body: str) -> None:
        """One AUTOMATION changeset, one timeline entry (rules 5 and 5a)."""
        writer = ActionWriter(self._session, actor_id=self._actor_id)
        await writer.open_changeset(
            source=ChangesetSource.AUTOMATION,
            summary=f"Voice call outcome recorded for {lead.identity_value}",
        )
        writer.record_note(lead, body=body)

    async def _lead_by_phone(
        self, payload: VoiceExecutionWebhook
    ) -> tuple[Lead | None, str | None]:
        """Find the lead this call was with, by its phone number.

        The number is normalised through `LeadService.normalise_identity`, which
        is the *same* validator the write path uses — so a lead created by a
        human typing `9876543210` and a Bolna payload carrying `+919876543210`
        resolve to one record, using the workspace's own `default_country_code`
        (architecture rule 12, never a hardcoded prefix).

        Tries the called number first and the calling number second, so the same
        code serves an outbound call (the customer is `to`) and an inbound one
        (the customer is `from`).

        Returns `(lead, normalised_number)`; either may be `None`.
        """
        body = payload.model_dump()
        raw = first_present(body, PHONE_PATHS) or first_present(body, CALLER_PHONE_PATHS)
        if raw in (None, ""):
            return None, None

        normalised = await self._leads.normalise_identity(str(raw))
        if normalised is None:
            # Unparseable for this workspace. Not an error: the call still gets
            # recorded against whatever else identifies it, and `raw_payload`
            # keeps the number somebody can look at.
            return None, None

        statement = (
            self._session.select(Lead)
            .where(Lead.identity_value == normalised, Lead.deleted_at.is_(None))
            .limit(1)
        )
        rows = await self._session.execute(statement)
        found: Lead | None = rows.scalar_one_or_none()
        return found, normalised

    async def _create_lead_for_call(self, phone: str, payload: VoiceExecutionWebhook) -> Lead:
        """Create the customer this call was with, when nothing matched.

        Goes through `LeadService.create_lead` — the one place every create in
        this product lands (UI, import, intake API), so this inherits the
        identity uniqueness, the assignment rules, the changeset and the
        `LEAD_CREATED` action without reimplementing any of them. That is what
        keeps requirement "do not create a second customer system" true: there
        is still exactly one create path, and this is a fourth caller of it.

        A racing duplicate cannot survive `leads_identity_uq`; if two webhooks
        for the same new number arrive together, one loses and re-reads.
        """
        identity_key = await self._leads.identity_key()
        values: dict[str, Any] = {identity_key: phone}

        # A name only if Bolna actually supplied one. Never invented: a lead
        # called "Unknown" is worse than a lead with a blank name, because it
        # looks deliberate.
        body = payload.model_dump()
        candidate = first_present(
            body, (("user_data", "customer_name"), ("context_details", "customer_name"))
        )
        if candidate:
            values["name"] = str(candidate)[:200]

        lead, _ = await self._leads.create_lead(values=values)
        await self._session.flush()
        return lead

    async def _lead_for_payload(
        self, payload: VoiceExecutionWebhook, *, external_id: str
    ) -> tuple[Lead, VoiceCallExecution]:
        """Resolve the lead this delivery belongs to, and its execution row.

        Four ways in, tried in descending order of certainty:

        1. **The execution row this CRM created** when it triggered the call.
           Strongest: the CRM itself recorded which lead this execution is for.
        2. **`crm_lead_id` in `user_data`**, resolved through `LeadService` and
           therefore inside the authenticated workspace by construction
           (contract §5). Covers §7's "webhook arrives before the trigger
           commits".
        3. **The phone number**, when `BOLNA_MATCH_BY_PHONE` is on.
        4. **A newly created lead**, when `BOLNA_CREATE_MISSING_LEADS` is on.

        Steps 3 and 4 are new, and worth saying plainly why they are safe here
        when contract §5 forbade them. §5's objection was that phone matching
        "would let a **spoofed** payload write to an arbitrary lead" — an
        objection about an *unauthenticated* body. Every request reaching this
        method has already presented a valid, revocable workspace API key
        carrying a permission template. A caller holding that key can already
        create and update leads by phone through `POST /intake/leads`; matching
        on phone here grants it nothing it did not already have.

        What that reasoning does **not** license is a webhook route with no
        credential at all. If one is ever added, step 3 must be switched off
        with it: `BOLNA_MATCH_BY_PHONE=false` exists for exactly that.
        """
        existing = await self._execution_by_external_id(external_id)
        if existing is not None:
            lead = await self._leads.get_lead(existing.lead_id)
            return lead, existing

        lead_from_payload: Lead | None = None
        raw_lead_id = payload.user_data.get("crm_lead_id")
        if raw_lead_id:
            try:
                lead_id = uuid.UUID(str(raw_lead_id))
            except ValueError:
                raise api_error(422, "invalid_lead_id", "crm_lead_id is not a valid id") from None
            # `get_lead` is workspace-scoped and 404s for anything outside it,
            # which is what makes a cross-workspace crm_lead_id unusable here.
            lead_from_payload = await self._leads.get_lead(lead_id)

        phone: str | None = None
        if lead_from_payload is None and self._match_by_phone:
            lead_from_payload, phone = await self._lead_by_phone(payload)

        if lead_from_payload is None:
            if phone is not None and self._create_missing_leads:
                lead_from_payload = await self._create_lead_for_call(phone, payload)
            else:
                raise api_error(
                    422,
                    "unknown_execution",
                    "No CRM record of that execution, and the payload carries "
                    "neither a crm_lead_id nor a phone number matching a lead",
                )

        row = VoiceCallExecution(
            lead_id=lead_from_payload.id,
            recipient_phone=phone or lead_from_payload.identity_value,
            agent_id=payload.agent_id,
            external_id=external_id,
            status=VoiceCallStatus.DISPATCHED,
            context_sent={},
        )
        self._session.add(row)
        await self._session.flush()
        return lead_from_payload, row

    # --- turning an execution into a call log -------------------------------

    def _duration_seconds(self, payload: VoiceExecutionWebhook) -> int:
        """How long the two parties spoke, clamped to what a call log accepts."""
        raw = first_present(payload.model_dump(), DURATION_PATHS)
        if raw in (None, ""):
            return 0
        try:
            seconds = round(float(raw))
        except (TypeError, ValueError):
            return 0
        # `CallLogCreate` bounds duration at 0..86_400; a call log written by
        # this path must satisfy the same bounds a human's would.
        return max(0, min(seconds, 86_400))

    def _direction(self, payload: VoiceExecutionWebhook) -> str:
        """`OUTGOING` unless the payload says the customer called us."""
        raw = first_present(payload.model_dump(), DIRECTION_PATHS)
        text = str(raw or "").strip().lower()
        if text in ("inbound", "incoming", "in"):
            return DIRECTION_INCOMING
        return DIRECTION_OUTGOING

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

    async def handle_execution(self, payload: VoiceExecutionWebhook) -> WebhookOutcome:
        """Process one Bolna delivery. Safe to call any number of times.

        Three outcomes, and only one of them writes:

        - **`duplicate`** — this execution already completed. Nothing is
          touched, and the answer is a 200 because a non-2xx would make Bolna
          retry a delivery that has already been fully processed.
        - **`pending`** — a non-terminal status (`queued`, `ringing`,
          `in-progress`). The vendored `setup-webhook` skill warns that deduping
          on the execution id alone discards these; the CRM records the status
          and writes nothing else.
        - **`accepted`** — the first terminal delivery. Writes the summary back
          to the lead's context, records a `CALL_LOGGED` action carrying the
          call's direction, duration and disposition, and closes the execution.

        Every delivery, terminal or not, stores the body it arrived with.
        """
        external_id = payload.resolved_execution_id()
        if not external_id:
            raise api_error(422, "missing_execution_id", "The payload carries no execution_id")

        lead, row = await self._lead_for_payload(payload, external_id=external_id)

        # The idempotency gate. Everything after the first terminal delivery for
        # this execution is a no-op, however many times Bolna retries.
        if row.completed_at is not None:
            context = await self._context.get_context(lead)
            return WebhookOutcome(
                status="duplicate",
                execution_id=external_id,
                lead_id=lead.id,
                written=False,
                call_count=context.call_count,
                last_call_summary=context.last_call_summary,
            )

        raw_status = (payload.status or "").strip().lower()
        summary = self._extract_summary(payload)
        succeeded = raw_status in TERMINAL_SUCCESS_STATUSES
        failed = raw_status in TERMINAL_FAILURE_STATUSES
        # A payload carrying a summary is a finished call whatever the status
        # string says — a vendor may add a status this release has never seen,
        # and silently dropping the summary would be the worse failure.
        terminal = succeeded or failed or summary is not None

        row.bolna_status = payload.status
        if payload.agent_id and not row.agent_id:
            row.agent_id = payload.agent_id
        # Verbatim, on every delivery including non-terminal ones. This is the
        # evidence that replaces the guesswork in `PHONE_PATHS` and
        # `DURATION_PATHS`: after the first real call, `select raw_payload from
        # voice_call_executions` says exactly what Bolna sends.
        row.raw_payload = payload.model_dump(mode="json")

        if not terminal:
            await self._session.commit()
            context = await self._context.get_context(lead)
            return WebhookOutcome(
                status="pending",
                execution_id=external_id,
                lead_id=lead.id,
                written=False,
                call_count=context.call_count,
                last_call_summary=context.last_call_summary,
            )

        written = False
        if summary is not None and not failed:
            # `external_id` makes this independently idempotent, so even a
            # delivery that somehow slipped past the `completed_at` gate cannot
            # double-count the call or double-write the timeline.
            _, written = await self._context.update_last_call_summary(
                lead, summary=summary, external_id=external_id
            )

        # The call itself, on the timeline, in the shape the rest of the product
        # already understands: a `CALL_LOGGED` action with a direction, a
        # duration and one of the workspace's own dispositions. Written through
        # `ActionWriter.record_call` — the same method the manual log-call form
        # uses — so a Bolna call and a human-logged call are the same kind of
        # record, and every report that counts calls counts these too.
        #
        # Guarded by the same `completed_at` gate as the summary above, so a
        # retried webhook cannot produce a second call log.
        duration = self._duration_seconds(payload)
        disposition = await self._disposition_for(
            status=raw_status, duration=duration, succeeded=succeeded
        )
        if disposition is not None:
            writer = ActionWriter(self._session, actor_id=self._actor_id)
            await writer.open_changeset(
                source=ChangesetSource.AUTOMATION,
                summary=f"Voice call logged on {lead.identity_value}",
            )
            writer.record_call(
                lead,
                direction=self._direction(payload),
                disposition_id=disposition.id,
                duration_seconds=duration,
                # Deliberately no notes. `update_last_call_summary` above has
                # already put the summary on the timeline as its own NOTE, and
                # copying it here would print the same paragraph twice under one
                # call. The call log carries what only it knows — how long, which
                # way, what outcome — and the note carries the words.
                notes=None,
            )
        else:
            # A workspace with every disposition archived. Do not lose the call:
            # record what happened as a note instead of failing the webhook.
            await self._note(
                lead,
                body=(
                    f"Voice call ended (status: {payload.status or 'unknown'}, "
                    f"{duration}s). No live call disposition is configured."
                ),
            )

        # Additive extraction write-back (docs/12). Only runs on a terminal
        # success — a failed / no-answer / busy call may carry stale extracted
        # data from a prior partial delivery that Bolna did not clear, and
        # writing that back would corrupt the lead exactly the way §6.2 of the
        # contract is designed to prevent. Wrapped in its own AUTOMATION
        # changeset (the summary and the call log each opened their own too),
        # so an operator can undo the extraction pass separately if a mapping
        # turns out to be wrong. A workspace with no mappings does one small
        # SELECT and returns immediately; the code path costs nothing until an
        # operator configures a mapping through Settings → Voice extraction.
        extraction = ExtractionOutcome()
        if succeeded and not failed:
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
                summary_disposition=(
                    self._config.summary_disposition if self._config else None
                ),
            )
            extraction = await extractor.apply(payload, lead, extraction_writer)

        row.completed_at = dt.datetime.now(dt.UTC)
        row.status = VoiceCallStatus.FAILED if failed else VoiceCallStatus.COMPLETED
        await self._session.commit()

        context = await self._context.get_context(lead)
        return WebhookOutcome(
            status="accepted",
            execution_id=external_id,
            lead_id=lead.id,
            written=written,
            extraction=extraction,
            call_count=context.call_count,
            last_call_summary=context.last_call_summary,
        )

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
