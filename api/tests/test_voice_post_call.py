"""Post-call automation: Bolna result → summary → one AI Call log (docs/13).

What the CRM does when Bolna reports a finished call:

    webhook ─▶ normalise ─▶ match the lead ─▶ store transcript/duration
            ─▶ summarise (Bolna's AI summary, or a safe fallback)
            ─▶ exactly one CALL_LOGGED action, `source: AI_CALL`, body = summary
            ─▶ extraction write-back (changed, meaningful values only)

The payloads are the shape Bolna documents for `GET /executions/{id}` — which
it says the webhook body matches — not the older guesses some fixtures in
`test_bolna_integration.py` use. Nothing here reaches Bolna.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import WorkspaceFixture, build_workspace, login

from app.auth.passwords import PasswordHasherService
from app.integrations.bolna import BolnaSettings, RecordingBolnaClient
from app.models.enums import StageKind, SystemActionKind, VoiceCallStatus
from app.models.lead import Action, Lead
from app.models.pipeline import Stage
from app.models.voice import VoiceCallExecution
from app.observability import redact_path
from app.services.voice_postcall import (
    FALLBACK_NO_SUMMARY,
    FALLBACK_NO_TRANSCRIPT,
    SUMMARY_SOURCE_AI,
    SUMMARY_SOURCE_FALLBACK,
    NormalizedCallResult,
    VendorCallSummarizer,
    normalise_execution,
    summarize_call,
)
from app.services.voice_settings import VOICE_OUTCOME_STAGES

FAKE_BOLNA_KEY = "bn-test-key-do-not-use-0000000000"
FAKE_AGENT_ID = "11111111-2222-3333-4444-555555555555"

NAME = "Perumal"
PHONE = "+919087822357"
EMAIL = "perumal@example.com"
AI_SUMMARY = (
    "Perumal confirmed his interest in Breakthrough Filmmaking. He asked about "
    "mentor support and requested a follow-up in the evening."
)
TRANSCRIPT = "assistant: Hi Perumal, calling from LevelUp.\nuser: Yes, I'm interested."


def _bolna_body(
    execution_id: str,
    *,
    status: str = "completed",
    to_number: str = PHONE,
    summary: str | None = AI_SUMMARY,
    transcript: str | None = TRANSCRIPT,
    duration: float | None = 154,
    recipient_data: dict[str, Any] | None = None,
    extracted_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bolna's documented execution object. Null where Bolna sends null."""
    body: dict[str, Any] = {
        "id": execution_id,
        "agent_id": FAKE_AGENT_ID,
        "status": status,
        "conversation_duration": duration,
        "total_cost": 3.2,
        "transcript": transcript,
        "user_number": to_number,
        "agent_number": "+918071580188",
        "extracted_data": extracted_data,
        "context_details": {"recipient_data": recipient_data} if recipient_data else None,
        "error_message": None,
        "answered_by_voice_mail": False,
        "created_at": "2026-09-19T10:00:00Z",
        "updated_at": "2026-09-19T10:02:34Z",
        "telephony_data": {
            "duration": str(duration) if duration is not None else None,
            "to_number": to_number,
            "from_number": "+918071580188",
            "call_type": "outbound",
            "provider": "vobiz",
            "hosted_telephony": True,
            "hangup_reason": "normal",
        },
    }
    if summary is not None:
        body["summary"] = summary
    return body


# --- fixtures -----------------------------------------------------------------


@pytest.fixture
def hasher(wired_app: FastAPI) -> PasswordHasherService:
    hasher = wired_app.state.password_hasher
    assert isinstance(hasher, PasswordHasherService)
    return hasher


@pytest.fixture
def bolna(wired_app: FastAPI) -> RecordingBolnaClient:
    client = RecordingBolnaClient()
    wired_app.state.bolna_client = client
    wired_app.state.bolna_settings = BolnaSettings(
        api_key=FAKE_BOLNA_KEY, base_url="https://api.bolna.invalid", agent_id=FAKE_AGENT_ID
    )
    return client


class _StubSummarizer:
    """Stands in for a summariser: returns `text`, or raises `error`."""

    def __init__(self, text: str | None = None, error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls = 0

    async def summarize(self, call: NormalizedCallResult) -> str | None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.text


@pytest.fixture
def install_summarizer(wired_app: FastAPI) -> Iterator[Any]:
    def install(summarizer: _StubSummarizer) -> _StubSummarizer:
        wired_app.state.call_summarizer = summarizer
        return summarizer

    yield install
    wired_app.state.call_summarizer = None


@pytest.fixture
async def ws(db_session: AsyncSession, hasher: PasswordHasherService) -> WorkspaceFixture:
    """Production's shape: the identity field is **Name**, Phone is separate."""
    fixture = await build_workspace(
        db_session, hasher, name="Post Call Co", owner_email="owner@postcall.example"
    )
    fixture.workspace.identity_field_id = fixture.fields["name"].id
    await db_session.commit()
    return fixture


async def _setup(api: AsyncClient, ws: WorkspaceFixture) -> str:
    await login(api, ws.owner)
    response = await api.post(
        ws.path("/settings/api-keys"),
        headers=ws.owner.auth,
        json={"name": "Bolna webhook", "permission_template_id": str(ws.templates["Root"].id)},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["key"])


async def _lead(
    api: AsyncClient, ws: WorkspaceFixture, *, name: str = NAME, phone: str = PHONE
) -> dict[str, Any]:
    response = await api.post(
        ws.path("/leads"),
        headers=ws.owner.auth,
        json={"values": {"name": name, "phone": phone, "email": EMAIL}},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def _trigger(api: AsyncClient, ws: WorkspaceFixture, lead_id: str) -> str:
    response = await api.post(
        ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": lead_id}
    )
    assert response.status_code == 200, response.text
    return str(response.json()["execution_id"])


async def _post(api: AsyncClient, key: str, body: Any) -> Any:
    """Exactly how Bolna calls the CRM: key in the URL, no headers."""
    return await api.post(f"/api/v1/voice/bolna/{key}", json=body)


async def _call_logs(session: AsyncSession, ws: WorkspaceFixture) -> list[Action]:
    rows = await session.execute(
        select(Action).where(
            Action.workspace_id == ws.id, Action.kind == SystemActionKind.CALL_LOGGED
        )
    )
    return list(rows.scalars().all())


async def _execution(session: AsyncSession, ws: WorkspaceFixture, external_id: str) -> Any:
    rows = await session.execute(
        select(VoiceCallExecution)
        .where(
            VoiceCallExecution.workspace_id == ws.id,
            VoiceCallExecution.external_id == external_id,
        )
        .execution_options(populate_existing=True)
    )
    return rows.scalar_one()


async def _values(session: AsyncSession, lead_id: str) -> dict[str, Any]:
    rows = await session.execute(
        select(Lead).where(Lead.id == uuid.UUID(lead_id)).execution_options(populate_existing=True)
    )
    return dict(rows.scalar_one().values or {})


# --- 1. the happy path ----------------------------------------------------------


@pytest.mark.integration
async def test_a_completed_call_becomes_one_ai_call_log_with_the_summary(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    response = await _post(api, key, _bolna_body(execution_id))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "accepted"
    assert body["lead_id"] == lead["id"]
    assert body["call_summary"] == AI_SUMMARY
    assert body["summary_source"] == SUMMARY_SOURCE_AI

    logs = await _call_logs(db_session, ws)
    assert len(logs) == 1
    log = logs[0]
    assert str(log.lead_id) == lead["id"]
    assert log.body == AI_SUMMARY
    assert log.payload["source"] == "AI_CALL"
    assert log.payload["execution_id"] == execution_id
    assert log.payload["call_status"] == "completed"
    assert log.payload["duration_seconds"] == 154
    assert log.payload["summary_source"] == SUMMARY_SOURCE_AI
    assert log.payload["has_transcript"] is True
    assert body["call_log_id"] == str(log.id)

    row = await _execution(db_session, ws, execution_id)
    assert row.status == VoiceCallStatus.COMPLETED
    assert row.transcript == TRANSCRIPT
    assert row.duration_seconds == 154
    assert row.summary == AI_SUMMARY
    assert row.summary_source == SUMMARY_SOURCE_AI
    assert row.summary_error is None
    assert row.webhook_received_at is not None
    assert row.call_action_id == log.id
    assert log.payload["call_id"] == str(row.id)

    # The continuity summary the next call carries is the AI summary.
    context = await api.get(ws.path(f"/voice/context/{lead['id']}"), headers=ws.owner.auth)
    assert context.json()["last_call_summary"] == AI_SUMMARY
    assert context.json()["call_count"] == 1

    # One call, one timeline entry: no duplicate NOTE carrying the same text.
    notes = await db_session.execute(
        select(Action).where(Action.workspace_id == ws.id, Action.kind == SystemActionKind.NOTE)
    )
    assert list(notes.scalars().all()) == []


@pytest.mark.integration
async def test_the_timeline_endpoint_returns_the_ai_call_log(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """What the frontend actually reads to draw the AI Call entry."""
    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])
    await _post(api, key, _bolna_body(execution_id))

    timeline = await api.get(ws.path(f"/leads/{lead['id']}/actions"), headers=ws.owner.auth)
    assert timeline.status_code == 200, timeline.text
    calls = [a for a in timeline.json()["items"] if a["kind"] == "CALL_LOGGED"]
    assert len(calls) == 1
    assert calls[0]["payload"]["source"] == "AI_CALL"
    assert calls[0]["payload"]["execution_id"] == execution_id
    assert calls[0]["body"] == AI_SUMMARY


# --- 2-5. authentication, malformed and unmatched deliveries ------------------


@pytest.mark.integration
async def test_a_bad_key_is_refused_and_writes_nothing(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    await _setup(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    response = await _post(api, "crmk_not-a-real-key-at-all", _bolna_body(execution_id))
    assert response.status_code == 401
    assert await _call_logs(db_session, ws) == []


@pytest.mark.integration
async def test_a_malformed_body_is_refused(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    key = await _setup(api, ws)
    as_list = await _post(api, key, [{"id": "x"}])
    assert as_list.status_code == 422

    not_json = await api.post(
        f"/api/v1/voice/bolna/{key}",
        content=b"this is not json",
        headers={"Content-Type": "application/json"},
    )
    assert not_json.status_code == 422


@pytest.mark.integration
async def test_bolnas_null_objects_are_accepted_not_rejected(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Bolna sends `extracted_data: null` and `context_details: null`.

    The schema used to 422 on that, which would have refused every real
    in-progress delivery.
    """
    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    in_progress = _bolna_body(execution_id, status="in-progress", summary=None, transcript=None)
    assert in_progress["extracted_data"] is None
    response = await _post(api, key, in_progress)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"


@pytest.mark.integration
async def test_a_payload_without_an_execution_id_is_refused(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    key = await _setup(api, ws)
    body = _bolna_body("ignored")
    del body["id"]
    response = await _post(api, key, body)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "missing_execution_id"


@pytest.mark.integration
async def test_an_unmatched_result_is_refused_with_a_reference_and_creates_nothing(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = await _setup(api, ws)
    await _lead(api, ws)

    with caplog.at_level(logging.WARNING, logger="app.services.voice_calls"):
        response = await _post(api, key, _bolna_body("never-seen", to_number="+919000000999"))
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "unknown_execution"
    assert detail["reference"] in caplog.text
    # Last four digits only in the log, never the full number.
    assert "+919000000999" not in caplog.text

    leads = await db_session.execute(select(Lead).where(Lead.workspace_id == ws.id))
    assert len(list(leads.scalars().all())) == 1
    assert await _call_logs(db_session, ws) == []


@pytest.mark.integration
async def test_auto_create_never_names_a_lead_after_a_phone_number(
    api: AsyncClient,
    wired_app: FastAPI,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """Even opted in, a Name-identity workspace cannot get a lead from a number.

    The old code wrote the number into the identity field — a lead called
    "+91…" — because it assumed the identity was the phone.
    """
    key = await _setup(api, ws)
    original = wired_app.state.settings.bolna_create_missing_leads
    wired_app.state.settings.bolna_create_missing_leads = True
    try:
        response = await _post(api, key, _bolna_body("new-number", to_number="+919000000555"))
    finally:
        wired_app.state.settings.bolna_create_missing_leads = original
    assert response.status_code == 422
    leads = await db_session.execute(select(Lead).where(Lead.workspace_id == ws.id))
    assert list(leads.scalars().all()) == []


# --- 6-7. matching the right lead ------------------------------------------------


@pytest.mark.integration
async def test_the_execution_id_wins_over_the_phone_number(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """The CRM recorded which lead it called; a payload's number cannot redirect it."""
    key = await _setup(api, ws)
    called = await _lead(api, ws)
    other = await _lead(api, ws, name="Someone Else", phone="+919000000111")
    execution_id = await _trigger(api, ws, called["id"])

    response = await _post(api, key, _bolna_body(execution_id, to_number="+919000000111"))
    assert response.status_code == 200, response.text
    assert response.json()["lead_id"] == called["id"]
    logs = await _call_logs(db_session, ws)
    assert [str(log.lead_id) for log in logs] == [called["id"]]
    assert other["id"] != called["id"]


@pytest.mark.integration
async def test_crm_lead_id_is_read_from_bolnas_recipient_data(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Bolna echoes `user_data` as `context_details.recipient_data`, not `user_data`."""
    key = await _setup(api, ws)
    lead = await _lead(api, ws)

    body = _bolna_body(
        "exec-not-yet-recorded",
        to_number="+919000000222",
        recipient_data={"crm_lead_id": lead["id"], "name": NAME},
    )
    response = await _post(api, key, body)
    assert response.status_code == 200, response.text
    assert response.json()["lead_id"] == lead["id"]


@pytest.mark.integration
async def test_the_phone_fallback_uses_the_phone_field_not_the_identity(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Identity is Name here; the number must still find the lead."""
    key = await _setup(api, ws)
    lead = await _lead(api, ws, phone="9087822357")  # stored normalised

    response = await _post(api, key, _bolna_body("dashboard-call", to_number=PHONE))
    assert response.status_code == 200, response.text
    assert response.json()["lead_id"] == lead["id"]


@pytest.mark.integration
async def test_an_ambiguous_phone_is_never_guessed(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Two leads share the number (legal when phone is not the identity)."""
    key = await _setup(api, ws)
    await _lead(api, ws, name="Twin One")
    await _lead(api, ws, name="Twin Two")

    response = await _post(api, key, _bolna_body("ambiguous", to_number=PHONE))
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unknown_execution"
    assert await _call_logs(db_session, ws) == []


# --- 8-12. transcript and summary ---------------------------------------------


@pytest.mark.integration
async def test_a_missing_transcript_gets_the_honest_fallback(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    response = await _post(api, key, _bolna_body(execution_id, summary=None, transcript=None))
    assert response.status_code == 200, response.text
    assert response.json()["call_summary"] == FALLBACK_NO_TRANSCRIPT
    assert response.json()["summary_source"] == SUMMARY_SOURCE_FALLBACK

    row = await _execution(db_session, ws, execution_id)
    assert row.transcript is None
    assert row.summary_error == "summary_unavailable"
    [log] = await _call_logs(db_session, ws)
    assert log.body == FALLBACK_NO_TRANSCRIPT

    # A placeholder is not continuity: the next call's prompt stays clean.
    context = await api.get(ws.path(f"/voice/context/{lead['id']}"), headers=ws.owner.auth)
    assert context.json()["last_call_summary"] is None


@pytest.mark.integration
async def test_a_transcript_without_a_summary_is_not_turned_into_one(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    response = await _post(api, key, _bolna_body(execution_id, summary=None))
    assert response.json()["call_summary"] == FALLBACK_NO_SUMMARY
    row = await _execution(db_session, ws, execution_id)
    assert row.transcript == TRANSCRIPT
    [log] = await _call_logs(db_session, ws)
    assert TRANSCRIPT not in (log.body or "")


@pytest.mark.integration
async def test_an_injected_summarizer_is_used(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    install_summarizer: Any,
) -> None:
    stub = install_summarizer(_StubSummarizer(text="  Confirmed interest;\n wants evening call. "))
    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    response = await _post(api, key, _bolna_body(execution_id))
    assert response.status_code == 200, response.text
    assert stub.calls == 1
    assert response.json()["call_summary"] == "Confirmed interest; wants evening call."
    assert response.json()["summary_source"] == SUMMARY_SOURCE_AI


@pytest.mark.integration
async def test_a_failing_summarizer_never_fails_the_webhook(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    install_summarizer: Any,
) -> None:
    install_summarizer(_StubSummarizer(error=RuntimeError("model said: " + TRANSCRIPT)))
    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    response = await _post(api, key, _bolna_body(execution_id))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"
    assert response.json()["call_summary"] == FALLBACK_NO_SUMMARY
    assert response.json()["summary_source"] == SUMMARY_SOURCE_FALLBACK

    row = await _execution(db_session, ws, execution_id)
    # The failure is recorded — by type only, never the message, which here
    # quotes the transcript.
    assert row.summary_error == "summarizer_failed: RuntimeError"
    assert row.transcript == TRANSCRIPT
    assert row.status == VoiceCallStatus.COMPLETED
    assert len(await _call_logs(db_session, ws)) == 1


# --- 13-14. idempotency -------------------------------------------------------------


@pytest.mark.integration
async def test_a_retried_delivery_changes_nothing(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    install_summarizer: Any,
) -> None:
    stub = install_summarizer(_StubSummarizer(text=AI_SUMMARY))
    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    first = await _post(api, key, _bolna_body(execution_id))
    retries = [await _post(api, key, _bolna_body(execution_id)) for _ in range(3)]

    assert first.json()["status"] == "accepted"
    for retry in retries:
        assert retry.status_code == 200
        assert retry.json()["status"] == "duplicate"
        # A duplicate still reports what the first delivery produced.
        assert retry.json()["call_summary"] == AI_SUMMARY
        assert retry.json()["call_log_id"] == first.json()["call_log_id"]

    assert len(await _call_logs(db_session, ws)) == 1
    assert stub.calls == 1  # summarised once, not per retry
    context = await api.get(ws.path(f"/voice/context/{lead['id']}"), headers=ws.owner.auth)
    assert context.json()["call_count"] == 1


# --- 15-16. extraction write-back through the post-call path ------------------------


@pytest.mark.integration
async def test_a_corrected_field_is_written_and_nothing_else_moves(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    mapping = await api.post(
        ws.path("/voice/extraction-mappings"),
        headers=ws.owner.auth,
        json={
            "disposition_name": "Corrected Email",
            "target_field_key": "email",
            "min_confidence": 0.7,
            "is_enabled": True,
        },
    )
    assert mapping.status_code == 201, mapping.text
    execution_id = await _trigger(api, ws, lead["id"])

    extracted = {"Corrected Email": {"value": "perumal.new@example.com", "confidence": 0.95}}
    response = await _post(api, key, _bolna_body(execution_id, extracted_data=extracted))
    assert response.status_code == 200, response.text
    assert response.json()["extraction_written"] == ["Corrected Email"]

    values = await _values(db_session, lead["id"])
    assert values["email"] == "perumal.new@example.com"
    assert values["name"] == NAME
    assert values["phone"].endswith("9087822357")


@pytest.mark.integration
async def test_empty_extracted_values_never_overwrite_the_crm(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    await api.post(
        ws.path("/voice/extraction-mappings"),
        headers=ws.owner.auth,
        json={
            "disposition_name": "Corrected Email",
            "target_field_key": "email",
            "min_confidence": 0.7,
            "is_enabled": True,
        },
    )
    execution_id = await _trigger(api, ws, lead["id"])

    extracted = {"Corrected Email": {"value": "  ", "confidence": 0.99}}
    response = await _post(api, key, _bolna_body(execution_id, extracted_data=extracted))
    assert response.status_code == 200, response.text
    assert response.json()["extraction_written"] == []
    assert (await _values(db_session, lead["id"]))["email"] == EMAIL


# --- 17. status handling --------------------------------------------------------------


@pytest.mark.integration
async def test_an_unanswered_call_is_logged_without_asking_for_a_summary(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    install_summarizer: Any,
) -> None:
    stub = install_summarizer(_StubSummarizer(text="should never be used"))
    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    response = await _post(
        api,
        key,
        _bolna_body(execution_id, status="no-answer", summary=None, transcript=None, duration=0),
    )
    assert response.status_code == 200, response.text
    assert response.json()["call_summary"] == (
        "AI call ended without a conversation (status: no-answer)."
    )
    assert stub.calls == 0
    row = await _execution(db_session, ws, execution_id)
    assert row.status == VoiceCallStatus.FAILED
    [log] = await _call_logs(db_session, ws)
    assert log.payload["call_status"] == "no-answer"


# --- production safety ------------------------------------------------------------------


@pytest.mark.integration
async def test_the_webhook_key_never_reaches_the_logs(
    api: AsyncClient,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The key is in the URL (Bolna allows nothing else); the logs must not be."""
    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    with caplog.at_level(logging.DEBUG):
        await _post(api, key, _bolna_body(execution_id))
    request_lines = [r.getMessage() for r in caplog.records if r.name == "api.request"]
    assert request_lines, "the request logger should have logged the webhook"
    assert all(key not in line for line in request_lines)
    assert any("/voice/bolna/<redacted>" in line for line in request_lines)
    # And the transcript is stored, not logged.
    assert TRANSCRIPT.splitlines()[1] not in caplog.text


def test_redact_path_and_the_access_log_filter() -> None:
    from app.observability import _RedactAccessLog

    assert redact_path("/api/v1/voice/bolna/crmk_abc123?x=1") == (
        "/api/v1/voice/bolna/<redacted>?x=1"
    )
    assert redact_path("/api/v1/workspaces/1/leads") == "/api/v1/workspaces/1/leads"

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "POST", "/api/v1/voice/bolna/crmk_secret", "1.1", 200),
        None,
    )
    assert _RedactAccessLog().filter(record)
    assert "crmk_secret" not in record.getMessage()


# --- the normaliser and summariser, as pure units -------------------------------------


def test_normalise_reads_bolnas_documented_shape() -> None:
    call = normalise_execution(
        _bolna_body("e-1", recipient_data={"crm_lead_id": "abc", "name": NAME})
    )
    assert call.execution_id == "e-1"
    assert call.status == "completed"
    assert call.succeeded and call.terminal and not call.failed
    assert call.recipient_phone == PHONE
    assert call.caller_phone == "+918071580188"
    assert call.direction == "OUTGOING"
    assert call.duration_seconds == 154
    assert call.transcript == TRANSCRIPT
    assert call.vendor_summary == AI_SUMMARY
    assert call.crm_lead_id == "abc"
    assert call.extracted_data == {}
    assert call.started_at is not None and call.ended_at is not None


def test_normalise_tolerates_a_bare_status_delivery() -> None:
    call = normalise_execution({"id": "e-2", "status": "queued", "telephony_data": None})
    assert call.terminal is False
    assert call.duration_seconds is None
    assert call.recipient_phone is None
    assert call.user_data == {}


def test_normalise_reads_the_duration_string_and_clamps() -> None:
    assert normalise_execution({"telephony_data": {"duration": "42"}}).duration_seconds == 42
    assert normalise_execution({"conversation_duration": -5}).duration_seconds == 0
    assert normalise_execution({"conversation_duration": 10**9}).duration_seconds == 86_400


async def test_summarize_prefers_the_vendor_summary_and_strips_its_label() -> None:
    call = normalise_execution({"id": "e", "status": "completed", "summary": "Summary: Hi."})
    result = await summarize_call(call, VendorCallSummarizer())
    assert result.text == "Hi."
    assert result.source == SUMMARY_SOURCE_AI


async def test_summarize_caps_a_runaway_summary() -> None:
    long = "Sentence one. " * 400
    call = normalise_execution({"id": "e", "status": "completed", "summary": long})
    result = await summarize_call(call, VendorCallSummarizer())
    assert len(result.text) <= 1_500
    assert result.text.endswith(".")


# --- the stage a finished call implies ----------------------------------------
#
# A workspace names its own stages, so the product cannot know which one means
# "we spoke to them". It is told: two stage ids in `workspace.settings`. Absent
# means the behaviour is off, which is what every existing workspace gets.
#
# The move only ever happens from the workspace's INITIAL stage, so a lead an
# operator has already worked is never dragged backwards — with one deliberate
# exception, the delayed delivery that overturns its own earlier verdict.


async def _stage_by_kind(session: AsyncSession, ws: WorkspaceFixture, kind: StageKind) -> Stage:
    rows = await session.execute(
        select(Stage).where(Stage.workspace_id == ws.id, Stage.kind == kind)
    )
    return rows.scalars().one()


async def _add_active_stage(api: AsyncClient, ws: WorkspaceFixture, label: str) -> str:
    """Through the admin endpoint, because only ACTIVE stages can be created."""
    response = await api.post(
        ws.path("/settings/stages"), headers=ws.owner.auth, json={"label": label}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _configure(
    session: AsyncSession,
    ws: WorkspaceFixture,
    *,
    answered: str | None,
    unanswered: str | None,
) -> None:
    mapping: dict[str, str] = {}
    if answered:
        mapping["answered"] = answered
    if unanswered:
        mapping["unanswered"] = unanswered
    ws.workspace.settings = {**(ws.workspace.settings or {}), VOICE_OUTCOME_STAGES: mapping}
    await session.commit()


async def _lead_stage(session: AsyncSession, lead_id: str) -> uuid.UUID | None:
    rows = await session.execute(select(Lead).where(Lead.id == uuid.UUID(lead_id)))
    return rows.scalars().one().stage_id


async def _set_stage(session: AsyncSession, lead_id: str, stage_id: str) -> None:
    rows = await session.execute(select(Lead).where(Lead.id == uuid.UUID(lead_id)))
    lead = rows.scalars().one()
    lead.stage_id = uuid.UUID(stage_id)
    await session.commit()


async def _stage_changes(session: AsyncSession, ws: WorkspaceFixture) -> list[Action]:
    rows = await session.execute(
        select(Action)
        .where(Action.workspace_id == ws.id, Action.kind == SystemActionKind.STAGE_CHANGE)
        .order_by(Action.created_at)
    )
    return list(rows.scalars().all())


@pytest.fixture
async def outcome_stages(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture
) -> dict[str, str]:
    """The stages these tests need, with the mapping configured.

    The provisioned set has one ACTIVE stage; the unanswered one does not exist
    and is created the way an admin would — which is also the proof that the
    product needs no such stage to exist by default.
    """
    await login(api, ws.owner)
    initial = await _stage_by_kind(db_session, ws, StageKind.INITIAL)
    rows = await db_session.execute(
        select(Stage).where(Stage.workspace_id == ws.id, Stage.kind == StageKind.ACTIVE)
    )
    answered = rows.scalars().first()
    assert answered is not None, "the provisioned set should contain one ACTIVE stage"
    unanswered = await _add_active_stage(api, ws, "Busy")
    await _configure(db_session, ws, answered=str(answered.id), unanswered=unanswered)
    return {
        "initial": str(initial.id),
        "answered": str(answered.id),
        "unanswered": unanswered,
        "won": str((await _stage_by_kind(db_session, ws, StageKind.WON)).id),
        "lost": str((await _stage_by_kind(db_session, ws, StageKind.LOST)).id),
    }


@pytest.mark.integration
async def test_a_new_lead_that_answered_moves_to_the_answered_stage(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    outcome_stages: dict[str, str],
) -> None:
    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    assert str(await _lead_stage(db_session, lead["id"])) == outcome_stages["initial"]

    execution_id = await _trigger(api, ws, lead["id"])
    # Triggering alone must not move it.
    assert str(await _lead_stage(db_session, lead["id"])) == outcome_stages["initial"]

    response = await _post(api, key, _bolna_body(execution_id, status="completed"))
    assert response.status_code == 200, response.text
    assert str(await _lead_stage(db_session, lead["id"])) == outcome_stages["answered"]

    # One STAGE_CHANGE, carrying the old and new ids (rule 5b).
    changes = await _stage_changes(db_session, ws)
    assert len(changes) == 1
    payload = changes[0].payload
    assert payload["old_stage_id"] == outcome_stages["initial"]
    assert payload["new_stage_id"] == outcome_stages["answered"]


@pytest.mark.integration
async def test_a_new_lead_that_did_not_answer_moves_to_the_unanswered_stage(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    outcome_stages: dict[str, str],
) -> None:
    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    response = await _post(
        api,
        key,
        _bolna_body(execution_id, status="no-answer", summary=None, transcript=None, duration=0),
    )
    assert response.status_code == 200, response.text
    assert str(await _lead_stage(db_session, lead["id"])) == outcome_stages["unanswered"]


@pytest.mark.integration
async def test_a_busy_status_is_also_unanswered(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    outcome_stages: dict[str, str],
) -> None:
    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    response = await _post(
        api,
        key,
        _bolna_body(execution_id, status="busy", summary=None, transcript=None, duration=0),
    )
    assert response.status_code == 200, response.text
    assert str(await _lead_stage(db_session, lead["id"])) == outcome_stages["unanswered"]


@pytest.mark.integration
async def test_a_lead_already_past_the_initial_stage_is_left_alone(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    outcome_stages: dict[str, str],
) -> None:
    """Already in the answered stage, and the call did not answer."""
    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    await _set_stage(db_session, lead["id"], outcome_stages["answered"])
    execution_id = await _trigger(api, ws, lead["id"])

    response = await _post(
        api,
        key,
        _bolna_body(execution_id, status="no-answer", summary=None, transcript=None, duration=0),
    )
    assert response.status_code == 200, response.text
    assert str(await _lead_stage(db_session, lead["id"])) == outcome_stages["answered"]
    assert await _stage_changes(db_session, ws) == []


@pytest.mark.integration
async def test_a_won_lead_is_never_moved_by_a_call(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    outcome_stages: dict[str, str],
) -> None:
    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    await _set_stage(db_session, lead["id"], outcome_stages["won"])
    execution_id = await _trigger(api, ws, lead["id"])

    response = await _post(api, key, _bolna_body(execution_id, status="completed"))
    assert response.status_code == 200, response.text
    assert str(await _lead_stage(db_session, lead["id"])) == outcome_stages["won"]
    assert await _stage_changes(db_session, ws) == []


@pytest.mark.integration
async def test_a_lost_lead_is_never_moved_by_a_call(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    outcome_stages: dict[str, str],
) -> None:
    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    await _set_stage(db_session, lead["id"], outcome_stages["lost"])
    execution_id = await _trigger(api, ws, lead["id"])

    response = await _post(api, key, _bolna_body(execution_id, status="completed"))
    assert response.status_code == 200, response.text
    assert str(await _lead_stage(db_session, lead["id"])) == outcome_stages["lost"]
    assert await _stage_changes(db_session, ws) == []


@pytest.mark.integration
async def test_a_delayed_delivery_corrects_unanswered_to_answered(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    outcome_stages: dict[str, str],
) -> None:
    """The hangup arrives first and looks unanswered; the completed result
    seconds later proves there was a conversation."""
    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    first = await _post(
        api,
        key,
        _bolna_body(
            execution_id, status="call-disconnected", summary=None, transcript=None, duration=0
        ),
    )
    assert first.status_code == 200, first.text
    assert str(await _lead_stage(db_session, lead["id"])) == outcome_stages["unanswered"]

    second = await _post(api, key, _bolna_body(execution_id, status="completed"))
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "upgraded"
    assert str(await _lead_stage(db_session, lead["id"])) == outcome_stages["answered"]

    # Two moves, the second correcting the first, each with its own old/new ids.
    changes = await _stage_changes(db_session, ws)
    assert [c.payload["new_stage_id"] for c in changes] == [
        outcome_stages["unanswered"],
        outcome_stages["answered"],
    ]
    assert changes[1].payload["old_stage_id"] == outcome_stages["unanswered"]


@pytest.mark.integration
async def test_no_mapping_means_no_stage_change(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """Every existing workspace. The call is still logged; nothing moves."""
    await login(api, ws.owner)
    initial = await _stage_by_kind(db_session, ws, StageKind.INITIAL)
    assert VOICE_OUTCOME_STAGES not in (ws.workspace.settings or {})

    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    response = await _post(api, key, _bolna_body(execution_id, status="completed"))
    assert response.status_code == 200, response.text
    assert await _lead_stage(db_session, lead["id"]) == initial.id
    assert await _stage_changes(db_session, ws) == []
    # And the call itself was still recorded.
    assert len(await _call_logs(db_session, ws)) == 1


@pytest.mark.integration
async def test_a_mapping_naming_another_workspaces_stage_is_ignored(
    api: AsyncClient,
    db_session: AsyncSession,
    hasher: PasswordHasherService,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """Scoping, on this path like every other: another tenant's id reads as absent."""
    other = await build_workspace(
        db_session, hasher, name="Other Stage Co", owner_email="owner@otherstage.example"
    )
    their_active = await _stage_by_kind(db_session, other, StageKind.ACTIVE)
    initial = await _stage_by_kind(db_session, ws, StageKind.INITIAL)
    await _configure(db_session, ws, answered=str(their_active.id), unanswered=None)

    await login(api, ws.owner)
    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    response = await _post(api, key, _bolna_body(execution_id, status="completed"))
    assert response.status_code == 200, response.text
    assert await _lead_stage(db_session, lead["id"]) == initial.id
    assert await _stage_changes(db_session, ws) == []


# --- the outcome has to be evidenced, not merely successful -------------------


@pytest.mark.integration
async def test_an_unreachable_number_moves_to_the_unanswered_stage(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    outcome_stages: dict[str, str],
) -> None:
    """A switched-off or unreachable number arrives as one of Bolna's failures."""
    key = await _setup(api, ws)
    for index, status in enumerate(("failed", "canceled", "balance-low")):
        lead = await _lead(api, ws, name=f"Unreachable {status}", phone=f"+91900001{index:04d}")
        execution_id = await _trigger(api, ws, lead["id"])
        response = await _post(
            api,
            key,
            _bolna_body(execution_id, status=status, summary=None, transcript=None, duration=0),
        )
        assert response.status_code == 200, response.text
        assert str(await _lead_stage(db_session, lead["id"])) == outcome_stages["unanswered"], (
            status
        )


@pytest.mark.integration
async def test_a_completed_call_with_no_conversation_evidence_moves_nothing(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    outcome_stages: dict[str, str],
) -> None:
    """An accepted API response is not proof that the caller spoke.

    `completed` with no transcript and no talk time satisfies `succeeded` but
    not `had_conversation`, so it is neither answered nor unanswered — and in
    particular must not be filed as unanswered.
    """
    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    response = await _post(
        api,
        key,
        _bolna_body(execution_id, status="completed", summary=None, transcript=None, duration=0),
    )
    assert response.status_code == 200, response.text
    assert str(await _lead_stage(db_session, lead["id"])) == outcome_stages["initial"]
    assert await _stage_changes(db_session, ws) == []
    # The call is still logged; only the stage move is withheld.
    assert len(await _call_logs(db_session, ws)) == 1


@pytest.mark.integration
async def test_an_unknown_status_carrying_a_summary_is_not_filed_as_unanswered(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    outcome_stages: dict[str, str],
) -> None:
    """`terminal` is also true for a status this release has never seen.

    That is a processing unknown, not evidence the caller could not be reached,
    so it must not become the unanswered stage.
    """
    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    response = await _post(
        api,
        key,
        _bolna_body(
            execution_id, status="some-status-we-have-never-seen", transcript=None, duration=0
        ),
    )
    assert response.status_code == 200, response.text
    assert str(await _lead_stage(db_session, lead["id"])) == outcome_stages["initial"]
    assert await _stage_changes(db_session, ws) == []


@pytest.mark.integration
async def test_a_summariser_failure_never_files_the_lead_as_unanswered(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    install_summarizer: Any,
    outcome_stages: dict[str, str],
) -> None:
    """A technical error on our side is not a failed phone call.

    The summariser raises; the conversation evidence is untouched, so the lead
    still moves to the answered stage.
    """
    install_summarizer(_StubSummarizer(error=RuntimeError("summariser down")))
    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    response = await _post(api, key, _bolna_body(execution_id, status="completed", summary=None))
    assert response.status_code == 200, response.text
    assert str(await _lead_stage(db_session, lead["id"])) == outcome_stages["answered"]


@pytest.mark.integration
async def test_a_duplicate_delivery_moves_nothing_a_second_time(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    outcome_stages: dict[str, str],
) -> None:
    """Idempotent: the same body twice leaves exactly one stage change."""
    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])
    body = _bolna_body(execution_id, status="completed")

    first = await _post(api, key, body)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "accepted"
    second = await _post(api, key, body)
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "duplicate"

    assert str(await _lead_stage(db_session, lead["id"])) == outcome_stages["answered"]
    assert len(await _stage_changes(db_session, ws)) == 1


@pytest.mark.integration
async def test_a_manual_move_is_not_overwritten_by_a_delayed_correction(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    outcome_stages: dict[str, str],
) -> None:
    """The correction only overturns this automation's own verdict.

    The hangup files the lead as unanswered; a person then moves it back to the
    unanswered stage deliberately. The later completed delivery must leave that
    human decision alone, because the newest stage change is now theirs.
    """
    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    first = await _post(
        api,
        key,
        _bolna_body(
            execution_id, status="call-disconnected", summary=None, transcript=None, duration=0
        ),
    )
    assert first.status_code == 200, first.text
    assert str(await _lead_stage(db_session, lead["id"])) == outcome_stages["unanswered"]

    # A person touches the stage: away and back, through the real endpoint, so
    # the newest STAGE_CHANGE carries their membership as the actor.
    for stage_id in (outcome_stages["answered"], outcome_stages["unanswered"]):
        patched = await api.patch(
            ws.path(f"/leads/{lead['id']}"), headers=ws.owner.auth, json={"stage_id": stage_id}
        )
        assert patched.status_code == 200, patched.text

    second = await _post(api, key, _bolna_body(execution_id, status="completed"))
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "upgraded"
    # Still where the person left it.
    assert str(await _lead_stage(db_session, lead["id"])) == outcome_stages["unanswered"]


@pytest.mark.integration
async def test_an_ambiguous_mapping_moves_nothing(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """Both outcomes pointed at one stage is a configuration mistake."""
    await login(api, ws.owner)
    initial = await _stage_by_kind(db_session, ws, StageKind.INITIAL)
    rows = await db_session.execute(
        select(Stage).where(Stage.workspace_id == ws.id, Stage.kind == StageKind.ACTIVE)
    )
    same = rows.scalars().first()
    assert same is not None
    await _configure(db_session, ws, answered=str(same.id), unanswered=str(same.id))

    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    response = await _post(api, key, _bolna_body(execution_id, status="completed"))
    assert response.status_code == 200, response.text
    assert await _lead_stage(db_session, lead["id"]) == initial.id
    assert await _stage_changes(db_session, ws) == []


@pytest.mark.integration
async def test_a_non_active_target_stage_is_refused(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """Won and lost are decisions about the deal, not records of a phone call."""
    await login(api, ws.owner)
    initial = await _stage_by_kind(db_session, ws, StageKind.INITIAL)
    won = await _stage_by_kind(db_session, ws, StageKind.WON)
    await _configure(db_session, ws, answered=str(won.id), unanswered=None)

    key = await _setup(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    response = await _post(api, key, _bolna_body(execution_id, status="completed"))
    assert response.status_code == 200, response.text
    assert await _lead_stage(db_session, lead["id"]) == initial.id
    assert await _stage_changes(db_session, ws) == []
