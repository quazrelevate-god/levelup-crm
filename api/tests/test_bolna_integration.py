"""The CRM ↔ Bolna integration (Phase 2).

Phase 1 proved a lead's voice context is durable. This file proves the loop
around it closes: the CRM hands Bolna the context before a call, and folds the
result of that call back onto the *same* lead afterwards, so the second call
starts where the first one stopped.

**No test here needs a Bolna account, a network, or a paid minute.**
`RecordingBolnaClient` stands in at the one seam
(`app.integrations.bolna.BolnaClient`) and records the exact `user_data` that
would have been sent, which is what makes assertions like "call 2's payload
carries call 1's summary" possible at all.

The scenario the milestone exists for is
`test_call_two_carries_call_one_summary_into_the_bolna_payload`. Everything else
is a guard around it.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.factories import WorkspaceFixture, add_member, build_workspace, login

from app.auth.passwords import PasswordHasherService
from app.fields.rendering import render_for_voice
from app.integrations.bolna import (
    BolnaCallRequest,
    BolnaCallResult,
    BolnaSettings,
    RecordingBolnaClient,
)
from app.models.enums import LeadFieldType, SystemActionKind, VoiceCallStatus
from app.models.field import FieldOption, LeadField
from app.models.lead import Action, Lead
from app.models.pipeline import CallDisposition
from app.models.voice import VoiceCallContext, VoiceCallExecution

pytestmark = pytest.mark.integration

CALL_ONE_SUMMARY = "Customer is interested in the Python course and prefers weekend classes."
CALL_TWO_SUMMARY = "Confirmed Saturday 10am batch; will pay the deposit on Friday."

#: Not a real credential. A recognisable sentinel, so the "never exposed" tests
#: are asserting on something that would be unmistakable if it ever leaked.
FAKE_BOLNA_KEY = "bn-test-key-do-not-use-0000000000"
FAKE_AGENT_ID = "11111111-2222-3333-4444-555555555555"


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def hasher(wired_app: FastAPI) -> PasswordHasherService:
    hasher = wired_app.state.password_hasher
    assert isinstance(hasher, PasswordHasherService)
    return hasher


@pytest.fixture
def bolna(wired_app: FastAPI) -> RecordingBolnaClient:
    """Install the fake client and a configuration, for this test only.

    Mirrors how the SMTP sender is overridden: the app looks at
    `app.state.<thing>` first and only falls back to building a real one.
    """
    client = RecordingBolnaClient()
    wired_app.state.bolna_client = client
    wired_app.state.bolna_settings = BolnaSettings(
        api_key=FAKE_BOLNA_KEY,
        base_url="https://api.bolna.invalid",
        agent_id=FAKE_AGENT_ID,
    )
    return client


@pytest.fixture
async def workspace(db_session: AsyncSession, hasher: PasswordHasherService) -> WorkspaceFixture:
    fixture = await build_workspace(
        db_session, hasher, name="Bolna Co", owner_email="owner@bolnaco.example"
    )
    await add_member(
        db_session,
        hasher,
        fixture,
        key="caller",
        email="caller@bolnaco.example",
        template_name="Caller",
    )
    await add_member(
        db_session,
        hasher,
        fixture,
        key="marketing",
        email="marketing@bolnaco.example",
        template_name="Marketing",
    )
    return fixture


async def _admin(api: AsyncClient, workspace: WorkspaceFixture) -> dict[str, str]:
    await login(api, workspace.owner)
    return workspace.owner.auth


async def _marketing(api: AsyncClient, workspace: WorkspaceFixture) -> dict[str, str]:
    await login(api, workspace.members["marketing"])
    return workspace.members["marketing"].auth


async def _create_lead(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    headers: dict[str, str] | None = None,
    *,
    name: str = "Test Customer",
    phone: str = "9876543210",
    email: str = "test.customer@example.com",
) -> dict[str, Any]:
    response = await api.post(
        workspace.path("/leads"),
        headers=headers if headers is not None else workspace.owner.auth,
        json={"values": {"name": name, "phone": phone, "email": email}},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def _api_key(api: AsyncClient, workspace: WorkspaceFixture, *, template: str = "Root") -> str:
    response = await api.post(
        workspace.path("/settings/api-keys"),
        headers=workspace.owner.auth,
        json={
            "name": f"Bolna {template}",
            "permission_template_id": str(workspace.templates[template].id),
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["key"])


async def _trigger(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    headers: dict[str, str],
    **body: Any,
) -> Any:
    return await api.post(workspace.path("/voice/calls"), headers=headers, json=body)


async def _webhook(
    api: AsyncClient, workspace: WorkspaceFixture, key: str, body: dict[str, Any]
) -> Any:
    return await api.post(
        workspace.path("/voice/executions"), headers={"X-API-Key": key}, json=body
    )


# --- the scenario this milestone exists for ---------------------------------


async def test_call_two_carries_call_one_summary_into_the_bolna_payload(
    api: AsyncClient, workspace: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """CALL 1 → summary → CALL 2 sees it. The whole point, end to end.

    Asserted at the boundary that actually matters: not "the CRM stored it", but
    "the payload Bolna received on the second call contained it". A context
    system that keeps a summary the agent is never told is continuity on paper
    only.
    """
    headers = await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace)

    # --- CALL 1 -------------------------------------------------------------
    before = await api.get(workspace.path(f"/voice/context/{lead['id']}"), headers=headers)
    assert before.status_code == 200
    assert before.json()["last_call_summary"] is None
    assert before.json()["call_count"] == 0

    first = await _trigger(api, workspace, headers, lead_id=lead["id"])
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["status"] == VoiceCallStatus.DISPATCHED.value
    assert first_body["execution_id"]
    # Nothing to carry in yet — this is a cold call.
    assert first_body["previous_call_summary"] is None
    assert "crm_last_call_summary" not in first_body["user_data"]
    assert first_body["user_data"]["crm_is_repeat_caller"] == "no"

    completed = await _webhook(
        api,
        workspace,
        key,
        {
            "execution_id": first_body["execution_id"],
            "status": "completed",
            "summary": CALL_ONE_SUMMARY,
        },
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "accepted"
    assert completed.json()["written"] is True
    assert completed.json()["last_call_summary"] == CALL_ONE_SUMMARY
    assert completed.json()["call_count"] == 1

    # --- CALL 2 -------------------------------------------------------------
    second = await _trigger(api, workspace, headers, lead_id=lead["id"])
    assert second.status_code == 200, second.text
    second_body = second.json()

    # The same customer, not a new one.
    assert second_body["lead_id"] == lead["id"]
    # A different Bolna execution.
    assert second_body["execution_id"] != first_body["execution_id"]
    # And it was told what happened last time.
    assert second_body["previous_call_summary"] == CALL_ONE_SUMMARY
    assert second_body["user_data"]["crm_last_call_summary"] == CALL_ONE_SUMMARY
    assert second_body["user_data"]["crm_call_count"] == "1"
    assert second_body["user_data"]["crm_is_repeat_caller"] == "yes"

    # And the payload the fake client actually received says the same thing —
    # asserted against the client, not just the HTTP response, so a bug that
    # only decorated the response could not pass this.
    sent = bolna.last
    assert isinstance(sent, BolnaCallRequest)
    assert sent.user_data["crm_last_call_summary"] == CALL_ONE_SUMMARY
    assert sent.recipient_phone_number == "+919876543210"
    assert sent.agent_id == FAKE_AGENT_ID

    # --- and call 2's own outcome lands on the same lead --------------------
    await _webhook(
        api,
        workspace,
        key,
        {
            "execution_id": second_body["execution_id"],
            "status": "completed",
            "summary": CALL_TWO_SUMMARY,
        },
    )
    after = await api.get(workspace.path(f"/voice/context/{lead['id']}"), headers=headers)
    assert after.json()["last_call_summary"] == CALL_TWO_SUMMARY
    assert after.json()["call_count"] == 2


# --- resolution -------------------------------------------------------------


async def test_an_existing_lead_is_resolved_by_phone_number(
    api: AsyncClient, workspace: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """The lookup a Bolna trigger has before it knows any CRM id."""
    headers = await _admin(api, workspace)
    lead = await _create_lead(api, workspace)

    response = await _trigger(api, workspace, headers, phone="9876543210")
    assert response.status_code == 200, response.text
    assert response.json()["lead_id"] == lead["id"]
    # Normalised through the workspace's default country code (rule 12), not a
    # hardcoded prefix.
    assert bolna.last.recipient_phone_number == "+919876543210"


async def test_triggering_by_phone_never_creates_a_second_lead(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    db_session: AsyncSession,
) -> None:
    """Three calls to the same number, still one customer."""
    headers = await _admin(api, workspace)
    lead = await _create_lead(api, workspace)

    for _ in range(3):
        response = await _trigger(api, workspace, headers, phone="+919876543210")
        assert response.status_code == 200, response.text
        assert response.json()["lead_id"] == lead["id"]

    rows = await db_session.execute(
        select(Lead).where(
            Lead.workspace_id == workspace.id, Lead.identity_value == "+919876543210"
        )
    )
    assert len(list(rows.scalars().all())) == 1


async def test_an_unknown_phone_number_is_not_found(
    api: AsyncClient, workspace: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """A well-formed number nobody in this workspace has. 404, and no call."""
    headers = await _admin(api, workspace)
    await _create_lead(api, workspace)

    response = await _trigger(api, workspace, headers, phone="9000000001")
    assert response.status_code == 404
    assert bolna.calls == []


async def test_an_invalid_phone_number_is_rejected(
    api: AsyncClient, workspace: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Unparseable is 422, distinct from valid-but-unknown, which is 404."""
    headers = await _admin(api, workspace)
    response = await _trigger(api, workspace, headers, phone="not-a-phone")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_phone"
    assert bolna.calls == []


async def test_a_trigger_needs_exactly_one_identifier(
    api: AsyncClient, workspace: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    headers = await _admin(api, workspace)
    lead = await _create_lead(api, workspace)

    neither = await _trigger(api, workspace, headers)
    assert neither.status_code == 422
    both = await _trigger(api, workspace, headers, lead_id=lead["id"], phone="9876543210")
    assert both.status_code == 422


# --- the outbound payload ---------------------------------------------------


async def test_the_outbound_payload_carries_the_customer_context(
    api: AsyncClient, workspace: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Everything the agent needs, and it is the projected context, not raw rows."""
    headers = await _admin(api, workspace)
    lead = await _create_lead(api, workspace)

    response = await _trigger(api, workspace, headers, lead_id=lead["id"])
    assert response.status_code == 200, response.text

    sent = bolna.last.user_data
    # The workspace's own lead-field keys, bare (contract §4).
    assert sent["name"] == "Test Customer"
    assert sent["email"] == "test.customer@example.com"
    assert sent["phone"] == "+919876543210"
    # The reserved namespace the contract freezes.
    assert sent["crm_lead_id"] == lead["id"]
    assert sent["crm_workspace_id"] == str(workspace.id)
    assert uuid.UUID(sent["crm_idempotency"])
    # Pipeline position travels too — requirement: "lead status, pipeline/stage".
    assert sent["crm_stage"]


async def test_a_field_the_template_cannot_view_never_reaches_bolna(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    db_session: AsyncSession,
) -> None:
    """Contract §3's PII control, asserted at the boundary.

    The trigger builds `user_data` from `VoiceContextService.get_context`, whose
    values are already View-projected. A caller whose template grants no View on
    a field must not be able to launder it out through the voice payload.
    """
    headers = await _admin(api, workspace)
    lead = await _create_lead(api, workspace)

    # A field nobody has been granted anything on.
    secret_field = LeadField(
        workspace_id=workspace.id,
        key="internal_note",
        label="Internal Note",
        field_type=LeadFieldType.TEXT,
        sort_order=99,
    )
    db_session.add(secret_field)
    await db_session.commit()

    response = await _trigger(api, workspace, headers, lead_id=lead["id"])
    assert response.status_code == 200
    assert "internal_note" not in bolna.last.user_data


# --- credentials ------------------------------------------------------------


async def test_the_bolna_credential_never_appears_in_any_response(
    api: AsyncClient, workspace: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Requirement 9's hard rule, checked on every surface that could leak it."""
    headers = await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace)

    triggered = await _trigger(api, workspace, headers, lead_id=lead["id"])
    execution_id = triggered.json()["execution_id"]
    completed = await _webhook(
        api,
        workspace,
        key,
        {"execution_id": execution_id, "status": "completed", "summary": CALL_ONE_SUMMARY},
    )
    context = await api.get(workspace.path(f"/voice/context/{lead['id']}"), headers=headers)

    forbidden = (
        FAKE_BOLNA_KEY,
        "bolna_api_key",
        "password",
        "hashed_key",
        "jwt_secret",
        "api_key",
        "Authorization",
    )
    for response in (triggered, completed, context):
        raw = response.text
        for needle in forbidden:
            assert needle not in raw, f"{needle!r} leaked into {response.request.url}"


async def test_the_credential_is_not_in_the_stored_execution_row(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    db_session: AsyncSession,
) -> None:
    """`context_sent` is an audit record. It must never become a key store."""
    headers = await _admin(api, workspace)
    lead = await _create_lead(api, workspace)
    await _trigger(api, workspace, headers, lead_id=lead["id"])

    rows = await db_session.execute(
        select(VoiceCallExecution).where(VoiceCallExecution.workspace_id == workspace.id)
    )
    row = rows.scalars().one()
    assert FAKE_BOLNA_KEY not in str(row.context_sent)
    assert FAKE_BOLNA_KEY not in (row.last_error or "")


def test_the_settings_repr_redacts_the_key() -> None:
    """A traceback or a log line must not be a credential disclosure."""
    settings = BolnaSettings(api_key=FAKE_BOLNA_KEY, base_url="https://x.invalid", agent_id="a")
    assert FAKE_BOLNA_KEY not in repr(settings)
    assert "redacted" in repr(settings)


# --- the execution record ---------------------------------------------------


async def test_the_external_execution_id_is_stored_against_the_lead(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    db_session: AsyncSession,
) -> None:
    headers = await _admin(api, workspace)
    lead = await _create_lead(api, workspace)

    response = await _trigger(api, workspace, headers, lead_id=lead["id"])
    execution_id = response.json()["execution_id"]

    rows = await db_session.execute(
        select(VoiceCallExecution).where(VoiceCallExecution.external_id == execution_id)
    )
    row = rows.scalars().one()
    assert str(row.lead_id) == lead["id"]
    assert row.status is VoiceCallStatus.DISPATCHED
    assert row.agent_id == FAKE_AGENT_ID
    assert row.dispatched_at is not None
    assert row.attempts == 1


async def test_a_bolna_failure_is_recorded_and_does_not_lose_the_call(
    api: AsyncClient,
    wired_app: FastAPI,
    workspace: WorkspaceFixture,
    db_session: AsyncSession,
) -> None:
    """The claim-then-send shape: an unreachable vendor leaves a visible row."""
    wired_app.state.bolna_client = RecordingBolnaClient(
        result=BolnaCallResult(execution_id=None, status=None, error="ConnectError: nope")
    )
    wired_app.state.bolna_settings = BolnaSettings(
        api_key=FAKE_BOLNA_KEY, base_url="https://api.bolna.invalid", agent_id=FAKE_AGENT_ID
    )
    headers = await _admin(api, workspace)
    lead = await _create_lead(api, workspace)

    response = await _trigger(api, workspace, headers, lead_id=lead["id"])
    assert response.status_code == 200, response.text
    assert response.json()["status"] == VoiceCallStatus.FAILED.value
    assert response.json()["execution_id"] is None
    assert response.json()["error"]

    rows = await db_session.execute(
        select(VoiceCallExecution).where(VoiceCallExecution.workspace_id == workspace.id)
    )
    row = rows.scalars().one()
    assert row.status is VoiceCallStatus.FAILED
    assert row.completed_at is None
    assert row.last_error == "ConnectError: nope"


async def test_an_unconfigured_deployment_says_so(
    api: AsyncClient, wired_app: FastAPI, workspace: WorkspaceFixture
) -> None:
    """422 with a code, not a 500 from a missing environment variable."""
    wired_app.state.bolna_client = None
    wired_app.state.bolna_settings = None
    headers = await _admin(api, workspace)
    lead = await _create_lead(api, workspace)

    response = await _trigger(api, workspace, headers, lead_id=lead["id"])
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "voice_not_configured"


# --- the inbound webhook ----------------------------------------------------


async def test_a_completed_call_updates_the_same_lead_context(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    db_session: AsyncSession,
) -> None:
    headers = await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace)

    triggered = await _trigger(api, workspace, headers, lead_id=lead["id"])
    execution_id = triggered.json()["execution_id"]

    response = await _webhook(
        api,
        workspace,
        key,
        {"execution_id": execution_id, "status": "completed", "summary": CALL_ONE_SUMMARY},
    )
    assert response.status_code == 200, response.text
    assert response.json()["lead_id"] == lead["id"]

    rows = await db_session.execute(
        select(VoiceCallContext).where(VoiceCallContext.workspace_id == workspace.id)
    )
    contexts = list(rows.scalars().all())
    # One context row for one lead, however many calls it has had.
    assert len(contexts) == 1
    assert contexts[0].last_call_summary == CALL_ONE_SUMMARY
    assert contexts[0].last_call_external_id == execution_id
    assert contexts[0].call_count == 1


async def test_a_repeated_webhook_is_idempotent(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    db_session: AsyncSession,
) -> None:
    """Five identical deliveries. One summary, one timeline entry, one call.

    Bolna delivers at least once and retries on anything non-2xx, so this is the
    ordinary case rather than an edge one.
    """
    headers = await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace)

    triggered = await _trigger(api, workspace, headers, lead_id=lead["id"])
    execution_id = triggered.json()["execution_id"]
    body = {"execution_id": execution_id, "status": "completed", "summary": CALL_ONE_SUMMARY}

    first = await _webhook(api, workspace, key, body)
    assert first.json()["status"] == "accepted"
    assert first.json()["written"] is True

    for _ in range(4):
        repeat = await _webhook(api, workspace, key, body)
        assert repeat.status_code == 200
        assert repeat.json()["status"] == "duplicate"
        assert repeat.json()["written"] is False
        assert repeat.json()["call_count"] == 1

    rows = await db_session.execute(
        select(VoiceCallContext).where(VoiceCallContext.workspace_id == workspace.id)
    )
    assert rows.scalars().one().call_count == 1

    notes = await db_session.execute(
        select(Action).where(Action.workspace_id == workspace.id, Action.body == CALL_ONE_SUMMARY)
    )
    assert len(list(notes.scalars().all())) == 1


async def test_a_non_terminal_delivery_records_status_and_writes_nothing(
    api: AsyncClient, workspace: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """`queued` → `ringing` → `completed` is three deliveries, one write.

    The vendored `setup-webhook` skill warns that deduping on the execution id
    alone throws the later ones away; the CRM keeps them and writes on the last.
    """
    headers = await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace)

    triggered = await _trigger(api, workspace, headers, lead_id=lead["id"])
    execution_id = triggered.json()["execution_id"]

    for status in ("queued", "ringing", "in-progress"):
        response = await _webhook(
            api, workspace, key, {"execution_id": execution_id, "status": status}
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "pending"
        assert response.json()["written"] is False
        assert response.json()["call_count"] == 0

    final = await _webhook(
        api,
        workspace,
        key,
        {"execution_id": execution_id, "status": "completed", "summary": CALL_ONE_SUMMARY},
    )
    assert final.json()["status"] == "accepted"
    assert final.json()["call_count"] == 1


async def test_a_failed_call_is_recorded_without_overwriting_the_summary(
    api: AsyncClient, workspace: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Contract §7: a failed call writes an action, not a field.

    The important half is the second assertion — a no-answer must not blank the
    useful summary the previous successful call left behind.
    """
    headers = await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace)

    good = await _trigger(api, workspace, headers, lead_id=lead["id"])
    await _webhook(
        api,
        workspace,
        key,
        {
            "execution_id": good.json()["execution_id"],
            "status": "completed",
            "summary": CALL_ONE_SUMMARY,
        },
    )

    bad = await _trigger(api, workspace, headers, lead_id=lead["id"])
    response = await _webhook(
        api, workspace, key, {"execution_id": bad.json()["execution_id"], "status": "no-answer"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["written"] is False
    assert response.json()["last_call_summary"] == CALL_ONE_SUMMARY
    assert response.json()["call_count"] == 1


async def test_a_late_duplicate_of_an_older_call_cannot_rewrite_a_newer_summary(
    api: AsyncClient, workspace: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Out-of-order delivery — the case a single `last_external_id` cannot survive.

    Call 1 completes, call 2 completes, then call 1's webhook is redelivered.
    Without a row per execution the CRM would see an id it no longer recognises
    as "last" and roll the lead back to the older summary.
    """
    headers = await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace)

    first = await _trigger(api, workspace, headers, lead_id=lead["id"])
    first_id = first.json()["execution_id"]
    await _webhook(
        api,
        workspace,
        key,
        {"execution_id": first_id, "status": "completed", "summary": CALL_ONE_SUMMARY},
    )
    second = await _trigger(api, workspace, headers, lead_id=lead["id"])
    await _webhook(
        api,
        workspace,
        key,
        {
            "execution_id": second.json()["execution_id"],
            "status": "completed",
            "summary": CALL_TWO_SUMMARY,
        },
    )

    stale = await _webhook(
        api,
        workspace,
        key,
        {"execution_id": first_id, "status": "completed", "summary": CALL_ONE_SUMMARY},
    )
    assert stale.json()["status"] == "duplicate"
    assert stale.json()["last_call_summary"] == CALL_TWO_SUMMARY
    assert stale.json()["call_count"] == 2


async def test_a_webhook_for_an_unknown_execution_is_refused(
    api: AsyncClient, workspace: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """No stored execution and no `crm_lead_id`: nothing to write to.

    Contract §5 forbids falling back to the phone number, which is the only
    other identifier such a payload could carry — so this has to be a refusal.
    """
    await _admin(api, workspace)
    key = await _api_key(api, workspace)

    response = await _webhook(
        api, workspace, key, {"execution_id": "exec-never-seen", "status": "completed"}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unknown_execution"


async def test_a_webhook_without_an_execution_id_is_refused(
    api: AsyncClient, workspace: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    await _admin(api, workspace)
    key = await _api_key(api, workspace)
    response = await _webhook(api, workspace, key, {"status": "completed"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "missing_execution_id"


async def test_a_webhook_arriving_before_the_trigger_is_accepted_via_crm_lead_id(
    api: AsyncClient, workspace: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Contract §7's race, and any call placed outside this CRM.

    `crm_lead_id` resolves through `LeadService`, so it is workspace-scoped by
    construction — a spoofed id from another tenant 404s rather than writing.
    """
    headers = await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace)

    response = await _webhook(
        api,
        workspace,
        key,
        {
            "execution_id": "exec-out-of-band",
            "status": "completed",
            "summary": CALL_ONE_SUMMARY,
            "user_data": {"crm_lead_id": lead["id"]},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"
    assert response.json()["lead_id"] == lead["id"]

    context = await api.get(workspace.path(f"/voice/context/{lead['id']}"), headers=headers)
    assert context.json()["last_call_summary"] == CALL_ONE_SUMMARY


async def test_the_receiver_accepts_the_get_executions_spelling_of_the_id(
    api: AsyncClient, workspace: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """The webhook says `execution_id`; `GET /executions/{id}` says `id`."""
    headers = await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace)
    triggered = await _trigger(api, workspace, headers, lead_id=lead["id"])

    response = await _webhook(
        api,
        workspace,
        key,
        {
            "id": triggered.json()["execution_id"],
            "status": "completed",
            "summary": CALL_ONE_SUMMARY,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"


async def test_an_unknown_vendor_field_does_not_break_the_receiver(
    api: AsyncClient, workspace: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """A vendor release must not become a dropped call summary."""
    headers = await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace)
    triggered = await _trigger(api, workspace, headers, lead_id=lead["id"])

    response = await _webhook(
        api,
        workspace,
        key,
        {
            "execution_id": triggered.json()["execution_id"],
            "status": "completed",
            "summary": CALL_ONE_SUMMARY,
            "telephony_data": {"provider": "twilio", "hangup_code": 200},
            "some_field_invented_next_year": ["anything"],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["written"] is True


async def test_a_summary_from_a_configured_disposition_is_used(
    api: AsyncClient, wired_app: FastAPI, workspace: WorkspaceFixture
) -> None:
    """Disposition names are the customer's vocabulary, so they are config.

    Nothing in the product may hardcode one; setting it here is what an admin
    would do for their own agent.
    """
    wired_app.state.bolna_client = RecordingBolnaClient()
    wired_app.state.bolna_settings = BolnaSettings(
        api_key=FAKE_BOLNA_KEY,
        base_url="https://api.bolna.invalid",
        agent_id=FAKE_AGENT_ID,
        summary_disposition="Call Recap",
    )
    headers = await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace)
    triggered = await _trigger(api, workspace, headers, lead_id=lead["id"])

    response = await _webhook(
        api,
        workspace,
        key,
        {
            "execution_id": triggered.json()["execution_id"],
            "status": "completed",
            "extracted_data": {
                "Call Recap": {"value": CALL_ONE_SUMMARY, "confidence": 0.94},
                "Budget": {"value": "45000", "confidence": 0.61},
            },
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["last_call_summary"] == CALL_ONE_SUMMARY


# --- authorization and isolation --------------------------------------------


async def test_an_unauthenticated_trigger_is_refused(
    api: AsyncClient, workspace: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    await _admin(api, workspace)
    response = await api.post(workspace.path("/voice/calls"), json={"phone": "9876543210"})
    assert response.status_code == 401
    assert bolna.calls == []


async def test_a_template_without_calling_access_cannot_trigger_a_call(
    api: AsyncClient, workspace: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Marketing has no `calling` group at all."""
    admin = await _admin(api, workspace)
    lead = await _create_lead(api, workspace, admin)
    marketing = await _marketing(api, workspace)

    response = await _trigger(api, workspace, marketing, lead_id=lead["id"])
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "insufficient_permissions"
    assert bolna.calls == []


async def test_the_webhook_refuses_a_missing_or_bad_api_key(
    api: AsyncClient, workspace: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    await _admin(api, workspace)
    body = {"execution_id": "exec-1", "status": "completed"}

    missing = await api.post(workspace.path("/voice/executions"), json=body)
    assert missing.status_code == 401

    wrong = await api.post(
        workspace.path("/voice/executions"),
        headers={"X-API-Key": "crmk_totally-made-up-value"},
        json=body,
    )
    assert wrong.status_code == 401


async def test_a_key_cannot_post_to_another_workspaces_path(
    api: AsyncClient,
    db_session: AsyncSession,
    hasher: PasswordHasherService,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """The key is the authority; a mismatched path fails loudly, as a 404."""
    await _admin(api, workspace)
    key = await _api_key(api, workspace)
    other = await build_workspace(
        db_session, hasher, name="Other Co", owner_email="owner@otherco.example"
    )

    response = await api.post(
        other.path("/voice/executions"),
        headers={"X-API-Key": key},
        json={"execution_id": "exec-1", "status": "completed"},
    )
    assert response.status_code == 404


async def test_a_lead_in_another_workspace_cannot_be_called_or_written(
    api: AsyncClient,
    db_session: AsyncSession,
    hasher: PasswordHasherService,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """Cross-tenant isolation, on both halves of the integration."""
    headers = await _admin(api, workspace)
    key = await _api_key(api, workspace)

    other = await build_workspace(
        db_session, hasher, name="Rival Co", owner_email="owner@rivalco.example"
    )
    await login(api, other.owner)
    other_lead = await _create_lead(api, other, other.owner.auth, phone="9000000009")
    await login(api, workspace.owner)

    # Outbound: cannot place a call to a lead we cannot see.
    triggered = await api.post(
        workspace.path("/voice/calls"), headers=headers, json={"lead_id": other_lead["id"]}
    )
    assert triggered.status_code == 404
    assert bolna.calls == []

    # Inbound: cannot write to one either, even naming it explicitly.
    written = await _webhook(
        api,
        workspace,
        key,
        {
            "execution_id": "exec-cross-tenant",
            "status": "completed",
            "summary": "should never land",
            "user_data": {"crm_lead_id": other_lead["id"]},
        },
    )
    assert written.status_code == 404


# --- persistence ------------------------------------------------------------


async def test_the_whole_loop_survives_a_restart(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Read the result back through a session that shares no in-process state.

    The nearest a component test gets to "restart the application": a brand-new
    session, a brand-new connection, nothing carried over but what is in
    Postgres. The live demonstration in the report does the real thing.
    """
    headers = await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace)

    triggered = await _trigger(api, workspace, headers, lead_id=lead["id"])
    execution_id = triggered.json()["execution_id"]
    await _webhook(
        api,
        workspace,
        key,
        {"execution_id": execution_id, "status": "completed", "summary": CALL_ONE_SUMMARY},
    )

    async with session_factory() as fresh:
        contexts = await fresh.execute(
            select(VoiceCallContext).where(VoiceCallContext.lead_id == uuid.UUID(str(lead["id"])))
        )
        context = contexts.scalars().one()
        assert context.last_call_summary == CALL_ONE_SUMMARY
        assert context.call_count == 1

        executions = await fresh.execute(
            select(VoiceCallExecution).where(VoiceCallExecution.external_id == execution_id)
        )
        execution = executions.scalars().one()
        assert execution.completed_at is not None
        assert execution.status is VoiceCallStatus.COMPLETED
        assert execution.context_sent["crm_lead_id"] == lead["id"]


# --- rendering for speech (contract §4) -------------------------------------


def _field(key: str, kind: LeadFieldType) -> LeadField:
    return LeadField(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        key=key,
        label=key.title(),
        field_type=kind,
        sort_order=0,
    )


def test_render_for_voice_speaks_dates_money_and_options() -> None:
    """ "An agent reading `1755129600` aloud is a defect" — contract §4."""
    when = _field("last_contact", LeadFieldType.DATE)
    budget = _field("budget", LeadFieldType.MONEY)
    course = _field("course", LeadFieldType.DROPDOWN)
    subscribed = _field("subscribed", LeadFieldType.CHECKBOX)

    option = FieldOption(field_id=course.id, code="found", label="Foundations", sort_order=0)

    rendered = render_for_voice(
        {
            "last_contact": "2026-08-14T00:00:00+00:00",
            "budget": 45000,
            "course": "found",
            "subscribed": True,
        },
        [when, budget, course, subscribed],
        timezone_name="Asia/Kolkata",
        currency="INR",
        options_by_field={course.id: [option]},
    )

    assert rendered["last_contact"].startswith("14 August 2026")
    assert rendered["budget"] == "45,000 INR"
    # The label, not the stored code.
    assert rendered["course"] == "Foundations"
    assert rendered["subscribed"] == "yes"


def test_render_for_voice_drops_absent_values_rather_than_sending_blanks() -> None:
    """Contract §4: "Absent means not granted or not set."

    A prompt referencing `{budget}` has to be able to degrade, and it can only
    do that if the key is genuinely missing.
    """
    budget = _field("budget", LeadFieldType.MONEY)
    note = _field("note", LeadFieldType.TEXT)

    rendered = render_for_voice({"budget": None, "note": "   "}, [budget, note], currency="INR")
    assert rendered == {}


def test_render_for_voice_renders_an_epoch_as_a_spoken_date() -> None:
    when = _field("last_contact", LeadFieldType.DATE)
    rendered = render_for_voice({"last_contact": 1755129600}, [when], timezone_name="UTC")
    assert "1755129600" not in rendered["last_contact"]
    assert "2025" in rendered["last_contact"]


def test_render_for_voice_keeps_a_value_whose_field_is_gone() -> None:
    """An archived or renamed field must not silently empty a live prompt."""
    rendered = render_for_voice({"mystery": "keep me"}, [])
    assert rendered == {"mystery": "keep me"}


# --- the fake client itself -------------------------------------------------


async def test_the_recording_client_hands_back_distinct_execution_ids() -> None:
    """Guard on the test double: two calls must not look like one retry."""
    client = RecordingBolnaClient()
    first = await client.place_call(
        BolnaCallRequest(agent_id="a", recipient_phone_number="+911", user_data={})
    )
    second = await client.place_call(
        BolnaCallRequest(agent_id="a", recipient_phone_number="+911", user_data={})
    )
    assert first.execution_id != second.execution_id
    assert len(client.calls) == 2


def test_terminal_status_sets_cover_bolnas_documented_vocabulary() -> None:
    """`.claude/skills/get-executions/SKILL.md` lists these; drift is a bug."""
    from app.services.voice_calls import (
        TERMINAL_FAILURE_STATUSES,
        TERMINAL_SUCCESS_STATUSES,
    )

    documented_terminal = {
        "completed",
        "balance-low",
        "busy",
        "no-answer",
        "canceled",
        "failed",
        "stopped",
        "error",
        "call-disconnected",
    }
    covered = TERMINAL_SUCCESS_STATUSES | TERMINAL_FAILURE_STATUSES
    assert documented_terminal <= covered

    documented_pending = {"scheduled", "queued", "rescheduled", "initiated", "ringing"}
    assert not (documented_pending & covered)


def test_the_reserved_namespace_cannot_collide_with_a_customer_field() -> None:
    """Contract §4: `crm_*` is reserved so CRM context cannot shadow a field."""
    from app.services.voice_calls import RESERVED_PREFIX

    assert RESERVED_PREFIX == "crm_"


# --- Phase 3: what a REAL Bolna delivery looks like -------------------------
#
# Everything above this line drives the header-authenticated route with a
# hand-shaped body. A real Bolna agent can do neither: it authenticates with a
# URL and it sends its own execution object. These exercise that path.


def _execution_payload(
    execution_id: str,
    *,
    to_number: str = "+919876543210",
    status: str = "completed",
    summary: str | None = CALL_ONE_SUMMARY,
    conversation_time: float | None = 92.0,
    call_type: str = "outbound",
) -> dict[str, Any]:
    """A body shaped like `GET /executions/{id}`, per the vendored skill.

    Field names inside `telephony_data` are the CRM's best reading of the prose
    in `.claude/skills/get-executions/SKILL.md`; the receiver deliberately
    accepts several spellings and stores `raw_payload` so a real call can
    correct this fixture rather than the fixture defining reality.
    """
    body: dict[str, Any] = {
        "id": execution_id,
        "agent_id": FAKE_AGENT_ID,
        "status": status,
        "transcript": "agent: hello ... user: yes please",
        "recording_url": "https://recordings.bolna.invalid/x.wav",
        "conversation_time": conversation_time,
        "total_cost": 3.2,
        "telephony_data": {
            "provider": "twilio",
            "to_number": to_number,
            "from_number": "+911140000000",
            "call_type": call_type,
            "hangup_reason": "agent_hangup",
        },
        "extracted_data": {},
        "context_details": {},
    }
    if summary is not None:
        body["summary"] = summary
    return body


async def _bolna_post(api: AsyncClient, key: str, body: dict[str, Any]) -> Any:
    """Exactly how Bolna calls us: a URL with the key in it, no headers."""
    return await api.post(f"/api/v1/voice/bolna/{key}", json=body)


async def test_a_real_bolna_delivery_authenticates_by_url_and_matches_by_phone(
    api: AsyncClient, workspace: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """The end-to-end shape of a real call, with no CRM-side trigger at all.

    No `crm_lead_id`, no execution row, no header — just the number Bolna dialled
    and a key in the path. This is the case every previous version of the
    receiver answered with a 401 or a 422.
    """
    headers = await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace)

    response = await _bolna_post(api, key, _execution_payload("real-exec-1"))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"
    assert response.json()["lead_id"] == lead["id"]
    assert response.json()["last_call_summary"] == CALL_ONE_SUMMARY

    context = await api.get(workspace.path(f"/voice/context/{lead['id']}"), headers=headers)
    assert context.json()["last_call_summary"] == CALL_ONE_SUMMARY
    assert context.json()["call_count"] == 1


async def test_an_unnormalised_number_still_matches_the_same_lead(
    api: AsyncClient, workspace: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """`9876543210` from the CRM and `+919876543210` from Bolna are one person.

    Matching on the raw string would miss every lead a human typed, and
    auto-create would then manufacture the duplicate this whole milestone exists
    to prevent.
    """
    await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace, phone="9876543210")

    response = await _bolna_post(
        api, key, _execution_payload("real-exec-2", to_number="+91 98765 43210")
    )
    assert response.status_code == 200, response.text
    assert response.json()["lead_id"] == lead["id"]


async def test_a_completed_call_writes_a_call_logged_action(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    db_session: AsyncSession,
) -> None:
    """Duration, direction and disposition — the same shape a human's log has."""
    await _admin(api, workspace)
    key = await _api_key(api, workspace)
    await _create_lead(api, workspace)

    await _bolna_post(api, key, _execution_payload("real-exec-3", conversation_time=92.0))

    rows = await db_session.execute(
        select(Action).where(
            Action.workspace_id == workspace.id,
            Action.kind == SystemActionKind.CALL_LOGGED,
        )
    )
    logged = list(rows.scalars().all())
    assert len(logged) == 1
    payload = logged[0].payload
    assert payload["duration_seconds"] == 92
    assert payload["direction"] == "OUTGOING"
    assert payload["disposition_id"]
    # Post-call automation: the summary is the call log's own body, and the
    # log is marked as an AI call. No separate NOTE — one call, one entry.
    assert logged[0].body == CALL_ONE_SUMMARY
    assert payload["source"] == "AI_CALL"
    assert payload["execution_id"] == "real-exec-3"
    notes = await db_session.execute(
        select(Action).where(
            Action.workspace_id == workspace.id, Action.kind == SystemActionKind.NOTE
        )
    )
    assert list(notes.scalars().all()) == []

    # A 92-second answered call is "connected", so it gets the workspace's
    # default disposition — the same rule the manual log-call form follows.
    dispositions = await db_session.execute(
        select(CallDisposition).where(
            CallDisposition.workspace_id == workspace.id,
            CallDisposition.is_default.is_(True),
        )
    )
    assert payload["disposition_id"] == str(dispositions.scalars().one().id)


async def test_an_inbound_call_is_recorded_as_incoming(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    db_session: AsyncSession,
) -> None:
    await _admin(api, workspace)
    key = await _api_key(api, workspace)
    await _create_lead(api, workspace)

    await _bolna_post(api, key, _execution_payload("real-exec-4", call_type="inbound"))
    rows = await db_session.execute(
        select(Action).where(Action.kind == SystemActionKind.CALL_LOGGED)
    )
    assert rows.scalars().one().payload["direction"] == "INCOMING"


async def test_an_unanswered_call_gets_the_no_answer_disposition(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    db_session: AsyncSession,
) -> None:
    """Contract §7: a failed call is recorded, and writes no summary."""
    await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace)

    response = await _bolna_post(
        api,
        key,
        _execution_payload("real-exec-5", status="no-answer", summary=None, conversation_time=0.0),
    )
    assert response.status_code == 200, response.text
    assert response.json()["written"] is False
    assert response.json()["last_call_summary"] is None

    rows = await db_session.execute(
        select(Action).where(Action.kind == SystemActionKind.CALL_LOGGED)
    )
    logged = rows.scalars().one()
    assert logged.payload["duration_seconds"] == 0

    disposition = await db_session.get(CallDisposition, uuid.UUID(logged.payload["disposition_id"]))
    assert disposition is not None
    assert disposition.label == "No Answer"
    assert str(lead["id"]) == str(logged.lead_id)


@pytest.fixture
def auto_create(wired_app: FastAPI) -> Any:
    """`BOLNA_CREATE_MISSING_LEADS=true`, for the tests that exercise it.

    Off by default since post-call automation: an unmatched webhook must not
    invent a customer. The behaviour still exists for deployments that opt in.
    """
    original = wired_app.state.settings.bolna_create_missing_leads
    wired_app.state.settings.bolna_create_missing_leads = True
    yield
    wired_app.state.settings.bolna_create_missing_leads = original


async def test_a_call_to_an_unknown_number_creates_exactly_one_lead(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    db_session: AsyncSession,
    auto_create: None,
) -> None:
    """The customer Bolna called is not in the CRM yet. Create them, once."""
    await _admin(api, workspace)
    key = await _api_key(api, workspace)

    response = await _bolna_post(
        api, key, _execution_payload("real-exec-6", to_number="+919000000123")
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"
    created_id = response.json()["lead_id"]

    rows = await db_session.execute(
        select(Lead).where(
            Lead.workspace_id == workspace.id, Lead.identity_value == "+919000000123"
        )
    )
    assert len(list(rows.scalars().all())) == 1
    assert created_id is not None


async def test_call_two_to_the_same_new_number_reuses_the_created_lead(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    db_session: AsyncSession,
    auto_create: None,
) -> None:
    """The CALL 1 / CALL 2 requirement, entirely through real webhooks.

    Two independent Bolna calls to a number the CRM had never seen. The first
    creates the customer; the second must find them, not manufacture a twin.
    """
    headers = await _admin(api, workspace)
    key = await _api_key(api, workspace)
    number = "+919000000456"

    first = await _bolna_post(api, key, _execution_payload("real-exec-7a", to_number=number))
    assert first.status_code == 200, first.text
    lead_id = first.json()["lead_id"]

    second = await _bolna_post(
        api,
        key,
        _execution_payload("real-exec-7b", to_number=number, summary=CALL_TWO_SUMMARY),
    )
    assert second.status_code == 200, second.text
    assert second.json()["lead_id"] == lead_id
    assert second.json()["call_count"] == 2
    assert second.json()["last_call_summary"] == CALL_TWO_SUMMARY

    rows = await db_session.execute(
        select(Lead).where(Lead.workspace_id == workspace.id, Lead.identity_value == number)
    )
    assert len(list(rows.scalars().all())) == 1

    # Two calls, two call logs, one customer.
    logs = await db_session.execute(
        select(Action).where(
            Action.workspace_id == workspace.id,
            Action.kind == SystemActionKind.CALL_LOGGED,
        )
    )
    assert len(list(logs.scalars().all())) == 2

    # And the context the next call would carry is the newer summary.
    context = await api.get(workspace.path(f"/voice/context/{lead_id}"), headers=headers)
    assert context.json()["last_call_summary"] == CALL_TWO_SUMMARY


async def test_a_retried_real_webhook_writes_nothing_twice(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    db_session: AsyncSession,
) -> None:
    """Bolna retries. One call log, one summary, one call_count — always."""
    await _admin(api, workspace)
    key = await _api_key(api, workspace)
    await _create_lead(api, workspace)
    body = _execution_payload("real-exec-8")

    first = await _bolna_post(api, key, body)
    assert first.json()["status"] == "accepted"
    for _ in range(4):
        repeat = await _bolna_post(api, key, body)
        assert repeat.status_code == 200
        assert repeat.json()["status"] == "duplicate"
        assert repeat.json()["call_count"] == 1

    logs = await db_session.execute(
        select(Action).where(Action.kind == SystemActionKind.CALL_LOGGED)
    )
    assert len(list(logs.scalars().all())) == 1


async def test_the_status_transitions_bolna_actually_sends_do_not_write_early(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    db_session: AsyncSession,
) -> None:
    """queued → ringing → in-progress → completed is four deliveries, one write."""
    await _admin(api, workspace)
    key = await _api_key(api, workspace)
    await _create_lead(api, workspace)

    for status in ("queued", "ringing", "in-progress"):
        response = await _bolna_post(
            api,
            key,
            _execution_payload("real-exec-9", status=status, summary=None, conversation_time=None),
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "pending"

    logs = await db_session.execute(
        select(Action).where(Action.kind == SystemActionKind.CALL_LOGGED)
    )
    assert list(logs.scalars().all()) == []

    final = await _bolna_post(api, key, _execution_payload("real-exec-9"))
    assert final.json()["status"] == "accepted"
    assert final.json()["call_count"] == 1


async def test_the_raw_bolna_body_is_kept_verbatim(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    db_session: AsyncSession,
) -> None:
    """The evidence that replaces the guessed field names.

    After the first real call, this row is what tells us whether `to_number` was
    the right key — so it has to survive, including the parts the schema does
    not name.
    """
    await _admin(api, workspace)
    key = await _api_key(api, workspace)
    await _create_lead(api, workspace)

    await _bolna_post(api, key, _execution_payload("real-exec-10"))

    rows = await db_session.execute(
        select(VoiceCallExecution).where(VoiceCallExecution.external_id == "real-exec-10")
    )
    stored = rows.scalars().one().raw_payload
    assert stored["telephony_data"]["to_number"] == "+919876543210"
    assert stored["telephony_data"]["hangup_reason"] == "agent_hangup"
    assert stored["conversation_time"] == 92.0


async def test_the_url_route_refuses_a_bad_or_revoked_key(
    api: AsyncClient, workspace: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """The whole security boundary of this route is that path segment."""
    await _admin(api, workspace)
    await _create_lead(api, workspace)
    body = _execution_payload("real-exec-11")

    made_up = await _bolna_post(api, "crmk_totally-made-up-value", body)
    assert made_up.status_code == 401

    not_even_a_key = await _bolna_post(api, "hunter2hunter2", body)
    assert not_even_a_key.status_code == 401


async def test_a_revoked_key_stops_working_immediately(
    api: AsyncClient, workspace: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Revocation is the mitigation for a URL secret. It has to actually work."""
    headers = await _admin(api, workspace)
    await _create_lead(api, workspace)

    created = await api.post(
        workspace.path("/settings/api-keys"),
        headers=headers,
        json={
            "name": "Bolna Revocable",
            "permission_template_id": str(workspace.templates["Root"].id),
        },
    )
    key = created.json()["key"]
    assert (await _bolna_post(api, key, _execution_payload("rev-1"))).status_code == 200

    revoked = await api.delete(
        workspace.path(f"/settings/api-keys/{created.json()['id']}"), headers=headers
    )
    assert revoked.status_code == 204
    assert (await _bolna_post(api, key, _execution_payload("rev-2"))).status_code == 401


async def test_a_keys_workspace_is_the_only_one_it_can_write_to(
    api: AsyncClient,
    db_session: AsyncSession,
    hasher: PasswordHasherService,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """Phone matching must not become a cross-tenant hole.

    Two workspaces, the same phone number, one key. The number belongs to a lead
    in each; the key may only ever touch its own.
    """
    await _admin(api, workspace)
    key = await _api_key(api, workspace)
    mine = await _create_lead(api, workspace, phone="9876500001")

    other = await build_workspace(
        db_session, hasher, name="Rival Co", owner_email="owner@rival-phone.example"
    )
    await login(api, other.owner)
    theirs = await _create_lead(api, other, other.owner.auth, phone="9876500001")
    await login(api, workspace.owner)

    response = await _bolna_post(api, key, _execution_payload("cross-1", to_number="+919876500001"))
    assert response.status_code == 200, response.text
    assert response.json()["lead_id"] == mine["id"]
    assert response.json()["lead_id"] != theirs["id"]


async def test_phone_matching_can_be_switched_off(
    api: AsyncClient, wired_app: FastAPI, workspace: WorkspaceFixture
) -> None:
    """`BOLNA_MATCH_BY_PHONE=false` restores the strict contract-§5 behaviour.

    It exists so that adding an unauthenticated webhook route later cannot
    silently inherit permission to write by phone number.
    """
    wired_app.state.bolna_client = RecordingBolnaClient()
    wired_app.state.bolna_settings = BolnaSettings(
        api_key=FAKE_BOLNA_KEY, base_url="https://api.bolna.invalid", agent_id=FAKE_AGENT_ID
    )
    original = wired_app.state.settings.bolna_match_by_phone
    wired_app.state.settings.bolna_match_by_phone = False
    try:
        await _admin(api, workspace)
        key = await _api_key(api, workspace)
        await _create_lead(api, workspace)

        response = await _bolna_post(api, key, _execution_payload("strict-1"))
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "unknown_execution"
    finally:
        wired_app.state.settings.bolna_match_by_phone = original


async def test_auto_create_can_be_switched_off(
    api: AsyncClient, wired_app: FastAPI, workspace: WorkspaceFixture
) -> None:
    """A workspace that wants calls only from known customers can have that."""
    wired_app.state.bolna_client = RecordingBolnaClient()
    wired_app.state.bolna_settings = BolnaSettings(
        api_key=FAKE_BOLNA_KEY, base_url="https://api.bolna.invalid", agent_id=FAKE_AGENT_ID
    )
    original = wired_app.state.settings.bolna_create_missing_leads
    wired_app.state.settings.bolna_create_missing_leads = False
    try:
        await _admin(api, workspace)
        key = await _api_key(api, workspace)
        response = await _bolna_post(
            api, key, _execution_payload("strict-2", to_number="+919000009999")
        )
        assert response.status_code == 422
    finally:
        wired_app.state.settings.bolna_create_missing_leads = original


async def test_the_source_ip_allowlist_hides_the_route(
    api: AsyncClient, wired_app: FastAPI, workspace: WorkspaceFixture
) -> None:
    """Defence in depth for a secret that lives in a URL.

    404 rather than 403, so a source that is not on the list learns nothing
    about whether the path it guessed was a real key.
    """
    wired_app.state.bolna_client = RecordingBolnaClient()
    wired_app.state.bolna_settings = BolnaSettings(
        api_key=FAKE_BOLNA_KEY, base_url="https://api.bolna.invalid", agent_id=FAKE_AGENT_ID
    )
    original = list(wired_app.state.settings.bolna_webhook_allowed_ips)
    wired_app.state.settings.bolna_webhook_allowed_ips = ["13.203.39.153"]
    try:
        await _admin(api, workspace)
        key = await _api_key(api, workspace)
        await _create_lead(api, workspace)

        blocked = await _bolna_post(api, key, _execution_payload("ip-1"))
        assert blocked.status_code == 404

        allowed = await api.post(
            f"/api/v1/voice/bolna/{key}",
            json=_execution_payload("ip-2"),
            headers={"X-Forwarded-For": "13.203.39.153"},
        )
        assert allowed.status_code == 200, allowed.text
    finally:
        wired_app.state.settings.bolna_webhook_allowed_ips = original


def test_the_duration_and_phone_readers_tolerate_a_missing_telephony_block() -> None:
    """A status-only delivery has no telephony data at all. Must not explode."""
    from app.services.voice_calls import DURATION_PATHS, PHONE_PATHS, first_present

    assert first_present({}, PHONE_PATHS) is None
    assert first_present({"telephony_data": None}, PHONE_PATHS) is None
    assert first_present({"conversation_time": 12}, DURATION_PATHS) == 12
    assert first_present({"telephony_data": {"to_number": "+91"}}, PHONE_PATHS) == "+91"
