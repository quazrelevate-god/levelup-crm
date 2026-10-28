"""Approval for AI calling: the per-lead switch batches obey (docs/15).

A person opens a lead, checks what the agent will know, and switches it on.
Tested end to end through the API:

1. **The switch.** On and off, shown on the lead read, recorded with who
   switched it on.
2. **The audit trail.** Every flip is one timeline note in its own changeset;
   setting it to where it already is writes nothing.
3. **Who may flip it.** The same permission that places a call, and only on a
   lead the member can see.
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
from app.services.voice_approval import APPROVED_NOTE, REVOKED_NOTE


@pytest.fixture
def hasher(wired_app: FastAPI) -> PasswordHasherService:
    hasher = wired_app.state.password_hasher
    assert isinstance(hasher, PasswordHasherService)
    return hasher


@pytest.fixture
async def ws(db_session: AsyncSession, hasher: PasswordHasherService) -> WorkspaceFixture:
    fixture = await build_workspace(
        db_session, hasher, name="Approval Co", owner_email="owner@approval.example"
    )
    fixture.workspace.default_country_code = "91"
    await db_session.commit()
    return fixture


async def _lead(api: AsyncClient, ws: WorkspaceFixture) -> str:
    await login(api, ws.owner)
    response = await api.post(
        ws.path("/leads"),
        headers=ws.owner.auth,
        json={"values": {"name": "Perumal", "phone": "+919087822357"}},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _approve(
    api: AsyncClient, ws: WorkspaceFixture, lead_id: str, approved: bool, auth: dict[str, str]
) -> Any:
    return await api.put(
        ws.path(f"/voice/approvals/{lead_id}"), headers=auth, json={"approved": approved}
    )


async def _lead_read(api: AsyncClient, ws: WorkspaceFixture, lead_id: str) -> dict[str, Any]:
    response = await api.get(ws.path(f"/leads/{lead_id}"), headers=ws.owner.auth)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _notes(api: AsyncClient, ws: WorkspaceFixture, lead_id: str) -> list[dict[str, Any]]:
    response = await api.get(ws.path(f"/leads/{lead_id}/actions"), headers=ws.owner.auth)
    assert response.status_code == 200, response.text
    return [a for a in response.json()["items"] if a["kind"] == "NOTE"]


# --- 1. the switch -----------------------------------------------------------


async def test_a_new_lead_is_not_approved(api: AsyncClient, ws: WorkspaceFixture) -> None:
    lead_id = await _lead(api, ws)

    lead = await _lead_read(api, ws, lead_id)
    assert lead["voice_approved_at"] is None
    assert lead["voice_approved_by_id"] is None


async def test_approving_shows_on_the_lead_with_who_approved_it(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    lead_id = await _lead(api, ws)

    response = await _approve(api, ws, lead_id, True, ws.owner.auth)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["approved"] is True
    assert body["changed"] is True
    assert body["approved_by_id"] == str(ws.owner.membership.id)
    lead = await _lead_read(api, ws, lead_id)
    assert lead["voice_approved_at"] is not None
    assert lead["voice_approved_by_id"] == str(ws.owner.membership.id)


async def test_removing_the_approval_clears_it(api: AsyncClient, ws: WorkspaceFixture) -> None:
    lead_id = await _lead(api, ws)
    await _approve(api, ws, lead_id, True, ws.owner.auth)

    response = await _approve(api, ws, lead_id, False, ws.owner.auth)

    assert response.status_code == 200, response.text
    assert response.json()["approved"] is False
    lead = await _lead_read(api, ws, lead_id)
    assert lead["voice_approved_at"] is None
    assert lead["voice_approved_by_id"] is None


# --- 2. the audit trail ------------------------------------------------------


async def test_each_flip_is_one_note_in_its_own_changeset(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    lead_id = await _lead(api, ws)

    await _approve(api, ws, lead_id, True, ws.owner.auth)
    await _approve(api, ws, lead_id, False, ws.owner.auth)

    notes = await _notes(api, ws, lead_id)
    assert sorted(n["body"] for n in notes) == sorted([APPROVED_NOTE, REVOKED_NOTE])
    assert all(n["actor_id"] == str(ws.owner.membership.id) for n in notes)
    changesets = {n["changeset_id"] for n in notes}
    assert None not in changesets
    assert len(changesets) == 2


@pytest.mark.parametrize("approved", [True, False])
async def test_setting_it_to_where_it_already_is_writes_nothing(
    api: AsyncClient, ws: WorkspaceFixture, approved: bool
) -> None:
    lead_id = await _lead(api, ws)
    if approved:
        await _approve(api, ws, lead_id, True, ws.owner.auth)
    before = await _notes(api, ws, lead_id)

    response = await _approve(api, ws, lead_id, approved, ws.owner.auth)

    assert response.status_code == 200, response.text
    assert response.json()["changed"] is False
    assert response.json()["approved"] is approved
    assert await _notes(api, ws, lead_id) == before


# --- 3. who may flip it ------------------------------------------------------


async def test_a_member_who_cannot_place_calls_cannot_approve(
    api: AsyncClient,
    ws: WorkspaceFixture,
    db_session: AsyncSession,
    hasher: PasswordHasherService,
) -> None:
    lead_id = await _lead(api, ws)
    marketing = await add_member(
        db_session,
        hasher,
        ws,
        key="marketing",
        email="marketing@approval.example",
        template_name="Marketing",
    )
    await login(api, marketing)

    response = await _approve(api, ws, lead_id, True, marketing.auth)

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "insufficient_permissions"
    assert (await _lead_read(api, ws, lead_id))["voice_approved_at"] is None
    assert await _notes(api, ws, lead_id) == []


async def test_a_caller_can_approve_their_own_lead_but_not_someone_else_s(
    api: AsyncClient,
    ws: WorkspaceFixture,
    db_session: AsyncSession,
    hasher: PasswordHasherService,
) -> None:
    """A Caller sees their own leads and unassigned ones, not a colleague's;
    approval follows visibility exactly."""
    theirs = await _lead(api, ws)
    not_theirs = await _lead_other_phone(api, ws)
    caller = await add_member(
        db_session,
        hasher,
        ws,
        key="caller",
        email="caller@approval.example",
        template_name="Caller",
    )
    await login(api, caller)
    assign = await api.patch(
        ws.path(f"/leads/{theirs}"),
        headers=ws.owner.auth,
        json={"assignee_id": str(caller.membership.id)},
    )
    assert assign.status_code == 200, assign.text
    elsewhere = await api.patch(
        ws.path(f"/leads/{not_theirs}"),
        headers=ws.owner.auth,
        json={"assignee_id": str(ws.owner.membership.id)},
    )
    assert elsewhere.status_code == 200, elsewhere.text

    allowed = await _approve(api, ws, theirs, True, caller.auth)
    refused = await _approve(api, ws, not_theirs, True, caller.auth)

    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["approved_by_id"] == str(caller.membership.id)
    assert refused.status_code == 404, refused.text
    assert (await _lead_read(api, ws, not_theirs))["voice_approved_at"] is None


async def test_an_unknown_lead_is_a_404(api: AsyncClient, ws: WorkspaceFixture) -> None:
    await login(api, ws.owner)

    response = await _approve(api, ws, str(uuid.uuid4()), True, ws.owner.auth)

    assert response.status_code == 404, response.text


async def _lead_other_phone(api: AsyncClient, ws: WorkspaceFixture) -> str:
    response = await api.post(
        ws.path("/leads"),
        headers=ws.owner.auth,
        json={"values": {"name": "Selvi", "phone": "+919000000358"}},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])
