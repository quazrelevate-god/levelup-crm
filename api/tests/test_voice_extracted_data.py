"""Bolna's grouped `extracted_data`, end to end.

The bug this file exists for: Bolna groups its extractions. A real call came
back as

    {"General": {"Call Summary": {"subjective": "…"}}}

and the CRM read the *top* level as the extraction's name. "General" is a
group, not an extraction, so it matched no mapping, was reported as
`extraction_unmapped: ["General"]`, and the only thing anybody wanted to read —
the Call Summary — was never surfaced as an extraction at all.

`flatten_extractions` now reads group → name → value, so both shapes work:
grouped, and the flat `{"Customer Name": {"value": …}}` the earlier tests use.
Nothing is dropped on the way: an extraction with no mapping is still returned
for display, because "not written to a lead field" is not "discarded".
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import WorkspaceFixture, build_workspace, login

from app.auth.passwords import PasswordHasherService
from app.integrations.bolna import BolnaSettings, RecordingBolnaClient
from app.services.voice_postcall import extractions_from_payload, flatten_extractions

FAKE_BOLNA_KEY = "bn-test-key-do-not-use-0000000000"
FAKE_AGENT_ID = "11111111-2222-3333-4444-555555555555"

NAME = "Test Customer"
PHONE = "+919000000001"
EMAIL = "test.customer@example.com"
SUMMARY = "Confirmed enrolment and asked for the fee structure."
TRANSCRIPT = "assistant: Hello.\nuser: Yes, I am interested."

#: Exactly the shape the real call produced.
GROUPED = {"General": {"Call Summary": {"subjective": SUMMARY}}}
#: The shape the earlier tests and the documented contract use.
FLAT = {"Customer Name": {"value": "Asha", "confidence": 0.94}}


# --- the normaliser, on its own ------------------------------------------------


def test_a_grouped_extraction_reads_as_group_and_name() -> None:
    [item] = flatten_extractions(GROUPED)
    assert item.group == "General"
    assert item.name == "Call Summary"
    assert item.path == "General / Call Summary"
    assert item.value == SUMMARY
    assert item.confidence is None
    # The node is kept whole for anything the flattening summarised away.
    assert item.raw == {"subjective": SUMMARY}


def test_a_flat_extraction_is_unchanged_by_the_fix() -> None:
    """The documented shape must behave exactly as it always did."""
    [item] = flatten_extractions(FLAT)
    assert item.group is None
    assert item.name == "Customer Name"
    assert item.path == "Customer Name"
    assert item.value == "Asha"
    assert item.confidence == 0.94


def test_a_group_with_several_extractions_becomes_several_entries() -> None:
    entries = flatten_extractions(
        {
            "General": {"Call Summary": {"subjective": SUMMARY}, "Sentiment": {"value": "warm"}},
            "Course": {"value": "Breakthrough Filmmaking", "confidence": 0.8},
        }
    )
    assert [(item.group, item.name) for item in entries] == [
        ("General", "Call Summary"),
        ("General", "Sentiment"),
        (None, "Course"),
    ]
    assert entries[1].value == "warm"
    assert entries[2].value == "Breakthrough Filmmaking"


def test_empty_and_malformed_payloads_are_not_errors() -> None:
    assert flatten_extractions({}) == []
    assert flatten_extractions(None) == []
    assert flatten_extractions("nonsense") == []
    assert flatten_extractions([1, 2]) == []
    # A shape nobody anticipated still yields something displayable.
    [odd] = flatten_extractions({"Thing": ["a", "b"]})
    assert odd.name == "Thing"
    assert odd.value == ["a", "b"]


def test_an_empty_leaf_stays_empty_rather_than_becoming_its_node() -> None:
    """The regression an existing write-back test caught.

    `{"value": null, "confidence": 0.99}` means "nothing was extracted". An
    earlier version of the flattener fell through to returning the whole node,
    which turned an empty extraction into a dict — and the write-back then
    tried to store that dict in a text field.
    """
    for empty in (None, "", "   "):
        [item] = flatten_extractions({"Customer Name": {"value": empty, "confidence": 0.99}})
        assert item.value in (None, empty)
        assert not isinstance(item.value, dict)
        assert item.confidence == 0.99


def test_a_bare_scalar_extraction_survives() -> None:
    [item] = flatten_extractions({"Course": "Breakthrough Filmmaking"})
    assert item.value == "Breakthrough Filmmaking"
    assert item.group is None


def test_extractions_are_read_from_the_first_source_that_has_any() -> None:
    """`extracted_data` first; the alternates only when it is absent."""
    raw, entries = extractions_from_payload({"extracted_data": GROUPED})
    assert raw == GROUPED
    assert entries[0].name == "Call Summary"

    raw, entries = extractions_from_payload(
        {"extracted_data": None, "custom_extractions": {"Course": {"value": "Editing"}}}
    )
    assert entries[0].name == "Course"
    assert raw == {"Course": {"value": "Editing"}}

    assert extractions_from_payload({"extracted_data": {}}) == ({}, [])
    assert extractions_from_payload(None) == ({}, [])


# --- through the API -----------------------------------------------------------


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
        api_key=FAKE_BOLNA_KEY, base_url="https://api.bolna.invalid", agent_id=FAKE_AGENT_ID
    )
    return client


@pytest.fixture
async def ws(db_session: AsyncSession, hasher: PasswordHasherService) -> WorkspaceFixture:
    fixture = await build_workspace(
        db_session, hasher, name="Extracted Data Co", owner_email="owner@extracted.example"
    )
    fixture.workspace.identity_field_id = fixture.fields["name"].id
    await db_session.commit()
    return fixture


async def _key(api: AsyncClient, ws: WorkspaceFixture) -> str:
    await login(api, ws.owner)
    response = await api.post(
        ws.path("/settings/api-keys"),
        headers=ws.owner.auth,
        json={"name": "Bolna webhook", "permission_template_id": str(ws.templates["Root"].id)},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["key"])


async def _lead(api: AsyncClient, ws: WorkspaceFixture) -> dict[str, Any]:
    response = await api.post(
        ws.path("/leads"),
        headers=ws.owner.auth,
        json={"values": {"name": NAME, "phone": PHONE, "email": EMAIL}},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def _mapping(api: AsyncClient, ws: WorkspaceFixture, *, name: str, field: str) -> None:
    response = await api.post(
        ws.path("/voice/extraction-mappings"),
        headers=ws.owner.auth,
        json={
            "disposition_name": name,
            "target_field_key": field,
            "min_confidence": 0.5,
            "is_enabled": True,
        },
    )
    assert response.status_code == 201, response.text


async def _run_call(
    api: AsyncClient,
    ws: WorkspaceFixture,
    key: str,
    lead_id: str,
    extracted: Any,
) -> dict[str, Any]:
    trigger = await api.post(
        ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": lead_id}
    )
    assert trigger.status_code == 200, trigger.text
    body = {
        "id": trigger.json()["execution_id"],
        "agent_id": FAKE_AGENT_ID,
        "status": "completed",
        "conversation_duration": 117,
        "transcript": TRANSCRIPT,
        "summary": SUMMARY,
        "user_number": PHONE,
        "extracted_data": extracted,
        "telephony_data": {"to_number": PHONE, "call_type": "outbound"},
    }
    delivered = await api.post(f"/api/v1/voice/bolna/{key}", json=body)
    assert delivered.status_code == 200, delivered.text
    result: dict[str, Any] = delivered.json()
    return result


@pytest.mark.integration
async def test_a_grouped_extraction_is_displayable_even_with_no_mapping(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """The production case. No mapping exists, and it must still be shown.

    Before: `extractions` did not exist and the group was reported as the
    unmapped item, so Call Details had nothing to render.
    """
    key = await _key(api, ws)
    lead = await _lead(api, ws)
    result = await _run_call(api, ws, key, lead["id"], GROUPED)

    # Unmapped, and now named for what it actually is.
    assert result["extraction_written"] == []
    assert result["extraction_unmapped"] == ["General / Call Summary"]

    detail = (
        await api.get(ws.path(f"/voice/calls/{result['call_id']}"), headers=ws.owner.auth)
    ).json()

    # The fix: the extraction is present, flattened and readable.
    assert len(detail["extractions"]) == 1
    [item] = detail["extractions"]
    assert item["group"] == "General"
    assert item["name"] == "Call Summary"
    assert item["path"] == "General / Call Summary"
    assert item["value"] == SUMMARY
    # And the original nesting is still there for the JSON view.
    assert detail["extracted_data"]["General"]["Call Summary"]["subjective"] == SUMMARY


@pytest.mark.integration
async def test_a_grouped_extraction_can_be_mapped_by_name_or_path(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """A mapping named for the extraction now reaches a nested one.

    Only an explicit mapping writes: the payload below carries two
    extractions and exactly one of them is mapped.
    """
    key = await _key(api, ws)
    await _mapping(api, ws, name="Corrected Email", field="email")
    lead = await _lead(api, ws)

    result = await _run_call(
        api,
        ws,
        key,
        lead["id"],
        {
            "General": {
                "Corrected Email": {"value": "new.address@example.com", "confidence": 0.99},
                "Call Summary": {"subjective": SUMMARY},
            }
        },
    )

    assert result["extraction_written"] == ["General / Corrected Email"]
    assert result["extraction_unmapped"] == ["General / Call Summary"]

    lead_after = (await api.get(ws.path(f"/leads/{lead['id']}"), headers=ws.owner.auth)).json()
    assert lead_after["values"]["email"] == "new.address@example.com"
    # The unmapped sibling changed nothing else.
    assert lead_after["values"]["name"] == NAME


@pytest.mark.integration
async def test_an_unmapped_extraction_never_writes_a_field(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Displaying an extraction must not imply writing it."""
    key = await _key(api, ws)
    lead = await _lead(api, ws)
    result = await _run_call(
        api, ws, key, lead["id"], {"General": {"Course": {"value": "Something Else"}}}
    )

    assert result["extraction_written"] == []
    lead_after = (await api.get(ws.path(f"/leads/{lead['id']}"), headers=ws.owner.auth)).json()
    assert lead_after["values"]["name"] == NAME
    assert lead_after["values"]["email"] == EMAIL


@pytest.mark.integration
async def test_empty_extracted_data_shows_nothing_and_breaks_nothing(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    key = await _key(api, ws)
    lead = await _lead(api, ws)

    for empty in ({}, None):
        result = await _run_call(api, ws, key, lead["id"], empty)
        detail = (
            await api.get(ws.path(f"/voice/calls/{result['call_id']}"), headers=ws.owner.auth)
        ).json()
        assert detail["extractions"] == []
        assert detail["extracted_data"] == {}
        # The rest of the call is unaffected — this is the regression guard.
        assert detail["summary"] == SUMMARY
        assert detail["transcript"] == TRANSCRIPT
        assert detail["duration_seconds"] == 117


@pytest.mark.integration
async def test_summary_transcript_duration_and_call_log_are_untouched(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """The regression that matters: fixing extraction changed nothing else."""
    key = await _key(api, ws)
    lead = await _lead(api, ws)
    result = await _run_call(api, ws, key, lead["id"], GROUPED)

    assert result["status"] == "accepted"
    assert result["call_summary"] == SUMMARY
    assert result["summary_source"] == "AI"
    assert result["call_log_id"] is not None

    detail = (
        await api.get(ws.path(f"/voice/calls/{result['call_id']}"), headers=ws.owner.auth)
    ).json()
    assert detail["summary"] == SUMMARY
    assert detail["transcript"] == TRANSCRIPT
    assert detail["duration_seconds"] == 117
    assert detail["status"] == "COMPLETED"
    assert detail["call_log_id"] == result["call_log_id"]

    # One call, one timeline entry, still.
    timeline = (
        await api.get(ws.path(f"/leads/{lead['id']}/actions"), headers=ws.owner.auth)
    ).json()
    calls = [a for a in timeline["items"] if a["kind"] == "CALL_LOGGED"]
    assert len(calls) == 1
    assert calls[0]["payload"]["source"] == "AI_CALL"
    assert calls[0]["body"] == SUMMARY


@pytest.mark.integration
async def test_a_redacted_payload_field_stays_redacted_in_the_extractions(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Extractions are read from the sanitised body, not around it."""
    key = await _key(api, ws)
    lead = await _lead(api, ws)
    result = await _run_call(
        api,
        ws,
        key,
        lead["id"],
        {
            "General": {
                "api_key": {"value": FAKE_BOLNA_KEY},
                "Call Summary": {"subjective": SUMMARY},
            }
        },
    )

    detail = (
        await api.get(ws.path(f"/voice/calls/{result['call_id']}"), headers=ws.owner.auth)
    ).json()
    assert FAKE_BOLNA_KEY not in str(detail)
    names = {item["name"]: item["value"] for item in detail["extractions"]}
    assert names["Call Summary"] == SUMMARY
    assert names["api_key"] == "<redacted>"


@pytest.mark.integration
async def test_two_calls_keep_their_own_extractions(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    key = await _key(api, ws)
    lead = await _lead(api, ws)
    first = await _run_call(
        api, ws, key, lead["id"], {"General": {"Call Summary": {"subjective": "The first call."}}}
    )
    second = await _run_call(
        api, ws, key, lead["id"], {"General": {"Call Summary": {"subjective": "The second call."}}}
    )

    one = (await api.get(ws.path(f"/voice/calls/{first['call_id']}"), headers=ws.owner.auth)).json()
    two = (
        await api.get(ws.path(f"/voice/calls/{second['call_id']}"), headers=ws.owner.auth)
    ).json()
    assert one["extractions"][0]["value"] == "The first call."
    assert two["extractions"][0]["value"] == "The second call."
    assert uuid.UUID(one["id"]) != uuid.UUID(two["id"])
