"""Pre-call caller context: this lead's own record, labelled, never invented.

`LeadContextBuilder` turns one lead's already-projected, already-rendered
values into a single compact string for the voice agent, so it can answer
"what did I tell you I was looking for?" from the record — and, because the
string lists only what is recorded, can say plainly that an enrolment or
payment date is not.

These tests pin the three properties the design rests on:

- **nothing is named in code** — exclusions are by field *type*, so a
  workspace whose phone field is called anything at all is still excluded, and
  `test_the_source_names_no_customer_field` reads this module's own source to
  prove no customer's field name is written into the product;
- **nothing is invented** — `created_at` is the enquiry date and is labelled as
  exactly that; no application, enrolment, selection or payment date is
  fabricated from it, because no such field exists;
- **the chokepoints still hold** — the builder is fed `render_for_voice`'s
  output, so a field the caller's template denies View on never reaches it.

The fixture workspace below is fictional. Nothing here reaches the network.
"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import WorkspaceFixture, build_workspace, login

from app.auth.passwords import PasswordHasherService
from app.integrations.bolna import BolnaSettings, RecordingBolnaClient
from app.models.enums import LeadFieldType
from app.models.field import LeadField
from app.services.lead_context import (
    MAX_CONTEXT_CHARS,
    LeadContextBuilder,
)
from app.services.tally import UNRESOLVED_SUFFIX

FAKE_BOLNA_KEY = "bn-test-key-do-not-use-0000000000"
FAKE_AGENT_ID = "66666666-7777-8888-9999-aaaaaaaaaaaa"

WORKSPACE = uuid.uuid4()
#: A fictional workspace's schema, in the order an admin put it in.
SCHEMA: tuple[tuple[str, str, LeadFieldType], ...] = (
    ("name", "Name", LeadFieldType.TEXT),
    ("phone", "Phone", LeadFieldType.PHONE),
    ("email", "Email", LeadFieldType.EMAIL),
    ("mobile_backup", "Second Mobile", LeadFieldType.PHONE),
    ("whichworkshop", "Which Workshop", LeadFieldType.DROPDOWN),
    ("whenshallwecall", "When Shall We Call", LeadFieldType.TEXT),
    ("whatareyoulookingfor", "What Are You Looking For", LeadFieldType.TEXT),
    ("howmuchhaveyoudone", "How Much Have You Done", LeadFieldType.DROPDOWN),
)


def _field(key: str, label: str, kind: LeadFieldType, order: int, **kwargs: object) -> LeadField:
    return LeadField(
        workspace_id=WORKSPACE,
        key=key,
        label=label,
        field_type=kind,
        sort_order=order,
        **kwargs,
    )


@pytest.fixture
def fields() -> list[LeadField]:
    return [_field(key, label, kind, order) for order, (key, label, kind) in enumerate(SCHEMA)]


@pytest.fixture
def builder() -> LeadContextBuilder:
    return LeadContextBuilder()


def _rendered() -> dict[str, str]:
    """What `render_for_voice` would hand the builder for one full lead."""
    return {
        "name": "Ada",
        "phone": "+910000000000",
        "email": "ada@example.invalid",
        "mobile_backup": "+910000000001",
        "whichworkshop": "Tide Pool Diving",
        "whenshallwecall": "Evenings",
        "whatareyoulookingfor": "A career change",
        "howmuchhaveyoudone": "Beginner",
    }


# --- 1-2: the record reads as labelled facts ------------------------------------


def test_the_whole_record_reads_as_labelled_lines(
    builder: LeadContextBuilder, fields: list[LeadField]
) -> None:
    context = builder.build(
        _rendered(),
        fields,
        stage_name="New",
        timezone_name="Asia/Kolkata",
        created_at=dt.datetime(2026, 9, 20, 6, 30, tzinfo=dt.UTC),
    )
    assert "Name: Ada" in context
    assert "Which Workshop: Tide Pool Diving" in context
    assert "When Shall We Call: Evenings" in context
    assert "What Are You Looking For: A career change" in context
    assert "Stage: New" in context


def test_experience_level_is_included_when_present(
    builder: LeadContextBuilder, fields: list[LeadField]
) -> None:
    """Whatever the workspace calls it. Here, 'How Much Have You Done'."""
    context = builder.build(_rendered(), fields)
    assert "How Much Have You Done: Beginner" in context


def test_the_admins_order_is_the_order(
    builder: LeadContextBuilder, fields: list[LeadField]
) -> None:
    context = builder.build(_rendered(), fields, stage_name="New")
    lines = context.splitlines()
    assert lines[0].startswith("Name:")
    assert lines[-1] == "Stage: New"


# --- 3-7: what must never appear ------------------------------------------------


def test_phone_and_email_never_appear(builder: LeadContextBuilder, fields: list[LeadField]) -> None:
    """Excluded by field *type*, so the second phone field goes too."""
    context = builder.build(_rendered(), fields, stage_name="New")
    for forbidden in (
        "+910000000000",
        "+910000000001",
        "ada@example.invalid",
        "Phone",
        "Second Mobile",
        "Email",
    ):
        assert forbidden not in context, f"{forbidden!r} reached the caller context"


def test_internal_identifiers_never_appear(
    builder: LeadContextBuilder, fields: list[LeadField]
) -> None:
    """The builder is only ever handed lead values, never the reserved keys.

    Passing the reserved namespace in anyway proves nothing leaks even if a
    caller one day hands it the wrong map.
    """
    lead_id = str(uuid.uuid4())
    workspace_id = str(uuid.uuid4())
    idempotency = str(uuid.uuid4())
    polluted = {
        **_rendered(),
        "crm_lead_id": lead_id,
        "crm_workspace_id": workspace_id,
        "crm_idempotency": idempotency,
        "crm_owner": "Priya Raman",
        "crm_recent_notes": "AI call ended without a conversation",
        "crm_call_count": "3",
        "crm_is_repeat_caller": "yes",
    }
    context = builder.build(polluted, fields, stage_name="New")
    for forbidden in (
        lead_id,
        workspace_id,
        idempotency,
        "Priya Raman",
        "crm_owner",
        "AI call ended",
        "crm_recent_notes",
        "crm_call_count",
        "crm_is_repeat_caller",
    ):
        assert forbidden not in context, f"{forbidden!r} reached the caller context"


def test_the_stage_id_and_assignee_id_never_appear(
    builder: LeadContextBuilder, fields: list[LeadField]
) -> None:
    """Only the stage's human label is said. Its id is not a fact for a caller."""
    stage_id = str(uuid.uuid4())
    context = builder.build(
        {**_rendered(), "stage_id": stage_id, "assignee_id": str(uuid.uuid4())},
        fields,
        stage_name="New",
    )
    assert "Stage: New" in context
    assert stage_id not in context


# --- 8-9: unresolved Tally text and raw keys ------------------------------------


def test_an_unresolved_tally_field_is_excluded(
    builder: LeadContextBuilder, fields: list[LeadField]
) -> None:
    """A parked answer carries raw form text. It is not spoken back."""
    consent = "I agree to be contacted about my enquiry and accept the terms"
    parked = f"consent{UNRESOLVED_SUFFIX}"
    context = builder.build(
        {**_rendered(), parked: consent},
        [
            *fields,
            # Such a key has no field definition in production; one is forced
            # into the schema here so the explicit guard is what rejects it.
            _field(parked, "Consent", LeadFieldType.TEXT, 99),
        ],
        stage_name="New",
    )
    assert consent not in context
    assert UNRESOLVED_SUFFIX not in context
    assert "Consent" not in context


def test_a_value_with_no_field_definition_is_excluded(
    builder: LeadContextBuilder, fields: list[LeadField]
) -> None:
    """No definition means no label, and an unlabelled fact is not said."""
    context = builder.build({**_rendered(), "orphan_key": "orphan value"}, fields)
    assert "orphan value" not in context
    assert "orphan_key" not in context


def test_labels_are_used_and_raw_keys_are_not(
    builder: LeadContextBuilder, fields: list[LeadField]
) -> None:
    context = builder.build(_rendered(), fields, stage_name="New")
    assert "What Are You Looking For:" in context
    for raw in ("whatareyoulookingfor", "whichworkshop", "howmuchhaveyoudone"):
        assert raw not in context, f"the raw key {raw!r} was exposed"


def test_a_hidden_field_is_excluded(builder: LeadContextBuilder) -> None:
    """A field the admin hid from their own team is not read to a caller."""
    schema = [
        _field("name", "Name", LeadFieldType.TEXT, 0, is_hidden=False),
        _field("scratch", "Scratch Notes", LeadFieldType.TEXT, 1, is_hidden=True),
    ]
    context = builder.build({"name": "Ada", "scratch": "do not read aloud"}, schema)
    assert context == "Name: Ada"


# --- 10, 16: thin and empty records ---------------------------------------------


def test_a_lead_with_only_a_name_is_still_a_valid_context(
    builder: LeadContextBuilder, fields: list[LeadField]
) -> None:
    context = builder.build({"name": "Ada"}, fields)
    assert context == "Name: Ada"


def test_empty_values_are_ignored(builder: LeadContextBuilder, fields: list[LeadField]) -> None:
    context = builder.build(
        {"name": "Ada", "whatareyoulookingfor": "", "whenshallwecall": "   "},
        fields,
    )
    assert context == "Name: Ada"


def test_a_record_with_nothing_to_say_returns_the_empty_string(
    builder: LeadContextBuilder, fields: list[LeadField]
) -> None:
    """And the caller omits the key entirely — see the trigger test below."""
    assert builder.build({}, fields) == ""
    assert builder.build({"phone": "+910000000000"}, fields) == ""


# --- 11-12: the ceiling ---------------------------------------------------------


def test_the_context_never_exceeds_the_ceiling(
    builder: LeadContextBuilder, fields: list[LeadField]
) -> None:
    talkative = {
        "name": "Ada",
        "whatareyoulookingfor": "career " * 600,
        "whenshallwecall": "Evenings",
    }
    context = builder.build(talkative, fields, stage_name="New")
    assert len(context) <= MAX_CONTEXT_CHARS


def test_a_line_that_does_not_fit_is_dropped_whole_not_cut(
    builder: LeadContextBuilder, fields: list[LeadField]
) -> None:
    """A half-line reads as a fact that stops mid-word. Short lines survive."""
    context = builder.build(
        {"name": "Ada", "whatareyoulookingfor": "career " * 600, "whenshallwecall": "Evenings"},
        fields,
        stage_name="New",
    )
    assert "Name: Ada" in context
    assert "When Shall We Call: Evenings" in context
    assert "Stage: New" in context
    assert "What Are You Looking For" not in context
    for line in context.splitlines():
        assert line.count(":") >= 1


def test_a_single_oversized_line_is_cut_on_a_word_boundary(
    builder: LeadContextBuilder,
) -> None:
    """The one case where there is no shorter honest answer than part of it."""
    schema = [_field("whatareyoulookingfor", "What Are You Looking For", LeadFieldType.TEXT, 0)]
    context = builder.build({"whatareyoulookingfor": "career " * 600}, schema)
    assert len(context) <= MAX_CONTEXT_CHARS
    assert context.endswith("…")
    # Cut between words: the character before the ellipsis is not a half-word
    # boundary introduced by slicing mid-token.
    assert context[:-1].rstrip().endswith("career")


# --- 13-15: the date, and the dates that do not exist ---------------------------


def test_created_at_is_rendered_in_the_workspace_timezone(
    builder: LeadContextBuilder, fields: list[LeadField]
) -> None:
    """Architecture rule 10. 19:00 UTC is already the 21st in Asia/Kolkata."""
    created = dt.datetime(2026, 9, 20, 19, 0, tzinfo=dt.UTC)
    kolkata = builder.build(
        {"name": "Ada"}, fields, timezone_name="Asia/Kolkata", created_at=created
    )
    utc = builder.build({"name": "Ada"}, fields, timezone_name="UTC", created_at=created)
    assert "Enquiry received: 21 September 2026" in kolkata
    assert "Enquiry received: 20 September 2026" in utc


def test_a_naive_created_at_is_treated_as_utc(
    builder: LeadContextBuilder, fields: list[LeadField]
) -> None:
    context = builder.build(
        {"name": "Ada"},
        fields,
        timezone_name="Asia/Kolkata",
        created_at=dt.datetime(2026, 9, 20, 19, 0),
    )
    assert "Enquiry received: 21 September 2026" in context


def test_an_unknown_timezone_falls_back_to_utc_rather_than_failing(
    builder: LeadContextBuilder, fields: list[LeadField]
) -> None:
    context = builder.build(
        {"name": "Ada"},
        fields,
        timezone_name="Mars/Olympus_Mons",
        created_at=dt.datetime(2026, 9, 20, 19, 0, tzinfo=dt.UTC),
    )
    assert "Enquiry received: 20 September 2026" in context


def test_created_at_is_never_labelled_as_a_date_it_is_not(
    builder: LeadContextBuilder, fields: list[LeadField]
) -> None:
    """The safety rule. This timestamp is when the enquiry arrived, nothing else."""
    context = builder.build(
        _rendered(),
        fields,
        stage_name="New",
        timezone_name="Asia/Kolkata",
        created_at=dt.datetime(2026, 9, 20, 6, 30, tzinfo=dt.UTC),
    )
    assert "Enquiry received:" in context
    lowered = context.lower()
    for forbidden in (
        "enrol",
        "enroll",
        "application date",
        "applied",
        "payment",
        "paid",
        "selection",
    ):
        assert forbidden not in lowered, f"{forbidden!r} appeared in the caller context"


def test_no_date_is_fabricated_when_the_record_has_none(
    builder: LeadContextBuilder, fields: list[LeadField]
) -> None:
    """No `created_at`, no date line. Nothing stands in for it."""
    context = builder.build(_rendered(), fields, stage_name="New")
    assert "Enquiry received" not in context
    assert "2026" not in context
    assert "September" not in context


def test_a_new_stage_lead_carries_no_enrolment_claim(
    builder: LeadContextBuilder, fields: list[LeadField]
) -> None:
    context = builder.build(_rendered(), fields, stage_name="New")
    assert "Stage: New" in context
    assert "enrol" not in context.lower()


# --- the product carries no customer's vocabulary -------------------------------


def test_the_source_names_no_customer_field() -> None:
    """Read the implementation and prove no workspace's field name is in it.

    The same guard `program_context` carries. A field name compiled into the
    product is the mistake CLAUDE.md opens with.
    """
    source = Path("app/services/lead_context.py").read_text(encoding="utf-8").lower()
    for forbidden in (
        "whatareyoulookingfor",
        "preferred_contact_time",
        "experience_level",
        "course",
        "programme name",
        "levelup",
        '"name"',
        "'name'",
    ):
        assert forbidden not in source, f"{forbidden!r} is hardcoded in lead_context.py"
    assert "if key ==" not in source


# --- 17-18: through the real trigger --------------------------------------------


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
    return await build_workspace(
        db_session, hasher, name="Lead Context Co", owner_email="owner@leadctx.example"
    )


async def _lead_with_a_goal(api: AsyncClient, ws: WorkspaceFixture, goal: str) -> dict[str, object]:
    created = await api.post(
        ws.path("/settings/lead-fields"),
        headers=ws.owner.auth,
        json={"label": "What Are You Looking For", "field_type": "TEXT"},
    )
    assert created.status_code == 201, created.text
    key = created.json()["key"]
    lead = await api.post(
        ws.path("/leads"),
        headers=ws.owner.auth,
        json={
            "values": {
                "name": "Ada",
                "phone": "+919000000321",
                "email": "ada@leadctx.example",
                key: goal,
            }
        },
    )
    assert lead.status_code == 201, lead.text
    return {"id": lead.json()["id"], "key": key}


@pytest.mark.integration
async def test_the_caller_context_reaches_the_bolna_request(
    api: AsyncClient,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """What the vendor would actually receive, and what it would not."""
    await login(api, ws.owner)
    lead = await _lead_with_a_goal(api, ws, "A career change")

    triggered = await api.post(
        ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": lead["id"]}
    )
    assert triggered.status_code == 200, triggered.text

    sent = bolna.last.user_data
    context = sent["crm_lead_context"]
    assert "Name: Ada" in context
    assert "What Are You Looking For: A career change" in context
    assert "Enquiry received:" in context
    # The bare keys still carry the contact details; the caller context does not.
    assert sent["phone"] == "+919000000321"
    assert "+919000000321" not in context
    assert "ada@leadctx.example" not in context
    assert str(lead["id"]) not in context


@pytest.mark.integration
async def test_a_field_without_view_permission_never_reaches_the_context(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """The projection chokepoint, checked on this path like every other.

    Mirrors the existing guard in `test_bolna_integration.py`: a field nobody
    has been granted anything on must not be laundered out through the voice
    payload, and so cannot appear in the caller context either.
    """
    await login(api, ws.owner)
    lead = await _lead_with_a_goal(api, ws, "A career change")

    ungranted = LeadField(
        workspace_id=ws.id,
        key="internal_note",
        label="Internal Note",
        field_type=LeadFieldType.TEXT,
        sort_order=99,
    )
    db_session.add(ungranted)
    await db_session.commit()

    triggered = await api.post(
        ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": lead["id"]}
    )
    assert triggered.status_code == 200, triggered.text
    sent = bolna.last.user_data
    assert "internal_note" not in sent
    assert "Internal Note" not in sent["crm_lead_context"]


@pytest.mark.integration
async def test_one_workspaces_context_never_carries_anothers(
    api: AsyncClient,
    db_session: AsyncSession,
    hasher: PasswordHasherService,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """Two tenants, same-shaped schema, different answers."""
    other = await build_workspace(
        db_session, hasher, name="Other Context Co", owner_email="owner@otherctx.example"
    )
    await login(api, ws.owner)
    await login(api, other.owner)

    mine = await _lead_with_a_goal(api, ws, "A career change")
    theirs = await _lead_with_a_goal(api, other, "Something else entirely")

    triggered = await api.post(
        ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": mine["id"]}
    )
    assert triggered.status_code == 200, triggered.text
    assert "Something else entirely" not in bolna.last.user_data["crm_lead_context"]

    triggered = await api.post(
        other.path("/voice/calls"), headers=other.owner.auth, json={"lead_id": theirs["id"]}
    )
    assert triggered.status_code == 200, triggered.text
    assert "A career change" not in bolna.last.user_data["crm_lead_context"]

    # And the other tenant's lead is not reachable through this one's path.
    crossed = await api.post(
        ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": theirs["id"]}
    )
    assert crossed.status_code == 404, crossed.text


@pytest.mark.integration
async def test_a_lead_with_only_a_number_says_only_what_is_recorded(
    api: AsyncClient,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
) -> None:
    """No answers to report, so the context is the record's own facts alone.

    The number itself is excluded by type, so nothing the caller submitted is
    left to say — what remains is the stage and when the enquiry arrived. The
    key is never sent as an empty string.
    """
    await login(api, ws.owner)
    lead = await api.post(
        ws.path("/leads"),
        headers=ws.owner.auth,
        json={"values": {"phone": "+919000000322"}},
    )
    assert lead.status_code == 201, lead.text

    triggered = await api.post(
        ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": lead.json()["id"]}
    )
    assert triggered.status_code == 200, triggered.text
    context = bolna.last.user_data["crm_lead_context"]
    assert context.strip() != ""
    assert "Enquiry received:" in context
    assert "+919000000322" not in context
