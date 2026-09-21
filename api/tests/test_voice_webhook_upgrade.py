"""A call reported twice: `call-disconnected`, then `completed`.

Production, 2026-09-20, execution 308fc0a6. Bolna reported one 83-second
conversation in two deliveries four seconds apart:

    11:04:26  status=call-disconnected   transcript  (no summary, no extraction)
    11:04:30  status=completed           transcript  summary  extracted_data

The CRM read the first as a call that never connected — "AI call ended without
a conversation", status FAILED, extraction skipped — and set `completed_at`.
The second, which carried everything worth keeping, then hit the idempotency
gate and was discarded as a duplicate.

Two fixes, both pinned here:

- a disconnect **with conversation evidence** (a transcript, or talk time) is a
  completed call, not a failed one;
- a later terminal delivery carrying what the stored result lacks **upgrades**
  the same row and the same call log — never a second one — while a genuine
  duplicate stays a no-op.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import WorkspaceFixture, build_workspace, login

from app.auth.passwords import PasswordHasherService
from app.integrations.bolna import BolnaSettings, RecordingBolnaClient
from app.models.enums import SystemActionKind
from app.models.lead import Action
from app.services.voice_postcall import (
    FALLBACK_FAILED,
    FALLBACK_NO_SUMMARY,
    VendorCallSummarizer,
    normalise_execution,
    summarize_call,
)

FAKE_BOLNA_KEY = "bn-test-key-do-not-use-0000000000"
FAKE_AGENT_ID = "11111111-2222-3333-4444-555555555555"

NAME = "Test Customer"
PHONE = "+919000000002"
EMAIL = "test.customer@example.com"
TRANSCRIPT = "assistant: Hi, is now a good time?\nuser: Yes. What is the fee?"
AI_SUMMARY = "The customer confirmed enrolment and asked for the fee structure."
EXTRACTED = {"General": {"Call Summary": {"subjective": AI_SUMMARY}}}


def _disconnected(execution_id: str, **overrides: Any) -> dict[str, Any]:
    """Bolna's first delivery: at hangup, before post-call processing."""
    body: dict[str, Any] = {
        "id": execution_id,
        "agent_id": FAKE_AGENT_ID,
        "status": "call-disconnected",
        "conversation_duration": 83.0,
        "transcript": TRANSCRIPT,
        "summary": None,
        "extracted_data": None,
        "user_number": PHONE,
        "telephony_data": {
            "to_number": PHONE,
            "call_type": "outbound",
            "duration": 83.0,
            "hangup_by": "Callee",
            "hangup_reason": "Call recipient hungup",
        },
    }
    body.update(overrides)
    return body


def _completed(execution_id: str, **overrides: Any) -> dict[str, Any]:
    """Bolna's second delivery: the authoritative result."""
    body = _disconnected(execution_id)
    body.update({"status": "completed", "summary": AI_SUMMARY, "extracted_data": EXTRACTED})
    body.update(overrides)
    return body


# --- classification, on its own -------------------------------------------------


def test_a_disconnect_with_a_transcript_is_a_completed_call() -> None:
    call = normalise_execution(_disconnected("e"))
    assert call.had_conversation
    assert call.succeeded
    assert not call.failed
    assert call.terminal


def test_a_disconnect_with_talk_time_alone_is_a_completed_call() -> None:
    call = normalise_execution(_disconnected("e", transcript=None))
    assert call.had_conversation
    assert call.succeeded and not call.failed


def test_a_disconnect_with_no_conversation_is_still_a_failure() -> None:
    call = normalise_execution(
        _disconnected("e", transcript=None, conversation_duration=0, telephony_data={})
    )
    assert not call.had_conversation
    assert call.failed and not call.succeeded
    assert call.terminal


def test_busy_and_no_answer_are_unaffected() -> None:
    for status in ("busy", "no-answer"):
        call = normalise_execution({"id": "e", "status": status, "conversation_duration": 0})
        assert call.failed and not call.succeeded


async def test_the_no_conversation_fallback_is_never_claimed_over_a_transcript() -> None:
    """The exact sentence production showed must not appear with a transcript."""
    call = normalise_execution(_disconnected("e"))
    result = await summarize_call(call, VendorCallSummarizer())
    assert "without a conversation" not in result.text
    assert result.text == FALLBACK_NO_SUMMARY

    silent = normalise_execution(
        _disconnected("e", transcript=None, conversation_duration=0, telephony_data={})
    )
    result = await summarize_call(silent, VendorCallSummarizer())
    assert result.text == FALLBACK_FAILED.format(status="call-disconnected")


# --- through the webhook ---------------------------------------------------------


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


@pytest.fixture
async def ws(db_session: AsyncSession, hasher: PasswordHasherService) -> WorkspaceFixture:
    fixture = await build_workspace(
        db_session, hasher, name="Upgrade Co", owner_email="owner@upgrade.example"
    )
    fixture.workspace.identity_field_id = fixture.fields["name"].id
    await db_session.commit()
    return fixture


async def _key(api: AsyncClient, ws: WorkspaceFixture) -> str:
    await login(api, ws.owner)
    response = await api.post(
        ws.path("/settings/api-keys"),
        headers=ws.owner.auth,
        json={"name": "Bolna webhook", "permission_template_id": str(ws.templates["Root"].id)},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["key"])


async def _lead(api: AsyncClient, ws: WorkspaceFixture) -> dict[str, Any]:
    response = await api.post(
        ws.path("/leads"),
        headers=ws.owner.auth,
        json={"values": {"name": NAME, "phone": PHONE, "email": EMAIL}},
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


async def _deliver(api: AsyncClient, key: str, body: dict[str, Any]) -> dict[str, Any]:
    response = await api.post(f"/api/v1/voice/bolna/{key}", json=body)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


async def _detail(api: AsyncClient, ws: WorkspaceFixture, call_id: str) -> dict[str, Any]:
    response = await api.get(ws.path(f"/voice/calls/{call_id}"), headers=ws.owner.auth)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _call_logs(session: AsyncSession, ws: WorkspaceFixture) -> list[Action]:
    rows = await session.execute(
        select(Action)
        .where(Action.workspace_id == ws.id, Action.kind == SystemActionKind.CALL_LOGGED)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


async def _context(api: AsyncClient, ws: WorkspaceFixture, lead_id: str) -> dict[str, Any]:
    response = await api.get(ws.path(f"/voice/context/{lead_id}"), headers=ws.owner.auth)
    body: dict[str, Any] = response.json()
    return body


@pytest.mark.integration
async def test_the_production_sequence_upgrades_one_call(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """call-disconnected → completed, exactly as production delivered it."""
    key = await _key(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    # 1. The hangup notice: a completed conversation, not a failed call.
    first = await _deliver(api, key, _disconnected(execution_id))
    assert first["status"] == "accepted"
    assert first["summary_source"] == "FALLBACK"
    assert "without a conversation" not in first["call_summary"]
    after_first = await _detail(api, ws, first["call_id"])
    assert after_first["status"] == "COMPLETED"
    assert after_first["transcript"] == TRANSCRIPT
    assert after_first["duration_seconds"] == 83
    assert after_first["extractions"] == []

    # 2. The authoritative result: upgrades, rather than being dropped.
    second = await _deliver(api, key, _completed(execution_id))
    assert second["status"] == "upgraded"
    assert second["call_id"] == first["call_id"]
    assert second["call_log_id"] == first["call_log_id"]
    assert second["call_summary"] == AI_SUMMARY
    assert second["summary_source"] == "AI"

    detail = await _detail(api, ws, first["call_id"])
    assert detail["summary"] == AI_SUMMARY
    assert detail["summary_source"] == "AI"
    assert detail["status"] == "COMPLETED"
    assert detail["bolna_status"] == "completed"
    # Preserved from the first delivery, not lost in the upgrade.
    assert detail["transcript"] == TRANSCRIPT
    assert detail["duration_seconds"] == 83
    # The nested extraction, now present and flattened.
    [item] = detail["extractions"]
    assert item["path"] == "General / Call Summary"
    assert item["value"] == AI_SUMMARY

    # Exactly one call, one timeline entry — updated in place.
    [log] = await _call_logs(db_session, ws)
    assert str(log.id) == first["call_log_id"]
    assert log.body == AI_SUMMARY
    assert log.payload["summary_source"] == "AI"
    assert log.payload["call_status"] == "completed"
    assert log.payload["duration_seconds"] == 83
    assert log.payload["source"] == "AI_CALL"

    # And the lead's continuity summary is the real one.
    context = await _context(api, ws, lead["id"])
    assert context["last_call_summary"] == AI_SUMMARY
    assert context["call_count"] == 1


@pytest.mark.integration
async def test_the_completed_result_redelivered_after_an_upgrade_is_a_duplicate(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    key = await _key(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    await _deliver(api, key, _disconnected(execution_id))
    await _deliver(api, key, _completed(execution_id))
    for _ in range(3):
        again = await _deliver(api, key, _completed(execution_id))
        assert again["status"] == "duplicate"
        assert again["call_summary"] == AI_SUMMARY

    assert len(await _call_logs(db_session, ws)) == 1
    assert (await _context(api, ws, lead["id"]))["call_count"] == 1


@pytest.mark.integration
async def test_completed_then_completed_again_is_a_plain_duplicate(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """The ordinary retry, with no disconnect first. Unchanged behaviour."""
    key = await _key(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    first = await _deliver(api, key, _completed(execution_id))
    assert first["status"] == "accepted"
    before = await _detail(api, ws, first["call_id"])

    again = await _deliver(api, key, _completed(execution_id))
    assert again["status"] == "duplicate"

    after = await _detail(api, ws, first["call_id"])
    for field in ("summary", "summary_source", "transcript", "duration_seconds", "status"):
        assert after[field] == before[field], field
    assert len(await _call_logs(db_session, ws)) == 1


@pytest.mark.integration
async def test_a_late_disconnect_never_downgrades_a_completed_call(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Out of order the other way: the poorer delivery arrives last."""
    key = await _key(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    first = await _deliver(api, key, _completed(execution_id))
    late = await _deliver(api, key, _disconnected(execution_id))
    assert late["status"] == "duplicate"

    detail = await _detail(api, ws, first["call_id"])
    assert detail["summary"] == AI_SUMMARY
    assert detail["summary_source"] == "AI"
    assert detail["status"] == "COMPLETED"
    assert detail["bolna_status"] == "completed"
    assert len(detail["extractions"]) == 1
    assert len(await _call_logs(db_session, ws)) == 1


@pytest.mark.integration
async def test_a_disconnect_with_no_conversation_is_logged_as_a_failed_call(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    key = await _key(api, ws)
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    result = await _deliver(
        api,
        key,
        _disconnected(execution_id, transcript=None, conversation_duration=0, telephony_data={}),
    )
    assert result["status"] == "accepted"
    assert result["call_summary"] == FALLBACK_FAILED.format(status="call-disconnected")

    detail = await _detail(api, ws, result["call_id"])
    assert detail["status"] == "FAILED"
    assert detail["transcript"] is None
    assert len(await _call_logs(db_session, ws)) == 1


@pytest.mark.integration
async def test_a_mapped_extraction_is_applied_once_across_the_upgrade(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """The extraction arrives only with the second delivery, and writes once."""
    key = await _key(api, ws)
    mapping = await api.post(
        ws.path("/voice/extraction-mappings"),
        headers=ws.owner.auth,
        json={
            "disposition_name": "Corrected Email",
            "target_field_key": "email",
            "min_confidence": 0.5,
            "is_enabled": True,
        },
    )
    assert mapping.status_code == 201, mapping.text
    lead = await _lead(api, ws)
    execution_id = await _trigger(api, ws, lead["id"])

    extracted = {
        "General": {
            "Call Summary": {"subjective": AI_SUMMARY},
            "Corrected Email": {"value": "corrected@example.com", "confidence": 0.95},
        }
    }
    first = await _deliver(api, key, _disconnected(execution_id))
    assert first["extraction_written"] == []

    second = await _deliver(api, key, _completed(execution_id, extracted_data=extracted))
    assert second["status"] == "upgraded"
    assert second["extraction_written"] == ["General / Corrected Email"]

    again = await _deliver(api, key, _completed(execution_id, extracted_data=extracted))
    assert again["status"] == "duplicate"

    lead_after = (await api.get(ws.path(f"/leads/{lead['id']}"), headers=ws.owner.auth)).json()
    assert lead_after["values"]["email"] == "corrected@example.com"

    # One field change for the email, not two.
    rows = await db_session.execute(
        select(Action).where(
            Action.workspace_id == ws.id, Action.kind == SystemActionKind.FIELD_CHANGE
        )
    )
    email_changes = [a for a in rows.scalars().all() if a.payload.get("field_key") == "email"]
    assert len(email_changes) == 1


@pytest.mark.integration
async def test_an_upgrade_to_an_older_call_does_not_roll_back_continuity(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Call A's full result lands after call B completed. B keeps continuity."""
    key = await _key(api, ws)
    lead = await _lead(api, ws)

    call_a = await _trigger(api, ws, lead["id"])
    first_a = await _deliver(api, key, _disconnected(call_a))

    call_b = await _trigger(api, ws, lead["id"])
    await _deliver(api, key, _completed(call_b, summary="Call B's own summary."))

    upgraded = await _deliver(api, key, _completed(call_a, summary="Call A's late summary."))
    assert upgraded["status"] == "upgraded"

    # A's own record gets A's summary…
    assert (await _detail(api, ws, first_a["call_id"]))["summary"] == "Call A's late summary."
    # …but the lead's continuity stays with the newer call.
    context = await _context(api, ws, lead["id"])
    assert context["last_call_summary"] == "Call B's own summary."
    # Two calls, two logs — not three.
    assert len(await _call_logs(db_session, ws)) == 2
