"""Placing a follow-up call: which conversation it is, and when it is refused.

The voice agent holds three follow-up openings and picks one by reading
`crm_call_purpose` out of the `user_data` the CRM sends. So the whole of this
feature, from the CRM's side, is one string in one dictionary — and these tests
pin the two things that make that string safe to send.

**That a first call carries no purpose at all.** The agent's greeting node
hands over to the follow-up node on `crm_call_purpose contains "followup"`. If
the key leaked onto an ordinary call, the first thing a brand-new lead would
hear is "last time you were asking about…".

**That a follow-up is refused when there is nothing to follow up.** The
follow-up node opens by referring to a conversation that has already happened.
Placing one against a lead nobody has spoken to would have the agent invent a
shared history, which is worse than not calling.

Nothing here reaches Bolna: `RecordingBolnaClient` captures what would have
been sent, which is exactly the thing under test.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import WorkspaceFixture, build_workspace, login

from app.auth.passwords import PasswordHasherService
from app.integrations.bolna import BolnaSettings, RecordingBolnaClient
from app.models.enums import VoiceCallPurpose
from app.models.voice import VoiceCallExecution
from app.services.voice_followup import (
    FOLLOWUP_CALL_OPENING,
    OPENING_KEY,
    PURPOSE_KEY,
    VoiceFollowupService,
)
from app.tenancy.session import ScopedSession

FAKE_BOLNA_KEY = "bn-test-key-do-not-use-0000000000"
FAKE_AGENT_ID = "11111111-2222-3333-4444-555555555555"

NAME = "Sakthivel"
PHONE = "+919000000001"


@pytest.fixture
def hasher(wired_app: FastAPI) -> PasswordHasherService:
    hasher = wired_app.state.password_hasher
    assert isinstance(hasher, PasswordHasherService)
    return hasher


@pytest.fixture
async def ws(db_session: AsyncSession, hasher: PasswordHasherService) -> WorkspaceFixture:
    return await build_workspace(
        db_session, hasher, name="Follow Up Co", owner_email="owner@followup.example"
    )


@pytest.fixture
def bolna(wired_app: FastAPI) -> RecordingBolnaClient:
    client = RecordingBolnaClient()
    wired_app.state.bolna_client = client
    wired_app.state.bolna_settings = BolnaSettings(
        api_key=FAKE_BOLNA_KEY, base_url="https://api.bolna.invalid", agent_id=FAKE_AGENT_ID
    )
    return client


async def _lead(api: AsyncClient, ws: WorkspaceFixture) -> str:
    await login(api, ws.owner)
    response = await api.post(
        ws.path("/leads"), headers=ws.owner.auth, json={"values": {"name": NAME, "phone": PHONE}}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _call(
    api: AsyncClient, ws: WorkspaceFixture, lead_id: str, *, followup: bool = False
) -> Any:
    body: dict[str, Any] = {"lead_id": lead_id}
    if followup:
        body["followup"] = True
    return await api.post(ws.path("/voice/calls"), headers=ws.owner.auth, json=body)


async def _finish(session: AsyncSession, lead_id: str) -> None:
    """Mark this lead's latest attempt finished, as the webhook would.

    `completed_at` rather than `status`, because that is the column the webhook
    sets on the first terminal delivery and the one the readiness check reads.
    """
    rows = await session.execute(
        select(VoiceCallExecution)
        .where(VoiceCallExecution.lead_id == lead_id)
        .order_by(VoiceCallExecution.created_at.desc())
        .limit(1)
    )
    row = rows.scalar_one()
    row.completed_at = dt.datetime.now(dt.UTC)
    await session.commit()


# --- a first call is not a follow-up ------------------------------------------


async def test_an_ordinary_call_carries_no_purpose_at_all(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """The key is absent, not empty.

    The agent's edge fires on "contains followup". An empty string would not
    match it today, but the absence is the signal the graph is written against
    and a blank value is one template-engine change away from being a surprise.
    """
    lead_id = await _lead(api, ws)
    response = await _call(api, ws, lead_id)

    assert response.status_code == 200, response.text
    assert PURPOSE_KEY not in response.json()["user_data"]
    assert PURPOSE_KEY not in bolna.calls[-1].user_data


# --- refusing a follow-up that has nothing to follow --------------------------


async def test_a_lead_with_no_finished_call_cannot_be_followed_up(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    lead_id = await _lead(api, ws)
    response = await _call(api, ws, lead_id, followup=True)

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "followup_not_available"
    # Refused before dialling. A follow-up that quietly became a first call
    # would have the agent greet a stranger as an old acquaintance.
    assert bolna.calls == []


async def test_a_call_that_was_placed_but_never_finished_is_not_enough(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """A dispatched call is not a conversation.

    The number rang; nobody necessarily spoke. The follow-up node's opening
    refers to what was discussed last time, so an attempt that never completed
    gives it nothing true to say.
    """
    lead_id = await _lead(api, ws)
    assert (await _call(api, ws, lead_id)).status_code == 200

    response = await _call(api, ws, lead_id, followup=True)
    assert response.status_code == 422, response.text
    assert len(bolna.calls) == 1


# --- which follow-up it is ----------------------------------------------------


async def test_the_first_follow_up_opens_as_a_first_follow_up(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    lead_id = await _lead(api, ws)
    assert (await _call(api, ws, lead_id)).status_code == 200
    await _finish(db_session, lead_id)

    response = await _call(api, ws, lead_id, followup=True)

    assert response.status_code == 200, response.text
    assert response.json()["user_data"][PURPOSE_KEY] == VoiceCallPurpose.FOLLOWUP.value
    assert bolna.calls[-1].user_data[PURPOSE_KEY] == "followup"


async def test_the_second_follow_up_opens_as_a_missed_callback(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Counted from attempts, not from what the summary said.

    Reading a promise to call back out of a model's prose would be guesswork,
    and being wrong means telling somebody they broke a promise they never
    made. A second follow-up is proof enough that the first did not land.
    """
    lead_id = await _lead(api, ws)
    assert (await _call(api, ws, lead_id)).status_code == 200
    await _finish(db_session, lead_id)
    assert (await _call(api, ws, lead_id, followup=True)).status_code == 200

    response = await _call(api, ws, lead_id, followup=True)

    assert response.status_code == 200, response.text
    assert response.json()["user_data"][PURPOSE_KEY] == VoiceCallPurpose.FOLLOWUP_MISSED.value


async def test_an_unanswered_follow_up_still_counts_as_sent(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """The follow-up that rang out is still one the caller did not take.

    Counting only completed follow-ups would reopen the same conversation with
    the same opening every time the phone went unanswered.
    """
    lead_id = await _lead(api, ws)
    assert (await _call(api, ws, lead_id)).status_code == 200
    await _finish(db_session, lead_id)

    # Placed, never completed — no `_finish` for this one.
    assert (await _call(api, ws, lead_id, followup=True)).status_code == 200
    response = await _call(api, ws, lead_id, followup=True)

    assert response.json()["user_data"][PURPOSE_KEY] == VoiceCallPurpose.FOLLOWUP_MISSED.value


async def test_the_booking_opening_is_unreachable_while_payment_is_not_integrated(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Nothing may send `followup_booking` yet.

    Its opening says "your application fee came through, thank you". The CRM
    cannot see payments, so there is no honest way to reach it, and the only
    safe number of routes into it today is none.
    """
    lead_id = await _lead(api, ws)
    assert (await _call(api, ws, lead_id)).status_code == 200
    await _finish(db_session, lead_id)

    for _ in range(4):
        await _call(api, ws, lead_id, followup=True)

    sent = {call.user_data.get(PURPOSE_KEY) for call in bolna.calls}
    assert VoiceCallPurpose.FOLLOWUP_BOOKING.value not in sent


# --- the service on its own ---------------------------------------------------


async def test_readiness_reads_only_this_workspaces_calls(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    hasher: PasswordHasherService,
    bolna: RecordingBolnaClient,
) -> None:
    """Counts are workspace-scoped, like every other read.

    `ScopedSession` filters on `workspace_id`, so a lead id from another
    workspace reads as a lead with no calls at all — and that is the refusing
    answer, not the permissive one. Worth pinning here because the failure
    would be silent: a follow-up placed against somebody else's lead.
    """
    lead_id = await _lead(api, ws)
    assert (await _call(api, ws, lead_id)).status_code == 200
    await _finish(db_session, lead_id)

    other = await build_workspace(
        db_session, hasher, name="Somebody Else", owner_email="owner@elsewhere.example"
    )
    service = VoiceFollowupService(ScopedSession(db_session, other.id))

    assert await service.completed_calls(uuid.UUID(lead_id)) == 0
    assert await service.followups_sent(uuid.UUID(lead_id)) == 0
    readiness = await service.readiness(uuid.UUID(lead_id))
    assert not readiness.allowed
    assert readiness.purpose is None


# --- the first thing the caller hears -----------------------------------------


async def test_a_first_call_opens_with_the_whole_greeting(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """The agent's welcome message is one variable, so this line is the call.

    Sent fully rendered, name included: a `{name}` inside a substituted
    variable is not itself substituted, so a template left here would be read
    out as literal curly brackets.
    """
    lead_id = await _lead(api, ws)
    assert (await _call(api, ws, lead_id)).status_code == 200

    opening = bolna.calls[-1].user_data[OPENING_KEY]
    assert (
        opening
        == f"Hi {NAME}, this is Aisha from Level Up Learning. Is now a good time for a quick call?"
    )
    assert "{" not in opening


async def test_a_follow_up_opens_with_one_word_and_waits(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """A caller who already knows Aisha is greeted the way a person would.

    The name and the reason come from the follow-up node afterwards, in its
    own words. Putting them in the welcome would make the caller's first
    "hello" collide with a sentence already in progress.
    """
    lead_id = await _lead(api, ws)
    assert (await _call(api, ws, lead_id)).status_code == 200
    await _finish(db_session, lead_id)

    assert (await _call(api, ws, lead_id, followup=True)).status_code == 200

    assert bolna.calls[-1].user_data[OPENING_KEY] == FOLLOWUP_CALL_OPENING
    assert NAME not in bolna.calls[-1].user_data[OPENING_KEY]


async def test_a_lead_with_no_name_is_not_greeted_as_hi_comma(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Regression: the welcome used to render "Hi ," out loud.

    `{name}` substituted to nothing and the comma stayed. A separate nameless
    line is the fix, because there is no way to make one template read well
    both with and without a name.
    """
    await login(api, ws.owner)
    created = await api.post(
        ws.path("/leads"), headers=ws.owner.auth, json={"values": {"phone": "+919000000002"}}
    )
    assert created.status_code == 201, created.text

    assert (await _call(api, ws, str(created.json()["id"]))).status_code == 200

    opening = bolna.calls[-1].user_data[OPENING_KEY]
    assert opening.startswith("Hi, this is Aisha")
    assert "Hi ," not in opening
