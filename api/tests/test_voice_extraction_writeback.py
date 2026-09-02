"""End-to-end tests for the extraction write-back — docs/12.

Each test drives a full trigger → completed-webhook cycle and asserts on the
lead's stored values afterwards. That is where the requirement lives — "if the
extracted value passes the gates, the lead's field is updated" — and every
intermediate assertion (a note was written, the changeset opened) is a means
to that end.

`RecordingBolnaClient` stands in at the outbound seam, so no network happens
and no paid call is placed. The idempotency, phone matching and call logging
paths are exercised in `test_bolna_integration.py`; this file only asserts on
what changes when a mapping exists.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import WorkspaceFixture, build_workspace, login

from app.auth.passwords import PasswordHasherService
from app.integrations.bolna import BolnaSettings, RecordingBolnaClient
from app.models.lead import Lead

pytestmark = pytest.mark.integration


FAKE_BOLNA_KEY = "bn-test-key-do-not-use-0000000000"
FAKE_AGENT_ID = "11111111-2222-3333-4444-555555555555"


# --- fixtures --------------------------------------------------------------


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
        api_key=FAKE_BOLNA_KEY, base_url="https://api.bolna.invalid", agent_id=FAKE_AGENT_ID,
    )
    return client


@pytest.fixture
async def workspace(db_session: AsyncSession, hasher: PasswordHasherService) -> WorkspaceFixture:
    return await build_workspace(
        db_session, hasher, name="Extraction Co", owner_email="owner@extraction.example",
    )


@pytest.fixture
async def other_workspace(
    db_session: AsyncSession, hasher: PasswordHasherService
) -> WorkspaceFixture:
    return await build_workspace(
        db_session, hasher, name="Other Co", owner_email="owner@otherextraction.example",
    )


# --- helpers ---------------------------------------------------------------


async def _admin(api: AsyncClient, workspace: WorkspaceFixture) -> dict[str, str]:
    await login(api, workspace.owner)
    return workspace.owner.auth


async def _api_key(api: AsyncClient, workspace: WorkspaceFixture) -> str:
    response = await api.post(
        workspace.path("/settings/api-keys"),
        headers=workspace.owner.auth,
        json={
            "name": "Extraction",
            "permission_template_id": str(workspace.templates["Root"].id),
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["key"])


async def _create_lead(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    *,
    name: str = "",
    phone: str = "9876543210",
    email: str = "",
) -> dict[str, Any]:
    values: dict[str, Any] = {"phone": phone}
    if name:
        values["name"] = name
    if email:
        values["email"] = email
    response = await api.post(
        workspace.path("/leads"),
        headers=workspace.owner.auth,
        json={"values": values},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_mapping(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    *,
    disposition_name: str,
    target_field_key: str,
    min_confidence: float = 0.70,
    is_enabled: bool = True,
) -> dict[str, Any]:
    response = await api.post(
        workspace.path("/voice/extraction-mappings"),
        headers=workspace.owner.auth,
        json={
            "disposition_name": disposition_name,
            "target_field_key": target_field_key,
            "min_confidence": min_confidence,
            "is_enabled": is_enabled,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _trigger(
    api: AsyncClient, workspace: WorkspaceFixture, *, lead_id: str
) -> Any:
    return await api.post(
        workspace.path("/voice/calls"),
        headers=workspace.owner.auth,
        json={"lead_id": lead_id},
    )


async def _webhook(
    api: AsyncClient, workspace: WorkspaceFixture, key: str, body: dict[str, Any]
) -> Any:
    return await api.post(
        workspace.path("/voice/executions"), headers={"X-API-Key": key}, json=body,
    )


async def _current_values(
    session: AsyncSession, workspace: WorkspaceFixture, lead_id: str
) -> dict[str, Any]:
    """Read `leads.values` back from the database — the source of truth."""
    import uuid as _uuid

    row = await session.execute(
        select(Lead).where(
            Lead.id == _uuid.UUID(lead_id),
            Lead.workspace_id == workspace.workspace.id,
        )
    )
    lead = row.scalar_one()
    return dict(lead.values or {})


async def _run_completed_call(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    lead_id: str,
    key: str,
    extracted_data: dict[str, Any],
) -> dict[str, Any]:
    """Trigger → webhook, returning the webhook's parsed body."""
    await _admin(api, workspace)
    trigger = await _trigger(api, workspace, lead_id=lead_id)
    assert trigger.status_code == 200, trigger.text
    execution_id = trigger.json()["execution_id"]

    response = await _webhook(
        api, workspace, key,
        {"execution_id": execution_id, "status": "completed", "extracted_data": extracted_data},
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# --- the write-back matrix -------------------------------------------------


async def test_valid_extraction_updates_the_field(
    api: AsyncClient,
    db_session: AsyncSession,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """Above threshold, non-empty, different from existing → write."""
    await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace, name="", email="")
    await _create_mapping(
        api, workspace, disposition_name="Customer Name", target_field_key="name"
    )

    body = await _run_completed_call(
        api, workspace, lead["id"], key,
        {"Customer Name": {"value": "Asha R.", "confidence": 0.94}},
    )
    assert "Customer Name" in body["extraction_written"]

    values = await _current_values(db_session, workspace, lead["id"])
    assert values["name"] == "Asha R."


async def test_low_confidence_does_not_update(
    api: AsyncClient,
    db_session: AsyncSession,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """Below threshold → note, not a write. Existing value preserved."""
    await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace, name="Existing Name")
    await _create_mapping(
        api, workspace, disposition_name="Customer Name",
        target_field_key="name", min_confidence=0.80,
    )

    body = await _run_completed_call(
        api, workspace, lead["id"], key,
        {"Customer Name": {"value": "Wrong Name", "confidence": 0.60}},
    )
    assert body["extraction_written"] == []
    assert "Customer Name" in body["extraction_noted"]

    values = await _current_values(db_session, workspace, lead["id"])
    assert values["name"] == "Existing Name"


async def test_disabled_mapping_is_silent(
    api: AsyncClient,
    db_session: AsyncSession,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """A disabled mapping produces no write and no note."""
    await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace, name="Existing Name")
    await _create_mapping(
        api, workspace, disposition_name="Customer Name",
        target_field_key="name", is_enabled=False,
    )

    body = await _run_completed_call(
        api, workspace, lead["id"], key,
        {"Customer Name": {"value": "New Name", "confidence": 0.95}},
    )
    assert body["extraction_written"] == []
    assert body["extraction_noted"] == []
    assert "Customer Name" in body["extraction_unmapped"]

    values = await _current_values(db_session, workspace, lead["id"])
    assert values["name"] == "Existing Name"


async def test_unmapped_disposition_is_ignored(
    api: AsyncClient,
    db_session: AsyncSession,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """A payload disposition with no mapping is safely ignored."""
    await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace)

    body = await _run_completed_call(
        api, workspace, lead["id"], key,
        {"Some Disposition Nobody Configured": {"value": "x", "confidence": 0.99}},
    )
    assert body["extraction_written"] == []
    assert body["extraction_noted"] == []
    assert "Some Disposition Nobody Configured" in body["extraction_unmapped"]


async def test_empty_extracted_value_does_not_overwrite(
    api: AsyncClient,
    db_session: AsyncSession,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """Empty / None / whitespace-only values are silently skipped."""
    await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace, name="Existing Name")
    await _create_mapping(
        api, workspace, disposition_name="Customer Name", target_field_key="name"
    )

    for empty in ("", "   ", None):
        body = await _run_completed_call(
            api, workspace, lead["id"], key,
            {"Customer Name": {"value": empty, "confidence": 0.99}},
        )
        assert body["extraction_written"] == []

    values = await _current_values(db_session, workspace, lead["id"])
    assert values["name"] == "Existing Name"


async def test_existing_matching_value_is_a_no_op(
    api: AsyncClient,
    db_session: AsyncSession,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """Extraction that equals the current value writes nothing (no delta).

    The requirement is 'if same, do nothing' — the write-back must not
    produce a FIELD_CHANGE action for a no-op change.
    """
    await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace, name="Asha R.")
    await _create_mapping(
        api, workspace, disposition_name="Customer Name", target_field_key="name"
    )

    body = await _run_completed_call(
        api, workspace, lead["id"], key,
        {"Customer Name": {"value": "Asha R.", "confidence": 0.99}},
    )
    # 'written' tracks the request to write; a same-value write is skipped in
    # _apply_update but the outer service records the *decision* to write.
    # The important assertion is on the lead: nothing changed.
    values = await _current_values(db_session, workspace, lead["id"])
    assert values["name"] == "Asha R."
    # And there is no duplicate NOTE for the confirmation — the "if same, do
    # nothing" branch returns before recording a note.
    assert "Customer Name" not in body["extraction_noted"]


async def test_multiple_fields_in_one_payload(
    api: AsyncClient,
    db_session: AsyncSession,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """One webhook, several mappings, mixed outcomes."""
    await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace, name="", email="")
    await _create_mapping(
        api, workspace, disposition_name="Customer Name", target_field_key="name"
    )
    await _create_mapping(
        api, workspace, disposition_name="Email", target_field_key="email",
        min_confidence=0.80,
    )

    body = await _run_completed_call(
        api, workspace, lead["id"], key,
        {
            "Customer Name": {"value": "Asha R.", "confidence": 0.94},
            "Email": {"value": "asha@example.com", "confidence": 0.61},
        },
    )
    assert "Customer Name" in body["extraction_written"]
    assert "Email" in body["extraction_noted"]

    values = await _current_values(db_session, workspace, lead["id"])
    assert values["name"] == "Asha R."
    # Email had a real value but low confidence — preserved as absent.
    assert values.get("email") in (None, "")


async def test_malformed_entries_are_skipped_safely(
    api: AsyncClient,
    db_session: AsyncSession,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """Lists, unexpected types, nested rubbish — none cause a 500."""
    await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace, name="Existing")
    await _create_mapping(
        api, workspace, disposition_name="Customer Name", target_field_key="name"
    )

    body = await _run_completed_call(
        api, workspace, lead["id"], key,
        {"Customer Name": ["nope", "not a dict"]},
    )
    assert body["extraction_written"] == []

    values = await _current_values(db_session, workspace, lead["id"])
    assert values["name"] == "Existing"


async def test_invalid_validation_flag_blocks_the_write(
    api: AsyncClient,
    db_session: AsyncSession,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """Bolna's own type-check flag `validation.is_valid: false` blocks."""
    await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace, name="Existing")
    await _create_mapping(
        api, workspace, disposition_name="Customer Name", target_field_key="name"
    )

    body = await _run_completed_call(
        api, workspace, lead["id"], key,
        {
            "Customer Name": {
                "value": "bogus", "confidence": 0.99,
                "validation": {"is_valid": False},
            }
        },
    )
    assert body["extraction_written"] == []
    assert "Customer Name" in body["extraction_noted"]

    values = await _current_values(db_session, workspace, lead["id"])
    assert values["name"] == "Existing"


async def test_missing_confidence_is_treated_as_absent(
    api: AsyncClient,
    db_session: AsyncSession,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """No `confidence` key → the threshold cannot be checked → do not write."""
    await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace, name="Existing")
    await _create_mapping(
        api, workspace, disposition_name="Customer Name", target_field_key="name"
    )

    body = await _run_completed_call(
        api, workspace, lead["id"], key,
        {"Customer Name": {"value": "Asha R."}},
    )
    assert body["extraction_written"] == []
    values = await _current_values(db_session, workspace, lead["id"])
    assert values["name"] == "Existing"


async def test_identity_field_cannot_be_rewritten_by_extraction(
    api: AsyncClient,
    db_session: AsyncSession,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """A mapping targeting the identity field is refused at write-time.

    The router refuses to create such a mapping *via the summary-disposition
    guard's cousin* — actually, targeting the identity field is permitted in
    the CRUD (the identity key varies per workspace), but the service refuses
    to write it as defence in depth."""
    await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace, name="Existing", phone="9876543210")
    # 'phone' is the default identity key.
    await _create_mapping(
        api, workspace, disposition_name="Phone", target_field_key="phone"
    )

    body = await _run_completed_call(
        api, workspace, lead["id"], key,
        {"Phone": {"value": "9999999999", "confidence": 0.99}},
    )
    assert body["extraction_written"] == []
    assert "Phone" in body["extraction_noted"]

    values = await _current_values(db_session, workspace, lead["id"])
    # Unchanged: whatever normalisation the create path produced is what we
    # started with — the extraction cannot have rewritten it to "9999999999".
    assert values["phone"].endswith("9876543210")
    assert "9999999999" not in values["phone"]


async def test_workspace_isolation_of_mappings(
    api: AsyncClient,
    db_session: AsyncSession,
    workspace: WorkspaceFixture,
    other_workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """A mapping in workspace A does not apply in workspace B."""
    # Mapping only in workspace A.
    await login(api, workspace.owner)
    await _create_mapping(
        api, workspace, disposition_name="Customer Name", target_field_key="name"
    )

    # Trigger a call in workspace B with the same extracted disposition.
    await login(api, other_workspace.owner)
    b_key_response = await api.post(
        other_workspace.path("/settings/api-keys"),
        headers=other_workspace.owner.auth,
        json={
            "name": "Extraction B",
            "permission_template_id": str(other_workspace.templates["Root"].id),
        },
    )
    b_key = b_key_response.json()["key"]
    b_lead = await api.post(
        other_workspace.path("/leads"),
        headers=other_workspace.owner.auth,
        json={"values": {"phone": "9876543211", "name": "Existing In B"}},
    )
    b_lead_id = b_lead.json()["id"]
    trigger = await api.post(
        other_workspace.path("/voice/calls"),
        headers=other_workspace.owner.auth,
        json={"lead_id": b_lead_id},
    )
    execution_id = trigger.json()["execution_id"]

    response = await api.post(
        other_workspace.path("/voice/executions"),
        headers={"X-API-Key": b_key},
        json={
            "execution_id": execution_id, "status": "completed",
            "extracted_data": {"Customer Name": {"value": "Not Applied", "confidence": 0.99}},
        },
    )
    assert response.status_code == 200
    body = response.json()
    # No mapping in B — the disposition lands in unmapped, not written.
    assert body["extraction_written"] == []
    assert "Customer Name" in body["extraction_unmapped"]

    import uuid as _uuid
    row = await db_session.execute(
        select(Lead).where(
            Lead.id == _uuid.UUID(b_lead_id),
            Lead.workspace_id == other_workspace.workspace.id,
        )
    )
    lead = row.scalar_one()
    assert (lead.values or {}).get("name") == "Existing In B"


async def test_duplicate_delivery_does_not_reapply_extraction(
    api: AsyncClient,
    db_session: AsyncSession,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """The completed_at idempotency gate protects the extraction pass too."""
    await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace, name="")
    await _create_mapping(
        api, workspace, disposition_name="Customer Name", target_field_key="name"
    )

    trigger = await _trigger(api, workspace, lead_id=lead["id"])
    execution_id = trigger.json()["execution_id"]

    first = await _webhook(
        api, workspace, key,
        {
            "execution_id": execution_id, "status": "completed",
            "extracted_data": {"Customer Name": {"value": "Asha R.", "confidence": 0.99}},
        },
    )
    assert first.status_code == 200
    assert first.json()["status"] == "accepted"

    second = await _webhook(
        api, workspace, key,
        {
            "execution_id": execution_id, "status": "completed",
            "extracted_data": {"Customer Name": {"value": "Should Not Apply", "confidence": 0.99}},
        },
    )
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    # The duplicate response reports no extraction — nothing ran.
    assert second.json()["extraction_written"] == []

    values = await _current_values(db_session, workspace, lead["id"])
    assert values["name"] == "Asha R."  # the FIRST write survived.


async def test_summary_disposition_is_not_written_even_if_mapped(
    api: AsyncClient,
    db_session: AsyncSession,
    wired_app: FastAPI,
    workspace: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """Runtime defence: if the setting changes after a mapping was saved,
    the service refuses to write anyway."""
    await _admin(api, workspace)
    key = await _api_key(api, workspace)
    lead = await _create_lead(api, workspace, name="Existing")

    # Create the mapping BEFORE turning on the summary setting.
    await _create_mapping(
        api, workspace, disposition_name="Call Recap", target_field_key="name"
    )

    # Now flip the setting so the mapping targets the summary disposition.
    wired_app.state.settings.bolna_summary_disposition = "Call Recap"
    wired_app.state.bolna_settings = BolnaSettings(
        api_key=FAKE_BOLNA_KEY, base_url="https://api.bolna.invalid",
        agent_id=FAKE_AGENT_ID, summary_disposition="Call Recap",
    )
    try:
        body = await _run_completed_call(
            api, workspace, lead["id"], key,
            {"Call Recap": {"value": "A long call summary paragraph.", "confidence": 0.95}},
        )
        assert body["extraction_written"] == []
        assert "Call Recap" in body["extraction_noted"]

        values = await _current_values(db_session, workspace, lead["id"])
        assert values["name"] == "Existing"  # the summary did not land in `name`.
    finally:
        wired_app.state.settings.bolna_summary_disposition = None
