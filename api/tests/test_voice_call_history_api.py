"""Reading AI calls back out: `GET /voice/calls` and `/voice/calls/{id}`.

The reads behind the lead panel's AI Call Summary card and the Call Details
module (docs/13 §6). Every call here is created the way a real one is —
trigger, then webhook — so the rows under test are the rows production writes.

Two properties carry the weight:

- **isolation** — one lead's calls, one workspace's calls, one call's data,
  never mixed (`test_another_leads_call_is_not_in_this_leads_list`,
  `test_a_call_in_another_workspace_is_not_found`);
- **the raw payload is sanitised on the way out** — it is a vendor body kept
  verbatim in the database, and `test_credential_shaped_fields_are_redacted`
  is what stops one reaching a browser.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import WorkspaceFixture, add_member, build_workspace, login

from app.auth.passwords import PasswordHasherService
from app.integrations.bolna import BolnaSettings, RecordingBolnaClient
from app.services.voice_history import REDACTED, redact_payload

pytestmark = pytest.mark.integration

FAKE_BOLNA_KEY = "bn-test-key-do-not-use-0000000000"
FAKE_AGENT_ID = "11111111-2222-3333-4444-555555555555"

NAME = "Perumal"
PHONE = "+919087822357"
COURSE = "Breakthrough Filmmaking"

SUMMARY_ONE = "Perumal asked about the fee structure and requested a callback."
SUMMARY_TWO = "Perumal confirmed enrolment and will pay the booking amount."
TRANSCRIPT = "assistant: Hello Perumal.\nuser: Yes, I am interested."


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
    """Production's shape: identity is Name, and H2 is a customer field."""
    fixture = await build_workspace(
        db_session, hasher, name="Call History Co", owner_email="owner@callhistory.example"
    )
    fixture.workspace.identity_field_id = fixture.fields["name"].id
    await db_session.commit()
    await add_member(
        db_session,
        hasher,
        fixture,
        key="marketing",
        email="marketing@callhistory.example",
        template_name="Marketing",
    )
    return fixture


async def _setup(api: AsyncClient, ws: WorkspaceFixture, db_session: AsyncSession) -> str:
    """Log the owner in, give the workspace a Course field as H2, return a key.

    The Course dropdown is *fixture vocabulary*, created here the way an admin
    would create it — the product ships no such field, and the endpoints under
    test only ever speak of H1/H2.
    """
    await login(api, ws.owner)
    created = await api.post(
        ws.path("/settings/lead-fields"),
        headers=ws.owner.auth,
        json={"label": "Course", "field_type": "DROPDOWN"},
    )
    assert created.status_code == 201, created.text
    field = created.json()
    added = await api.post(
        ws.path(f"/settings/lead-fields/{field['id']}/options/bulk"),
        headers=ws.owner.auth,
        json={"labels": [COURSE]},
    )
    assert added.status_code in (200, 201), added.text

    ws.workspace.primary_field_1_id = ws.fields["name"].id
    ws.workspace.primary_field_2_id = uuid.UUID(field["id"])
    await db_session.commit()

    return await _api_key(api, ws)


async def _lead(
    api: AsyncClient, ws: WorkspaceFixture, *, name: str = NAME, phone: str = PHONE
) -> dict[str, Any]:
    response = await api.post(
        ws.path("/leads"),
        headers=ws.owner.auth,
        json={"values": {"name": name, "phone": phone, "course": "breakthrough_filmmaking"}},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def _api_key(api: AsyncClient, ws: WorkspaceFixture) -> str:
    response = await api.post(
        ws.path("/settings/api-keys"),
        headers=ws.owner.auth,
        json={"name": "Bolna webhook", "permission_template_id": str(ws.templates["Root"].id)},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["key"])


async def _run_call(
    api: AsyncClient,
    ws: WorkspaceFixture,
    key: str,
    lead_id: str,
    *,
    summary: str,
    duration: float = 117,
    transcript: str | None = TRANSCRIPT,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Trigger a call and complete it exactly as the vendor would."""
    trigger = await api.post(
        ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": lead_id}
    )
    assert trigger.status_code == 200, trigger.text
    execution_id = trigger.json()["execution_id"]

    body: dict[str, Any] = {
        "id": execution_id,
        "agent_id": FAKE_AGENT_ID,
        "status": "completed",
        "conversation_duration": duration,
        "transcript": transcript,
        "summary": summary,
        "user_number": PHONE,
        "extracted_data": {"General": {"Call Summary": {"subjective": summary}}},
        "telephony_data": {"to_number": PHONE, "call_type": "outbound", "duration": str(duration)},
    }
    if extra:
        body.update(extra)
    delivered = await api.post(f"/api/v1/voice/bolna/{key}", json=body)
    assert delivered.status_code == 200, delivered.text
    result: dict[str, Any] = delivered.json()
    assert result["status"] == "accepted"
    return result


# --- the lead card's read -----------------------------------------------------


async def test_the_most_recent_completed_call_is_first(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Three calls, newest first — the card takes item 0 and gets call #3."""
    key = await _setup(api, ws, db_session)
    lead = await _lead(api, ws)
    first = await _run_call(api, ws, key, lead["id"], summary=SUMMARY_ONE, duration=60)
    second = await _run_call(api, ws, key, lead["id"], summary=SUMMARY_TWO, duration=117)

    response = await api.get(
        ws.path("/voice/calls"),
        headers=ws.owner.auth,
        params={"lead_id": lead["id"], "completed_only": "true", "limit": 1},
    )
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 2
    assert len(page["items"]) == 1

    newest = page["items"][0]
    assert newest["id"] == second["call_id"]
    assert newest["id"] != first["call_id"]
    assert newest["summary"] == SUMMARY_TWO
    assert newest["summary_source"] == "AI"
    assert newest["duration_seconds"] == 117
    assert newest["status"] == "COMPLETED"
    assert newest["completed_at"] is not None


async def test_the_list_carries_the_workspaces_own_headline_fields(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Name and Course arrive as H1/H2 with their *configured* labels."""
    key = await _setup(api, ws, db_session)
    lead = await _lead(api, ws)
    await _run_call(api, ws, key, lead["id"], summary=SUMMARY_ONE)

    response = await api.get(ws.path("/voice/calls"), headers=ws.owner.auth)
    entry = response.json()["items"][0]["lead"]
    assert entry["lead_id"] == lead["id"]
    assert entry["identity_value"] == NAME
    assert entry["primary_h1"] == NAME
    assert entry["primary_h1_label"] == "Name"
    assert entry["primary_h2_label"] == "Course"


async def test_a_lead_with_no_calls_returns_an_empty_page(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """The card's empty state is a 200 with nothing in it, never an error."""
    await _setup(api, ws, db_session)
    lead = await _lead(api, ws)

    response = await api.get(
        ws.path("/voice/calls"),
        headers=ws.owner.auth,
        params={"lead_id": lead["id"], "completed_only": "true"},
    )
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}


async def test_an_unfinished_call_is_excluded_when_completed_only(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """A dispatched-but-never-finished call has no summary to show."""
    await _setup(api, ws, db_session)
    lead = await _lead(api, ws)
    triggered = await api.post(
        ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": lead["id"]}
    )
    assert triggered.status_code == 200, triggered.text

    finished = await api.get(
        ws.path("/voice/calls"),
        headers=ws.owner.auth,
        params={"lead_id": lead["id"], "completed_only": "true"},
    )
    assert finished.json()["total"] == 0

    everything = await api.get(
        ws.path("/voice/calls"), headers=ws.owner.auth, params={"lead_id": lead["id"]}
    )
    assert everything.json()["total"] == 1
    assert everything.json()["items"][0]["status"] == "DISPATCHED"


# --- the detail read ----------------------------------------------------------


async def test_one_call_returns_its_own_transcript_summary_and_payload(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    key = await _setup(api, ws, db_session)
    lead = await _lead(api, ws)
    call = await _run_call(api, ws, key, lead["id"], summary=SUMMARY_ONE)

    response = await api.get(ws.path(f"/voice/calls/{call['call_id']}"), headers=ws.owner.auth)
    assert response.status_code == 200, response.text
    detail = response.json()

    assert detail["id"] == call["call_id"]
    assert detail["execution_id"] == call["execution_id"]
    assert detail["lead"]["lead_id"] == lead["id"]
    assert detail["summary"] == SUMMARY_ONE
    assert detail["transcript"] == TRANSCRIPT
    assert detail["duration_seconds"] == 117
    assert detail["recipient_phone"] == PHONE
    assert detail["agent_id"] == FAKE_AGENT_ID
    assert detail["webhook_received_at"] is not None
    assert detail["call_log_id"] is not None
    # The extraction, as Bolna nests it.
    assert detail["extracted_data"]["General"]["Call Summary"]["subjective"] == SUMMARY_ONE
    # And the vendor body, whole enough to be evidence.
    assert detail["raw_payload"]["telephony_data"]["to_number"] == PHONE
    assert detail["raw_payload"]["status"] == "completed"


async def test_two_calls_on_one_lead_keep_their_own_data(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Call #2 must not overwrite call #1 — each keeps its own everything."""
    key = await _setup(api, ws, db_session)
    lead = await _lead(api, ws)
    first = await _run_call(
        api, ws, key, lead["id"], summary=SUMMARY_ONE, duration=60, transcript="assistant: one"
    )
    second = await _run_call(
        api, ws, key, lead["id"], summary=SUMMARY_TWO, duration=117, transcript="assistant: two"
    )

    one = (await api.get(ws.path(f"/voice/calls/{first['call_id']}"), headers=ws.owner.auth)).json()
    two = (
        await api.get(ws.path(f"/voice/calls/{second['call_id']}"), headers=ws.owner.auth)
    ).json()

    assert one["summary"] == SUMMARY_ONE and two["summary"] == SUMMARY_TWO
    assert one["transcript"] == "assistant: one" and two["transcript"] == "assistant: two"
    assert one["duration_seconds"] == 60 and two["duration_seconds"] == 117
    assert one["execution_id"] != two["execution_id"]
    assert one["raw_payload"]["summary"] != two["raw_payload"]["summary"]


async def test_a_nonexistent_call_is_not_found(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    await _setup(api, ws, db_session)
    response = await api.get(ws.path(f"/voice/calls/{uuid.uuid4()}"), headers=ws.owner.auth)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"


# --- isolation ----------------------------------------------------------------


async def test_another_leads_call_is_not_in_this_leads_list(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    key = await _setup(api, ws, db_session)
    perumal = await _lead(api, ws)
    other = await _lead(api, ws, name="Someone Else", phone="+919000000123")
    mine = await _run_call(api, ws, key, perumal["id"], summary=SUMMARY_ONE)
    theirs = await _run_call(api, ws, key, other["id"], summary="A different conversation.")

    response = await api.get(
        ws.path("/voice/calls"), headers=ws.owner.auth, params={"lead_id": perumal["id"]}
    )
    items = response.json()["items"]
    assert [item["id"] for item in items] == [mine["call_id"]]
    assert theirs["call_id"] not in [item["id"] for item in items]
    assert all(item["lead"]["lead_id"] == perumal["id"] for item in items)
    assert all(item["summary"] != "A different conversation." for item in items)


async def test_a_call_in_another_workspace_is_not_found(
    api: AsyncClient,
    db_session: AsyncSession,
    hasher: PasswordHasherService,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """The cross-workspace check every tenant endpoint gets."""
    key = await _setup(api, ws, db_session)
    lead = await _lead(api, ws)
    call = await _run_call(api, ws, key, lead["id"], summary=SUMMARY_ONE)

    other = await build_workspace(
        db_session, hasher, name="Rival Co", owner_email="owner@rival.example"
    )
    await login(api, other.owner)

    # Their workspace, our call id: not found.
    leaked = await api.get(other.path(f"/voice/calls/{call['call_id']}"), headers=other.owner.auth)
    assert leaked.status_code == 404
    # And their list is empty, not ours.
    listed = await api.get(other.path("/voice/calls"), headers=other.owner.auth)
    assert listed.json()["total"] == 0

    # Our own path still works with our own session.
    await login(api, ws.owner)
    ours = await api.get(ws.path(f"/voice/calls/{call['call_id']}"), headers=ws.owner.auth)
    assert ours.status_code == 200


async def test_a_template_without_call_history_is_refused(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Gated on the existing `calling.view_call_history`, like the context read."""
    key = await _setup(api, ws, db_session)
    lead = await _lead(api, ws)
    call = await _run_call(api, ws, key, lead["id"], summary=SUMMARY_ONE)

    await login(api, ws.members["marketing"])
    headers = ws.members["marketing"].auth
    listed = await api.get(ws.path("/voice/calls"), headers=headers)
    assert listed.status_code == 403
    assert listed.json()["detail"]["code"] == "insufficient_permissions"

    detail = await api.get(ws.path(f"/voice/calls/{call['call_id']}"), headers=headers)
    assert detail.status_code == 403


async def test_unauthenticated_reads_are_refused(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    await _setup(api, ws, db_session)
    response = await api.get(ws.path("/voice/calls"))
    assert response.status_code == 401


# --- the raw payload is safe to ship ------------------------------------------


async def test_credential_shaped_fields_are_redacted(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """An upstream that echoes secrets back must not reach the browser.

    The row keeps what arrived — it is evidence — and the *response* is what
    gets cleaned.
    """
    key = await _setup(api, ws, db_session)
    lead = await _lead(api, ws)
    call = await _run_call(
        api,
        ws,
        key,
        lead["id"],
        summary=SUMMARY_ONE,
        extra={
            "api_key": FAKE_BOLNA_KEY,
            "provider_auth": {"Authorization": f"Bearer {FAKE_BOLNA_KEY}"},
            "nested": [{"access_token": "abc123", "call_type": "outbound"}],
            "debug_note": f"key {FAKE_BOLNA_KEY} was used",
        },
    )

    detail = (
        await api.get(ws.path(f"/voice/calls/{call['call_id']}"), headers=ws.owner.auth)
    ).json()
    body = detail["raw_payload"]

    assert body["api_key"] == REDACTED
    assert body["provider_auth"] == REDACTED
    assert body["nested"][0]["access_token"] == REDACTED
    # The configured Bolna credential, even loose in prose.
    assert FAKE_BOLNA_KEY not in str(body)
    assert REDACTED in body["debug_note"]
    # Over-redaction would defeat the purpose: ordinary fields survive.
    assert body["nested"][0]["call_type"] == "outbound"
    assert body["telephony_data"]["to_number"] == PHONE
    # And nothing anywhere in the response carries the key.
    assert FAKE_BOLNA_KEY not in str(detail)


def test_the_redactor_on_its_own() -> None:
    out = redact_payload(
        {
            "ok": "keep me",
            "api_key": "secret",
            "deep": {"list": [{"Authorization": "Bearer x"}, {"fine": 1}]},
            "prose": "the value sk-live-123 appears here",
        },
        extra_secrets=("sk-live-123",),
    )
    assert out["ok"] == "keep me"
    assert out["api_key"] == REDACTED
    assert out["deep"]["list"][0]["Authorization"] == REDACTED
    assert out["deep"]["list"][1]["fine"] == 1
    assert "sk-live-123" not in out["prose"]
    assert REDACTED in out["prose"]


def test_the_redactor_cannot_recurse_forever() -> None:
    payload: dict[str, Any] = {}
    node = payload
    for _ in range(40):
        child: dict[str, Any] = {}
        node["next"] = child
        node = child
    node["leaf"] = "bottom"
    assert redact_payload(payload)  # does not raise
