"""Configuring which stages a finished AI call moves a lead to.

`workspace.settings` is an open JSONB bag, and exposing all of it would accept
anything and validate none of it. So there is one narrow door instead:
`/settings/voice-outcome-stages` takes exactly two stage ids, checks both
against this workspace's live active stages, and leaves every other setting
alone.

What these tests pin is the refusals. Writing the happy path is easy; what
makes this safe to hand an admin is that a cross-workspace id, an archived
stage, the won stage, the same stage twice, and a non-admin caller are each
turned away *before* anything is stored.

No stage id is written into this file — they are read back from whatever the
workspace was provisioned with.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import WorkspaceFixture, add_member, build_workspace, login

from app.auth.passwords import PasswordHasherService
from app.models.enums import StageKind
from app.models.pipeline import Stage
from app.services.voice_settings import (
    VOICE_OUTCOME_STAGES,
    read_outcome_stages,
    write_outcome_stages,
)

PATH = "/settings/voice-outcome-stages"


@pytest.fixture
def hasher(wired_app: FastAPI) -> PasswordHasherService:
    hasher = wired_app.state.password_hasher
    assert isinstance(hasher, PasswordHasherService)
    return hasher


@pytest.fixture
async def ws(db_session: AsyncSession, hasher: PasswordHasherService) -> WorkspaceFixture:
    fixture = await build_workspace(
        db_session, hasher, name="Outcome Stage Co", owner_email="owner@outcome.example"
    )
    await add_member(
        db_session, hasher, fixture, key="rep", email="rep@outcome.example", template_name="Caller"
    )
    return fixture


async def _stage(session: AsyncSession, ws: WorkspaceFixture, kind: StageKind) -> Stage:
    rows = await session.execute(
        select(Stage).where(Stage.workspace_id == ws.id, Stage.kind == kind)
    )
    return rows.scalars().first()  # type: ignore[return-value]


async def _active_stages(session: AsyncSession, ws: WorkspaceFixture) -> list[Stage]:
    rows = await session.execute(
        select(Stage).where(
            Stage.workspace_id == ws.id,
            Stage.kind == StageKind.ACTIVE,
            Stage.is_archived.is_(False),
        )
    )
    return list(rows.scalars().all())


async def _two_active(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture
) -> tuple[str, str]:
    """The provisioned set has one ACTIVE stage, so a second is created."""
    existing = await _active_stages(db_session, ws)
    assert existing, "the provisioned pipeline should have one active stage"
    created = await api.post(
        ws.path("/settings/stages"), headers=ws.owner.auth, json={"label": "Busy"}
    )
    assert created.status_code == 201, created.text
    return str(existing[0].id), str(created.json()["id"])


async def _settings(session: AsyncSession, ws: WorkspaceFixture) -> dict[str, Any]:
    await session.refresh(ws.workspace)
    return dict(ws.workspace.settings or {})


# --- the happy path -----------------------------------------------------------


async def test_nothing_is_configured_by_default(api: AsyncClient, ws: WorkspaceFixture) -> None:
    """Every existing workspace. Both null, and the automation stays off."""
    await login(api, ws.owner)
    response = await api.get(ws.path(PATH), headers=ws.owner.auth)
    assert response.status_code == 200, response.text
    assert response.json() == {"answered": None, "unanswered": None}


async def test_the_pair_is_stored_and_read_back_in_full(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture
) -> None:
    await login(api, ws.owner)
    answered, unanswered = await _two_active(api, db_session, ws)

    put = await api.put(
        ws.path(PATH), headers=ws.owner.auth, json={"answered": answered, "unanswered": unanswered}
    )
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["answered"]["id"] == answered
    assert body["unanswered"]["id"] == unanswered
    # The response carries the label and kind, so a caller can see what it set.
    assert body["answered"]["kind"] == "ACTIVE"
    assert body["unanswered"]["label"] == "Busy"

    got = await api.get(ws.path(PATH), headers=ws.owner.auth)
    assert got.status_code == 200, got.text
    assert got.json() == body

    # And it landed in the one namespace, as ids.
    stored = await _settings(db_session, ws)
    assert stored[VOICE_OUTCOME_STAGES] == {"answered": answered, "unanswered": unanswered}
    assert read_outcome_stages(stored) == (uuid.UUID(answered), uuid.UUID(unanswered))


async def test_unrelated_settings_are_preserved(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture
) -> None:
    """The door is narrow in both directions: it writes one key and no other."""
    ws.workspace.settings = {"something_else": {"kept": True}, "a_number": 7}
    await db_session.commit()

    await login(api, ws.owner)
    answered, unanswered = await _two_active(api, db_session, ws)
    put = await api.put(
        ws.path(PATH), headers=ws.owner.auth, json={"answered": answered, "unanswered": unanswered}
    )
    assert put.status_code == 200, put.text

    stored = await _settings(db_session, ws)
    assert stored["something_else"] == {"kept": True}
    assert stored["a_number"] == 7
    assert VOICE_OUTCOME_STAGES in stored


async def test_the_mapping_can_be_replaced(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture
) -> None:
    await login(api, ws.owner)
    answered, unanswered = await _two_active(api, db_session, ws)
    third = await api.post(
        ws.path("/settings/stages"), headers=ws.owner.auth, json={"label": "Callback due"}
    )
    assert third.status_code == 201, third.text

    for pair in ((answered, unanswered), (str(third.json()["id"]), unanswered)):
        put = await api.put(
            ws.path(PATH), headers=ws.owner.auth, json={"answered": pair[0], "unanswered": pair[1]}
        )
        assert put.status_code == 200, put.text
        assert put.json()["answered"]["id"] == pair[0]


async def test_clearing_stops_the_behaviour_and_keeps_other_settings(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture
) -> None:
    ws.workspace.settings = {"something_else": True}
    await db_session.commit()
    await login(api, ws.owner)
    answered, unanswered = await _two_active(api, db_session, ws)
    await api.put(
        ws.path(PATH), headers=ws.owner.auth, json={"answered": answered, "unanswered": unanswered}
    )

    deleted = await api.delete(ws.path(PATH), headers=ws.owner.auth)
    assert deleted.status_code == 204, deleted.text

    stored = await _settings(db_session, ws)
    assert VOICE_OUTCOME_STAGES not in stored
    assert stored["something_else"] is True
    got = await api.get(ws.path(PATH), headers=ws.owner.auth)
    assert got.json() == {"answered": None, "unanswered": None}


# --- the refusals -------------------------------------------------------------


async def test_the_same_stage_for_both_outcomes_is_refused(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture
) -> None:
    await login(api, ws.owner)
    answered, _ = await _two_active(api, db_session, ws)
    response = await api.put(
        ws.path(PATH), headers=ws.owner.auth, json={"answered": answered, "unanswered": answered}
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "stage_mapping_ambiguous"
    assert VOICE_OUTCOME_STAGES not in await _settings(db_session, ws)


async def test_an_unknown_stage_is_refused(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture
) -> None:
    await login(api, ws.owner)
    answered, _ = await _two_active(api, db_session, ws)
    response = await api.put(
        ws.path(PATH),
        headers=ws.owner.auth,
        json={"answered": answered, "unanswered": str(uuid.uuid4())},
    )
    assert response.status_code == 404, response.text
    assert VOICE_OUTCOME_STAGES not in await _settings(db_session, ws)


async def test_another_workspaces_stage_is_refused_as_absent(
    api: AsyncClient,
    db_session: AsyncSession,
    hasher: PasswordHasherService,
    ws: WorkspaceFixture,
) -> None:
    """A cross-tenant id must read exactly like one that does not exist."""
    other = await build_workspace(
        db_session, hasher, name="Other Outcome Co", owner_email="owner@otheroutcome.example"
    )
    theirs = await _stage(db_session, other, StageKind.ACTIVE)

    await login(api, ws.owner)
    answered, _ = await _two_active(api, db_session, ws)
    response = await api.put(
        ws.path(PATH),
        headers=ws.owner.auth,
        json={"answered": answered, "unanswered": str(theirs.id)},
    )
    assert response.status_code == 404, response.text
    assert VOICE_OUTCOME_STAGES not in await _settings(db_session, ws)


@pytest.mark.parametrize("kind", [StageKind.INITIAL, StageKind.WON, StageKind.LOST])
async def test_a_singleton_stage_is_refused(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, kind: StageKind
) -> None:
    """Won, lost and the initial stage are the pipeline's own ends.

    Moving a lead into one of them is a decision about the deal, not a record
    of a phone call.
    """
    await login(api, ws.owner)
    answered, _ = await _two_active(api, db_session, ws)
    singleton = await _stage(db_session, ws, kind)

    response = await api.put(
        ws.path(PATH),
        headers=ws.owner.auth,
        json={"answered": answered, "unanswered": str(singleton.id)},
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "stage_kind_not_active"
    assert detail["kind"] == kind.value
    assert VOICE_OUTCOME_STAGES not in await _settings(db_session, ws)


async def test_an_archived_stage_is_refused(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture
) -> None:
    await login(api, ws.owner)
    answered, unanswered = await _two_active(api, db_session, ws)
    archived = await api.delete(ws.path(f"/settings/stages/{unanswered}"), headers=ws.owner.auth)
    assert archived.status_code in (200, 204), archived.text

    response = await api.put(
        ws.path(PATH), headers=ws.owner.auth, json={"answered": answered, "unanswered": unanswered}
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "stage_archived"
    assert VOICE_OUTCOME_STAGES not in await _settings(db_session, ws)


async def test_a_stage_archived_after_configuration_reads_as_absent(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture
) -> None:
    """The mapping describes intent; the read resolves it against reality."""
    await login(api, ws.owner)
    answered, unanswered = await _two_active(api, db_session, ws)
    await api.put(
        ws.path(PATH), headers=ws.owner.auth, json={"answered": answered, "unanswered": unanswered}
    )
    await api.delete(ws.path(f"/settings/stages/{unanswered}"), headers=ws.owner.auth)

    got = await api.get(ws.path(PATH), headers=ws.owner.auth)
    assert got.status_code == 200, got.text
    assert got.json()["answered"]["id"] == answered
    assert got.json()["unanswered"] is None


# --- the schema ---------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"answered": str(uuid.uuid4())},
        {"unanswered": str(uuid.uuid4())},
        {"answered": "not-a-uuid", "unanswered": str(uuid.uuid4())},
    ],
)
async def test_a_half_or_malformed_body_is_refused(
    api: AsyncClient, ws: WorkspaceFixture, body: dict[str, Any]
) -> None:
    """Both ids are required: a half-configured mapping reads as a bug."""
    await login(api, ws.owner)
    response = await api.put(ws.path(PATH), headers=ws.owner.auth, json=body)
    assert response.status_code == 422, response.text


async def test_an_unexpected_field_is_refused(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture
) -> None:
    """`extra="forbid"`: this endpoint is not a way into the settings bag."""
    await login(api, ws.owner)
    answered, unanswered = await _two_active(api, db_session, ws)
    response = await api.put(
        ws.path(PATH),
        headers=ws.owner.auth,
        json={"answered": answered, "unanswered": unanswered, "jwt_secret": "nope"},
    )
    assert response.status_code == 422, response.text
    assert "jwt_secret" not in await _settings(db_session, ws)


# --- authorisation ------------------------------------------------------------


async def test_a_non_admin_cannot_configure_it(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture
) -> None:
    await login(api, ws.owner)
    answered, unanswered = await _two_active(api, db_session, ws)

    rep = ws.members["rep"]
    await login(api, rep)
    for method, call in (
        (
            "PUT",
            api.put(
                ws.path(PATH),
                headers=rep.auth,
                json={"answered": answered, "unanswered": unanswered},
            ),
        ),
        ("DELETE", api.delete(ws.path(PATH), headers=rep.auth)),
    ):
        response = await call
        assert response.status_code == 403, f"{method}: {response.text}"
    assert VOICE_OUTCOME_STAGES not in await _settings(db_session, ws)


async def test_an_unauthenticated_caller_is_refused(api: AsyncClient, ws: WorkspaceFixture) -> None:
    for response in (
        await api.get(ws.path(PATH)),
        await api.put(
            ws.path(PATH), json={"answered": str(uuid.uuid4()), "unanswered": str(uuid.uuid4())}
        ),
        await api.delete(ws.path(PATH)),
    ):
        assert response.status_code == 401, response.text


async def test_one_workspace_cannot_configure_another(
    api: AsyncClient,
    db_session: AsyncSession,
    hasher: PasswordHasherService,
    ws: WorkspaceFixture,
) -> None:
    other = await build_workspace(
        db_session, hasher, name="Third Outcome Co", owner_email="owner@thirdoutcome.example"
    )
    await login(api, ws.owner)
    answered, unanswered = await _two_active(api, db_session, ws)

    response = await api.put(
        other.path(PATH),
        headers=ws.owner.auth,
        json={"answered": answered, "unanswered": unanswered},
    )
    assert response.status_code in (403, 404), response.text
    await db_session.refresh(other.workspace)
    assert VOICE_OUTCOME_STAGES not in (other.workspace.settings or {})


# --- the settings helpers, without a database ---------------------------------


def test_write_preserves_other_keys_and_returns_a_new_dict() -> None:
    answered, unanswered = uuid.uuid4(), uuid.uuid4()
    original = {"other": 1}
    result = write_outcome_stages(original, answered=answered, unanswered=unanswered)
    assert result[VOICE_OUTCOME_STAGES] == {
        "answered": str(answered),
        "unanswered": str(unanswered),
    }
    assert result["other"] == 1
    # A new dict, so SQLAlchemy sees the JSONB column change.
    assert original == {"other": 1}


@pytest.mark.parametrize(
    "settings",
    [
        None,
        {},
        {VOICE_OUTCOME_STAGES: None},
        {VOICE_OUTCOME_STAGES: "not-a-dict"},
        {VOICE_OUTCOME_STAGES: {"answered": "not-a-uuid"}},
        {VOICE_OUTCOME_STAGES: {}},
    ],
)
def test_a_malformed_mapping_reads_as_absent_rather_than_raising(settings: Any) -> None:
    """Read on the webhook path: a settings typo must not cost a call result."""
    assert read_outcome_stages(settings) == (None, None)
