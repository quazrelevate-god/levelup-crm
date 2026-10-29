"""What only the platform operator may change (docs/16).

A client's Root or Admin runs their own workspace, but what they bought — how
many workspaces exist, how many seats each has, which paid modules are on — is
the operator's. These tests are the revenue boundary: each one is a way a
client could otherwise give themselves something for free.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import WorkspaceFixture, build_workspace, login

from app.auth.passwords import PasswordHasherService
from app.config import Settings
from app.models import Workspace

OPERATOR = "operator@example.com"


@pytest.fixture
def hasher(wired_app: FastAPI) -> PasswordHasherService:
    hasher = wired_app.state.password_hasher
    assert isinstance(hasher, PasswordHasherService)
    return hasher


@pytest.fixture
async def client(db_session: AsyncSession, hasher: PasswordHasherService) -> WorkspaceFixture:
    """A paying client: its owner is the workspace's Root, not the operator."""
    fixture = await build_workspace(
        db_session, hasher, name="Client Co", owner_email="root@client.example"
    )
    fixture.workspace.seat_limit = 3
    await db_session.commit()
    return fixture


async def _workspace_count(db_session: AsyncSession) -> int:
    return int((await db_session.execute(select(func.count()).select_from(Workspace))).scalar_one())


# --- creating workspaces ------------------------------------------------------


async def test_a_client_cannot_create_another_workspace(
    api: AsyncClient,
    client: WorkspaceFixture,
    db_session: AsyncSession,
    make_operator: Callable[..., None],
) -> None:
    make_operator(OPERATOR)
    await login(api, client.owner)
    before = await _workspace_count(db_session)

    response = await api.post(
        "/api/v1/workspaces",
        headers=client.owner.auth,
        json={"name": "Free Extra", "seat_limit": 10_000},
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "operator_only"
    assert await _workspace_count(db_session) == before


async def test_with_no_operator_configured_nobody_can_create_a_workspace(
    api: AsyncClient, client: WorkspaceFixture, db_session: AsyncSession
) -> None:
    """The safe default: a deployment that forgot the setting sells nothing away."""
    await login(api, client.owner)
    before = await _workspace_count(db_session)

    response = await api.post(
        "/api/v1/workspaces", headers=client.owner.auth, json={"name": "Free Extra"}
    )

    assert response.status_code == 403
    assert await _workspace_count(db_session) == before


async def test_the_operator_can_create_a_workspace(
    api: AsyncClient,
    client: WorkspaceFixture,
    make_operator: Callable[..., None],
) -> None:
    make_operator(client.owner.user.email.upper())  # matched case-insensitively
    await login(api, client.owner)

    response = await api.post(
        "/api/v1/workspaces",
        headers=client.owner.auth,
        json={"name": "New Client", "seat_limit": 5},
    )

    assert response.status_code == 201, response.text
    assert response.json()["seat_limit"] == 5


# --- seats and paid modules ---------------------------------------------------


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("", {"seat_limit": 10_000}),
        ("", {"features": {"sales_group": True}}),
        ("", {"name": "Renamed", "seat_limit": 50}),
        ("/settings/preferences", {"features": {"sales_group": True}}),
    ],
    ids=["seats", "features", "seats-hidden-in-a-rename", "features-via-preferences"],
)
async def test_a_client_admin_cannot_change_what_they_bought(
    api: AsyncClient,
    client: WorkspaceFixture,
    db_session: AsyncSession,
    make_operator: Callable[..., None],
    path: str,
    body: dict[str, object],
) -> None:
    make_operator(OPERATOR)
    await login(api, client.owner)

    response = await api.patch(client.path(path), headers=client.owner.auth, json=body)

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "operator_only"
    await db_session.refresh(client.workspace)
    assert client.workspace.seat_limit == 3
    assert client.workspace.name == "Client Co"
    assert not client.workspace.features.get("sales_group")


async def test_a_client_admin_still_runs_everything_else(
    api: AsyncClient, client: WorkspaceFixture, make_operator: Callable[..., None]
) -> None:
    """The lock is on what was sold, not on the client's own settings."""
    make_operator(OPERATOR)
    await login(api, client.owner)

    renamed = await api.patch(client.path(), headers=client.owner.auth, json={"name": "Renamed"})
    timezone = await api.patch(
        client.path("/settings/preferences"),
        headers=client.owner.auth,
        json={"timezone": "Asia/Kolkata"},
    )

    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Renamed"
    assert timezone.status_code == 200, timezone.text


async def test_the_operator_can_change_seats_and_modules(
    api: AsyncClient, client: WorkspaceFixture, make_operator: Callable[..., None]
) -> None:
    make_operator(client.owner.user.email)
    await login(api, client.owner)

    response = await api.patch(
        client.path(),
        headers=client.owner.auth,
        json={"seat_limit": 8, "features": {"sales_group": True}},
    )

    assert response.status_code == 200, response.text
    assert response.json()["seat_limit"] == 8
    assert response.json()["features"]["sales_group"] is True


# --- telling the app who is who ------------------------------------------------


async def test_me_says_whether_the_caller_is_the_operator(
    api: AsyncClient, client: WorkspaceFixture, make_operator: Callable[..., None]
) -> None:
    await login(api, client.owner)

    make_operator(OPERATOR)
    as_client = await api.get("/api/v1/me", headers=client.owner.auth)
    make_operator(client.owner.user.email)
    as_operator = await api.get("/api/v1/me", headers=client.owner.auth)

    assert as_client.json()["is_platform_operator"] is False
    assert as_operator.json()["is_platform_operator"] is True


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [
        ("", []),
        ("a@x.com", ["a@x.com"]),
        ("a@x.com, b@y.com", ["a@x.com", "b@y.com"]),
        ('["a@x.com"]', ["a@x.com"]),
    ],
)
def test_the_operator_setting_reads_a_plain_list(
    raw: str, parsed: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLATFORM_OPERATOR_EMAILS", raw)
    assert Settings().platform_operator_emails == parsed
