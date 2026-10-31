"""Connected apps, workflows and the never-AI-call rule (docs/18).

1. **Razorpay** posts a signed payment; the CRM matches the lead, notes the
   payment on its timeline and runs every switched-on workflow once.
2. **Workflows** follow their checks: the "yes" exit when the amount matches,
   the "no" exit when it does not; a step that cannot be done fails loudly
   without stopping its sibling branch.
3. **Drafts and tests**: a half-drawn workflow saves but cannot be switched on,
   and a test run shows what would happen while changing nothing.
4. **Never AI-call**: a lead the workspace's rule covers is never dialled.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import WorkspaceFixture, add_member, build_workspace, login

from app.auth.passwords import PasswordHasherService
from app.integrations.bolna import BolnaSettings, RecordingBolnaClient
from app.services.apps import normalise_razorpay, razorpay_signature
from app.services.workflows import compare, render

SECRET = "rzp-webhook-secret-for-tests-only"
PHONE = "+919000002001"


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
        api_key="bn-test-key-do-not-use-0000000000",
        base_url="https://api.bolna.invalid",
        agent_id="11111111-2222-3333-4444-555555555555",
    )
    return client


@pytest.fixture
async def ws(
    api: AsyncClient, db_session: AsyncSession, hasher: PasswordHasherService
) -> WorkspaceFixture:
    fixture = await build_workspace(
        db_session, hasher, name="Pay Co", owner_email="owner@pay.example"
    )
    fixture.workspace.default_country_code = "91"
    await db_session.commit()
    await login(api, fixture.owner)
    field = await api.post(
        fixture.path("/settings/lead-fields"),
        headers=fixture.owner.auth,
        json={"label": "Fee Status", "field_type": "TEXT"},
    )
    assert field.status_code == 201, field.text
    return fixture


# --- helpers -------------------------------------------------------------------


async def _stage(api: AsyncClient, ws: WorkspaceFixture, label: str) -> str:
    created = await api.post(
        ws.path("/settings/stages"), headers=ws.owner.auth, json={"label": label}
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def _lead(api: AsyncClient, ws: WorkspaceFixture, phone: str = PHONE, **values: Any) -> str:
    created = await api.post(
        ws.path("/leads"),
        headers=ws.owner.auth,
        json={"values": {"name": "Asha", "phone": phone, **values}},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def _get_lead(api: AsyncClient, ws: WorkspaceFixture, lead_id: str) -> dict[str, Any]:
    response = await api.get(ws.path(f"/leads/{lead_id}"), headers=ws.owner.auth)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _connect(api: AsyncClient, ws: WorkspaceFixture, *, secret: str | None = SECRET) -> str:
    """Connect Razorpay; the webhook path it hands back."""
    connected = await api.post(ws.path("/apps/razorpay/connect"), headers=ws.owner.auth)
    assert connected.status_code == 201, connected.text
    if secret is not None:
        saved = await api.patch(
            ws.path("/apps/razorpay"), headers=ws.owner.auth, json={"secret": secret}
        )
        assert saved.status_code == 200, saved.text
    path: str = connected.json()["webhook_path"]
    return path


def _payment(
    *,
    event: str = "payment.captured",
    paise: int = 40000,
    payment_id: str = "pay_TEST0001",
    phone: str = PHONE,
    email: str = "asha@example.com",
    notes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payment = {
        "id": payment_id,
        "amount": paise,
        "currency": "INR",
        "status": "captured" if event != "payment.failed" else "failed",
        "method": "upi",
        "contact": phone,
        "email": email,
        "notes": notes or {},
    }
    if event == "payment.failed":
        payment["error_description"] = "Payment declined by bank"
    body: dict[str, Any] = {"event": event, "payload": {"payment": {"entity": payment}}}
    if event == "payment_link.paid":
        body["payload"]["payment_link"] = {
            "entity": {"id": "plink_TEST", "customer": {"name": "Asha", "contact": phone}}
        }
    return body


async def _post(
    api: AsyncClient, path: str, payload: dict[str, Any], *, secret: str = SECRET
) -> Response:
    raw = json.dumps(payload).encode()
    return await api.post(
        path,
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": razorpay_signature(raw, secret),
        },
    )


async def _events(api: AsyncClient, ws: WorkspaceFixture) -> list[dict[str, Any]]:
    response = await api.get(ws.path("/apps/razorpay/events"), headers=ws.owner.auth)
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def _graph(*nodes: dict[str, Any], edges: list[tuple[str, str, str]]) -> dict[str, Any]:
    return {
        "nodes": [
            {
                "id": "trigger",
                "type": "trigger",
                "position": {"x": 0, "y": 0},
                "data": {"trigger": "razorpay.payment_captured"},
            },
            *nodes,
        ],
        "edges": [
            {"id": f"e{i}", "source": s, "sourceHandle": h, "target": t}
            for i, (s, h, t) in enumerate(edges)
        ],
    }


def _node(node_id: str, kind: str, y: int = 0, **data: Any) -> dict[str, Any]:
    return {"id": node_id, "type": kind, "position": {"x": 200, "y": y}, "data": data}


def _amount_is(value: str) -> dict[str, Any]:
    return {"match": "all", "rules": [{"variable": "amount", "operator": "equals", "value": value}]}


async def _workflow(
    api: AsyncClient, ws: WorkspaceFixture, graph: dict[str, Any], *, active: bool = True
) -> dict[str, Any]:
    created = await api.post(
        ws.path("/workflows"), headers=ws.owner.auth, json={"name": "Fee paid", "graph": graph}
    )
    assert created.status_code == 201, created.text
    body: dict[str, Any] = created.json()
    if active:
        switched = await api.patch(
            ws.path(f"/workflows/{body['id']}"), headers=ws.owner.auth, json={"is_active": True}
        )
        assert switched.status_code == 200, switched.text
        body = switched.json()
    return body


async def _fee_workflow(api: AsyncClient, ws: WorkspaceFixture) -> tuple[dict[str, Any], str]:
    """If amount is 400: stage → Fee Paid and Fee Status → Paid."""
    fee_paid = await _stage(api, ws, "Fee Paid")
    graph = _graph(
        _node("check", "condition", **_amount_is("400")),
        _node("stage", "update_stage", 0, stage_id=fee_paid),
        _node("field", "update_fields", 100, values=[{"field_key": "fee_status", "value": "Paid"}]),
        _node("note", "add_note", 200, text="Not the fee: {{amount}} via {{method}}"),
        edges=[
            ("trigger", "out", "check"),
            ("check", "yes", "stage"),
            ("check", "yes", "field"),
            ("check", "no", "note"),
        ],
    )
    return await _workflow(api, ws, graph), fee_paid


async def _runs(api: AsyncClient, ws: WorkspaceFixture, workflow_id: str) -> list[dict[str, Any]]:
    response = await api.get(ws.path(f"/workflows/{workflow_id}/runs"), headers=ws.owner.auth)
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


# --- 1. Razorpay ------------------------------------------------------------------


async def test_connecting_gives_a_url_and_the_secret_is_never_shown(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    path = await _connect(api, ws)

    assert path.startswith("/api/v1/hooks/razorpay/")
    [app] = (await api.get(ws.path("/apps"), headers=ws.owner.auth)).json()
    assert (app["provider"], app["connected"], app["secret_set"]) == ("razorpay", True, True)
    assert SECRET not in json.dumps(app)


async def test_a_signed_payment_runs_the_workflow_and_notes_the_payment(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    path = await _connect(api, ws)
    lead_id = await _lead(api, ws)
    workflow, fee_paid = await _fee_workflow(api, ws)

    response = await _post(api, path, _payment())

    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "MATCHED"
    lead = await _get_lead(api, ws, lead_id)
    assert lead["stage_id"] == fee_paid
    assert lead["values"]["fee_status"] == "Paid"
    [run] = await _runs(api, ws, workflow["id"])
    assert run["status"] == "SUCCESS"
    assert [(s["node_id"], s["result"]) for s in run["steps"]] == [
        ("check", "yes"),
        ("stage", "done"),
        ("field", "done"),
    ]
    [event] = await _events(api, ws)
    assert (event["outcome"], event["workflows_run"], event["lead_id"]) == ("MATCHED", 1, lead_id)
    assert event["data"]["amount"] == "400"
    timeline = await api.get(ws.path(f"/leads/{lead_id}/actions"), headers=ws.owner.auth)
    notes = [a["body"] for a in timeline.json()["items"] if a["kind"] == "NOTE"]
    assert any("payment received" in body and "400" in body for body in notes)


async def test_another_amount_takes_the_no_exit_and_fills_in_its_variables(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    path = await _connect(api, ws)
    lead_id = await _lead(api, ws)
    workflow, _ = await _fee_workflow(api, ws)

    await _post(api, path, _payment(paise=800000))

    lead = await _get_lead(api, ws, lead_id)
    assert lead["values"].get("fee_status") in (None, "")
    [run] = await _runs(api, ws, workflow["id"])
    assert [(s["node_id"], s["result"]) for s in run["steps"]] == [
        ("check", "no"),
        ("note", "done"),
    ]
    timeline = await api.get(ws.path(f"/leads/{lead_id}/actions"), headers=ws.owner.auth)
    bodies = [a["body"] for a in timeline.json()["items"] if a["kind"] == "NOTE"]
    assert "Not the fee: 8000 via upi" in bodies


async def test_one_payment_sent_twice_runs_its_workflow_once(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    """Razorpay sends payment.captured and payment_link.paid for one payment."""
    path = await _connect(api, ws)
    await _lead(api, ws)
    workflow, _ = await _fee_workflow(api, ws)

    first = await _post(api, path, _payment())
    second = await _post(api, path, _payment(event="payment_link.paid"))

    assert (first.json()["outcome"], second.json()["outcome"]) == ("MATCHED", "DUPLICATE")
    assert len(await _runs(api, ws, workflow["id"])) == 1


async def test_a_bad_signature_is_refused_and_logged_without_its_body(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    path = await _connect(api, ws)
    lead_id = await _lead(api, ws)
    await _fee_workflow(api, ws)

    response = await _post(api, path, _payment(), secret="the-wrong-secret")

    assert response.status_code == 401
    [event] = await _events(api, ws)
    assert event["outcome"] == "REJECTED"
    assert "signature" in event["error"]
    assert event["data"] == {}
    assert (await _get_lead(api, ws, lead_id))["values"].get("fee_status") in (None, "")


async def test_without_a_secret_nothing_is_accepted(api: AsyncClient, ws: WorkspaceFixture) -> None:
    path = await _connect(api, ws, secret=None)
    await _lead(api, ws)

    response = await _post(api, path, _payment())

    assert response.status_code == 401
    [event] = await _events(api, ws)
    assert "secret is not set" in event["error"]


async def test_an_unknown_url_is_a_404(api: AsyncClient, ws: WorkspaceFixture) -> None:
    await _connect(api, ws)

    response = await _post(api, "/api/v1/hooks/razorpay/" + "x" * 43, _payment())

    assert response.status_code == 404


async def test_a_payment_matches_by_email_when_the_phone_is_unknown(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    path = await _connect(api, ws)
    lead_id = await _lead(api, ws, email="Asha@Example.com")

    response = await _post(api, path, _payment(phone="+919000009999"))

    assert response.json()["outcome"] == "MATCHED"
    [event] = await _events(api, ws)
    assert event["lead_id"] == lead_id


async def test_a_lead_id_in_the_notes_beats_the_phone(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    await _lead(api, ws)
    other = await _lead(api, ws, phone="+919000002002")
    path = await _connect(api, ws)

    await _post(api, path, _payment(notes={"crm_lead_id": other}))

    [event] = await _events(api, ws)
    assert event["lead_id"] == other


async def test_no_matching_lead_is_logged_and_optionally_created(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    path = await _connect(api, ws)

    first = await _post(api, path, _payment(payment_id="pay_A"))
    assert (first.status_code, first.json()["outcome"]) == (200, "NO_LEAD")

    await api.patch(
        ws.path("/apps/razorpay"),
        headers=ws.owner.auth,
        json={"settings": {"create_lead_if_missing": True}},
    )
    second = await _post(api, path, _payment(payment_id="pay_B"))

    assert second.json()["outcome"] == "CREATED_LEAD"
    newest = (await _events(api, ws))[0]
    lead = await _get_lead(api, ws, newest["lead_id"])
    assert lead["values"]["phone"] == PHONE
    assert lead["values"]["email"] == "asha@example.com"


async def test_a_failed_payment_starts_only_failed_payment_workflows(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    path = await _connect(api, ws)
    lead_id = await _lead(api, ws)
    workflow, _ = await _fee_workflow(api, ws)

    await _post(api, path, _payment(event="payment.failed"))

    assert await _runs(api, ws, workflow["id"]) == []
    timeline = await api.get(ws.path(f"/leads/{lead_id}/actions"), headers=ws.owner.auth)
    notes = [a["body"] for a in timeline.json()["items"] if a["kind"] == "NOTE"]
    assert any("failed" in body and "declined" in body for body in notes)


# --- 2. workflows -------------------------------------------------------------------


async def test_a_step_that_cannot_be_done_fails_without_stopping_its_sibling(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    path = await _connect(api, ws)
    lead_id = await _lead(api, ws)
    number = await api.post(
        ws.path("/settings/lead-fields"),
        headers=ws.owner.auth,
        json={"label": "Paid Amount", "field_type": "NUMBER"},
    )
    assert number.status_code == 201, number.text
    graph = _graph(
        _node("bad", "update_fields", 0, values=[{"field_key": "paid_amount", "value": "a lot"}]),
        _node("good", "update_fields", 100, values=[{"field_key": "fee_status", "value": "Paid"}]),
        edges=[("trigger", "out", "bad"), ("trigger", "out", "good")],
    )
    workflow = await _workflow(api, ws, graph)

    await _post(api, path, _payment(payment_id="pay_sib"))

    [run] = await _runs(api, ws, workflow["id"])
    assert run["status"] == "FAILED"
    assert [(s["node_id"], s["result"]) for s in run["steps"]] == [
        ("bad", "failed"),
        ("good", "done"),
    ]
    assert (await _get_lead(api, ws, lead_id))["values"]["fee_status"] == "Paid"


async def test_a_lead_condition_checks_the_lead_itself(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    path = await _connect(api, ws)
    await _lead(api, ws, fee_status="Paid")
    rule = {
        "type": "group",
        "op": "AND",
        "children": [{"type": "field", "key": "fee_status", "op": "eq", "value": "Paid"}],
    }
    graph = _graph(
        _node("already", "lead_condition", conditions=rule),
        _node("note", "add_note", text="Paid twice? {{payment_id}}"),
        edges=[("trigger", "out", "already"), ("already", "yes", "note")],
    )
    workflow = await _workflow(api, ws, graph)

    await _post(api, path, _payment(payment_id="pay_twice"))

    [run] = await _runs(api, ws, workflow["id"])
    assert [(s["node_id"], s["result"]) for s in run["steps"]] == [
        ("already", "yes"),
        ("note", "done"),
    ]


async def test_a_switched_off_workflow_does_not_run(api: AsyncClient, ws: WorkspaceFixture) -> None:
    path = await _connect(api, ws)
    lead_id = await _lead(api, ws)
    workflow, _ = await _fee_workflow(api, ws)
    off = await api.patch(
        ws.path(f"/workflows/{workflow['id']}"), headers=ws.owner.auth, json={"is_active": False}
    )
    assert off.status_code == 200, off.text

    await _post(api, path, _payment())

    assert await _runs(api, ws, workflow["id"]) == []
    assert (await _get_lead(api, ws, lead_id))["values"].get("fee_status") in (None, "")


# --- 3. drafts and tests -------------------------------------------------------------


async def test_a_half_drawn_workflow_saves_but_cannot_be_switched_on(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    graph = _graph(_node("check", "condition", match="all", rules=[]), edges=[])
    draft = await _workflow(api, ws, graph, active=False)

    assert draft["is_active"] is False
    assert {p["message"] for p in draft["problems"]} >= {
        "Add at least one check",
        "Link the trigger to a first step",
    }
    refused = await api.patch(
        ws.path(f"/workflows/{draft['id']}"), headers=ws.owner.auth, json={"is_active": True}
    )
    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "workflow_incomplete"


async def test_a_broken_drawing_is_never_saved(api: AsyncClient, ws: WorkspaceFixture) -> None:
    graph = _graph(edges=[("trigger", "out", "nowhere")])

    response = await api.post(
        ws.path("/workflows"), headers=ws.owner.auth, json={"name": "Broken", "graph": graph}
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_workflow"


async def test_a_test_run_shows_what_would_happen_and_changes_nothing(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    lead_id = await _lead(api, ws)
    workflow, _ = await _fee_workflow(api, ws)
    before = await _get_lead(api, ws, lead_id)

    response = await api.post(
        ws.path(f"/workflows/{workflow['id']}/test"),
        headers=ws.owner.auth,
        json={"lead_id": lead_id, "data": {"amount": "400"}},
    )

    assert response.status_code == 200, response.text
    assert [(s["node_id"], s["result"]) for s in response.json()["steps"]] == [
        ("check", "yes"),
        ("stage", "done"),
        ("field", "done"),
    ]
    after = await _get_lead(api, ws, lead_id)
    assert (after["stage_id"], after["values"]) == (before["stage_id"], before["values"])
    assert await _runs(api, ws, workflow["id"]) == []


async def test_workflow_names_are_unique_and_deleting_keeps_history(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    workflow, _ = await _fee_workflow(api, ws)
    clash = await api.post(ws.path("/workflows"), headers=ws.owner.auth, json={"name": "Fee paid"})
    assert clash.status_code == 409

    deleted = await api.delete(ws.path(f"/workflows/{workflow['id']}"), headers=ws.owner.auth)

    assert deleted.status_code == 204
    assert (await api.get(ws.path("/workflows"), headers=ws.owner.auth)).json() == []
    again = await api.post(ws.path("/workflows"), headers=ws.owner.auth, json={"name": "Fee paid"})
    assert again.status_code == 201


async def test_only_an_admin_manages_automations(
    api: AsyncClient,
    ws: WorkspaceFixture,
    db_session: AsyncSession,
    hasher: PasswordHasherService,
) -> None:
    caller = await add_member(
        db_session, hasher, ws, key="caller", email="caller@pay.example", template_name="Caller"
    )
    await login(api, caller)

    for method, route in (("GET", "/workflows"), ("GET", "/apps"), ("POST", "/workflows")):
        response = await api.request(
            method, ws.path(route), headers=caller.auth, json={"name": "x"}
        )
        assert response.status_code == 403, f"{method} {route}: {response.text}"


async def test_with_the_feature_off_nothing_runs(
    api: AsyncClient, ws: WorkspaceFixture, db_session: AsyncSession
) -> None:
    path = await _connect(api, ws)
    ws.workspace.features = {**(ws.workspace.features or {}), "automations": False}
    await db_session.commit()

    assert (await api.get(ws.path("/workflows"), headers=ws.owner.auth)).status_code == 403
    assert (await _post(api, path, _payment())).status_code == 404


# --- 4. never AI-call -----------------------------------------------------------------


async def test_a_lead_the_rule_covers_is_never_called(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    paid = await _lead(api, ws, fee_status="Paid")
    unpaid = await _lead(api, ws, phone="+919000002002")
    rule = {
        "type": "group",
        "op": "AND",
        "children": [{"type": "field", "key": "fee_status", "op": "eq", "value": "Paid"}],
    }
    saved = await api.put(
        ws.path("/settings/voice-never-call"), headers=ws.owner.auth, json={"conditions": rule}
    )
    assert saved.status_code == 200, saved.text

    refused = await api.post(ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": paid})
    placed = await api.post(
        ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": unpaid}
    )

    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "lead_excluded_from_ai_calls"
    assert placed.status_code == 200, placed.text
    assert [c.recipient_phone_number for c in bolna.calls] == ["+919000002002"]

    cleared = await api.put(
        ws.path("/settings/voice-never-call"), headers=ws.owner.auth, json={"conditions": {}}
    )
    assert cleared.json() == {"conditions": {}}


async def test_a_broken_never_call_rule_is_refused(api: AsyncClient, ws: WorkspaceFixture) -> None:
    response = await api.put(
        ws.path("/settings/voice-never-call"),
        headers=ws.owner.auth,
        json={"conditions": {"type": "nonsense"}},
    )

    assert response.status_code == 422


# --- the small pieces ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("actual", "operator", "expected", "result"),
    [
        ("400", "equals", "400.00", True),
        ("400.5", "equals", "400", False),
        ("UPI", "equals", "upi", True),
        ("8000", "greater_than", "400", True),
        ("abc", "greater_than", "1", False),
        ("Video Editing", "contains", "editing", True),
        (None, "is_empty", None, True),
        ("", "is_not_empty", None, False),
        ("card", "not_equals", "upi", True),
    ],
)
def test_comparisons(actual: Any, operator: str, expected: Any, result: bool) -> None:
    assert compare(actual, operator, expected) is result


def test_rendering_never_leaves_a_raw_variable() -> None:
    data = {"amount": "400", "notes": {"course": "VE"}}
    assert render("₹{{amount}} for {{ notes.course }} {{missing}}!", data) == "₹400 for VE !"


def test_a_payment_link_payment_is_normalised() -> None:
    data = normalise_razorpay(_payment(event="payment_link.paid", paise=40050))
    assert data["amount"] == "400.50"
    assert data["name"] == "Asha"
    assert data["payment_id"] == "pay_TEST0001"
    assert data["payment_link_id"] == "plink_TEST"
