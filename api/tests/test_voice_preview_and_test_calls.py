"""Personalised calls you can check before anyone is called (docs/15).

Three things, tested end to end through the API:

1. **Every field reaches the agent.** Each lead field the caller may view is
   sent on every call — empty when the lead has no value — so the prompt never
   holds a raw `{age}` for the model to fill in. A field the caller may *not*
   view is still absent, as it always was.
2. **Preview.** `POST /voice/calls/preview` returns exactly what a real call
   would send, and writes and dials nothing.
3. **Test calls.** `test_phone` dials the tester instead of the lead, with the
   lead's full context. Recorded, remembered and charged like any call — but it
   never moves the lead's stage, because it was not the lead on the phone.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import WorkspaceFixture, add_member, build_workspace, login

from app.auth.passwords import PasswordHasherService
from app.integrations.bolna import BolnaSettings, RecordingBolnaClient
from app.models.billing import WalletTransaction
from app.models.enums import WalletTransactionKind
from app.models.voice import VoiceCallExecution
from app.services.voice_variables import RESERVED_VARIABLES
from app.services.wallet import WalletService
from app.tenancy.session import ScopedSession

FAKE_AGENT_ID = "11111111-2222-3333-4444-555555555555"
LEAD_PHONE = "+919087822357"
TESTER_TYPED = "90000 12345"  # as a person types it
TESTER_E164 = "+919000012345"  # after the workspace's country code (91)


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
        api_key="bn-test-key-do-not-use-0000000000",
        base_url="https://api.bolna.invalid",
        agent_id=FAKE_AGENT_ID,
    )
    return client


@pytest.fixture
async def ws(db_session: AsyncSession, hasher: PasswordHasherService) -> WorkspaceFixture:
    fixture = await build_workspace(
        db_session, hasher, name="Preview Co", owner_email="owner@preview.example"
    )
    fixture.workspace.identity_field_id = fixture.fields["name"].id
    fixture.workspace.default_country_code = "91"
    await db_session.commit()
    return fixture


async def _field(api: AsyncClient, ws: WorkspaceFixture, label: str, kind: str) -> dict[str, Any]:
    response = await api.post(
        ws.path("/settings/lead-fields"),
        headers=ws.owner.auth,
        json={"label": label, "field_type": kind},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def _lead(api: AsyncClient, ws: WorkspaceFixture, **values: Any) -> str:
    response = await api.post(
        ws.path("/leads"),
        headers=ws.owner.auth,
        json={"values": {"name": "Perumal", "phone": LEAD_PHONE, **values}},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _setup(api: AsyncClient, ws: WorkspaceFixture) -> tuple[str, str]:
    """A workspace with Program (filled) and Age (left empty) fields, and a lead."""
    await login(api, ws.owner)
    program = await _field(api, ws, "Program", "TEXT")
    age = await _field(api, ws, "Age", "NUMBER")
    lead_id = await _lead(api, ws, **{program["key"]: "BFP"})
    return lead_id, age["key"]


# --- 1. every field reaches the agent ------------------------------------------


@pytest.mark.integration
async def test_an_empty_field_is_sent_blank_not_left_out(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    lead_id, age_key = await _setup(api, ws)

    response = await api.post(
        ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": lead_id}
    )

    assert response.status_code == 200, response.text
    sent = bolna.calls[-1].user_data
    assert sent["program"] == "BFP"
    assert sent[age_key] == ""  # present and empty: "not known", never "{age}"
    assert sent["name"] == "Perumal"


@pytest.mark.integration
async def test_a_field_the_caller_cannot_view_is_still_never_sent(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    hasher: PasswordHasherService,
    bolna: RecordingBolnaClient,
) -> None:
    """Blank-filling must not become a way around field permissions."""
    lead_id, age_key = await _setup(api, ws)
    fields = (await api.get(ws.path("/settings/lead-fields"), headers=ws.owner.auth)).json()
    age_id = next(f["id"] for f in fields if f["key"] == age_key)
    caller_template = ws.templates["Caller"].id
    revoke = await api.put(
        ws.path(f"/settings/permission-templates/{caller_template}/field-grants/bulk"),
        headers=ws.owner.auth,
        json={"grant": "VIEW", "value": False, "field_ids": [age_id]},
    )
    assert revoke.status_code == 200, revoke.text
    caller = await add_member(
        db_session, hasher, ws, key="caller", email="caller@preview.example", template_name="Caller"
    )
    await login(api, caller)
    # The lead must be one the caller can see.
    assign = await api.patch(
        ws.path(f"/leads/{lead_id}"),
        headers=ws.owner.auth,
        json={"assignee_id": str(caller.membership.id)},
    )
    assert assign.status_code == 200, assign.text

    preview = await api.post(
        ws.path("/voice/calls/preview"), headers=caller.auth, json={"lead_id": lead_id}
    )

    assert preview.status_code == 200, preview.text
    assert age_key not in preview.json()["user_data"]
    variables = await api.get(ws.path("/voice/variables"), headers=caller.auth)
    assert age_key not in {v["name"] for v in variables.json()}


# --- 2. preview ------------------------------------------------------------------


@pytest.mark.integration
async def test_the_preview_is_exactly_what_a_real_call_sends(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    lead_id, age_key = await _setup(api, ws)

    preview = await api.post(
        ws.path("/voice/calls/preview"), headers=ws.owner.auth, json={"lead_id": lead_id}
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()

    # Nothing happened.
    assert bolna.calls == []
    rows = await db_session.execute(select(func.count()).select_from(VoiceCallExecution))
    assert rows.scalar() == 0

    # And it matches the call that is then placed, apart from the per-call key.
    real = await api.post(ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": lead_id})
    assert real.status_code == 200, real.text
    sent = dict(bolna.calls[-1].user_data)
    shown = dict(body["user_data"])
    assert shown.pop("crm_idempotency") == "(set when the call is placed)"
    sent.pop("crm_idempotency")
    assert shown == sent

    assert age_key in body["blank_variables"]
    assert "program" not in body["blank_variables"]
    assert body["recipient_phone"] == LEAD_PHONE
    assert body["agent_id"] == FAKE_AGENT_ID
    assert body["call_count"] == 0


@pytest.mark.integration
async def test_a_preview_works_before_bolna_is_configured(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    """Looking costs nothing and needs nothing: no key, no agent, no wallet."""
    lead_id, _ = await _setup(api, ws)
    preview = await api.post(
        ws.path("/voice/calls/preview"), headers=ws.owner.auth, json={"lead_id": lead_id}
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["agent_id"] is None


# --- 3. test calls ---------------------------------------------------------------


async def _two_active_stages(api: AsyncClient, ws: WorkspaceFixture) -> tuple[str, str]:
    """Configure "answered"/"unanswered" stages so a real call *would* move."""
    stages = (await api.get(ws.path("/settings/stages"), headers=ws.owner.auth)).json()
    answered = stages["active"][0]["id"]
    made = await api.post(
        ws.path("/settings/stages"), headers=ws.owner.auth, json={"label": "Not reached"}
    )
    assert made.status_code in (200, 201), made.text
    unanswered = made.json()["id"]
    put = await api.put(
        ws.path("/settings/voice-outcome-stages"),
        headers=ws.owner.auth,
        json={"answered": answered, "unanswered": unanswered},
    )
    assert put.status_code == 200, put.text
    return answered, stages["initial"]["id"]


async def _webhook_key(api: AsyncClient, ws: WorkspaceFixture) -> str:
    response = await api.post(
        ws.path("/settings/api-keys"),
        headers=ws.owner.auth,
        json={"name": "Bolna webhook", "permission_template_id": str(ws.templates["Root"].id)},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["key"])


def _completed(execution_id: str, to_number: str) -> dict[str, Any]:
    return {
        "id": execution_id,
        "agent_id": FAKE_AGENT_ID,
        "status": "completed",
        "conversation_duration": 75,
        "transcript": "assistant: Hello?\nuser: Yes, it's me.",
        "summary": "Tester played the lead and asked about the fee.",
        "user_number": to_number,
        "telephony_data": {"to_number": to_number, "call_type": "outbound"},
    }


@pytest.mark.integration
async def test_a_test_call_dials_the_tester_with_the_leads_context(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    lead_id, _ = await _setup(api, ws)

    response = await api.post(
        ws.path("/voice/calls"),
        headers=ws.owner.auth,
        json={"lead_id": lead_id, "test_phone": TESTER_TYPED},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_test"] is True
    assert body["recipient_phone"] == TESTER_E164
    request = bolna.calls[-1]
    assert request.recipient_phone_number == TESTER_E164  # the tester, not the lead
    assert request.user_data["name"] == "Perumal"  # ... hearing the lead's call
    assert request.user_data["program"] == "BFP"


@pytest.mark.integration
async def test_a_test_call_is_recorded_and_remembered_but_never_moves_the_stage(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    lead_id, _ = await _setup(api, ws)
    answered, initial = await _two_active_stages(api, ws)
    key = await _webhook_key(api, ws)

    test = await api.post(
        ws.path("/voice/calls"),
        headers=ws.owner.auth,
        json={"lead_id": lead_id, "test_phone": TESTER_TYPED},
    )
    delivered = await api.post(
        f"/api/v1/voice/bolna/{key}", json=_completed(test.json()["execution_id"], TESTER_E164)
    )
    assert delivered.status_code == 200, delivered.text
    assert delivered.json()["status"] == "accepted"

    lead = (await api.get(ws.path(f"/leads/{lead_id}"), headers=ws.owner.auth)).json()
    assert lead["stage_id"] == initial, "a test call moved the lead's stage"

    calls = (
        await api.get(ws.path("/voice/calls"), headers=ws.owner.auth, params={"lead_id": lead_id})
    ).json()
    assert [c["is_test"] for c in calls["items"]] == [True]
    timeline = (await api.get(ws.path(f"/leads/{lead_id}/actions"), headers=ws.owner.auth)).json()
    [log] = [a for a in timeline["items"] if a["kind"] == "CALL_LOGGED"]
    assert log["payload"]["test"] is True

    # Remembered: the next preview carries this call's summary, which is how a
    # second test call proves the agent's memory.
    preview = await api.post(
        ws.path("/voice/calls/preview"), headers=ws.owner.auth, json={"lead_id": lead_id}
    )
    assert preview.json()["call_count"] == 1
    assert "asked about the fee" in preview.json()["user_data"]["crm_last_call_summary"]

    # The contrast: the same result on a real call does move the stage.
    other = await _lead(api, ws, phone="+919000000001", name="Other")
    real_other = await api.post(
        ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": other}
    )
    await api.post(
        f"/api/v1/voice/bolna/{key}",
        json=_completed(real_other.json()["execution_id"], "+919000000001"),
    )
    moved = (await api.get(ws.path(f"/leads/{other}"), headers=ws.owner.auth)).json()
    assert moved["stage_id"] == answered


@pytest.mark.integration
async def test_a_test_call_costs_wallet_minutes_like_any_call(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Bolna bills it, so the wallet does too — testing is not free airtime."""
    service = WalletService(ScopedSession(db_session, ws.id), workspace=ws.workspace)
    await service.create(rate_per_minute=Decimal("10"))
    await service.record(WalletTransactionKind.TOPUP, Decimal("100"), note=None, recorded_by="t")
    await db_session.commit()
    lead_id, _ = await _setup(api, ws)
    key = await _webhook_key(api, ws)

    test = await api.post(
        ws.path("/voice/calls"),
        headers=ws.owner.auth,
        json={"lead_id": lead_id, "test_phone": TESTER_TYPED},
    )
    await api.post(
        f"/api/v1/voice/bolna/{key}", json=_completed(test.json()["execution_id"], TESTER_E164)
    )

    rows = await db_session.execute(
        select(WalletTransaction).where(
            WalletTransaction.workspace_id == ws.id,
            WalletTransaction.kind == WalletTransactionKind.CALL_CHARGE,
        )
    )
    [charge] = rows.scalars().all()
    assert charge.amount == Decimal("-20.00")  # 75s = 2 started minutes x 10


@pytest.mark.integration
@pytest.mark.parametrize("typed", ["not a number", "12"])
async def test_an_unusable_test_number_is_refused_before_dialling(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient, typed: str
) -> None:
    lead_id, _ = await _setup(api, ws)
    response = await api.post(
        ws.path("/voice/calls"),
        headers=ws.owner.auth,
        json={"lead_id": lead_id, "test_phone": typed},
    )
    assert response.status_code == 422, response.text
    assert bolna.calls == []


@pytest.mark.integration
async def test_a_lead_with_no_phone_can_still_be_test_called(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """A made-up test lead needs no number of its own; the tester's is used."""
    await login(api, ws.owner)
    made = await api.post(
        ws.path("/leads"), headers=ws.owner.auth, json={"values": {"name": "Test Lead"}}
    )
    assert made.status_code == 201, made.text
    response = await api.post(
        ws.path("/voice/calls"),
        headers=ws.owner.auth,
        json={"lead_id": made.json()["id"], "test_phone": TESTER_TYPED},
    )
    assert response.status_code == 200, response.text
    assert bolna.calls[-1].recipient_phone_number == TESTER_E164


# --- the catalogue agrees with what is sent ---------------------------------------


@pytest.mark.integration
async def test_every_variable_a_call_sends_is_in_the_catalogue(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """The Settings page lists what the agent can use. If a call ever sends a
    key the page does not list, the script and the CRM have drifted apart."""
    lead_id, age_key = await _setup(api, ws)
    key = await _webhook_key(api, ws)
    first = await api.post(
        ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": lead_id}
    )
    await api.post(
        f"/api/v1/voice/bolna/{key}", json=_completed(first.json()["execution_id"], LEAD_PHONE)
    )
    await api.post(ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": lead_id})
    sent = set(bolna.calls[-1].user_data)

    variables = await api.get(ws.path("/voice/variables"), headers=ws.owner.auth)
    assert variables.status_code == 200, variables.text
    listed = {v["name"] for v in variables.json()}

    assert sent <= listed, f"sent but not listed: {sorted(sent - listed)}"
    assert {"name", "program", age_key} <= listed
    assert {v.name for v in RESERVED_VARIABLES} <= listed


@pytest.mark.integration
async def test_the_catalogue_needs_call_history_permission(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    hasher: PasswordHasherService,
) -> None:
    marketing = await add_member(
        db_session,
        hasher,
        ws,
        key="marketing",
        email="marketing@preview.example",
        template_name="Marketing",
    )
    await login(api, marketing)
    response = await api.get(ws.path("/voice/variables"), headers=marketing.auth)
    assert response.status_code == 403
