"""Cross-workspace isolation for the voice integration (docs/09-13).

The voice surface holds the most personal data in the product — a transcript is
a customer talking — and it has two kinds of caller, so it has two kinds of
probe:

- **members**, on `/voice/context`, `/voice/calls` and
  `/voice/extraction-mappings`: B's ids aimed at A's path, A's caller on B's
  path, B's lead or phone number passed *by reference* to A, and B's rows
  absent from A's lists;
- **the machine**, on `/voice/executions`: a key from the wrong workspace, and
  a payload from A's key that names B's execution or B's lead. The second is
  the dangerous one — the key is valid, the path is consistent, and only the
  webhook's own lead resolution stands between it and a write into B.

Every write probe is followed by B reading its own records back, so a refusal
is proven by B's state rather than inferred from a status code.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import WorkspaceFixture
from tests.isolation.conftest import TenantPair
from tests.isolation.test_cross_workspace import (
    _VOICE_BODIES,
    VOICE_CATALOGUE_ROUTES,
    VOICE_COLLECTION_ROUTES,
    VOICE_COLLECTION_WRITE_ROUTES,
    VOICE_ID_ROUTES,
    VOICE_LOOKUP_ROUTES,
)

from app.integrations.bolna import BolnaSettings, RecordingBolnaClient
from app.models.enums import WalletTransactionKind
from app.services.wallet import WalletService
from app.tenancy.session import ScopedSession

pytestmark = pytest.mark.integration

FAKE_BOLNA_KEY = "bn-test-key-do-not-use-0000000000"
FAKE_AGENT_ID = "11111111-2222-3333-4444-555555555555"

PHONE_A = "+919000000101"
PHONE_B = "+919000000202"

# Strings that exist only in B. Any of them in a response A receives is a leak,
# whichever field carried it.
B_NAME = "Bea Only-In-B"
B_TRANSCRIPT = "assistant: Hello Bea.\nuser: my B-only transcript marker"
B_SUMMARY = "Bea asked about the B-only course and wants a callback."
B_DISPOSITION = "B Only Disposition"


@dataclass(slots=True)
class VoiceRecords:
    """One workspace's voice data: a lead, a completed call, a mapping, a key."""

    lead_id: str
    call_id: str
    execution_id: str
    mapping_id: str
    api_key: str
    #: docs/16: a pending calling-number request, paid from the wallet.
    request_id: str


@pytest.fixture
def bolna(wired_app: FastAPI) -> RecordingBolnaClient:
    client = RecordingBolnaClient()
    wired_app.state.bolna_client = client
    wired_app.state.bolna_settings = BolnaSettings(
        api_key=FAKE_BOLNA_KEY, base_url="https://api.bolna.invalid", agent_id=FAKE_AGENT_ID
    )
    return client


def _execution_body(
    execution_id: str, *, phone: str, transcript: str, summary: str
) -> dict[str, Any]:
    return {
        "id": execution_id,
        "agent_id": FAKE_AGENT_ID,
        "status": "completed",
        "conversation_duration": 60,
        "transcript": transcript,
        "summary": summary,
        "user_number": phone,
        "telephony_data": {"to_number": phone, "call_type": "outbound", "duration": "60"},
    }


async def _populate(
    api: AsyncClient,
    session: AsyncSession,
    fixture: WorkspaceFixture,
    *,
    name: str,
    phone: str,
    transcript: str,
    summary: str,
    disposition: str,
) -> VoiceRecords:
    """Real rows, made the way production makes them: trigger, then webhook."""
    headers = fixture.owner.auth
    lead = await api.post(
        fixture.path("/leads"), headers=headers, json={"values": {"name": name, "phone": phone}}
    )
    assert lead.status_code == 201, lead.text
    lead_id = lead.json()["id"]

    key = await api.post(
        fixture.path("/settings/api-keys"),
        headers=headers,
        json={"name": "Bolna webhook", "permission_template_id": str(fixture.templates["Root"].id)},
    )
    assert key.status_code == 201, key.text
    api_key = str(key.json()["key"])

    triggered = await api.post(
        fixture.path("/voice/calls"), headers=headers, json={"lead_id": lead_id}
    )
    assert triggered.status_code == 200, triggered.text
    execution_id = triggered.json()["execution_id"]

    # Completed through the header-authenticated route, so the 404s below are
    # refusals from a route that demonstrably works.
    delivered = await api.post(
        fixture.path("/voice/executions"),
        headers={"X-API-Key": api_key},
        json=_execution_body(execution_id, phone=phone, transcript=transcript, summary=summary),
    )
    assert delivered.status_code == 200, delivered.text
    assert delivered.json()["status"] == "accepted"

    mapping = await api.post(
        fixture.path("/voice/extraction-mappings"),
        headers=headers,
        json={"disposition_name": disposition, "target_field_key": "name"},
    )
    assert mapping.status_code == 201, mapping.text

    # A funded wallet and a number request (docs/16). After the call, so the
    # call above stays unmetered and every earlier assertion is unchanged.
    wallet = WalletService(ScopedSession(session, fixture.id), workspace=fixture.workspace)
    await wallet.create(rate_per_minute=Decimal("10"))
    await wallet.record(WalletTransactionKind.TOPUP, Decimal("2000"), note="t", recorded_by="t")
    await session.commit()
    requested = await api.post(
        fixture.path("/voice/number-requests"), headers=headers, json={"region": name}
    )
    assert requested.status_code == 201, requested.text

    return VoiceRecords(
        lead_id=lead_id,
        call_id=triggered.json()["call_id"],
        execution_id=execution_id,
        mapping_id=mapping.json()["id"],
        api_key=api_key,
        request_id=requested.json()["id"],
    )


@pytest.fixture
async def voice(
    api: AsyncClient,
    tenants: TenantPair,
    bolna: RecordingBolnaClient,
    db_session: AsyncSession,
) -> tuple[VoiceRecords, VoiceRecords]:
    """The same shapes of voice data in A and in B."""
    a = await _populate(
        api,
        db_session,
        tenants.a,
        name="Arun In A",
        phone=PHONE_A,
        transcript="assistant: Hello Arun.\nuser: yes",
        summary="Arun asked about fees.",
        disposition="A Disposition",
    )
    b = await _populate(
        api,
        db_session,
        tenants.b,
        name=B_NAME,
        phone=PHONE_B,
        transcript=B_TRANSCRIPT,
        summary=B_SUMMARY,
        disposition=B_DISPOSITION,
    )
    return a, b


def _ids(records: VoiceRecords) -> dict[str, str]:
    return {
        "lead_id": records.lead_id,
        "call_id": records.call_id,
        "mapping_id": records.mapping_id,
        "request_id": records.request_id,
    }


def _assert_nothing_of_b(response: Response, tenants: TenantPair, b: VoiceRecords) -> None:
    """Checked against the raw text, so a leak through any field fails."""
    body = response.text
    for leaked in (
        str(tenants.b.id),
        b.lead_id,
        b.call_id,
        b.execution_id,
        b.mapping_id,
        B_NAME,
        PHONE_B,
        "B-only",
        B_DISPOSITION,
    ):
        assert leaked not in body, f"{response.request.url.path} leaked {leaked!r} from workspace B"


async def _b_is_untouched(api: AsyncClient, tenants: TenantPair, b: VoiceRecords) -> None:
    """B reads its own voice records back exactly as `_populate` left them."""
    headers = tenants.b.owner.auth

    context = await api.get(tenants.b.path(f"/voice/context/{b.lead_id}"), headers=headers)
    assert context.status_code == 200, context.text
    assert context.json()["last_call_summary"] == B_SUMMARY
    assert context.json()["call_count"] == 1

    call = await api.get(tenants.b.path(f"/voice/calls/{b.call_id}"), headers=headers)
    assert call.status_code == 200, call.text
    assert call.json()["transcript"] == B_TRANSCRIPT

    calls = await api.get(tenants.b.path("/voice/calls"), headers=headers)
    assert [item["id"] for item in calls.json()["items"]] == [b.call_id]

    mapping = await api.get(
        tenants.b.path(f"/voice/extraction-mappings/{b.mapping_id}"), headers=headers
    )
    assert mapping.status_code == 200, mapping.text
    assert mapping.json()["disposition_name"] == B_DISPOSITION
    assert mapping.json()["min_confidence"] == pytest.approx(0.70)

    mappings = await api.get(tenants.b.path("/voice/extraction-mappings"), headers=headers)
    assert [item["id"] for item in mappings.json()] == [b.mapping_id]

    # docs/16: B's number request is still B's, still pending, still paid for.
    requests = await api.get(tenants.b.path("/voice/number-requests"), headers=headers)
    assert [(r["id"], r["status"]) for r in requests.json()["items"]] == [(b.request_id, "PENDING")]

    # docs/15: nobody outside B can approve B's lead for AI calling.
    lead = await api.get(tenants.b.path(f"/leads/{b.lead_id}"), headers=headers)
    assert lead.status_code == 200, lead.text
    assert lead.json()["voice_approved_at"] is None


# --- 1. by direct id ---------------------------------------------------------


@pytest.mark.parametrize(("method", "template"), VOICE_ID_ROUTES)
async def test_a_voice_id_from_another_workspace_is_a_404(
    api: AsyncClient,
    tenants: TenantPair,
    voice: tuple[VoiceRecords, VoiceRecords],
    method: str,
    template: str,
) -> None:
    """A's owner, on A's own path, aims B's real lead, call and mapping ids."""
    _, b = voice
    response = await api.request(
        method,
        tenants.a.path(template.format(**_ids(b))),
        headers=tenants.a.owner.auth,
        json=_VOICE_BODIES.get(template),
    )

    assert response.status_code == 404, (
        f"{method} {template} reached workspace B: {response.status_code} {response.text}"
    )
    assert response.json()["detail"]["code"] == "not_found"
    _assert_nothing_of_b(response, tenants, b)
    await _b_is_untouched(api, tenants, b)


@pytest.mark.parametrize(
    ("method", "template"),
    [
        *VOICE_ID_ROUTES,
        *(("GET", route) for route in VOICE_COLLECTION_ROUTES),
        *VOICE_COLLECTION_WRITE_ROUTES,
        *VOICE_LOOKUP_ROUTES,
        *VOICE_CATALOGUE_ROUTES,
    ],
)
async def test_a_voice_route_under_another_workspace_s_path_is_a_404(
    api: AsyncClient,
    tenants: TenantPair,
    voice: tuple[VoiceRecords, VoiceRecords],
    method: str,
    template: str,
) -> None:
    """Consistent ids, wrong caller: A's owner on B's path, with valid bodies."""
    _, b = voice
    bodies: dict[str, dict[str, object]] = {
        **_VOICE_BODIES,
        "/voice/calls": {"lead_id": b.lead_id},
        "/voice/calls/preview": {"lead_id": b.lead_id},
    }
    response = await api.request(
        method,
        tenants.b.path(template.format(**_ids(b))),
        headers=tenants.a.owner.auth,
        params={"phone": PHONE_B} if template == "/voice/context" else None,
        json=bodies.get(template) if method != "GET" else None,
    )

    assert response.status_code == 404, (
        f"{method} {template} let a non-member into workspace B: {response.text}"
    )
    _assert_nothing_of_b(response, tenants, b)
    await _b_is_untouched(api, tenants, b)


@pytest.mark.parametrize(
    "template",
    [
        "/voice/context/{lead_id}",
        "/voice/calls/{call_id}",
        "/voice/extraction-mappings/{mapping_id}",
    ],
)
async def test_a_foreign_voice_id_reads_exactly_like_one_that_does_not_exist(
    api: AsyncClient,
    tenants: TenantPair,
    voice: tuple[VoiceRecords, VoiceRecords],
    template: str,
) -> None:
    """If the two answers differ at all, B's ids can be enumerated from A."""
    _, b = voice
    invented = str(uuid.uuid4())
    foreign = await api.get(
        tenants.a.path(template.format(**_ids(b))), headers=tenants.a.owner.auth
    )
    unknown = await api.get(
        tenants.a.path(template.format(lead_id=invented, call_id=invented, mapping_id=invented)),
        headers=tenants.a.owner.auth,
    )

    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json()


# --- 2. by list --------------------------------------------------------------


@pytest.mark.parametrize("route", VOICE_COLLECTION_ROUTES)
async def test_voice_collections_never_contain_another_workspace_s_rows(
    api: AsyncClient,
    tenants: TenantPair,
    voice: tuple[VoiceRecords, VoiceRecords],
    route: str,
) -> None:
    """A's list has A's row — so it is not empty by accident — and none of B's."""
    a, b = voice
    response = await api.get(tenants.a.path(route), headers=tenants.a.owner.auth)
    assert response.status_code == 200, response.text

    payload = response.json()
    items = payload["items"] if isinstance(payload, dict) else payload
    own = {
        "/voice/calls": a.call_id,
        "/voice/number-requests": a.request_id,
    }.get(route, a.mapping_id)
    assert [item["id"] for item in items] == [own]
    _assert_nothing_of_b(response, tenants, b)


# --- 3. by reference ---------------------------------------------------------


async def test_another_workspace_s_lead_id_filters_a_s_calls_to_nothing(
    api: AsyncClient,
    tenants: TenantPair,
    voice: tuple[VoiceRecords, VoiceRecords],
) -> None:
    """`?lead_id=` is a filter, so B's lead yields A's empty page — the same
    page an invented id yields, so it says nothing about whether B's exists."""
    _, b = voice
    foreign = await api.get(
        tenants.a.path("/voice/calls"),
        headers=tenants.a.owner.auth,
        params={"lead_id": b.lead_id},
    )
    unknown = await api.get(
        tenants.a.path("/voice/calls"),
        headers=tenants.a.owner.auth,
        params={"lead_id": str(uuid.uuid4())},
    )

    assert foreign.status_code == unknown.status_code == 200
    assert foreign.json()["total"] == 0
    assert foreign.json() == unknown.json()


async def test_another_workspace_s_phone_number_is_not_a_lead_here(
    api: AsyncClient,
    tenants: TenantPair,
    voice: tuple[VoiceRecords, VoiceRecords],
) -> None:
    """The phone lookup is the one a voice trigger has before it knows an id."""
    _, b = voice
    foreign = await api.get(
        tenants.a.path("/voice/context"),
        headers=tenants.a.owner.auth,
        params={"phone": PHONE_B},
    )
    unknown = await api.get(
        tenants.a.path("/voice/context"),
        headers=tenants.a.owner.auth,
        params={"phone": "+919000000999"},
    )

    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json()
    _assert_nothing_of_b(foreign, tenants, b)


@pytest.mark.parametrize(
    "body",
    [
        {"lead_id": "B"},
        {"lead_id": "B", "followup": True},
        {"phone": PHONE_B},
    ],
    ids=["by-lead-id", "follow-up-by-lead-id", "by-phone"],
)
async def test_a_call_cannot_be_placed_to_another_workspace_s_lead(
    api: AsyncClient,
    tenants: TenantPair,
    voice: tuple[VoiceRecords, VoiceRecords],
    bolna: RecordingBolnaClient,
    body: dict[str, object],
) -> None:
    """The most expensive leak on the surface: a paid call to B's customer,
    carrying B's context, placed from A."""
    _, b = voice
    if body.get("lead_id") == "B":
        body = {**body, "lead_id": b.lead_id}
    calls_before = len(bolna.calls)

    response = await api.post(
        tenants.a.path("/voice/calls"), headers=tenants.a.owner.auth, json=body
    )

    assert response.status_code == 404, response.text
    assert len(bolna.calls) == calls_before, "a call was placed to workspace B's lead"
    _assert_nothing_of_b(response, tenants, b)
    await _b_is_untouched(api, tenants, b)


@pytest.mark.parametrize(
    "body",
    [{"lead_id": "B"}, {"phone": PHONE_B}],
    ids=["by-lead-id", "by-phone"],
)
async def test_another_workspace_s_lead_cannot_be_previewed(
    api: AsyncClient,
    tenants: TenantPair,
    voice: tuple[VoiceRecords, VoiceRecords],
    body: dict[str, object],
) -> None:
    """A preview is B's whole agent context — name, programme, last call's
    summary — so it gets the same refusal as placing the call."""
    _, b = voice
    if body.get("lead_id") == "B":
        body = {"lead_id": b.lead_id}

    response = await api.post(
        tenants.a.path("/voice/calls/preview"), headers=tenants.a.owner.auth, json=body
    )

    assert response.status_code == 404, response.text
    _assert_nothing_of_b(response, tenants, b)


# --- 4. the machine route ----------------------------------------------------


@pytest.mark.parametrize("direction", ["a-key-on-b-path", "b-key-on-a-path"])
async def test_a_key_cannot_deliver_to_another_workspace_s_path(
    api: AsyncClient,
    tenants: TenantPair,
    voice: tuple[VoiceRecords, VoiceRecords],
    direction: str,
) -> None:
    """The key is the authority; a path naming another workspace is a 404."""
    a, b = voice
    key, fixture = (
        (a.api_key, tenants.b) if direction == "a-key-on-b-path" else (b.api_key, tenants.a)
    )
    response = await api.post(
        fixture.path("/voice/executions"),
        headers={"X-API-Key": key},
        json=_execution_body(
            str(uuid.uuid4()), phone=PHONE_B, transcript="smuggled", summary="smuggled"
        ),
    )

    assert response.status_code == 404, response.text
    _assert_nothing_of_b(response, tenants, b)
    await _b_is_untouched(api, tenants, b)


async def test_a_s_key_cannot_complete_another_workspace_s_execution(
    api: AsyncClient,
    tenants: TenantPair,
    voice: tuple[VoiceRecords, VoiceRecords],
) -> None:
    """A valid key, a consistent path, and B's real execution id in the body.

    The execution lookup is scoped, so B's id is simply unknown in A — and with
    no lead of A's matching, the delivery is refused rather than attached to B's
    call or to a guess.
    """
    a, b = voice
    response = await api.post(
        tenants.a.path("/voice/executions"),
        headers={"X-API-Key": a.api_key},
        json=_execution_body(
            b.execution_id,
            phone=PHONE_B,
            transcript="assistant: overwritten from A",
            summary="Overwritten from A.",
        ),
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "unknown_execution"
    _assert_nothing_of_b(response, tenants, b)
    await _b_is_untouched(api, tenants, b)


async def test_a_s_key_cannot_write_a_call_onto_another_workspace_s_lead(
    api: AsyncClient,
    tenants: TenantPair,
    voice: tuple[VoiceRecords, VoiceRecords],
) -> None:
    """`crm_lead_id` is how a webhook that beats its own trigger finds its lead.

    It is resolved through A's scoped `LeadService`, so B's lead id is a 404 —
    never a call log, a summary or an extraction written into B.
    """
    a, b = voice
    body = _execution_body(
        "exec-smuggled-0001",
        phone="+919000000998",
        transcript="assistant: written from A",
        summary="Written from A.",
    )
    body["user_data"] = {"crm_lead_id": b.lead_id, "crm_workspace_id": str(tenants.b.id)}

    response = await api.post(
        tenants.a.path("/voice/executions"), headers={"X-API-Key": a.api_key}, json=body
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "not_found"
    _assert_nothing_of_b(response, tenants, b)
    await _b_is_untouched(api, tenants, b)
