"""AI calls only inside the permitted window (`voice_calling_hours`).

India's telemarketing rules allow commercial calls between 09:00 and 21:00.
The CRM enforces that itself, in the workspace's own timezone, for every way a
call can be placed — the lead panel's button, a follow-up, a batch runner — so
a misclick at 22:00 cannot ring anybody.

The rest of the suite runs with the check off (conftest), because it places
calls at whatever hour it happens to run. These tests switch it on with a
fixed clock.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import WorkspaceFixture, build_workspace, login

from app.auth.passwords import PasswordHasherService
from app.config import Settings
from app.integrations.bolna import BolnaSettings, RecordingBolnaClient
from app.models.voice import VoiceCallExecution
from app.services.voice_hours import parse_calling_hours, within_calling_hours

KOLKATA = "Asia/Kolkata"  # UTC+05:30
WINDOW = parse_calling_hours("09:00-21:00")


def _utc(hour: int, minute: int) -> dt.datetime:
    return dt.datetime(2026, 10, 24, hour, minute, tzinfo=dt.UTC)


# --- the window itself ----------------------------------------------------------


@pytest.mark.parametrize(
    ("utc", "inside"),
    [
        (_utc(3, 29), False),  # 08:59 in Kolkata
        (_utc(3, 30), True),  # 09:00 — the first permitted minute
        (_utc(15, 29), True),  # 20:59 — the last permitted minute
        (_utc(15, 30), False),  # 21:00 — closed
        (_utc(20, 0), False),  # 01:30 the next day
    ],
)
def test_the_window_is_read_in_the_workspaces_own_time(utc: dt.datetime, inside: bool) -> None:
    assert WINDOW is not None
    assert within_calling_hours(WINDOW, timezone=KOLKATA, now=utc) is inside


def test_an_empty_setting_means_no_restriction() -> None:
    assert parse_calling_hours("") is None
    assert parse_calling_hours("   ") is None
    assert parse_calling_hours(None) is None


@pytest.mark.parametrize("spec", ["9-21", "21:00-09:00", "09:00", "09:00-25:00", "nine to nine"])
def test_a_malformed_window_is_refused(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_calling_hours(spec)


def test_a_malformed_setting_fails_at_boot_not_on_a_call(settings: Settings) -> None:
    with pytest.raises(ValueError, match="calling hours"):
        Settings(**{**settings.model_dump(), "voice_calling_hours": "21:00-09:00"})


# --- through the trigger --------------------------------------------------------


@pytest.fixture
def bolna(wired_app: FastAPI) -> RecordingBolnaClient:
    client = RecordingBolnaClient()
    wired_app.state.bolna_client = client
    wired_app.state.bolna_settings = BolnaSettings(
        api_key="bn-test-key-do-not-use-0000000000",
        base_url="https://api.bolna.invalid",
        agent_id="11111111-2222-3333-4444-555555555555",
    )
    return client


@pytest.fixture
def hours_on(wired_app: FastAPI, settings: Settings) -> Iterator[None]:
    original = wired_app.state.settings
    wired_app.state.settings = original.model_copy(update={"voice_calling_hours": "09:00-21:00"})
    yield
    wired_app.state.settings = original
    wired_app.state.clock = None


@pytest.fixture
async def ws(db_session: AsyncSession, wired_app: FastAPI) -> WorkspaceFixture:
    hasher = wired_app.state.password_hasher
    assert isinstance(hasher, PasswordHasherService)
    fixture = await build_workspace(
        db_session, hasher, name="Hours Co", owner_email="owner@hours.example"
    )
    fixture.workspace.timezone = KOLKATA
    await db_session.commit()
    return fixture


async def _dial(api: AsyncClient, ws: WorkspaceFixture) -> Response:
    await login(api, ws.owner)
    lead = await api.post(
        ws.path("/leads"),
        headers=ws.owner.auth,
        json={"values": {"name": "Perumal", "phone": "+919087822357"}},
    )
    assert lead.status_code == 201, lead.text
    return await api.post(
        ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": lead.json()["id"]}
    )


@pytest.mark.integration
async def test_a_call_outside_the_window_is_refused_and_leaves_no_trace(
    api: AsyncClient,
    db_session: AsyncSession,
    wired_app: FastAPI,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    hours_on: None,
) -> None:
    wired_app.state.clock = lambda: _utc(16, 45)  # 22:15 in Kolkata

    response = await _dial(api, ws)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "outside_calling_hours"
    assert (detail["start"], detail["end"], detail["timezone"]) == ("09:00", "21:00", KOLKATA)
    assert "22:15" in detail["message"]
    assert bolna.calls == []
    rows = await db_session.execute(select(func.count()).select_from(VoiceCallExecution))
    assert rows.scalar() == 0


@pytest.mark.integration
async def test_a_call_inside_the_window_goes_through(
    api: AsyncClient,
    wired_app: FastAPI,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    hours_on: None,
) -> None:
    wired_app.state.clock = lambda: _utc(5, 0)  # 10:30 in Kolkata

    response = await _dial(api, ws)

    assert response.status_code == 200, response.text
    assert len(bolna.calls) == 1
