"""CRUD for `voice_extraction_mappings` — settings-side (docs/12).

Every assertion here is on the API surface an admin actually uses. Nothing
here exercises the write-back — that lives in
`test_voice_extraction_writeback.py` — which lets each file fail for exactly
one reason and keeps the CRUD tests small.
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

pytestmark = pytest.mark.integration


@pytest.fixture
def hasher(wired_app: FastAPI) -> PasswordHasherService:
    hasher = wired_app.state.password_hasher
    assert isinstance(hasher, PasswordHasherService)
    return hasher


@pytest.fixture
async def workspace(db_session: AsyncSession, hasher: PasswordHasherService) -> WorkspaceFixture:
    fixture = await build_workspace(
        db_session, hasher, name="Mapping Co", owner_email="owner@mappingco.example"
    )
    await add_member(
        db_session, hasher, fixture,
        key="marketing", email="marketing@mappingco.example", template_name="Marketing",
    )
    return fixture


@pytest.fixture
async def other_workspace(
    db_session: AsyncSession, hasher: PasswordHasherService
) -> WorkspaceFixture:
    return await build_workspace(
        db_session, hasher, name="Other Co", owner_email="owner@otherco.example"
    )


async def _admin(api: AsyncClient, workspace: WorkspaceFixture) -> dict[str, str]:
    await login(api, workspace.owner)
    return workspace.owner.auth


async def _create(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    headers: dict[str, str],
    *,
    disposition_name: str = "Customer Name",
    target_field_key: str = "name",
    min_confidence: float = 0.70,
    is_enabled: bool = True,
) -> Any:
    return await api.post(
        workspace.path("/voice/extraction-mappings"),
        headers=headers,
        json={
            "disposition_name": disposition_name,
            "target_field_key": target_field_key,
            "min_confidence": min_confidence,
            "is_enabled": is_enabled,
        },
    )


# --- the happy path --------------------------------------------------------


async def test_create_read_update_delete(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)

    created = await _create(api, workspace, headers)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["disposition_name"] == "Customer Name"
    assert body["target_field_key"] == "name"
    assert body["min_confidence"] == pytest.approx(0.70)
    assert body["is_enabled"] is True
    mapping_id = body["id"]

    listed = await api.get(workspace.path("/voice/extraction-mappings"), headers=headers)
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [mapping_id]

    read = await api.get(
        workspace.path(f"/voice/extraction-mappings/{mapping_id}"), headers=headers
    )
    assert read.status_code == 200
    assert read.json()["id"] == mapping_id

    patched = await api.patch(
        workspace.path(f"/voice/extraction-mappings/{mapping_id}"),
        headers=headers,
        json={"min_confidence": 0.85, "is_enabled": False},
    )
    assert patched.status_code == 200
    assert patched.json()["min_confidence"] == pytest.approx(0.85)
    assert patched.json()["is_enabled"] is False

    deleted = await api.delete(
        workspace.path(f"/voice/extraction-mappings/{mapping_id}"), headers=headers
    )
    assert deleted.status_code == 204

    gone = await api.get(
        workspace.path(f"/voice/extraction-mappings/{mapping_id}"), headers=headers
    )
    assert gone.status_code == 404


# --- validation -----------------------------------------------------------


async def test_target_field_must_exist(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)
    response = await _create(api, workspace, headers, target_field_key="not_a_real_field")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unknown_field"


async def test_disposition_name_is_unique_per_workspace(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)
    first = await _create(
        api, workspace, headers, disposition_name="Budget", target_field_key="name"
    )
    assert first.status_code == 201

    dupe = await _create(
        api, workspace, headers, disposition_name="Budget", target_field_key="email"
    )
    assert dupe.status_code == 409
    assert dupe.json()["detail"]["code"] == "duplicate_disposition"


async def test_min_confidence_must_be_in_range(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)
    below = await _create(api, workspace, headers, min_confidence=-0.1)
    assert below.status_code == 422
    above = await _create(api, workspace, headers, min_confidence=1.1)
    assert above.status_code == 422


async def test_summary_disposition_cannot_be_mapped(
    api: AsyncClient, wired_app: FastAPI, workspace: WorkspaceFixture
) -> None:
    """A mapping named after the configured call-summary disposition would put
    the whole call recap into a lead field. Refused at create time."""
    wired_app.state.settings.bolna_summary_disposition = "Call Recap"
    try:
        headers = await _admin(api, workspace)
        response = await _create(api, workspace, headers, disposition_name="Call Recap")
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "summary_disposition_conflict"

        # Case-insensitive — a "call recap" mapping is refused too.
        response = await _create(api, workspace, headers, disposition_name="call recap")
        assert response.status_code == 422
    finally:
        wired_app.state.settings.bolna_summary_disposition = None


async def test_summary_disposition_check_is_case_insensitive_on_rename(
    api: AsyncClient, wired_app: FastAPI, workspace: WorkspaceFixture
) -> None:
    """A rename to the summary disposition name is refused too."""
    headers = await _admin(api, workspace)
    created = await _create(api, workspace, headers, disposition_name="Customer Name")
    assert created.status_code == 201
    mapping_id = created.json()["id"]

    wired_app.state.settings.bolna_summary_disposition = "Call Recap"
    try:
        rename = await api.patch(
            workspace.path(f"/voice/extraction-mappings/{mapping_id}"),
            headers=headers,
            json={"disposition_name": "call recap"},
        )
        assert rename.status_code == 422
        assert rename.json()["detail"]["code"] == "summary_disposition_conflict"
    finally:
        wired_app.state.settings.bolna_summary_disposition = None


# --- authorization / isolation --------------------------------------------


async def test_unauthenticated_read_is_refused(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    response = await api.get(workspace.path("/voice/extraction-mappings"))
    assert response.status_code == 401


async def test_a_template_without_manage_webhooks_cannot_mutate(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """Marketing has no automations group at all."""
    await login(api, workspace.members["marketing"])
    headers = workspace.members["marketing"].auth
    response = await _create(api, workspace, headers)
    assert response.status_code == 403


async def test_workspace_isolation(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    other_workspace: WorkspaceFixture,
) -> None:
    """A mapping in workspace A is invisible in workspace B."""
    a_headers = await _admin(api, workspace)
    created = await _create(api, workspace, a_headers)
    assert created.status_code == 201
    mapping_id = created.json()["id"]

    await login(api, other_workspace.owner)
    b_headers = other_workspace.owner.auth

    listed_b = await api.get(
        other_workspace.path("/voice/extraction-mappings"), headers=b_headers
    )
    assert listed_b.status_code == 200
    assert listed_b.json() == []

    peeked_b = await api.get(
        other_workspace.path(f"/voice/extraction-mappings/{mapping_id}"),
        headers=b_headers,
    )
    assert peeked_b.status_code == 404  # 404, not 403 — a 403 would confirm it exists.


async def test_reading_from_another_workspace_returns_404(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    other_workspace: WorkspaceFixture,
) -> None:
    a_headers = await _admin(api, workspace)
    created = await _create(api, workspace, a_headers)
    mapping_id = created.json()["id"]

    await login(api, other_workspace.owner)
    unknown = await api.get(
        other_workspace.path(f"/voice/extraction-mappings/{uuid.UUID(mapping_id)}"),
        headers=other_workspace.owner.auth,
    )
    assert unknown.status_code == 404
