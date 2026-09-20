"""Voice-agent endpoints — the CRM side of the Bolna integration.

`docs/06-voice-integration-contract.md`,
`docs/09-context-continuity-and-bolna-integration.md`.

Two routers, because they have two different callers and therefore two
different authentication models:

- **`router`** is the workspace-scoped, member-authenticated surface: read a
  lead's voice context, record a summary against it, and place a call. It hangs
  off `require_workspace` like every other tenant endpoint and gates on the
  *existing* `calling` capability group (`view_call_history`, `log_calls`)
  rather than inventing a new one — those two flags already exist for exactly
  this kind of access, and both the Caller and Manager default templates grant
  them.

- **`executions_router`** is the machine surface: the completion webhook Bolna
  posts to. It authenticates with an API key, not a session, because the caller
  is a service and has no member. That key carries a permission template, so the
  write-back it drives is filtered through the same field matrix a person's
  would be (contract §3) — the machine path is not a hole in the permission
  model.

The path for both is the one contract §5 froze:
`/api/v1/workspaces/{workspace_id}/voice/...`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.api_keys import ApiKeyScope, require_api_key, resolve_api_key
from app.dependencies import get_session
from app.errors import api_error
from app.integrations.bolna import (
    BolnaClient,
    BolnaSettings,
    HttpxBolnaClient,
    bolna_settings_from,
)
from app.models.field import LeadField
from app.permissions import FieldProjectionService, FieldWriteFilter, load_grants
from app.schemas.common import Page, PageParams, page_params
from app.schemas.voice import (
    ExtractionRead,
    VoiceCallDetailRead,
    VoiceCallLeadRef,
    VoiceCallSummaryRead,
    VoiceCallTrigger,
    VoiceCallTriggerResult,
    VoiceContextRead,
    VoiceExecutionResult,
    VoiceExecutionWebhook,
    VoiceInteraction,
    VoiceSummaryUpdate,
    VoiceSummaryUpdateResult,
)
from app.services.leads import LeadService
from app.services.voice_calls import VoiceCallService
from app.services.voice_context import VoiceContext, VoiceContextService
from app.services.voice_history import CallWithLead, VoiceCallHistoryService
from app.services.voice_postcall import CallSummarizer, extractions_from_payload
from app.tenancy.scoping import WorkspaceScope, require_workspace

router = APIRouter(tags=["voice"])

#: Mounted separately in `app.main` under the same tenant prefix. A second
#: router object in one module is the shape `routers/pipeline.py` already uses
#: for its custom-actions surface.
executions_router = APIRouter(tags=["voice"])

#: The route Bolna itself posts to. Unscoped by path — the workspace comes from
#: the key, exactly as `/api/v1/intake/*` already works — because a webhook URL
#: pasted into a vendor dashboard should carry one secret, not a secret plus a
#: workspace id somebody could edit.
bolna_router = APIRouter(tags=["voice"])


def _require(scope: WorkspaceScope, capability: str) -> None:
    if not scope.capability("calling", capability):
        raise api_error(
            403,
            "insufficient_permissions",
            f"This permission template does not allow: {capability.replace('_', ' ')}",
        )


async def _lead_service(
    scope: Annotated[WorkspaceScope, Depends(require_workspace)],
) -> LeadService:
    """Same construction `routers/leads.py` uses — the caller's projection
    and write filter are already bound before the service exists."""
    return LeadService(
        scope.session,
        workspace=scope.workspace,
        projection=await scope.projection(),
        write_filter=await scope.write_filter(),
        actor_id=scope.membership_id,
        visible_membership_ids=scope.visible_membership_ids,
        sees_all=scope.sees_all_members,
    )


async def _voice_service(
    scope: Annotated[WorkspaceScope, Depends(require_workspace)],
    leads: Annotated[LeadService, Depends(_lead_service)],
) -> VoiceContextService:
    return VoiceContextService(
        scope.session,
        workspace=scope.workspace,
        leads=leads,
        visible_membership_ids=scope.visible_membership_ids,
        sees_all=scope.sees_all_members,
        actor_id=scope.membership_id,
    )


# --- the Bolna seam --------------------------------------------------------


def bolna_config(request: Request) -> BolnaSettings | None:
    """Resolved Bolna configuration, or `None` when this deployment has none.

    `app.state.bolna_settings` wins when present, which is how the test suite
    supplies a configuration without an environment — the same override shape
    `app.state.email_sender` already uses for SMTP.
    """
    override = getattr(request.app.state, "bolna_settings", None)
    if isinstance(override, BolnaSettings):
        return override
    return bolna_settings_from(request.app.state.settings)


def bolna_client(request: Request) -> BolnaClient | None:
    """The client, or `None` when unconfigured.

    `app.state.bolna_client` wins when present. That is the seam the whole test
    suite runs through: `RecordingBolnaClient` captures the exact `user_data`
    that would have gone to Bolna, so the integration is verified end to end
    without a network call or a paid minute.
    """
    override = getattr(request.app.state, "bolna_client", None)
    if override is not None:
        client: BolnaClient = override
        return client
    config = bolna_config(request)
    if config is None:
        return None
    return HttpxBolnaClient(config)


def call_summarizer(request: Request) -> CallSummarizer | None:
    """`app.state.call_summarizer` when a test installs one; else the default.

    `None` means `VoiceCallService` uses `VendorCallSummarizer` — Bolna's own
    LLM summary. Same override seam as `bolna_client`.
    """
    override = getattr(request.app.state, "call_summarizer", None)
    if override is not None:
        summarizer: CallSummarizer = override
        return summarizer
    return None


async def _voice_call_service(
    request: Request,
    scope: Annotated[WorkspaceScope, Depends(require_workspace)],
    leads: Annotated[LeadService, Depends(_lead_service)],
    voice: Annotated[VoiceContextService, Depends(_voice_service)],
) -> VoiceCallService:
    return VoiceCallService(
        scope.session,
        workspace=scope.workspace,
        leads=leads,
        context=voice,
        client=bolna_client(request),
        config=bolna_config(request),
        actor_id=scope.membership_id,
        match_by_phone=request.app.state.settings.bolna_match_by_phone,
        create_missing_leads=request.app.state.settings.bolna_create_missing_leads,
    )


def _to_read(context: VoiceContext) -> VoiceContextRead:
    return VoiceContextRead(
        lead_id=context.lead_id,
        identity_value=context.identity_value,
        name=context.name,
        phone=context.phone,
        email=context.email,
        stage_id=context.stage_id,
        stage_name=context.stage_name,
        assignee_id=context.assignee_id,
        assignee_name=context.assignee_name,
        values=context.values,
        last_call_summary=context.last_call_summary,
        last_call_at=context.last_call_at,
        call_count=context.call_count,
        recent_interactions=[
            VoiceInteraction(
                kind=item["kind"], body=item.get("body"), performed_at=item["performed_at"]
            )
            for item in context.recent_interactions
        ],
        created_at=context.created_at,
        last_action_at=context.last_action_at,
    )


# --- context (Phase 1) -----------------------------------------------------


@router.get(
    "/voice/context/{lead_id}",
    response_model=VoiceContextRead,
    summary="Full voice-agent context for one lead, by CRM lead id",
)
async def get_voice_context_by_id(
    lead_id: uuid.UUID,
    scope: Annotated[WorkspaceScope, Depends(require_workspace)],
    leads: Annotated[LeadService, Depends(_lead_service)],
    voice: Annotated[VoiceContextService, Depends(_voice_service)],
) -> VoiceContextRead:
    _require(scope, "view_call_history")
    lead = await leads.get_lead(lead_id)
    return _to_read(await voice.get_context(lead))


@router.get(
    "/voice/context",
    response_model=VoiceContextRead,
    summary="Full voice-agent context for one lead, by phone number",
)
async def get_voice_context_by_phone(
    scope: Annotated[WorkspaceScope, Depends(require_workspace)],
    voice: Annotated[VoiceContextService, Depends(_voice_service)],
    phone: Annotated[str, Query(min_length=1, max_length=32)],
) -> VoiceContextRead:
    """The lookup a Bolna trigger actually has on hand — an inbound caller's
    number — before it knows any CRM id."""
    _require(scope, "view_call_history")
    lead = await voice.find_lead_by_phone(phone)
    return _to_read(await voice.get_context(lead))


@router.put(
    "/voice/context/{lead_id}/summary",
    response_model=VoiceSummaryUpdateResult,
    summary="Record the summary of the latest call (idempotent on external_id)",
)
async def update_voice_summary(
    lead_id: uuid.UUID,
    payload: VoiceSummaryUpdate,
    scope: Annotated[WorkspaceScope, Depends(require_workspace)],
    leads: Annotated[LeadService, Depends(_lead_service)],
    voice: Annotated[VoiceContextService, Depends(_voice_service)],
) -> VoiceSummaryUpdateResult:
    _require(scope, "log_calls")
    lead = await leads.get_lead(lead_id)
    _, written = await voice.update_last_call_summary(
        lead, summary=payload.summary, external_id=payload.external_id
    )
    await scope.session.commit()
    # Re-read after commit so the response reflects exactly what is now
    # durable, not just what this request thinks it wrote.
    context = await voice.get_context(lead)
    return VoiceSummaryUpdateResult(context=_to_read(context), written=written)


# --- outbound trigger (Phase 2) --------------------------------------------


@router.post(
    "/voice/calls",
    response_model=VoiceCallTriggerResult,
    summary="Place a Bolna call to an existing lead, carrying its CRM context",
)
async def trigger_voice_call(
    payload: VoiceCallTrigger,
    scope: Annotated[WorkspaceScope, Depends(require_workspace)],
    calls: Annotated[VoiceCallService, Depends(_voice_call_service)],
) -> VoiceCallTriggerResult:
    """Resolve the lead, load its context, and hand Bolna the call.

    Gated on `calling.log_calls` — the existing flag that governs writing call
    activity against a lead, which is what this does — rather than a new
    capability. Adding one would mean editing the five default permission
    templates, and this milestone is not allowed to change the permission model
    for everybody in order to ship a feature for one integration.

    The lead is resolved, never created. An unknown number is a 404: a voice
    trigger that invented a customer record is how a CRM ends up holding two of
    the same person.
    """
    _require(scope, "log_calls")
    lead = await calls.resolve_lead(lead_id=payload.lead_id, phone=payload.phone)
    outcome = await calls.trigger(lead, agent_id=payload.agent_id)
    row = outcome.execution
    return VoiceCallTriggerResult(
        call_id=row.id,
        lead_id=row.lead_id,
        execution_id=row.external_id,
        status=row.status.value,
        bolna_status=row.bolna_status,
        agent_id=row.agent_id,
        recipient_phone=row.recipient_phone,
        user_data=outcome.user_data,
        # What this call was told about the previous one. The single most
        # useful line in the response for anybody verifying continuity.
        previous_call_summary=outcome.context.last_call_summary,
        call_count=outcome.context.call_count,
        error=outcome.error,
    )


# --- reading past calls (docs/13 §6) ---------------------------------------


async def _history_service(
    scope: Annotated[WorkspaceScope, Depends(require_workspace)],
    leads: Annotated[LeadService, Depends(_lead_service)],
) -> VoiceCallHistoryService:
    return VoiceCallHistoryService(
        scope.session,
        workspace=scope.workspace,
        leads=leads,
        visible_membership_ids=scope.visible_membership_ids,
        sees_all=scope.sees_all_members,
    )


def _lead_ref(entry: CallWithLead) -> VoiceCallLeadRef:
    return VoiceCallLeadRef(
        lead_id=entry.lead.id,
        identity_value=entry.lead.identity_value,
        primary_h1=entry.primary_h1,
        primary_h2=entry.primary_h2,
        primary_h1_label=entry.primary_h1_label,
        primary_h2_label=entry.primary_h2_label,
    )


def _call_summary(entry: CallWithLead) -> VoiceCallSummaryRead:
    call = entry.call
    return VoiceCallSummaryRead(
        id=call.id,
        lead=_lead_ref(entry),
        execution_id=call.external_id,
        status=call.status.value,
        bolna_status=call.bolna_status,
        duration_seconds=call.duration_seconds,
        summary=call.summary,
        summary_source=call.summary_source,
        agent_id=call.agent_id,
        created_at=call.created_at,
        dispatched_at=call.dispatched_at,
        completed_at=call.completed_at,
    )


@router.get(
    "/voice/calls",
    response_model=Page[VoiceCallSummaryRead],
    summary="AI calls, newest first — the whole workspace's or one lead's",
)
async def list_voice_calls(
    scope: Annotated[WorkspaceScope, Depends(require_workspace)],
    history: Annotated[VoiceCallHistoryService, Depends(_history_service)],
    params: Annotated[PageParams, Depends(page_params)],
    lead_id: Annotated[uuid.UUID | None, Query()] = None,
    completed_only: Annotated[bool, Query()] = False,
) -> Page[VoiceCallSummaryRead]:
    """The call list, and the lead panel's "most recent completed call".

    Gated on the existing `calling.view_call_history` — the flag that already
    governs seeing a lead's call activity, which is exactly what this is.
    `lead_id` plus `completed_only=true&limit=1` is how the lead card asks for
    its one call; no separate endpoint exists for it, because this is the same
    question with a narrower filter.
    """
    _require(scope, "view_call_history")
    entries, total = await history.list_calls(
        lead_id=lead_id,
        completed_only=completed_only,
        limit=params.limit,
        offset=params.offset,
    )
    return Page(
        items=[_call_summary(entry) for entry in entries],
        total=total,
        limit=params.limit,
        offset=params.offset,
    )


@router.get(
    "/voice/calls/{call_id}",
    response_model=VoiceCallDetailRead,
    summary="One AI call in full: transcript, extracted data, sanitised payload",
)
async def get_voice_call(
    call_id: uuid.UUID,
    request: Request,
    scope: Annotated[WorkspaceScope, Depends(require_workspace)],
    history: Annotated[VoiceCallHistoryService, Depends(_history_service)],
) -> VoiceCallDetailRead:
    """Everything stored about one call.

    The raw provider payload is sanitised on the way out — redacted by key
    name, plus an exact-match pass for the deployment's own Bolna credential in
    case an upstream ever echoed it back. The stored row is untouched: this is
    a read, and nothing in the API writes `raw_payload`.
    """
    _require(scope, "view_call_history")
    entry = await history.get_call(call_id)
    call = entry.call
    config = bolna_config(request)
    base = _call_summary(entry)
    # Sanitise once, then read the extractions out of the *sanitised* body —
    # so anything redacted there is redacted here too, and the two sections
    # can never disagree about what this call produced.
    payload = history.sanitised_payload(call, secret=config.api_key if config else None)
    extracted, entries = extractions_from_payload(payload)
    return VoiceCallDetailRead(
        **base.model_dump(),
        recipient_phone=call.recipient_phone,
        transcript=call.transcript,
        extracted_data=extracted,
        extractions=[
            ExtractionRead(
                group=item.group,
                name=item.name,
                path=item.path,
                value=item.value,
                confidence=item.confidence,
            )
            for item in entries
        ],
        raw_payload=payload,
        webhook_received_at=call.webhook_received_at,
        call_log_id=call.call_action_id,
        summary_error=call.summary_error,
        last_error=call.last_error,
    )


# --- inbound completion webhook (Phase 2) ----------------------------------


async def _api_key_lead_service(scope: ApiKeyScope) -> LeadService:
    """A `LeadService` whose chokepoints are the *key's* permission template.

    Lifted from `app.events.intake.IntakeService._lead_service` on purpose: the
    machine path must build its services exactly the way the other machine path
    does, or the two would drift and one of them would end up as an admin
    bypass. A key that cannot Edit a field is refused that field by name, just
    as a person would be.
    """
    rows = await scope.session.execute(
        scope.session.select(LeadField).order_by(LeadField.sort_order)
    )
    fields = list(rows.scalars().all())
    grants = await load_grants(
        scope.session,
        template_id=scope.template.id,
        is_admin=scope.is_admin,
        all_field_keys={f.id: f.key for f in fields},
    )
    return LeadService(
        scope.session,
        workspace=scope.workspace,
        projection=FieldProjectionService(grants),
        write_filter=FieldWriteFilter(grants),
        # No membership, so no actor: the timeline records the change without
        # attributing it to a person who did not make it.
        actor_id=None,
        visible_membership_ids=frozenset(),
        sees_all=True,
    )


@executions_router.post(
    "/voice/executions",
    response_model=VoiceExecutionResult,
    summary="Bolna call-completion webhook (idempotent on execution_id)",
)
async def receive_voice_execution(
    workspace_id: uuid.UUID,
    payload: VoiceExecutionWebhook,
    request: Request,
    scope: Annotated[ApiKeyScope, Depends(require_api_key)],
) -> VoiceExecutionResult:
    """Fold a finished Bolna call back onto the lead it belongs to.

    Authenticated by API key (`X-API-Key`), which is what the CRM already has
    for machine callers — contract §5 wrote this as a bearer token, but the
    mechanism M10 actually shipped is the header, and inventing a second
    machine-auth scheme for one integration would be worse than the wording
    drift.

    The workspace in the path must match the key's own. The key is the
    authority; the path is checked so a misconfigured webhook URL fails loudly
    instead of writing somewhere surprising.

    Always a 200 for a delivery it understands, including a duplicate. Bolna
    retries on non-2xx, and retrying a delivery that has already been processed
    in full helps nobody.
    """
    if workspace_id != scope.workspace_id:
        # 404 rather than 403: a 403 would confirm the workspace exists.
        raise api_error(404, "not_found", "Workspace not found")

    return await _process_execution(scope, payload, request)


async def _process_execution(
    scope: ApiKeyScope, payload: VoiceExecutionWebhook, request: Request
) -> VoiceExecutionResult:
    """One implementation, whichever way the key arrived.

    Both webhook routes land here, so the header route and the URL route cannot
    drift into behaving differently — which is the failure mode that turns a
    second entry point into a second, weaker security boundary.
    """
    leads = await _api_key_lead_service(scope)
    context = VoiceContextService(
        scope.session,
        workspace=scope.workspace,
        leads=leads,
        visible_membership_ids=frozenset(),
        sees_all=True,
        actor_id=None,
    )
    settings = request.app.state.settings
    service = VoiceCallService(
        scope.session,
        workspace=scope.workspace,
        leads=leads,
        context=context,
        client=bolna_client(request),
        config=bolna_config(request),
        actor_id=None,
        match_by_phone=settings.bolna_match_by_phone,
        create_missing_leads=settings.bolna_create_missing_leads,
        summarizer=call_summarizer(request),
    )
    outcome = await service.handle_execution(payload)
    return VoiceExecutionResult(
        status=outcome.status,
        execution_id=outcome.execution_id,
        lead_id=outcome.lead_id,
        written=outcome.written,
        call_count=outcome.call_count,
        last_call_summary=outcome.last_call_summary,
        # Additive — a workspace with no mappings sees three empty lists.
        extraction_written=list(outcome.extraction.written),
        extraction_noted=list(outcome.extraction.noted),
        extraction_unmapped=list(outcome.extraction.unmapped),
        call_summary=outcome.call_summary,
        summary_source=outcome.summary_source,
        call_log_id=outcome.call_log_id,
        call_id=outcome.call_id,
    )


def _client_ip(request: Request) -> str:
    """The peer address, honouring one hop of `X-Forwarded-For`.

    Behind ngrok or any reverse proxy the socket peer is the proxy, so the
    forwarded header is the only thing that names the real sender. That header
    is also trivially spoofable by whoever can reach the socket, which is why
    the allowlist below is defence in depth and never the only check.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


@bolna_router.post(
    "/voice/bolna/{webhook_key}",
    response_model=VoiceExecutionResult,
    summary="Bolna call-completion webhook (key in the URL, idempotent)",
)
async def receive_bolna_webhook(
    payload: VoiceExecutionWebhook,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    webhook_key: Annotated[str, Path(min_length=8, max_length=200)],
) -> VoiceExecutionResult:
    """The endpoint a real Bolna agent posts to.

    **Why the key is in the URL.** Bolna's agent configuration accepts a bare
    `webhook_url` and nothing else — no custom headers, no signing secret
    (`.claude/skills/setup-webhook/SKILL.md`). So the header-authenticated route
    below cannot ever be called by Bolna: every real delivery would 401. A
    secret path segment is the only credential the vendor is capable of
    presenting.

    **What that costs, stated plainly.** A URL secret is weaker than a header or
    an HMAC signature: it can be captured by proxy access logs, browser history,
    or a `Referer`. It is mitigated, not eliminated, by four things — the key is
    an ordinary CRM API key, so it is stored Argon2-hashed, carries a narrow
    permission template, is revocable in one click from Settings → Integrations,
    and can be paired with `BOLNA_WEBHOOK_ALLOWED_IPS`. Use a key created *only*
    for this, never a shared one, and always over HTTPS.

    Everything downstream is identical to the header route, including the
    execution-id idempotency gate.
    """
    allowed = list(request.app.state.settings.bolna_webhook_allowed_ips)
    if allowed and _client_ip(request) not in allowed:
        # 404, not 403: an unexpected source learns nothing about whether the
        # path segment it tried is a real key.
        raise api_error(404, "not_found", "Not found")

    scope = await resolve_api_key(request, session, webhook_key)
    return await _process_execution(scope, payload, request)
