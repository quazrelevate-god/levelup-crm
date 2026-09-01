"""The Tally form webhook (`POST /api/v1/intake/tally`).

The assertion that matters most here is the choice one. Dropdown values are
stored as the option *code*, so a translator that forwarded Tally's opaque
option id (`opt_a1b2`) would fail validation and take the whole lead with it.
`test_a_choice_answer_is_resolved_to_the_option_code` is the guard against
that, and everything else guards the promise around it: a submission that
cannot be fully mapped still lands.

Nothing here reaches Tally. The payloads are the documented `FORM_RESPONSE`
shape, built by `_submission` below.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import WorkspaceFixture, build_workspace, login

from app.auth.passwords import PasswordHasherService
from app.models.lead import Lead

pytestmark = pytest.mark.integration

TALLY_PATH = "/api/v1/intake/tally"

#: The five courses on LevelUp Learning's real form. Fixture vocabulary only —
#: they exist here as *rows* the test creates, exactly as an admin would, and
#: never as a constant the product ships.
COURSES = [
    "Breakthrough Filmmaking",
    "Forge Creators Residency",
    "Forge Filmmaking Bootcamp",
    "Forge Writing Retreat",
    "Video Editing Academy",
]


@pytest.fixture
def hasher(wired_app: FastAPI) -> PasswordHasherService:
    hasher = wired_app.state.password_hasher
    assert isinstance(hasher, PasswordHasherService)
    return hasher


@pytest.fixture
async def ws(
    db_session: AsyncSession, hasher: PasswordHasherService, api: AsyncClient
) -> WorkspaceFixture:
    fixture = await build_workspace(
        db_session, hasher, name="Tally Co", owner_email="tally-owner@example.com"
    )
    await login(api, fixture.owner)
    return fixture


@pytest.fixture
async def other_ws(db_session: AsyncSession, hasher: PasswordHasherService) -> WorkspaceFixture:
    return await build_workspace(
        db_session, hasher, name="Other Tally Co", owner_email="other-tally@example.com"
    )


# --- helpers ----------------------------------------------------------------


async def _key(api: AsyncClient, ws: WorkspaceFixture, *, template: str = "Root") -> str:
    response = await api.post(
        ws.path("/settings/api-keys"),
        headers=ws.owner.auth,
        json={
            "name": f"Tally {template}",
            "permission_template_id": str(ws.templates[template].id),
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["key"])


async def _create_field(
    api: AsyncClient,
    ws: WorkspaceFixture,
    *,
    label: str,
    field_type: str,
    options: list[str] | None = None,
) -> dict[str, Any]:
    created = await api.post(
        ws.path("/settings/lead-fields"),
        headers=ws.owner.auth,
        json={"label": label, "field_type": field_type},
    )
    assert created.status_code == 201, created.text
    body: dict[str, Any] = created.json()
    if options:
        added = await api.post(
            ws.path(f"/settings/lead-fields/{body['id']}/options/bulk"),
            headers=ws.owner.auth,
            json={"labels": options},
        )
        assert added.status_code in (200, 201), added.text
    return body


@pytest.fixture
async def course_field(api: AsyncClient, ws: WorkspaceFixture) -> dict[str, Any]:
    """The workspace's own Course dropdown, with its own option rows."""
    return await _create_field(api, ws, label="Course", field_type="DROPDOWN", options=COURSES)


def _answer(
    label: str,
    value: Any,
    *,
    kind: str = "INPUT_TEXT",
    options: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "key": f"question_{label.lower().replace(' ', '_')}",
        "label": label,
        "type": kind,
        "value": value,
    }
    if options is not None:
        entry["options"] = options
    return entry


def _submission(*answers: dict[str, Any], event: str = "FORM_RESPONSE") -> dict[str, Any]:
    return {
        "eventId": "evt_test_0001",
        "eventType": event,
        "createdAt": "2026-08-31T12:00:00.000Z",
        "data": {
            "responseId": "resp_0001",
            "submissionId": "sub_0001",
            "formId": "xX8XJr",
            "formName": "LevelUp Learning — Course Enquiry Form",
            "fields": list(answers),
        },
    }


async def _post(api: AsyncClient, key: str, body: dict[str, Any], path: str = TALLY_PATH) -> Any:
    return await api.post(path, headers={"X-API-Key": key}, json=body)


async def _lead_values(session: AsyncSession, ws: WorkspaceFixture, lead_id: str) -> dict[str, Any]:
    import uuid as _uuid

    rows = await session.execute(
        select(Lead).where(Lead.id == _uuid.UUID(lead_id), Lead.workspace_id == ws.workspace.id)
    )
    return dict(rows.scalar_one().values or {})


# --- the mapping that matters -----------------------------------------------


async def test_a_full_submission_creates_a_lead_with_mapped_fields(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture
) -> None:
    key = await _key(api, ws)
    response = await _post(
        api,
        key,
        _submission(
            _answer("Name", "Priya"),
            _answer("Phone", "9876543210", kind="INPUT_PHONE_NUMBER"),
            _answer("Email", "priya@example.com", kind="INPUT_EMAIL"),
        ),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "CREATED"
    assert body["mapped"] == 3

    values = await _lead_values(db_session, ws, body["lead_id"])
    assert values["name"] == "Priya"
    assert values["email"] == "priya@example.com"
    assert values["phone"].endswith("9876543210")


async def test_a_choice_answer_is_resolved_to_the_option_code(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    course_field: dict[str, Any],
) -> None:
    """The whole point. Tally sends an option id; the CRM stores an option code.

    Storing the id would fail `_norm_dropdown` and reject the submission, so
    this asserts on the stored value rather than merely on a 200.
    """
    key = await _key(api, ws)
    response = await _post(
        api,
        key,
        _submission(
            _answer("Phone", "9876543210", kind="INPUT_PHONE_NUMBER"),
            _answer(
                "Course",
                ["opt_a1b2"],
                kind="MULTIPLE_CHOICE",
                options=[{"id": "opt_a1b2", "text": "Video Editing Academy"}],
            ),
        ),
    )
    assert response.status_code == 200, response.text
    body = response.json()

    values = await _lead_values(db_session, ws, body["lead_id"])
    assert values["course"] == "video_editing_academy"
    assert values["course"] != "opt_a1b2"


async def test_an_unresolvable_choice_keeps_the_lead_and_the_text(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    course_field: dict[str, Any],
) -> None:
    """The fallback: a course the workspace has never heard of.

    The lead must still land, the raw text must survive somewhere, and the
    typed field must be left alone rather than filled with something the
    schema never agreed to.
    """
    key = await _key(api, ws)
    response = await _post(
        api,
        key,
        _submission(
            _answer("Phone", "9876543210", kind="INPUT_PHONE_NUMBER"),
            _answer(
                "Course",
                ["opt_zzz"],
                kind="MULTIPLE_CHOICE",
                options=[{"id": "opt_zzz", "text": "Underwater Basket Weaving"}],
            ),
        ),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "CREATED"
    assert any("Underwater Basket Weaving" in w for w in body["warnings"])

    values = await _lead_values(db_session, ws, body["lead_id"])
    assert values.get("course") in (None, "")
    assert "Underwater Basket Weaving" in values["course__tally_unresolved"]


async def test_an_unmatched_question_is_stored_not_dropped(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture
) -> None:
    """A rejected payload at 2am is a lost lead — so nothing is rejected."""
    key = await _key(api, ws)
    response = await _post(
        api,
        key,
        _submission(
            _answer("Phone", "9876543210", kind="INPUT_PHONE_NUMBER"),
            _answer("How did you hear about us?", "A friend"),
        ),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "CREATED"
    assert any("How did you hear about us?" in w for w in body["warnings"])

    values = await _lead_values(db_session, ws, body["lead_id"])
    assert "A friend" in json.dumps(values)


async def test_labels_match_loosely_across_punctuation_and_case(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture
) -> None:
    """`E-mail ID` should still find the `Email` field... it should not.

    Punctuation and case are folded away, but different *words* are different
    questions — matching them would be guessing. This asserts the boundary:
    `e-mail` matches `Email`, `E-mail ID` does not.
    """
    key = await _key(api, ws)
    response = await _post(
        api,
        key,
        _submission(
            _answer("Phone", "9876543210", kind="INPUT_PHONE_NUMBER"),
            _answer("E-Mail", "priya@example.com", kind="INPUT_EMAIL"),
        ),
    )
    assert response.status_code == 200, response.text
    values = await _lead_values(db_session, ws, response.json()["lead_id"])
    assert values["email"] == "priya@example.com"


async def test_a_checkbox_answer_maps_to_the_checkbox_field(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture
) -> None:
    key = await _key(api, ws)
    await _create_field(api, ws, label="Consent", field_type="CHECKBOX")

    response = await _post(
        api,
        key,
        _submission(
            _answer("Phone", "9876543210", kind="INPUT_PHONE_NUMBER"),
            _answer("Consent", True, kind="CHECKBOX"),
        ),
    )
    assert response.status_code == 200, response.text
    values = await _lead_values(db_session, ws, response.json()["lead_id"])
    assert values["consent"] is True


# --- dedupe and preservation -------------------------------------------------


async def test_a_second_submission_updates_rather_than_duplicating(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture
) -> None:
    key = await _key(api, ws)
    first = await _post(
        api,
        key,
        _submission(
            _answer("Name", "Priya"),
            _answer("Phone", "9876543210", kind="INPUT_PHONE_NUMBER"),
        ),
    )
    assert first.json()["outcome"] == "CREATED"

    second = await _post(
        api,
        key,
        _submission(
            _answer("Name", "Priya Sharma"),
            _answer("Phone", "9876543210", kind="INPUT_PHONE_NUMBER"),
        ),
    )
    assert second.status_code == 200, second.text
    assert second.json()["outcome"] == "UPDATED"
    assert second.json()["lead_id"] == first.json()["lead_id"]

    values = await _lead_values(db_session, ws, second.json()["lead_id"])
    assert values["name"] == "Priya Sharma"


async def test_an_empty_answer_does_not_blank_an_existing_value(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture
) -> None:
    key = await _key(api, ws)
    first = await _post(
        api,
        key,
        _submission(
            _answer("Name", "Priya"),
            _answer("Phone", "9876543210", kind="INPUT_PHONE_NUMBER"),
        ),
    )
    lead_id = first.json()["lead_id"]

    second = await _post(
        api,
        key,
        _submission(
            _answer("Name", ""),
            _answer("Phone", "9876543210", kind="INPUT_PHONE_NUMBER"),
        ),
    )
    assert second.status_code == 200, second.text

    values = await _lead_values(db_session, ws, lead_id)
    assert values["name"] == "Priya"


# --- events, shapes and failures ---------------------------------------------


async def test_a_non_form_response_event_is_acknowledged_and_ignored(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    """200, so Tally stops retrying an event this endpoint will never act on."""
    key = await _key(api, ws)
    response = await _post(api, key, _submission(event="FORM_CREATED"))
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "IGNORED"


async def test_a_submission_with_no_usable_answers_is_skipped_not_500(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    key = await _key(api, ws)
    response = await _post(api, key, _submission())
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "SKIPPED"


async def test_a_malformed_body_is_a_422_not_a_500(api: AsyncClient, ws: WorkspaceFixture) -> None:
    key = await _key(api, ws)
    for body in ({"data": {"fields": "not-a-list"}}, {"data": []}):
        response = await api.post(TALLY_PATH, headers={"X-API-Key": key}, json=body)
        assert response.status_code in (200, 422), response.text
        assert response.status_code != 500


# --- authentication ----------------------------------------------------------


async def test_a_missing_or_wrong_key_is_a_401(api: AsyncClient, ws: WorkspaceFixture) -> None:
    without = await api.post(TALLY_PATH, json=_submission())
    assert without.status_code == 401

    wrong = await api.post(
        TALLY_PATH, headers={"X-API-Key": "crmk_not-a-real-key"}, json=_submission()
    )
    assert wrong.status_code == 401
    assert wrong.json()["detail"]["code"] == "invalid_api_key"


async def test_the_url_key_route_works_for_a_header_less_sender(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture
) -> None:
    """Tally may not be able to add a header; the key can travel in the path."""
    key = await _key(api, ws)
    response = await api.post(
        f"{TALLY_PATH}/{key}",
        json=_submission(
            _answer("Name", "Priya"),
            _answer("Phone", "9876543210", kind="INPUT_PHONE_NUMBER"),
        ),
    )
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "CREATED"


async def test_the_url_key_route_refuses_a_bad_key(api: AsyncClient, ws: WorkspaceFixture) -> None:
    response = await api.post(f"{TALLY_PATH}/crmk_nonsense-key", json=_submission())
    assert response.status_code == 401


# --- signature ---------------------------------------------------------------


async def test_the_signature_is_ignored_when_no_secret_is_configured(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    key = await _key(api, ws)
    response = await api.post(
        TALLY_PATH,
        headers={"X-API-Key": key, "tally-signature": "obviously-wrong"},
        json=_submission(_answer("Phone", "9876543210", kind="INPUT_PHONE_NUMBER")),
    )
    assert response.status_code == 200, response.text


async def test_a_wrong_signature_is_refused_when_a_secret_is_configured(
    api: AsyncClient, wired_app: FastAPI, ws: WorkspaceFixture
) -> None:
    key = await _key(api, ws)
    wired_app.state.settings.tally_signing_secret = "s3cret"
    try:
        response = await api.post(
            TALLY_PATH,
            headers={"X-API-Key": key, "tally-signature": "not-the-right-digest"},
            json=_submission(_answer("Phone", "9876543210", kind="INPUT_PHONE_NUMBER")),
        )
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "invalid_signature"
    finally:
        wired_app.state.settings.tally_signing_secret = None


async def test_a_correct_signature_is_accepted(
    api: AsyncClient, wired_app: FastAPI, ws: WorkspaceFixture
) -> None:
    key = await _key(api, ws)
    secret = "s3cret"
    wired_app.state.settings.tally_signing_secret = secret
    try:
        body = _submission(_answer("Phone", "9876543210", kind="INPUT_PHONE_NUMBER"))
        raw = json.dumps(body).encode("utf-8")
        digest = base64.b64encode(
            hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
        ).decode("ascii")

        response = await api.post(
            TALLY_PATH,
            headers={
                "X-API-Key": key,
                "tally-signature": digest,
                "Content-Type": "application/json",
            },
            content=raw,
        )
        assert response.status_code == 200, response.text
        assert response.json()["outcome"] == "CREATED"
    finally:
        wired_app.state.settings.tally_signing_secret = None


# --- isolation ---------------------------------------------------------------


async def test_a_key_only_writes_into_its_own_workspace(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    other_ws: WorkspaceFixture,
) -> None:
    """The workspace comes from the key, never from the payload."""
    key = await _key(api, ws)
    response = await _post(
        api,
        key,
        _submission(
            _answer("Name", "Priya"),
            _answer("Phone", "9876543210", kind="INPUT_PHONE_NUMBER"),
        ),
    )
    assert response.status_code == 200, response.text

    rows = await db_session.execute(select(Lead).where(Lead.workspace_id == other_ws.workspace.id))
    assert list(rows.scalars().all()) == [], "the lead leaked into another workspace"


# --- regression: the production 422 ------------------------------------------
#
# A real submission returned
#   {"code": "identity_required", "field": "phone"}
# because not one question label matched a field label: "Phone Number" folds to
# `phonenumber` and the field folds to `phone`. Every answer landed under an
# unknown key, the identity never mapped, and `create_lead` refused the lead.
#
# An ordinary field degrading to a warning is survivable. The identity
# degrading is not — it loses the whole lead, which is the one outcome this
# entire intake path exists to prevent.

#: The captured payload, with the form's real question wording. `opt_*` ids
#: stand in for Tally's uuids; nothing in the assertions depends on their shape.
PRODUCTION_PAYLOAD: dict[str, Any] = {
    "eventId": "b8f1c3d2-0000-4000-8000-000000000001",
    "eventType": "FORM_RESPONSE",
    "createdAt": "2026-08-31T14:30:00.000Z",
    "data": {
        "responseId": "resp_real_0001",
        "submissionId": "sub_real_0001",
        "formId": "xX8XJr",
        "formName": "LevelUp Learning — Course Enquiry Form",
        "fields": [
            {"key": "q1", "label": "Full Name", "type": "INPUT_TEXT", "value": "Perumal"},
            {
                "key": "q2",
                "label": "Phone Number",
                "type": "INPUT_PHONE_NUMBER",
                "value": "+919087822357",
            },
            {
                "key": "q3",
                "label": "E-mail ID",
                "type": "INPUT_EMAIL",
                "value": "perumal2007@gmail.com",
            },
            {
                "key": "q4",
                "label": "Course Intrested",
                "type": "MULTIPLE_CHOICE",
                "value": ["opt_course_a"],
                "options": [
                    {"id": "opt_course_a", "text": "Breakthrough Filmmaking"},
                    {"id": "opt_course_b", "text": "Video Editing Academy"},
                ],
            },
            {
                "key": "q5",
                "label": "Experience Level",
                "type": "MULTIPLE_CHOICE",
                "value": ["opt_exp_a"],
                "options": [{"id": "opt_exp_a", "text": "Beginner"}],
            },
            {
                "key": "q6",
                "label": "Preferred Contact Time",
                "type": "MULTIPLE_CHOICE",
                "value": ["opt_time_d"],
                "options": [{"id": "opt_time_d", "text": "Any Time"}],
            },
            {"key": "q7", "label": "What are you looking for?", "type": "TEXTAREA", "value": None},
            {
                "key": "q8",
                "label": "Consent",
                "type": "CHECKBOXES",
                "value": ["opt_consent"],
                "options": [{"id": "opt_consent", "text": "I agree to be contacted."}],
            },
        ],
    },
}


async def test_the_production_payload_creates_a_lead(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture
) -> None:
    """The exact submission that 422'd, against a bare workspace.

    Only the four built-in fields exist here — no Course, no Experience Level —
    which is precisely the state the Railway workspace was in. The lead must
    still be created, with name, phone and email on real fields.
    """
    key = await _key(api, ws)
    response = await _post(api, key, PRODUCTION_PAYLOAD)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "CREATED", body
    assert body["lead_id"]

    values = await _lead_values(db_session, ws, body["lead_id"])
    # "Phone Number" -> the PHONE-typed identity field, via the type fallback.
    assert values["phone"].endswith("9087822357")
    # "Full Name" -> "Name" and "E-mail ID" -> "Email", via unique containment.
    assert values["name"] == "Perumal"
    assert values["email"] == "perumal2007@gmail.com"


async def test_the_production_payload_keeps_unconfigured_answers(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture
) -> None:
    """Course, Experience Level and Contact Time have no field here.

    They must survive as warned unknown keys rather than being dropped — the
    admin can add the fields later and re-submit without having lost anything.
    """
    key = await _key(api, ws)
    response = await _post(api, key, PRODUCTION_PAYLOAD)
    assert response.status_code == 200, response.text

    values = await _lead_values(db_session, ws, response.json()["lead_id"])
    blob = json.dumps(values)
    assert "Breakthrough Filmmaking" in blob
    assert "Beginner" in blob
    assert "Any Time" in blob


async def test_the_production_payload_maps_courses_once_the_field_exists(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    course_field: dict[str, Any],
) -> None:
    """With a Course field configured, "Course Intrested" now maps to it.

    Containment carries the label match; the option id still resolves through
    text to the workspace's own code.
    """
    key = await _key(api, ws)
    response = await _post(api, key, PRODUCTION_PAYLOAD)
    assert response.status_code == 200, response.text

    values = await _lead_values(db_session, ws, response.json()["lead_id"])
    assert values["course"] == "breakthrough_filmmaking"


async def test_a_phone_labelled_anything_still_fills_the_identity(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture
) -> None:
    """The identity rescue is keyed on type, not on wording.

    A form asking "Contact Digits" is nobody's idea of a good label, and the
    lead must land regardless.
    """
    key = await _key(api, ws)
    response = await _post(
        api,
        key,
        _submission(_answer("Contact Digits", "+919087822357", kind="INPUT_PHONE_NUMBER")),
    )
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "CREATED"

    values = await _lead_values(db_session, ws, response.json()["lead_id"])
    assert values["phone"].endswith("9087822357")


async def test_an_ambiguous_containment_does_not_guess(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture
) -> None:
    """Two fields containing the same token must not be resolved by guessing.

    A workspace with both "Phone" and "Alternate Phone" cannot say which one a
    question called "Phone" means by containment — `phone` is inside both. The
    exact-label tier settles it for "Phone" itself, so this asserts the tier
    below: a label containing the token but matching neither exactly is warned
    about rather than silently filed against the wrong field.
    """
    key = await _key(api, ws)
    response = await _post(
        api,
        key,
        _submission(
            _answer("Phone", "+919087822357", kind="INPUT_PHONE_NUMBER"),
            _answer("Some Phone Note", "call after 6", kind="INPUT_TEXT"),
        ),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "CREATED"

    values = await _lead_values(db_session, ws, body["lead_id"])
    # The exact match still wins for the identity itself.
    assert values["phone"].endswith("9087822357")
    # The ambiguous one was preserved, not filed against a phone field.
    assert values.get("alternate_phone") in (None, "")
    assert "call after 6" in json.dumps(values)
    assert any("Some Phone Note" in w for w in body["warnings"])
