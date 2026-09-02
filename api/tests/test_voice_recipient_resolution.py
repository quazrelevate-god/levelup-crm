"""Which field a voice call actually dials.

Regression cover for a production bug. `VoiceContext.phone` was read from the
workspace's *identity* field, on the assumption that the identity is the phone
number. It usually is — provisioning designates Phone — but a workspace may
designate any field, and this one designated **Name**. So the CRM handed Bolna
the string "Perumal" as `recipient_phone_number` and got back
`400 … make sure country code is added`, which is a memorably indirect way of
being told you tried to dial a person's name.

The fix resolves the number by field *type* rather than by identity
designation, and these tests pin that: identity is Name throughout, and the
call must still find the Phone field.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import WorkspaceFixture, build_workspace, login

from app.auth.passwords import PasswordHasherService
from app.integrations.bolna import BolnaSettings, RecordingBolnaClient

pytestmark = pytest.mark.integration

FAKE_BOLNA_KEY = "bn-test-key-do-not-use-0000000000"
FAKE_AGENT_ID = "11111111-2222-3333-4444-555555555555"

NAME = "Perumal"
PHONE = "+919087822357"
EMAIL = "perumal@example.com"


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
async def ws(
    db_session: AsyncSession, hasher: PasswordHasherService, api: AsyncClient
) -> WorkspaceFixture:
    """A workspace whose identity field is **Name**, not Phone.

    This is the whole point of the file: the shape that broke production. It
    is a legitimate configuration — nothing in the product forbids it — so the
    voice path has to cope with it rather than assume it away.
    """
    fixture = await build_workspace(
        db_session, hasher, name="Identity Is Name Co", owner_email="owner@identityname.example"
    )
    fixture.workspace.identity_field_id = fixture.fields["name"].id
    await db_session.commit()
    await login(api, fixture.owner)
    return fixture


async def _create_lead(api: AsyncClient, ws: WorkspaceFixture) -> dict[str, Any]:
    response = await api.post(
        ws.path("/leads"),
        headers=ws.owner.auth,
        json={"values": {"name": NAME, "phone": PHONE, "email": EMAIL}},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


# --- the context -------------------------------------------------------------


async def test_the_identity_field_really_is_name(api: AsyncClient, ws: WorkspaceFixture) -> None:
    """Guard the premise. If this drifts, the rest proves nothing."""
    assert ws.workspace.identity_field_id == ws.fields["name"].id
    lead = await _create_lead(api, ws)
    # The lead is identified by its name, exactly as the workspace asked.
    assert lead["identity_value"] == NAME


async def test_context_phone_is_the_phone_not_the_identity(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    """The bug, stated directly: `phone` must not be "Perumal"."""
    lead = await _create_lead(api, ws)

    response = await api.get(ws.path(f"/voice/context/{lead['id']}"), headers=ws.owner.auth)
    assert response.status_code == 200, response.text
    context = response.json()

    assert context["phone"] == PHONE
    assert context["phone"] != NAME
    # The identity is still reported, unchanged — lookup behaviour is intact.
    assert context["identity_value"] == NAME
    assert context["name"] == NAME
    assert context["email"] == EMAIL


# --- the call ----------------------------------------------------------------


async def test_the_call_dials_the_phone_field(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """What Bolna actually receives as `recipient_phone_number`."""
    lead = await _create_lead(api, ws)

    response = await api.post(
        ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": lead["id"]}
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # The CRM's own report of the attempt.
    assert body["recipient_phone"] == PHONE
    assert body["recipient_phone"] != NAME

    # And the payload that would have reached the vendor.
    assert len(bolna.calls) == 1
    assert bolna.last.recipient_phone_number == PHONE
    assert bolna.last.recipient_phone_number != NAME


async def test_user_data_is_unchanged_by_the_fix(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """The fix touches the recipient only; the agent's variables must not move."""
    lead = await _create_lead(api, ws)

    response = await api.post(
        ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": lead["id"]}
    )
    assert response.status_code == 200, response.text
    user_data = response.json()["user_data"]

    assert user_data["phone"] == PHONE
    assert user_data["name"] == NAME
    assert user_data["email"] == EMAIL
    # The reserved block still travels intact.
    assert user_data["crm_lead_id"] == lead["id"]
    assert user_data["crm_is_repeat_caller"] == "no"
    assert bolna.last.user_data["phone"] == PHONE


async def test_a_lead_with_no_number_is_refused_rather_than_dialled_by_name(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """No phone means no call — never a fallback to the identity string.

    Before the fix this path dialled `lead.identity_value`, which under this
    workspace's configuration is a person's name.
    """
    response = await api.post(
        ws.path("/leads"), headers=ws.owner.auth, json={"values": {"name": "No Number"}}
    )
    assert response.status_code == 201, response.text
    lead = response.json()

    triggered = await api.post(
        ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": lead["id"]}
    )
    assert triggered.status_code == 422
    assert triggered.json()["detail"]["code"] == "lead_has_no_phone"
    # Nothing was sent to the vendor.
    assert bolna.calls == []


# --- the ordinary configuration still works ----------------------------------


async def test_a_phone_identity_workspace_is_unaffected(
    db_session: AsyncSession,
    hasher: PasswordHasherService,
    api: AsyncClient,
    bolna: RecordingBolnaClient,
) -> None:
    """The default shape — identity *is* Phone — must behave exactly as before.

    This is the case every existing voice test already covers; asserting it
    here as well keeps the two configurations honest about being one code path.
    """
    other = await build_workspace(
        db_session, hasher, name="Identity Is Phone Co", owner_email="owner@identityphone.example"
    )
    await login(api, other.owner)

    created = await api.post(
        other.path("/leads"),
        headers=other.owner.auth,
        json={"values": {"name": NAME, "phone": PHONE}},
    )
    assert created.status_code == 201, created.text
    lead = created.json()
    # Provisioning designates Phone, so identity and phone agree here.
    assert lead["identity_value"] == PHONE

    triggered = await api.post(
        other.path("/voice/calls"), headers=other.owner.auth, json={"lead_id": lead["id"]}
    )
    assert triggered.status_code == 200, triggered.text
    assert triggered.json()["recipient_phone"] == PHONE
    assert bolna.last.recipient_phone_number == PHONE


async def test_alternate_phone_does_not_win_over_phone(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Two phone-typed fields exist by default; the sorted first one wins.

    `Phone` is provisioned at sort 1 and `Alternate Phone` at sort 3, so a
    lead carrying both must be dialled on the primary.
    """
    alternate = "+919000000001"
    created = await api.post(
        ws.path("/leads"),
        headers=ws.owner.auth,
        json={"values": {"name": NAME, "phone": PHONE, "alternate_phone": alternate}},
    )
    assert created.status_code == 201, created.text

    triggered = await api.post(
        ws.path("/voice/calls"),
        headers=ws.owner.auth,
        json={"lead_id": created.json()["id"]},
    )
    assert triggered.status_code == 200, triggered.text
    assert triggered.json()["recipient_phone"] == PHONE
    assert triggered.json()["recipient_phone"] != alternate
