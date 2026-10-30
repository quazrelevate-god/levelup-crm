"""Call facts: reference details the agent is told, chosen by rule (docs/17).

A workspace keeps sets of facts (batch dates, mentors) each with a filter rule.
On every call the first active set the lead matches, in priority order,
supplies its facts as variables and as lines in the agent's lead notes.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import WorkspaceFixture, add_member, build_workspace, login

from app.auth.passwords import PasswordHasherService


def _program_is(value: str) -> dict[str, Any]:
    return {
        "type": "group",
        "op": "AND",
        "children": [{"type": "field", "key": "program", "op": "eq", "value": value}],
    }


@pytest.fixture
def hasher(wired_app: FastAPI) -> PasswordHasherService:
    hasher = wired_app.state.password_hasher
    assert isinstance(hasher, PasswordHasherService)
    return hasher


@pytest.fixture
async def ws(
    api: AsyncClient, db_session: AsyncSession, hasher: PasswordHasherService
) -> WorkspaceFixture:
    fixture = await build_workspace(
        db_session, hasher, name="Facts Co", owner_email="owner@facts.example"
    )
    fixture.workspace.default_country_code = "91"
    await db_session.commit()
    await login(api, fixture.owner)
    program = await api.post(
        fixture.path("/settings/lead-fields"),
        headers=fixture.owner.auth,
        json={"label": "Program", "field_type": "TEXT"},
    )
    assert program.status_code == 201, program.text
    return fixture


async def _set(api: AsyncClient, ws: WorkspaceFixture, **body: Any) -> Any:
    return await api.post(ws.path("/settings/call-facts"), headers=ws.owner.auth, json=body)


async def _lead(api: AsyncClient, ws: WorkspaceFixture, phone: str, program: str) -> str:
    response = await api.post(
        ws.path("/leads"),
        headers=ws.owner.auth,
        json={"values": {"name": "Asha", "phone": phone, "program": program}},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _sent(api: AsyncClient, ws: WorkspaceFixture, lead_id: str) -> dict[str, Any]:
    preview = await api.post(
        ws.path("/voice/calls/preview"), headers=ws.owner.auth, json={"lead_id": lead_id}
    )
    assert preview.status_code == 200, preview.text
    body: dict[str, Any] = preview.json()["user_data"]
    return body


async def test_a_lead_that_matches_gets_its_facts_as_variables_and_notes(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    created = await _set(
        api,
        ws,
        name="VE batch 6",
        conditions=_program_is("VE"),
        facts=[
            {"label": "Batch start date", "value": "12 December 2026"},
            {"label": "Mentors", "value": "Asha and Ravi"},
        ],
    )
    assert created.status_code == 201, created.text
    assert [f["key"] for f in created.json()["facts"]] == ["batch_start_date", "mentors"]
    assert created.json()["rule"] != "Every lead"

    sent = await _sent(api, ws, await _lead(api, ws, "+919000001001", "VE"))

    assert sent["batch_start_date"] == "12 December 2026"
    assert sent["mentors"] == "Asha and Ravi"
    assert "Batch start date: 12 December 2026" in sent["crm_lead_context"]


async def test_a_lead_that_matches_nothing_gets_each_fact_blank(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    """Blank, never left out: an absent key reaches the agent as a raw {{key}}."""
    await _set(
        api,
        ws,
        name="VE batch 6",
        conditions=_program_is("VE"),
        facts=[{"label": "Batch start date", "value": "12 December 2026"}],
    )

    sent = await _sent(api, ws, await _lead(api, ws, "+919000001002", "Filmmaking"))

    assert sent["batch_start_date"] == ""
    assert "Batch start date" not in sent.get("crm_lead_context", "")


async def test_the_lowest_priority_number_wins_and_an_empty_rule_is_the_default(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    await _set(
        api,
        ws,
        name="Default",
        priority=100,
        facts=[{"label": "Batch start date", "value": "Next month"}],
    )
    await _set(
        api,
        ws,
        name="VE batch 7",
        priority=2,
        conditions=_program_is("VE"),
        facts=[{"label": "Batch start date", "value": "15 February 2027"}],
    )
    await _set(
        api,
        ws,
        name="VE batch 6",
        priority=1,
        conditions=_program_is("VE"),
        facts=[{"label": "Batch start date", "value": "12 December 2026"}],
    )

    ve = await _sent(api, ws, await _lead(api, ws, "+919000001003", "VE"))
    other = await _sent(api, ws, await _lead(api, ws, "+919000001004", "Filmmaking"))

    assert ve["batch_start_date"] == "12 December 2026"
    assert other["batch_start_date"] == "Next month"


async def test_an_archived_set_no_longer_applies(api: AsyncClient, ws: WorkspaceFixture) -> None:
    created = await _set(
        api, ws, name="Old", facts=[{"label": "Mentors", "value": "Retired mentor"}]
    )
    archived = await api.delete(
        ws.path(f"/settings/call-facts/{created.json()['id']}"), headers=ws.owner.auth
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["is_active"] is False

    sent = await _sent(api, ws, await _lead(api, ws, "+919000001005", "VE"))

    assert "mentors" not in sent


@pytest.mark.parametrize(
    ("label", "code"),
    [
        ("Program", "fact_name_in_use"),  # a lead field's key
        ("CRM Stage", "fact_name_in_use"),  # a reserved crm_* variable
        ("Call opening", "fact_name_in_use"),
    ],
)
async def test_a_fact_cannot_take_a_name_already_in_use(
    api: AsyncClient, ws: WorkspaceFixture, label: str, code: str
) -> None:
    response = await _set(api, ws, name="Clash", facts=[{"label": label, "value": "x"}])

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == code


async def test_two_facts_with_one_name_and_a_broken_rule_are_refused(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    twice = await _set(
        api,
        ws,
        name="Twice",
        facts=[{"label": "Mentors", "value": "a"}, {"label": "mentors", "value": "b"}],
    )
    broken = await _set(api, ws, name="Broken", conditions={"type": "nonsense"})

    assert twice.json()["detail"]["code"] == "duplicate_fact"
    assert broken.json()["detail"]["code"] == "invalid_call_fact_rule"


async def test_editing_a_set_changes_what_the_next_call_says(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    created = await _set(
        api, ws, name="VE", facts=[{"label": "Batch start date", "value": "12 December"}]
    )

    edited = await api.patch(
        ws.path(f"/settings/call-facts/{created.json()['id']}"),
        headers=ws.owner.auth,
        json={"facts": [{"label": "Batch start date", "value": "19 December"}]},
    )

    assert edited.status_code == 200, edited.text
    sent = await _sent(api, ws, await _lead(api, ws, "+919000001006", "VE"))
    assert sent["batch_start_date"] == "19 December"


async def test_facts_are_listed_with_the_agent_variables(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    await _set(api, ws, name="VE", facts=[{"label": "Mentors", "value": "Asha"}])

    response = await api.get(ws.path("/voice/variables"), headers=ws.owner.auth)

    facts = [v for v in response.json() if v["kind"] == "fact"]
    assert [(v["name"], v["label"]) for v in facts] == [("mentors", "Mentors")]


async def test_only_a_workspace_admin_can_manage_facts(
    api: AsyncClient,
    ws: WorkspaceFixture,
    db_session: AsyncSession,
    hasher: PasswordHasherService,
) -> None:
    caller = await add_member(
        db_session, hasher, ws, key="caller", email="caller@facts.example", template_name="Caller"
    )
    await login(api, caller)

    listed = await api.get(ws.path("/settings/call-facts"), headers=caller.auth)
    created = await api.post(
        ws.path("/settings/call-facts"), headers=caller.auth, json={"name": "Sneaky"}
    )

    assert listed.status_code == 403
    assert created.status_code == 403
