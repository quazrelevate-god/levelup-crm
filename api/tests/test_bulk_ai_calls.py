"""Bulk AI calls from the lead list (docs/23).

1. **Queueing.** The selected leads are queued to be called now, in the order
   chosen; a lead already waiting for a call and the never-AI-call rule are
   reported, not silently dropped.
2. **The worker** keeps at most `voice_max_parallel_calls` going and starts the
   next as each one ends.
3. **Stopping** cancels only what is still waiting, and only bulk calls.
4. **Who may** — the calling permission, and nobody else's leads.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.factories import WorkspaceFixture, add_member, build_workspace, login

from app.auth.passwords import PasswordHasherService
from app.config import Settings
from app.integrations.bolna import RecordingBolnaClient
from app.models import VoiceCallExecution, VoiceScheduledCall
from app.services.scheduled_call_runner import place_due_calls

AGENT = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def hasher(wired_app: FastAPI) -> PasswordHasherService:
    hasher = wired_app.state.password_hasher
    assert isinstance(hasher, PasswordHasherService)
    return hasher


@pytest.fixture
def worker_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "bolna_api_key": "bn-test-key-do-not-use-0000000000",
            "bolna_base_url": "https://api.bolna.invalid",
            "bolna_agent_id": AGENT,
            "voice_calling_hours": "",
            "voice_max_parallel_calls": 2,
        }
    )


@pytest.fixture
async def ws(
    api: AsyncClient, db_session: AsyncSession, hasher: PasswordHasherService
) -> WorkspaceFixture:
    fixture = await build_workspace(
        db_session, hasher, name="Bulk Co", owner_email="owner@bulk.example"
    )
    fixture.workspace.default_country_code = "91"
    await db_session.commit()
    await login(api, fixture.owner)
    return fixture


async def _leads(api: AsyncClient, ws: WorkspaceFixture, count: int) -> list[str]:
    ids = []
    for n in range(count):
        response = await api.post(
            ws.path("/leads"),
            headers=ws.owner.auth,
            json={"values": {"name": f"Lead {n}", "phone": f"+91900000{7000 + n}"}},
        )
        assert response.status_code == 201, response.text
        ids.append(str(response.json()["id"]))
    return ids


async def _bulk(
    api: AsyncClient, ws: WorkspaceFixture, lead_ids: list[str], auth: dict[str, str] | None = None
) -> Any:
    return await api.post(
        ws.path("/voice/scheduled-calls/bulk"),
        headers=auth or ws.owner.auth,
        json={"lead_ids": lead_ids},
    )


async def _rows(db_session: AsyncSession) -> list[VoiceScheduledCall]:
    found = await db_session.execute(
        select(VoiceScheduledCall)
        .order_by(VoiceScheduledCall.due_at)
        .execution_options(populate_existing=True)
    )
    return list(found.scalars())


# --- 1. queueing ---------------------------------------------------------------------


async def test_selected_leads_are_queued_in_order(
    api: AsyncClient, ws: WorkspaceFixture, db_session: AsyncSession
) -> None:
    leads = await _leads(api, ws, 3)
    chosen = [leads[2], leads[0], leads[1]]

    response = await _bulk(api, ws, chosen)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["queued"] == 3
    assert body["already_waiting"] == body["never_call"] == body["not_found"] == 0
    assert body["at_once"] >= 1 and body["starts_at"] is not None
    rows = await _rows(db_session)
    assert [str(r.lead_id) for r in rows] == chosen
    assert {r.source for r in rows} == {"BULK"}

    timeline = await api.get(ws.path(f"/leads/{leads[0]}/actions"), headers=ws.owner.auth)
    notes = [a["body"] for a in timeline.json()["items"] if a["kind"] == "NOTE"]
    assert any("bulk AI call" in n for n in notes)

    status = await api.get(ws.path("/voice/scheduled-calls/bulk"), headers=ws.owner.auth)
    assert status.json()["waiting"] == 3


async def test_a_lead_already_waiting_and_the_never_call_rule_are_reported(
    api: AsyncClient, ws: WorkspaceFixture, db_session: AsyncSession
) -> None:
    waiting, excluded, fine = await _leads(api, ws, 3)
    booked = await api.post(
        ws.path("/voice/scheduled-calls"),
        headers=ws.owner.auth,
        json={"lead_id": waiting, "due_at": "2030-01-01T11:00:00+05:30"},
    )
    assert booked.status_code == 201
    await api.put(
        ws.path("/settings/voice-never-call"),
        headers=ws.owner.auth,
        json={
            "conditions": {
                "type": "group",
                "op": "AND",
                "children": [{"type": "field", "key": "name", "op": "eq", "value": "Lead 1"}],
            }
        },
    )

    body = (await _bulk(api, ws, [waiting, excluded, fine])).json()

    assert (body["queued"], body["already_waiting"], body["never_call"]) == (1, 1, 1)
    rows = await _rows(db_session)
    # The callback booked for 2030 is left exactly as it was.
    assert sorted((str(r.lead_id), r.source) for r in rows) == sorted(
        [(waiting, "MANUAL"), (fine, "BULK")]
    )


async def test_more_than_500_leads_is_refused(api: AsyncClient, ws: WorkspaceFixture) -> None:
    too_many = ["00000000-0000-4000-8000-000000000001"] * 501
    response = await _bulk(api, ws, too_many)
    assert response.status_code == 422


# --- 2. the worker -------------------------------------------------------------------


async def test_the_worker_calls_a_few_at_a_time_and_then_the_next(
    api: AsyncClient,
    ws: WorkspaceFixture,
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    worker_settings: Settings,
) -> None:
    leads = await _leads(api, ws, 3)
    await _bulk(api, ws, leads)
    phone = RecordingBolnaClient()

    first = await place_due_calls(session_factory, settings=worker_settings, client=phone)
    assert first["placed"] == 2 and first["waiting"] == 1
    again = await place_due_calls(session_factory, settings=worker_settings, client=phone)
    assert again["placed"] == 0  # both lines still busy

    # One call ends: the third lead's turn.
    await db_session.execute(
        update(VoiceCallExecution)
        .where(VoiceCallExecution.workspace_id == ws.id)
        .values(completed_at=dt.datetime.now(dt.UTC))
    )
    await db_session.commit()
    last = await place_due_calls(session_factory, settings=worker_settings, client=phone)

    assert last["placed"] == 1
    assert len(phone.calls) == 3
    assert [r.status for r in await _rows(db_session)] == ["PLACED"] * 3


# --- 3. stopping ---------------------------------------------------------------------


async def test_stop_cancels_only_waiting_bulk_calls(
    api: AsyncClient, ws: WorkspaceFixture, db_session: AsyncSession
) -> None:
    manual, *bulk = await _leads(api, ws, 3)
    await api.post(
        ws.path("/voice/scheduled-calls"),
        headers=ws.owner.auth,
        json={"lead_id": manual, "due_at": "2030-01-01T11:00:00+05:30"},
    )
    await _bulk(api, ws, bulk)

    stopped = await api.post(
        ws.path("/voice/scheduled-calls/bulk/stop"), headers=ws.owner.auth, json={}
    )

    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["cancelled"] == 2
    statuses = {(r.source, r.status) for r in await _rows(db_session)}
    assert statuses == {("MANUAL", "PENDING"), ("BULK", "CANCELLED")}
    status = await api.get(ws.path("/voice/scheduled-calls/bulk"), headers=ws.owner.auth)
    assert status.json()["waiting"] == 0


# --- 4. who may ----------------------------------------------------------------------


async def test_without_the_calling_permission_it_is_refused(
    api: AsyncClient,
    ws: WorkspaceFixture,
    db_session: AsyncSession,
    hasher: PasswordHasherService,
) -> None:
    leads = await _leads(api, ws, 1)
    marketer = await add_member(
        db_session, hasher, ws, key="marketer", email="m@bulk.example", template_name="Marketing"
    )
    await login(api, marketer)

    assert (await _bulk(api, ws, leads, auth=marketer.auth)).status_code == 403
    signed_out = await api.post(ws.path("/voice/scheduled-calls/bulk"), json={"lead_ids": leads})
    assert signed_out.status_code == 401
    assert await _rows(db_session) == []


async def test_another_company_s_leads_are_never_queued(
    api: AsyncClient,
    ws: WorkspaceFixture,
    db_session: AsyncSession,
    hasher: PasswordHasherService,
) -> None:
    other = await build_workspace(
        db_session, hasher, name="Other Co", owner_email="owner@other-bulk.example"
    )
    await login(api, other.owner)
    response = await api.post(
        other.path("/leads"),
        headers=other.owner.auth,
        json={"values": {"name": "Not yours", "phone": "+919000007999"}},
    )
    foreign = str(response.json()["id"])

    body = (await _bulk(api, ws, [foreign])).json()

    assert (body["queued"], body["not_found"]) == (0, 1)
    assert await _rows(db_session) == []


async def test_the_limit_is_shared_by_every_company(
    api: AsyncClient,
    ws: WorkspaceFixture,
    db_session: AsyncSession,
    hasher: PasswordHasherService,
    session_factory: async_sessionmaker[AsyncSession],
    worker_settings: Settings,
) -> None:
    """One Bolna account carries every company's calls, so its limit is shared."""
    other = await build_workspace(
        db_session, hasher, name="Second Co", owner_email="owner@second-bulk.example"
    )
    other.workspace.default_country_code = "91"
    await db_session.commit()
    await login(api, other.owner)
    await _bulk(api, ws, await _leads(api, ws, 2))
    theirs = []
    for n in range(2):
        made = await api.post(
            other.path("/leads"),
            headers=other.owner.auth,
            json={"values": {"name": f"Theirs {n}", "phone": f"+91900000{7100 + n}"}},
        )
        theirs.append(str(made.json()["id"]))
    await _bulk(api, other, theirs, auth=other.owner.auth)
    phone = RecordingBolnaClient()

    result = await place_due_calls(session_factory, settings=worker_settings, client=phone)

    assert result["placed"] == 2  # the limit is 2, across both companies
    assert len(phone.calls) == 2
