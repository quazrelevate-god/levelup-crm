"""Scheduled AI calls, workflow waits, WhatsApp, Call API and Calendly (docs/22).

1. **Reading "tomorrow 5:30 pm"** into an exact time, in calling hours.
2. **Callbacks** a lead asks for are booked automatically when switched on;
   anything unreadable becomes a task for a person.
3. **The worker** places what is due, a few calls per company at a time,
   through the same path as the AI Call button.
4. **Waits** pause a workflow and the worker carries it on.
5. **AI call, WhatsApp, Call API** blocks: the call is booked for the worker;
   messages are only ever sent from the worker.
6. **Calendly**: set up with the company's own token; signed bookings start
   workflows; forged or stale ones are refused.
7. **"AI call finished"** starts workflows.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import time
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.factories import WorkspaceFixture, build_workspace, login

from app.auth.passwords import PasswordHasherService
from app.config import Settings
from app.integrations.bolna import BolnaSettings, RecordingBolnaClient
from app.models import VoiceScheduledCall
from app.models.automation import WorkflowRun
from app.models.work import Task
from app.services.apps import razorpay_signature
from app.services.scheduled_call_runner import place_due_calls, resume_waiting_runs
from app.services.scheduled_calls import parse_callback

AGENT = "11111111-2222-3333-4444-555555555555"
PHONE = "+919000006001"
SECRET = "rzp-secret-for-waits"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


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
        agent_id=AGENT,
    )
    return client


@pytest.fixture
def worker_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "bolna_api_key": "bn-test-key-do-not-use-0000000000",
            "bolna_base_url": "https://api.bolna.invalid",
            "bolna_agent_id": AGENT,
            "voice_calling_hours": "",
        }
    )


@pytest.fixture
async def ws(
    api: AsyncClient, db_session: AsyncSession, hasher: PasswordHasherService
) -> WorkspaceFixture:
    fixture = await build_workspace(
        db_session, hasher, name="Waits Co", owner_email="owner@waits.example"
    )
    fixture.workspace.default_country_code = "91"
    await db_session.commit()
    await login(api, fixture.owner)
    return fixture


async def _lead(api: AsyncClient, ws: WorkspaceFixture, **values: Any) -> str:
    response = await api.post(
        ws.path("/leads"),
        headers=ws.owner.auth,
        json={"values": {"name": "Asha", "phone": PHONE, **values}},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _notes(api: AsyncClient, ws: WorkspaceFixture, lead_id: str) -> list[str]:
    timeline = await api.get(ws.path(f"/leads/{lead_id}/actions"), headers=ws.owner.auth)
    return [a["body"] for a in timeline.json()["items"] if a["kind"] == "NOTE"]


def _graph(
    trigger: str, *nodes: dict[str, Any], edges: list[tuple[str, str, str]]
) -> dict[str, Any]:
    return {
        "nodes": [
            {
                "id": "trigger",
                "type": "trigger",
                "position": {"x": 0, "y": 0},
                "data": {"trigger": trigger},
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


async def _workflow(
    api: AsyncClient, ws: WorkspaceFixture, graph: dict[str, Any], name: str = "W"
) -> str:
    created = await api.post(
        ws.path("/workflows"), headers=ws.owner.auth, json={"name": name, "graph": graph}
    )
    assert created.status_code == 201, created.text
    on = await api.patch(
        ws.path(f"/workflows/{created.json()['id']}"),
        headers=ws.owner.auth,
        json={"is_active": True},
    )
    assert on.status_code == 200, on.text
    return str(created.json()["id"])


async def _pay(api: AsyncClient, ws: WorkspaceFixture, *, payment_id: str = "pay_W1") -> None:
    connected = await api.post(ws.path("/apps/razorpay/connect"), headers=ws.owner.auth)
    await api.patch(ws.path("/apps/razorpay"), headers=ws.owner.auth, json={"secret": SECRET})
    raw = json.dumps(
        {
            "event": "payment.captured",
            "payload": {
                "payment": {"entity": {"id": payment_id, "amount": 40000, "contact": PHONE}}
            },
        }
    ).encode()
    response = await api.post(
        connected.json()["webhook_path"],
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": razorpay_signature(raw, SECRET),
        },
    )
    assert response.json()["outcome"] == "MATCHED", response.text


async def _run(db_session: AsyncSession, workflow_id: str) -> WorkflowRun:
    """The workflow's one run, read fresh (the worker wrote it in another session)."""
    rows = await db_session.execute(
        select(WorkflowRun)
        .where(WorkflowRun.workflow_id == workflow_id)
        .execution_options(populate_existing=True)
    )
    [run] = rows.scalars().all()
    return run


# --- 1. reading the time ---------------------------------------------------------

NOW = dt.datetime(2026, 10, 31, 9, 30, tzinfo=dt.UTC)  # 15:00 in Kolkata
HOURS = (dt.time(9), dt.time(21))


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("tomorrow 5:30 pm", "2026-11-01 17:30"),
        ("kal shaam 6 baje", "2026-11-01 18:00"),
        ("after 2 hours", "2026-10-31 17:00"),
        ("Monday morning", "2026-11-02 10:00"),
        ("10 am", "2026-11-01 10:00"),
        ("call me at 10 pm", "2026-11-01 09:00"),
        ("sometime", None),
        ("not now", None),
    ],
)
def test_the_callback_time_is_read_in_the_company_s_timezone(
    said: str, expected: str | None
) -> None:
    moment = parse_callback(said, now=NOW, timezone="Asia/Kolkata", hours=HOURS)
    got = moment.astimezone(IST).strftime("%Y-%m-%d %H:%M") if moment else None
    assert got == expected


# --- 2. callbacks the lead asks for -----------------------------------------------


async def _callback_setup(api: AsyncClient, ws: WorkspaceFixture, *, enabled: bool) -> str:
    field = await api.post(
        ws.path("/settings/lead-fields"),
        headers=ws.owner.auth,
        json={"label": "Callback Time", "field_type": "TEXT"},
    )
    assert field.status_code == 201, field.text
    mapping = await api.post(
        ws.path("/voice/extraction-mappings"),
        headers=ws.owner.auth,
        json={"disposition_name": "Callback Time", "target_field_key": "callback_time"},
    )
    assert mapping.status_code == 201, mapping.text
    saved = await api.put(
        ws.path("/settings/voice-auto-callback"),
        headers=ws.owner.auth,
        json={"enabled": enabled, "field_key": "callback_time"},
    )
    assert saved.status_code == 200, saved.text
    key = await api.post(
        ws.path("/settings/api-keys"),
        headers=ws.owner.auth,
        json={"name": "Bolna", "permission_template_id": str(ws.templates["Root"].id)},
    )
    return str(key.json()["key"])


async def _finish_call(
    api: AsyncClient, ws: WorkspaceFixture, key: str, lead_id: str, said: str
) -> None:
    trigger = await api.post(
        ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": lead_id}
    )
    assert trigger.status_code == 200, trigger.text
    done = await api.post(
        ws.path("/voice/executions"),
        headers={"X-API-Key": key},
        json={
            "execution_id": trigger.json()["execution_id"],
            "status": "completed",
            "conversation_duration": 50,
            "extracted_data": {"Callback Time": {"value": said, "confidence": 0.99}},
        },
    )
    assert done.status_code == 200, done.text


async def test_a_callback_the_lead_asks_for_is_booked(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    key = await _callback_setup(api, ws, enabled=True)
    lead_id = await _lead(api, ws)

    await _finish_call(api, ws, key, lead_id, "tomorrow 5:30 pm")

    listed = await api.get(
        ws.path("/voice/scheduled-calls"), headers=ws.owner.auth, params={"lead_id": lead_id}
    )
    [row] = listed.json()["items"]
    assert (row["status"], row["source"], row["reason"]) == (
        "PENDING",
        "CALLBACK",
        "tomorrow 5:30 pm",
    )
    assert dt.datetime.fromisoformat(row["due_at"]) > dt.datetime.now(dt.UTC)
    assert any("AI call scheduled" in n for n in await _notes(api, ws, lead_id))


async def test_an_unreadable_callback_becomes_a_task_and_off_means_nothing(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient, db_session: AsyncSession
) -> None:
    key = await _callback_setup(api, ws, enabled=True)
    lead_id = await _lead(api, ws)
    await _finish_call(api, ws, key, lead_id, "sometime later")

    listed = await api.get(
        ws.path("/voice/scheduled-calls"), headers=ws.owner.auth, params={"lead_id": lead_id}
    )
    assert listed.json()["items"] == []
    tasks = (await db_session.execute(select(Task))).scalars().all()
    assert any("sometime later" in t.title for t in tasks)

    await api.put(
        ws.path("/settings/voice-auto-callback"),
        headers=ws.owner.auth,
        json={"enabled": False, "field_key": "callback_time"},
    )
    other = await _lead(api, ws, phone="+919000006002")
    await _finish_call(api, ws, key, other, "tomorrow 11 am")
    listed = await api.get(
        ws.path("/voice/scheduled-calls"), headers=ws.owner.auth, params={"lead_id": other}
    )
    assert listed.json()["items"] == []


async def test_a_newer_schedule_replaces_the_older_one_and_can_be_cancelled(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    lead_id = await _lead(api, ws)
    first = await api.post(
        ws.path("/voice/scheduled-calls"),
        headers=ws.owner.auth,
        json={"lead_id": lead_id, "due_at": "2030-01-01T11:00:00+05:30"},
    )
    second = await api.post(
        ws.path("/voice/scheduled-calls"),
        headers=ws.owner.auth,
        json={"lead_id": lead_id, "due_at": "2030-01-02T11:00:00+05:30"},
    )
    assert first.status_code == second.status_code == 201

    every = await api.get(
        ws.path("/voice/scheduled-calls"),
        headers=ws.owner.auth,
        params={"lead_id": lead_id, "upcoming_only": "false"},
    )
    assert {(r["id"], r["status"]) for r in every.json()["items"]} == {
        (first.json()["id"], "CANCELLED"),
        (second.json()["id"], "PENDING"),
    }
    cancelled = await api.delete(
        ws.path(f"/voice/scheduled-calls/{second.json()['id']}"), headers=ws.owner.auth
    )
    assert cancelled.json()["status"] == "CANCELLED"
    again = await api.delete(
        ws.path(f"/voice/scheduled-calls/{second.json()['id']}"), headers=ws.owner.auth
    )
    assert again.status_code == 409


# --- 3. the worker places them -------------------------------------------------------


async def test_the_worker_keeps_to_the_company_s_call_limit(
    api: AsyncClient,
    ws: WorkspaceFixture,
    session_factory: async_sessionmaker[AsyncSession],
    worker_settings: Settings,
    db_session: AsyncSession,
) -> None:
    first = await _lead(api, ws)
    second = await _lead(api, ws, phone="+919000006003")
    for lead_id in (first, second):
        made = await api.post(
            ws.path("/voice/scheduled-calls"),
            headers=ws.owner.auth,
            json={"lead_id": lead_id, "due_at": "2020-01-01T10:00:00+05:30"},
        )
        assert made.status_code == 201, made.text
    phone = RecordingBolnaClient()
    one_at_a_time = worker_settings.model_copy(update={"voice_max_parallel_calls": 1})

    once = await place_due_calls(session_factory, settings=one_at_a_time, client=phone)
    twice = await place_due_calls(session_factory, settings=one_at_a_time, client=phone)

    assert once["placed"] == 1
    assert twice == {"placed": 0, "waiting": 1, "skipped": 0, "failed": 0}  # the first is still on
    assert len(phone.calls) == 1
    rows = (await db_session.execute(select(VoiceScheduledCall))).scalars().all()
    assert sorted(r.status for r in rows) == ["PENDING", "PLACED"]


async def test_the_never_call_rule_still_wins(
    api: AsyncClient,
    ws: WorkspaceFixture,
    session_factory: async_sessionmaker[AsyncSession],
    worker_settings: Settings,
    db_session: AsyncSession,
) -> None:
    lead_id = await _lead(api, ws)
    await api.put(
        ws.path("/settings/voice-never-call"),
        headers=ws.owner.auth,
        json={
            "conditions": {
                "type": "group",
                "op": "AND",
                "children": [{"type": "field", "key": "name", "op": "eq", "value": "Asha"}],
            }
        },
    )
    await api.post(
        ws.path("/voice/scheduled-calls"),
        headers=ws.owner.auth,
        json={"lead_id": lead_id, "due_at": "2020-01-01T10:00:00+05:30"},
    )
    phone = RecordingBolnaClient()

    result = await place_due_calls(session_factory, settings=worker_settings, client=phone)

    assert result["skipped"] == 1 and phone.calls == []
    assert any("not placed" in n for n in await _notes(api, ws, lead_id))


# --- 4 and 5. waits and the new blocks ---------------------------------------------------


async def test_a_wait_pauses_the_run_and_the_worker_carries_it_on(
    api: AsyncClient,
    ws: WorkspaceFixture,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    lead_id = await _lead(api, ws)
    workflow_id = await _workflow(
        api,
        ws,
        _graph(
            "razorpay.payment_captured",
            _node("wait", "wait", amount=10, unit="minutes"),
            _node("note", "add_note", text="Ten minutes on, {{lead.name}} paid {{amount}}"),
            edges=[("trigger", "out", "wait"), ("wait", "out", "note")],
        ),
    )
    await _pay(api, ws)

    run = await _run(db_session, workflow_id)
    assert run.status == "WAITING"
    assert run.resume_at is not None
    early = await resume_waiting_runs(
        session_factory, clock=lambda: run.resume_at - dt.timedelta(minutes=1)
    )
    assert early["resumed"] == 0

    later = run.resume_at + dt.timedelta(seconds=1)
    done = await resume_waiting_runs(session_factory, clock=lambda: later)

    assert done["resumed"] == 1
    run = await _run(db_session, workflow_id)
    assert run.status == "SUCCESS"
    assert [s["result"] for s in run.steps] == ["waiting", "done"]
    assert "Ten minutes on, Asha paid 400" in await _notes(api, ws, lead_id)


async def test_a_test_run_does_not_actually_wait(api: AsyncClient, ws: WorkspaceFixture) -> None:
    lead_id = await _lead(api, ws)
    graph = _graph(
        "razorpay.payment_captured",
        _node("wait", "wait", amount=1, unit="days"),
        _node("call", "ai_call", purpose="followup_booking"),
        edges=[("trigger", "out", "wait"), ("wait", "out", "call")],
    )
    created = await api.post(
        ws.path("/workflows"), headers=ws.owner.auth, json={"name": "T", "graph": graph}
    )

    tested = await api.post(
        ws.path(f"/workflows/{created.json()['id']}/test"),
        headers=ws.owner.auth,
        json={"lead_id": lead_id, "data": {"amount": "400"}},
    )

    details = [s["detail"] for s in tested.json()["steps"]]
    assert details[0] == "Would wait 1 day, then carry on"
    assert details[1].startswith("Would book an AI call")


async def test_an_ai_call_block_books_a_call_for_the_worker(
    api: AsyncClient, ws: WorkspaceFixture, db_session: AsyncSession, bolna: RecordingBolnaClient
) -> None:
    lead_id = await _lead(api, ws)
    workflow_id = await _workflow(
        api,
        ws,
        _graph(
            "razorpay.payment_captured",
            _node("call", "ai_call", purpose="followup_booking", reason="paid {{amount}}"),
            edges=[("trigger", "out", "call")],
        ),
    )

    await _pay(api, ws)

    run = await _run(db_session, workflow_id)
    [row] = (await db_session.execute(select(VoiceScheduledCall))).scalars().all()
    assert (row.source, row.purpose, row.workflow_run_id) == (
        "WORKFLOW",
        "followup_booking",
        run.id,
    )
    assert str(row.lead_id) == lead_id
    assert "paid 400" in (row.reason or "")
    assert bolna.calls == []  # never dialled from the webhook itself


async def test_whatsapp_is_sent_by_the_worker_never_by_the_webhook(
    api: AsyncClient,
    ws: WorkspaceFixture,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    lead_id = await _lead(api, ws)
    await api.post(ws.path("/apps/whatsapp/connect"), headers=ws.owner.auth)
    saved = await api.patch(
        ws.path("/apps/whatsapp"),
        headers=ws.owner.auth,
        json={"settings": {"phone_number_id": "1234567890"}, "credential": "wa-token-for-tests"},
    )
    assert saved.json()["credential_set"] is True
    assert "wa-token-for-tests" not in saved.text
    workflow_id = await _workflow(
        api,
        ws,
        _graph(
            "razorpay.payment_captured",
            _node(
                "wa",
                "send_whatsapp",
                template="fee_received",
                language="en",
                params=["{{lead.name}}", "{{amount}}"],
            ),
            edges=[("trigger", "out", "wa")],
        ),
    )
    await _pay(api, ws)
    run = await _run(db_session, workflow_id)
    assert (run.status, run.steps) == ("WAITING", [])

    sent: list[httpx.Request] = []

    def meta(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.1"}]})

    await resume_waiting_runs(
        session_factory,
        http=httpx.MockTransport(meta),
        clock=lambda: dt.datetime.now(dt.UTC) + dt.timedelta(seconds=5),
    )

    [request] = sent
    assert str(request.url) == "https://graph.facebook.com/v21.0/1234567890/messages"
    assert request.headers["authorization"] == "Bearer wa-token-for-tests"
    body = json.loads(request.content)
    assert body["to"] == "919000006001"
    assert [p["text"] for p in body["template"]["components"][0]["parameters"]] == ["Asha", "400"]
    run = await _run(db_session, workflow_id)
    assert run.status == "SUCCESS"
    assert any("WhatsApp sent" in n for n in await _notes(api, ws, lead_id))


async def test_a_call_api_block_must_be_https_and_public(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    for url in ("http://example.com/hook", "https://localhost/hook", "https://10.0.0.5/x"):
        graph = _graph(
            "razorpay.payment_captured",
            _node("api", "call_api", method="POST", url=url, body="{}"),
            edges=[("trigger", "out", "api")],
        )
        created = await api.post(
            ws.path("/workflows"), headers=ws.owner.auth, json={"name": url, "graph": graph}
        )
        refused = await api.patch(
            ws.path(f"/workflows/{created.json()['id']}"),
            headers=ws.owner.auth,
            json={"is_active": True},
        )
        assert refused.status_code == 422, url


async def test_a_call_api_block_posts_what_it_was_given(
    api: AsyncClient,
    ws: WorkspaceFixture,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    await _lead(api, ws)
    workflow_id = await _workflow(
        api,
        ws,
        _graph(
            "razorpay.payment_captured",
            _node(
                "api",
                "call_api",
                method="POST",
                url="https://hooks.example.com/paid",
                headers=[{"key": "X-Token", "value": "abc"}],
                body='{"phone": "{{lead.phone}}", "amount": "{{amount}}"}',
            ),
            edges=[("trigger", "out", "api")],
        ),
    )
    await _pay(api, ws)
    seen: list[httpx.Request] = []

    def hook(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    await resume_waiting_runs(
        session_factory,
        http=httpx.MockTransport(hook),
        clock=lambda: dt.datetime.now(dt.UTC) + dt.timedelta(seconds=5),
    )

    [request] = seen
    assert request.headers["x-token"] == "abc"
    assert json.loads(request.content) == {"phone": PHONE, "amount": "400"}
    assert (await _run(db_session, workflow_id)).status == "SUCCESS"


# --- 6. Calendly ----------------------------------------------------------------------------


def _calendly_transport(made: list[dict[str, Any]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users/me":
            return httpx.Response(
                200,
                json={
                    "resource": {
                        "uri": "https://api.calendly.com/users/U1",
                        "current_organization": "https://api.calendly.com/organizations/O1",
                        "email": "me@acme.example",
                    }
                },
            )
        if request.url.path == "/webhook_subscriptions":
            made.append(json.loads(request.content))
            return httpx.Response(
                201, json={"resource": {"uri": "https://api.calendly.com/webhook_subscriptions/W1"}}
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _signed(body: bytes, key: str, *, at: int | None = None) -> str:
    stamp = str(at if at is not None else int(time.time()))
    digest = hmac.new(key.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={stamp},v1={digest}"


async def test_calendly_is_set_up_with_a_token_and_a_booking_starts_workflows(
    api: AsyncClient, ws: WorkspaceFixture, wired_app: FastAPI, db_session: AsyncSession
) -> None:
    made: list[dict[str, Any]] = []
    wired_app.state.calendly_transport = _calendly_transport(made)
    lead_id = await _lead(api, ws, email="asha@example.com")
    stages = (await api.get(ws.path("/settings/stages"), headers=ws.owner.auth)).json()
    booked_stage = stages["active"][0]["id"]
    await _workflow(
        api,
        ws,
        _graph(
            "calendly.invitee_created",
            _node("stage", "update_stage", stage_id=booked_stage),
            edges=[("trigger", "out", "stage")],
        ),
    )

    setup = await api.post(
        ws.path("/apps/calendly/setup"),
        headers=ws.owner.auth,
        json={"token": "calendly-pat-for-tests"},
    )

    assert setup.status_code == 200, setup.text
    app = setup.json()
    assert (app["connected"], app["secret_set"], app["credential_set"]) == (True, True, True)
    assert "calendly-pat-for-tests" not in setup.text
    [subscription] = made
    assert subscription["events"] == ["invitee.created", "invitee.canceled"]
    assert subscription["url"].endswith(app["webhook_path"])

    from app.models.automation import AppConnection

    key = (
        await db_session.execute(
            select(AppConnection.secret).where(AppConnection.provider == "calendly")
        )
    ).scalar_one()
    body = json.dumps(
        {
            "event": "invitee.created",
            "payload": {
                "uri": "https://api.calendly.com/scheduled_events/E1/invitees/I1",
                "email": "Asha@Example.com",
                "name": "Asha",
                "scheduled_event": {
                    "name": "Interview",
                    "start_time": "2030-01-05T05:30:00.000000Z",
                },
            },
        }
    ).encode()

    forged = await api.post(
        app["webhook_path"],
        content=body,
        headers={"Calendly-Webhook-Signature": _signed(body, "wrong")},
    )
    stale = await api.post(
        app["webhook_path"],
        content=body,
        headers={"Calendly-Webhook-Signature": _signed(body, key, at=int(time.time()) - 3600)},
    )
    good = await api.post(
        app["webhook_path"],
        content=body,
        headers={"Calendly-Webhook-Signature": _signed(body, key)},
    )

    assert (forged.status_code, stale.status_code) == (401, 401)
    assert good.json()["outcome"] == "MATCHED", good.text
    lead = (await api.get(ws.path(f"/leads/{lead_id}"), headers=ws.owner.auth)).json()
    assert lead["stage_id"] == booked_stage
    assert any(
        "Interview" in n and "booked for Sat 05 Jan, 11:00 AM" in n
        for n in await _notes(api, ws, lead_id)
    )


async def test_a_bad_calendly_token_is_refused(
    api: AsyncClient, ws: WorkspaceFixture, wired_app: FastAPI
) -> None:
    wired_app.state.calendly_transport = httpx.MockTransport(lambda r: httpx.Response(401))

    refused = await api.post(
        ws.path("/apps/calendly/setup"), headers=ws.owner.auth, json={"token": "not-a-real-token"}
    )

    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "calendly_token_refused"


# --- 7. AI call finished -------------------------------------------------------------------


async def test_a_finished_ai_call_starts_workflows(
    api: AsyncClient, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    lead_id = await _lead(api, ws)
    await _workflow(
        api,
        ws,
        _graph(
            "voice.call_finished",
            _node(
                "answered",
                "condition",
                match="all",
                rules=[{"variable": "answered", "operator": "equals", "value": "yes"}],
            ),
            _node("note", "add_note", text="Talked for {{duration_seconds}}s"),
            edges=[("trigger", "out", "answered"), ("answered", "yes", "note")],
        ),
    )
    key = await api.post(
        ws.path("/settings/api-keys"),
        headers=ws.owner.auth,
        json={"name": "Bolna", "permission_template_id": str(ws.templates["Root"].id)},
    )
    trigger = await api.post(
        ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": lead_id}
    )
    await api.post(
        ws.path("/voice/executions"),
        headers={"X-API-Key": key.json()["key"]},
        json={
            "execution_id": trigger.json()["execution_id"],
            "status": "completed",
            "conversation_duration": 73,
        },
    )

    assert "Talked for 73s" in await _notes(api, ws, lead_id)
