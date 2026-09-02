"""Voice-context endpoints — the CRM-side half of the Bolna integration.

`docs/06-voice-integration-contract.md`,
`docs/09-context-continuity-and-bolna-integration.md`.

This is the CRM-only milestone: no Bolna call is made or mocked anywhere in
this file. What's exercised is the guarantee the next milestone depends on —
that a lead's voice context is durable, keyed to the same CRM identity every
time, reachable by phone as well as by id, and that recording a call summary
twice with the same idempotency key does not double-write.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.factories import WorkspaceFixture, add_member, build_workspace, login

from app.auth.passwords import PasswordHasherService
from app.models.enums import SystemActionKind
from app.models.lead import Action
from app.models.voice import VoiceCallContext

pytestmark = pytest.mark.integration


@pytest.fixture
def hasher(wired_app: FastAPI) -> PasswordHasherService:
    hasher = wired_app.state.password_hasher
    assert isinstance(hasher, PasswordHasherService)
    return hasher


@pytest.fixture
async def workspace(db_session: AsyncSession, hasher: PasswordHasherService) -> WorkspaceFixture:
    fixture = await build_workspace(
        db_session, hasher, name="Voice Co", owner_email="owner@voiceco.example"
    )
    await add_member(
        db_session,
        hasher,
        fixture,
        key="caller",
        email="caller@voiceco.example",
        template_name="Caller",
    )
    await add_member(
        db_session,
        hasher,
        fixture,
        key="marketing",
        email="marketing@voiceco.example",
        template_name="Marketing",
    )
    return fixture


async def _admin(api: AsyncClient, workspace: WorkspaceFixture) -> dict[str, str]:
    await login(api, workspace.owner)
    return workspace.owner.auth


async def _caller(api: AsyncClient, workspace: WorkspaceFixture) -> dict[str, str]:
    await login(api, workspace.members["caller"])
    return workspace.members["caller"].auth


async def _marketing(api: AsyncClient, workspace: WorkspaceFixture) -> dict[str, str]:
    await login(api, workspace.members["marketing"])
    return workspace.members["marketing"].auth


async def _create_lead(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    headers: dict[str, str],
    *,
    name: str = "Asha Rao",
    phone: str = "9876543210",
    email: str = "asha@example.com",
) -> dict:
    response = await api.post(
        workspace.path("/leads"),
        headers=headers,
        json={"values": {"name": name, "phone": phone, "email": email}},
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- retrieving context for a fresh lead --------------------------------------


async def test_a_fresh_lead_has_no_prior_call_context(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)
    lead = await _create_lead(api, workspace, headers)

    response = await api.get(workspace.path(f"/voice/context/{lead['id']}"), headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["lead_id"] == lead["id"]
    assert body["last_call_summary"] is None
    assert body["call_count"] == 0
    assert body["name"] == "Asha Rao"
    assert body["email"] == "asha@example.com"
    # Normalised on the way in, using the workspace's default country code.
    assert body["phone"] == "+919876543210"


# --- CALL 1 / CALL 2 continuity ------------------------------------------------


async def test_call_one_then_call_two_returns_the_same_stored_summary(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """The scenario this milestone exists to prove: a second call to the same
    lead sees what the first call wrote, not a blank slate."""
    headers = await _admin(api, workspace)
    lead = await _create_lead(api, workspace, headers)

    # CALL 1 — record what was learned.
    summary = "Customer is interested in the Python course and prefers weekend classes."
    update = await api.put(
        workspace.path(f"/voice/context/{lead['id']}/summary"),
        headers=headers,
        json={"summary": summary, "external_id": "call-1"},
    )
    assert update.status_code == 200, update.text
    assert update.json()["written"] is True
    assert update.json()["context"]["call_count"] == 1

    # CALL 2 — a fresh request retrieves the SAME customer by CRM id.
    by_id = await api.get(workspace.path(f"/voice/context/{lead['id']}"), headers=headers)
    assert by_id.status_code == 200
    assert by_id.json()["last_call_summary"] == summary
    assert by_id.json()["call_count"] == 1

    # And by the phone number alone — what a real inbound trigger would have.
    by_phone = await api.get(
        workspace.path("/voice/context"), headers=headers, params={"phone": "9876543210"}
    )
    assert by_phone.status_code == 200
    assert by_phone.json()["lead_id"] == lead["id"]
    assert by_phone.json()["last_call_summary"] == summary


async def test_repeated_retrieval_of_the_same_customer_is_stable(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)
    lead = await _create_lead(api, workspace, headers)
    await api.put(
        workspace.path(f"/voice/context/{lead['id']}/summary"),
        headers=headers,
        json={"summary": "Follow up next week.", "external_id": "call-1"},
    )

    first = await api.get(workspace.path(f"/voice/context/{lead['id']}"), headers=headers)
    second = await api.get(workspace.path(f"/voice/context/{lead['id']}"), headers=headers)

    assert first.json() == second.json()


async def test_repeated_phone_lookup_never_creates_a_second_lead(
    api: AsyncClient, workspace: WorkspaceFixture, db_session: AsyncSession
) -> None:
    """Calling the same phone number again must resolve to the one existing
    lead, never mint a duplicate — the requirement the identity uniqueness
    constraint (docs/01-data-model.md §4) exists to guarantee."""
    headers = await _admin(api, workspace)
    lead = await _create_lead(api, workspace, headers, phone="9123456780")

    first = await api.get(
        workspace.path("/voice/context"), headers=headers, params={"phone": "9123456780"}
    )
    second = await api.get(
        workspace.path("/voice/context"), headers=headers, params={"phone": "9123456780"}
    )

    assert first.json()["lead_id"] == lead["id"]
    assert second.json()["lead_id"] == lead["id"]


# --- updating the summary -------------------------------------------------


async def test_updating_summary_writes_a_context_row_and_a_timeline_note(
    api: AsyncClient, workspace: WorkspaceFixture, db_session: AsyncSession
) -> None:
    headers = await _admin(api, workspace)
    lead = await _create_lead(api, workspace, headers)

    response = await api.put(
        workspace.path(f"/voice/context/{lead['id']}/summary"),
        headers=headers,
        json={"summary": "Wants a callback after 6pm.", "external_id": "call-abc"},
    )
    assert response.status_code == 200, response.text

    rows = await db_session.execute(
        select(VoiceCallContext).where(VoiceCallContext.lead_id == uuid.UUID(lead["id"]))
    )
    row = rows.scalar_one()
    assert row.last_call_summary == "Wants a callback after 6pm."
    assert row.call_count == 1
    assert row.last_call_external_id == "call-abc"

    notes = await db_session.execute(
        select(Action).where(
            Action.lead_id == uuid.UUID(lead["id"]), Action.kind == SystemActionKind.NOTE
        )
    )
    assert len(notes.scalars().all()) == 1


async def test_update_is_idempotent_on_external_id(
    api: AsyncClient, workspace: WorkspaceFixture, db_session: AsyncSession
) -> None:
    headers = await _admin(api, workspace)
    lead = await _create_lead(api, workspace, headers)

    first = await api.put(
        workspace.path(f"/voice/context/{lead['id']}/summary"),
        headers=headers,
        json={"summary": "Interested, budget pending.", "external_id": "exec_123"},
    )
    assert first.json()["written"] is True

    # The same call reported a second time — a retried webhook, in the
    # eventual Bolna world. Must not double-write.
    second = await api.put(
        workspace.path(f"/voice/context/{lead['id']}/summary"),
        headers=headers,
        json={"summary": "Interested, budget pending. (retry)", "external_id": "exec_123"},
    )
    assert second.status_code == 200
    assert second.json()["written"] is False
    # The original text survives — the retry's (different) text never lands.
    assert second.json()["context"]["last_call_summary"] == "Interested, budget pending."
    assert second.json()["context"]["call_count"] == 1

    notes = await db_session.execute(
        select(Action).where(
            Action.lead_id == uuid.UUID(lead["id"]), Action.kind == SystemActionKind.NOTE
        )
    )
    assert len(notes.scalars().all()) == 1, "a duplicate delivery must not double the timeline"


async def test_update_without_an_external_id_is_a_genuinely_new_call_each_time(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)
    lead = await _create_lead(api, workspace, headers)

    await api.put(
        workspace.path(f"/voice/context/{lead['id']}/summary"),
        headers=headers,
        json={"summary": "First call: asked about pricing."},
    )
    second = await api.put(
        workspace.path(f"/voice/context/{lead['id']}/summary"),
        headers=headers,
        json={"summary": "Second call: confirmed enrollment."},
    )

    assert second.json()["written"] is True
    assert second.json()["context"]["call_count"] == 2
    assert second.json()["context"]["last_call_summary"] == "Second call: confirmed enrollment."


# --- unknown / invalid input -----------------------------------------------


async def test_unknown_lead_id_is_not_found(api: AsyncClient, workspace: WorkspaceFixture) -> None:
    headers = await _admin(api, workspace)
    response = await api.get(workspace.path(f"/voice/context/{uuid.uuid4()}"), headers=headers)
    assert response.status_code == 404


async def test_unknown_phone_number_is_not_found(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)
    response = await api.get(
        workspace.path("/voice/context"), headers=headers, params={"phone": "9000000000"}
    )
    assert response.status_code == 404


async def test_invalid_phone_number_is_rejected(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """Distinct from "unknown": this number cannot even be parsed as one, so
    it is a 422 rather than a 404 — the same distinction every other field
    validation path in this product makes."""
    headers = await _admin(api, workspace)
    response = await api.get(
        workspace.path("/voice/context"), headers=headers, params={"phone": "not-a-phone"}
    )
    assert response.status_code == 422


# --- authorization -----------------------------------------------------------


async def test_missing_credentials_are_unauthorized(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)
    lead = await _create_lead(api, workspace, headers)

    response = await api.get(workspace.path(f"/voice/context/{lead['id']}"))

    assert response.status_code == 401


async def test_a_template_without_calling_access_is_forbidden(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    admin_headers = await _admin(api, workspace)
    lead = await _create_lead(api, workspace, admin_headers)

    marketing_headers = await _marketing(api, workspace)
    response = await api.get(
        workspace.path(f"/voice/context/{lead['id']}"), headers=marketing_headers
    )

    assert response.status_code == 403


async def test_a_template_with_calling_access_can_read_and_write(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """Proves the capability gate against a real non-admin role, not just the
    workspace owner (who bypasses every check via `admin_access`)."""
    admin_headers = await _admin(api, workspace)
    lead = await _create_lead(api, workspace, admin_headers)

    caller_headers = await _caller(api, workspace)
    read = await api.get(workspace.path(f"/voice/context/{lead['id']}"), headers=caller_headers)
    assert read.status_code == 200

    write = await api.put(
        workspace.path(f"/voice/context/{lead['id']}/summary"),
        headers=caller_headers,
        json={"summary": "Logged by the Caller role."},
    )
    assert write.status_code == 200


async def test_a_lead_in_another_workspace_is_not_found(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    db_session: AsyncSession,
    hasher: PasswordHasherService,
) -> None:
    """Cross-tenant isolation, mirroring `tests/isolation/`: a valid token for
    workspace B must not reach workspace A's context by any path."""
    headers_a = await _admin(api, workspace)
    lead = await _create_lead(api, workspace, headers_a)

    other = await build_workspace(
        db_session, hasher, name="Other Co", owner_email="owner@other.example"
    )
    other_headers = await _admin(api, other)

    response = await api.get(other.path(f"/voice/context/{lead['id']}"), headers=other_headers)

    assert response.status_code == 404


# --- persistence ---------------------------------------------------------------


async def test_context_persists_in_a_separate_database_session(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Component-test proxy for "survives an application restart": the
    summary is written through one HTTP request (one session, one
    transaction) and then read back through a brand new session with no
    shared in-process state — the only thing connecting them is the Postgres
    row. A real process restart is additionally demonstrated against the
    live Docker stack as part of this milestone's manual verification."""
    headers = await _admin(api, workspace)
    lead = await _create_lead(api, workspace, headers)

    await api.put(
        workspace.path(f"/voice/context/{lead['id']}/summary"),
        headers=headers,
        json={
            "summary": "Customer is interested in the Python course and prefers weekend classes.",
            "external_id": "call-1",
        },
    )

    async with session_factory() as fresh_session:
        rows = await fresh_session.execute(
            select(VoiceCallContext).where(VoiceCallContext.lead_id == uuid.UUID(lead["id"]))
        )
        row = rows.scalar_one()
        assert row.last_call_summary == (
            "Customer is interested in the Python course and prefers weekend classes."
        )
        assert row.call_count == 1


# --- never exposes secrets -----------------------------------------------------


async def test_the_context_response_never_carries_credentials(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)
    lead = await _create_lead(api, workspace, headers)
    await api.put(
        workspace.path(f"/voice/context/{lead['id']}/summary"),
        headers=headers,
        json={"summary": "Nothing sensitive was discussed."},
    )

    response = await api.get(workspace.path(f"/voice/context/{lead['id']}"), headers=headers)
    raw = response.text.lower()

    for forbidden in ("password", "hashed_key", "jwt_secret", "api_key"):
        assert forbidden not in raw
