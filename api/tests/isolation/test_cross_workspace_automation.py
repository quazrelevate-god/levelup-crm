"""Workflows, connected apps and payments never cross a workspace boundary (docs/18).

A workflow changes leads with its workspace's authority, and an app's event
log holds customers' payments. Another workspace must not read, edit, test or
delete B's workflows by id, see them or B's payments in a list, run a workflow
against B's lead, or have B's Razorpay URL act on its own leads.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import AsyncClient
from tests.factories import WorkspaceFixture
from tests.isolation.conftest import TenantPair
from tests.isolation.test_cross_workspace import (
    AUTOMATION_COLLECTION_ROUTES,
    AUTOMATION_COLLECTION_WRITE_ROUTES,
    WORKFLOW_ID_ROUTES,
)

from app.services.apps import razorpay_signature

B_NAME = "B's secret fee workflow"
SECRET = "isolation-webhook-secret"


async def _workflow_in(api: AsyncClient, fixture: WorkspaceFixture, name: str) -> str:
    response = await api.post(
        fixture.path("/workflows"),
        headers=fixture.owner.auth,
        json={"name": name, "trigger": "razorpay.payment_captured"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _lead_in(api: AsyncClient, fixture: WorkspaceFixture, phone: str) -> str:
    response = await api.post(
        fixture.path("/leads"),
        headers=fixture.owner.auth,
        json={"values": {"name": "Lead", "phone": phone}},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


@pytest.mark.parametrize(("method", "template"), WORKFLOW_ID_ROUTES)
async def test_another_workspace_s_workflow_id_is_a_404(
    api: AsyncClient, tenants: TenantPair, method: str, template: str
) -> None:
    b_id = await _workflow_in(api, tenants.b, B_NAME)
    a_lead = await _lead_in(api, tenants.a, "+919000003001")
    body: dict[str, Any] | None = None
    if method == "PATCH":
        body = {"name": "Renamed By An Outsider"}
    elif template.endswith("/test"):
        body = {"lead_id": a_lead, "data": {"amount": "400"}}

    response = await api.request(
        method,
        tenants.a.path(template.format(workflow_id=b_id)),
        headers=tenants.a.owner.auth,
        json=body,
    )

    assert response.status_code == 404, f"{method} {template} leaked: {response.text}"
    assert B_NAME not in response.text
    listed = await api.get(tenants.b.path("/workflows"), headers=tenants.b.owner.auth)
    assert [row["name"] for row in listed.json()] == [B_NAME]


async def test_a_workflow_cannot_be_tested_against_another_workspace_s_lead(
    api: AsyncClient, tenants: TenantPair
) -> None:
    a_id = await _workflow_in(api, tenants.a, "A workflow")
    b_lead = await _lead_in(api, tenants.b, "+919000003002")

    response = await api.post(
        tenants.a.path(f"/workflows/{a_id}/test"),
        headers=tenants.a.owner.auth,
        json={"lead_id": b_lead, "data": {}},
    )

    assert response.status_code == 404, response.text


@pytest.mark.parametrize("route", AUTOMATION_COLLECTION_ROUTES)
async def test_automation_lists_show_only_their_own_workspace(
    api: AsyncClient, tenants: TenantPair, route: str
) -> None:
    await _workflow_in(api, tenants.b, B_NAME)
    for fixture in (tenants.a, tenants.b):
        connected = await api.post(
            fixture.path("/apps/razorpay/connect"), headers=fixture.owner.auth
        )
        assert connected.status_code == 201, connected.text

    response = await api.get(
        tenants.a.path(route.format(provider="razorpay")), headers=tenants.a.owner.auth
    )

    assert response.status_code == 200, response.text
    assert B_NAME not in response.text
    b_apps = await api.get(tenants.b.path("/apps"), headers=tenants.b.owner.auth)
    assert b_apps.json()[0]["webhook_path"] not in response.text


@pytest.mark.parametrize(("method", "route"), AUTOMATION_COLLECTION_WRITE_ROUTES)
async def test_a_non_member_cannot_write_another_workspace_s_automations(
    api: AsyncClient, tenants: TenantPair, method: str, route: str
) -> None:
    body: dict[str, Any] = {"name": "Smuggled"}
    if route == "/apps/{provider}":
        body = {"secret": "smuggled"}
    elif route == "/settings/voice-never-call":
        body = {"conditions": {}}
    elif route.startswith("/apps"):
        body = {}

    response = await api.request(
        method,
        tenants.b.path(route.format(provider="razorpay")),
        headers=tenants.a.owner.auth,
        json=body,
    )

    assert response.status_code == 404, response.text
    listed = await api.get(tenants.b.path("/workflows"), headers=tenants.b.owner.auth)
    assert listed.json() == []
    [app] = (await api.get(tenants.b.path("/apps"), headers=tenants.b.owner.auth)).json()
    assert app["connected"] is False


async def test_b_s_razorpay_url_never_touches_a_s_leads(
    api: AsyncClient, tenants: TenantPair
) -> None:
    """Same phone in both workspaces: B's payment lands on B's lead only."""
    phone = "+919000003003"
    a_lead = await _lead_in(api, tenants.a, phone)
    b_lead = await _lead_in(api, tenants.b, phone)
    connected = await api.post(
        tenants.b.path("/apps/razorpay/connect"), headers=tenants.b.owner.auth
    )
    await api.patch(
        tenants.b.path("/apps/razorpay"), headers=tenants.b.owner.auth, json={"secret": SECRET}
    )
    raw = json.dumps(
        {
            "event": "payment.captured",
            "payload": {
                "payment": {"entity": {"id": "pay_ISO", "amount": 40000, "contact": phone}}
            },
        }
    ).encode()

    delivered = await api.post(
        connected.json()["webhook_path"],
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": razorpay_signature(raw, SECRET),
        },
    )

    assert delivered.json()["outcome"] == "MATCHED"
    [event] = (
        await api.get(tenants.b.path("/apps/razorpay/events"), headers=tenants.b.owner.auth)
    ).json()["items"]
    assert event["lead_id"] == b_lead
    a_timeline = await api.get(
        tenants.a.path(f"/leads/{a_lead}/actions"), headers=tenants.a.owner.auth
    )
    assert all("Razorpay" not in (a.get("body") or "") for a in a_timeline.json()["items"])
