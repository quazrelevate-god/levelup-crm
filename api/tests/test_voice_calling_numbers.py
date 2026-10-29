"""Calling numbers: which of the operator's numbers AI calls go out from (docs/16).

1. **Setting them** is the operator's: the numbers are bought in the
   operator's Bolna account. A workspace admin can see them, not change them.
2. **Rotation**: calls go out from each number in turn, and each call records
   the number it used, so a number flagged as spam can be found and swapped.
3. **No numbers**: Bolna's default number, exactly as before.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import WorkspaceFixture, build_workspace, login

from app.auth.passwords import PasswordHasherService
from app.integrations.bolna import BolnaCallRequest, BolnaSettings, RecordingBolnaClient
from app.models.voice import VoiceCallExecution

FAKE_AGENT_ID = "11111111-2222-3333-4444-555555555555"
FIRST = "+919000000501"
SECOND = "+919000000502"


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
        db_session, hasher, name="Numbers Co", owner_email="owner@numbers.example"
    )
    fixture.workspace.default_country_code = "91"
    await db_session.commit()
    return fixture


async def _put(api: AsyncClient, ws: WorkspaceFixture, numbers: list[str]) -> Any:
    return await api.put(
        ws.path("/voice/calling-numbers"), headers=ws.owner.auth, json={"numbers": numbers}
    )


async def _call(api: AsyncClient, ws: WorkspaceFixture, phone: str) -> None:
    lead = await api.post(
        ws.path("/leads"), headers=ws.owner.auth, json={"values": {"name": "P", "phone": phone}}
    )
    assert lead.status_code == 201, lead.text
    placed = await api.post(
        ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": lead.json()["id"]}
    )
    assert placed.status_code == 200, placed.text


# --- 1. setting them -----------------------------------------------------------


async def test_the_operator_sets_numbers_cleaned_and_in_order(
    api: AsyncClient, ws: WorkspaceFixture, make_operator: Callable[..., None]
) -> None:
    make_operator(ws.owner.user.email)
    await login(api, ws.owner)

    # Typed as a person types them: spaces, no country code, a blank line, a repeat.
    response = await _put(api, ws, ["90000 00502", "", "+91 90000 00501", "9000000502"])

    assert response.status_code == 200, response.text
    assert response.json() == {"numbers": [SECOND, FIRST], "editable": True}
    read = await api.get(ws.path("/voice/calling-numbers"), headers=ws.owner.auth)
    assert read.json()["numbers"] == [SECOND, FIRST]


async def test_a_number_that_is_not_a_phone_number_is_refused(
    api: AsyncClient, ws: WorkspaceFixture, make_operator: Callable[..., None]
) -> None:
    make_operator(ws.owner.user.email)
    await login(api, ws.owner)

    response = await _put(api, ws, [FIRST, "call me maybe"])

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "invalid_calling_number"


async def test_a_workspace_admin_can_see_the_numbers_but_not_change_them(
    api: AsyncClient,
    ws: WorkspaceFixture,
    db_session: AsyncSession,
    make_operator: Callable[..., None],
) -> None:
    ws.workspace.voice_calling_numbers = [FIRST]
    await db_session.commit()
    make_operator("operator@example.com")
    await login(api, ws.owner)

    read = await api.get(ws.path("/voice/calling-numbers"), headers=ws.owner.auth)
    write = await _put(api, ws, [SECOND])

    assert read.json() == {"numbers": [FIRST], "editable": False}
    assert write.status_code == 403, write.text
    assert write.json()["detail"]["code"] == "operator_only"
    await db_session.refresh(ws.workspace)
    assert ws.workspace.voice_calling_numbers == [FIRST]


# --- 2. rotation -------------------------------------------------------------------


@pytest.mark.integration
async def test_calls_go_out_from_each_number_in_turn(
    api: AsyncClient,
    ws: WorkspaceFixture,
    db_session: AsyncSession,
    bolna: RecordingBolnaClient,
) -> None:
    ws.workspace.voice_calling_numbers = [FIRST, SECOND]
    await db_session.commit()
    await login(api, ws.owner)

    for phone in ("+919000000601", "+919000000602", "+919000000603"):
        await _call(api, ws, phone)

    assert [c.from_phone_number for c in bolna.calls] == [FIRST, SECOND, FIRST]
    assert [c.body()["from_phone_number"] for c in bolna.calls] == [FIRST, SECOND, FIRST]
    rows = (
        await db_session.execute(
            select(VoiceCallExecution.recipient_phone, VoiceCallExecution.caller_phone).order_by(
                VoiceCallExecution.created_at
            )
        )
    ).all()
    assert [r.caller_phone for r in rows] == [FIRST, SECOND, FIRST]


# --- 3. no numbers -----------------------------------------------------------------


@pytest.mark.integration
async def test_with_no_numbers_bolna_uses_its_default(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    await login(api, ws.owner)

    await _call(api, ws, "+919000000701")

    [call] = bolna.calls
    assert call.from_phone_number is None
    assert "from_phone_number" not in call.body()


def test_the_request_only_names_a_number_when_there_is_one() -> None:
    plain = BolnaCallRequest(agent_id="a", recipient_phone_number="+1", user_data={})
    chosen = BolnaCallRequest(
        agent_id="a", recipient_phone_number="+1", user_data={}, from_phone_number=FIRST
    )
    assert "from_phone_number" not in plain.body()
    assert chosen.body()["from_phone_number"] == FIRST
