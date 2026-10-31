"""Call recordings and how a call ended (docs/20).

1. A recorded call plays through the CRM, following Bolna's redirect to S3.
2. Bolna's open link never reaches the browser: it is redacted from the
   payload, and the call says only whether a recording exists.
3. The CRM fetches nothing but Bolna and its storage — a link to anywhere
   else is refused before any request is made.
4. Who hung up, why, and how long it rang are shown with the call.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import WorkspaceFixture, build_workspace, login

from app.auth.passwords import PasswordHasherService
from app.integrations.bolna import BolnaSettings, RecordingBolnaClient

FAKE_AGENT_ID = "11111111-2222-3333-4444-555555555555"
PHONE = "+919000005001"
AUDIO = b"\xff\xe3H\x04" + b"fake-mp3-bytes" * 100
BOLNA_LINK = "https://api.bolna.ai/recordings/call/abc"
S3_LINK = "https://bolna-recordings-india.s3.amazonaws.com/abc.mp3?X-Amz-Signature=x"


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
def fetched(wired_app: FastAPI) -> list[str]:
    """Every URL the CRM asked for, answered like Bolna and S3 would."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "api.bolna.ai":
            return httpx.Response(302, headers={"location": S3_LINK})
        if request.url.host.endswith(".amazonaws.com"):
            return httpx.Response(200, content=AUDIO, headers={"content-type": "audio/mpeg"})
        if request.url.host == "evil.example":
            return httpx.Response(200, content=b"secret")
        return httpx.Response(404)

    wired_app.state.recording_transport = httpx.MockTransport(handler)
    return seen


@pytest.fixture
async def ws(
    api: AsyncClient, db_session: AsyncSession, hasher: PasswordHasherService
) -> WorkspaceFixture:
    fixture = await build_workspace(
        db_session, hasher, name="Recordings Co", owner_email="owner@recordings.example"
    )
    fixture.workspace.default_country_code = "91"
    await db_session.commit()
    await login(api, fixture.owner)
    return fixture


async def _call(api: AsyncClient, ws: WorkspaceFixture, telephony: dict[str, Any]) -> str:
    """Place a call and complete it with this telephony block; its call id."""
    lead = await api.post(
        ws.path("/leads"), headers=ws.owner.auth, json={"values": {"name": "R", "phone": PHONE}}
    )
    key = await api.post(
        ws.path("/settings/api-keys"),
        headers=ws.owner.auth,
        json={"name": "Bolna", "permission_template_id": str(ws.templates["Root"].id)},
    )
    trigger = await api.post(
        ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": lead.json()["id"]}
    )
    assert trigger.status_code == 200, trigger.text
    delivered = await api.post(
        f"/api/v1/voice/bolna/{key.json()['key']}",
        json={
            "id": trigger.json()["execution_id"],
            "agent_id": FAKE_AGENT_ID,
            "status": "completed",
            "conversation_duration": 42,
            "transcript": "user: hello",
            "summary": "Said hello.",
            "telephony_data": {"to_number": PHONE, "call_type": "outbound", **telephony},
        },
    )
    assert delivered.status_code == 200, delivered.text
    return str(delivered.json()["call_id"])


async def test_a_recording_plays_through_the_crm(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient, fetched: list[str]
) -> None:
    call_id = await _call(api, ws, {"recording_url": BOLNA_LINK})

    response = await api.get(ws.path(f"/voice/calls/{call_id}/recording"), headers=ws.owner.auth)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content == AUDIO
    assert "private" in response.headers["cache-control"]
    assert fetched == [BOLNA_LINK, S3_LINK]


async def test_the_open_link_never_reaches_the_browser(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient, fetched: list[str]
) -> None:
    call_id = await _call(
        api,
        ws,
        {
            "recording_url": BOLNA_LINK,
            "hangup_by": "Callee",
            "hangup_reason": "Normal Hangup",
            "ring_duration": "7",
        },
    )

    detail = await api.get(ws.path(f"/voice/calls/{call_id}"), headers=ws.owner.auth)
    listed = await api.get(ws.path("/voice/calls"), headers=ws.owner.auth)

    assert BOLNA_LINK not in detail.text and BOLNA_LINK not in listed.text
    body = detail.json()
    assert body["has_recording"] is True
    assert (body["hangup_by"], body["hangup_reason"], body["ring_seconds"]) == (
        "lead",
        "Normal Hangup",
        7,
    )
    assert fetched == []


@pytest.mark.parametrize(
    "link",
    [
        "http://api.bolna.ai/recordings/call/abc",
        "https://evil.example/steal",
        "https://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
    ],
)
async def test_a_link_to_anywhere_but_bolna_is_never_fetched(
    api: AsyncClient,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    fetched: list[str],
    link: str,
) -> None:
    call_id = await _call(api, ws, {"recording_url": link})

    response = await api.get(ws.path(f"/voice/calls/{call_id}/recording"), headers=ws.owner.auth)

    assert response.status_code == 404
    assert fetched == []


async def test_a_redirect_away_from_storage_is_refused(
    api: AsyncClient,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    wired_app: FastAPI,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.bolna.ai":
            return httpx.Response(302, headers={"location": "https://evil.example/x"})
        return httpx.Response(200, content=b"should never be read")

    wired_app.state.recording_transport = httpx.MockTransport(handler)
    call_id = await _call(api, ws, {"recording_url": BOLNA_LINK})

    response = await api.get(ws.path(f"/voice/calls/{call_id}/recording"), headers=ws.owner.auth)

    assert response.status_code == 502
    assert b"should never be read" not in response.content


async def test_a_call_without_a_recording_says_so(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient, fetched: list[str]
) -> None:
    call_id = await _call(api, ws, {})

    detail = await api.get(ws.path(f"/voice/calls/{call_id}"), headers=ws.owner.auth)
    response = await api.get(ws.path(f"/voice/calls/{call_id}/recording"), headers=ws.owner.auth)

    assert detail.json()["has_recording"] is False
    assert response.status_code == 404
    assert json.loads(response.text)["detail"]["code"] == "no_recording"
