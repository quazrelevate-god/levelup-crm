"""The cross-workspace isolation matrix for M1.

Three families of probe, because a tenancy leak has three shapes:

1. **By direct id** — A asks for B's resource by its real uuid.
2. **By list** — A lists a collection and B's rows must not appear in it.
3. **By filter/reference** — A submits B's id as a *parameter* to a write in A,
   which is the leak that survives naive `WHERE workspace_id` filtering: the
   read is scoped, but the foreign key is not.

Every direct-id probe asserts 404 specifically, not merely "an error". A 403
would confirm existence.
"""

from __future__ import annotations

import uuid
from io import BytesIO

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from openpyxl import Workbook
from tests.isolation.conftest import TenantPair

pytestmark = pytest.mark.integration

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# The prefix every tenant-scoped route hangs off. Used by the coverage guard to
# work out which operations this matrix is responsible for.
_TENANT_PREFIX = "/api/v1/workspaces/{workspace_id}"


def _members_workbook() -> bytes:
    """A workbook that would import cleanly if the request were allowed.

    `Caller` is one of the five templates every workspace is provisioned with,
    so this names a template that genuinely exists in the target — the request
    fails on scoping alone, not on bad input.
    """
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["email", "full_name", "template"])
    sheet.append(["smuggled@example.com", "Smuggled Member", "Caller"])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# Routes under /workspaces/{workspace_id} that take a membership id. As
# milestones land, extend this list — the tests below pick up new entries with
# no further edits.
M1_MEMBERSHIP_ROUTES: list[tuple[str, str]] = [
    ("GET", "/members/{membership_id}"),
    ("PATCH", "/members/{membership_id}"),
    ("GET", "/members/{membership_id}/availability-log"),
    ("PUT", "/members/{membership_id}/availability"),
    ("POST", "/members/{membership_id}/license"),
    ("DELETE", "/members/{membership_id}/license"),
    ("POST", "/members/{membership_id}/deactivate"),
    ("POST", "/members/{membership_id}/reactivate"),
]

# Collection routes A may call in its own workspace. None of them may return a
# row belonging to B.
M1_COLLECTION_ROUTES: list[str] = [
    "/members",
    "/members/hierarchy",
    "/members/seats",
    "/settings/permission-templates",
]

# Collection *writes* — no resource id in the path, so the only probe that
# applies is "A posts to B's path". Each has its own test below, because their
# request bodies differ too much to parametrise usefully.
M1_COLLECTION_WRITE_ROUTES: list[tuple[str, str]] = [
    ("POST", "/members"),
    ("POST", "/members/bulk-upload"),
]

# The tenant root itself, covered by named tests further down.
M1_WORKSPACE_ROOT_ROUTES: list[tuple[str, str]] = [
    ("GET", ""),
    ("PATCH", ""),
]

# --- M2: the field definition engine -----------------------------------------

# Settings collections. A field, option or index belonging to B must never
# appear in one of A's responses.
M2_COLLECTION_ROUTES: list[str] = [
    "/settings/lead-fields",
    "/settings/indexed-fields",
    # The two registries are product constants, not tenant data — they return
    # the same 13 and 8 entries for every workspace. Listed so the coverage
    # guard sees them; the list test below skips them for that reason.
    "/settings/field-types",
    "/settings/action-field-types",
]

#: Registry routes carry no tenant data, so "B's ids must not appear" is
#: vacuous for them. They are still probed for the *access* check.
M2_TENANT_FREE_ROUTES: frozenset[str] = frozenset(
    {"/settings/field-types", "/settings/action-field-types"}
)

# Routes taking a field id. A's admin must not reach B's field by either
# spelling — under A's workspace path or under B's.
M2_FIELD_ROUTES: list[tuple[str, str]] = [
    ("GET", "/settings/lead-fields/{field_id}"),
    ("PATCH", "/settings/lead-fields/{field_id}"),
    ("POST", "/settings/lead-fields/{field_id}/hide"),
    ("POST", "/settings/lead-fields/{field_id}/unhide"),
    ("GET", "/settings/lead-fields/{field_id}/options"),
    ("POST", "/settings/lead-fields/{field_id}/options"),
    ("POST", "/settings/lead-fields/{field_id}/options/bulk"),
    ("PATCH", "/settings/lead-fields/{field_id}/options/reorder"),
    ("DELETE", "/settings/indexed-fields/{field_id}"),
]

M2_COLLECTION_WRITE_ROUTES: list[tuple[str, str]] = [
    ("POST", "/settings/lead-fields"),
    ("POST", "/settings/indexed-fields"),
    ("PUT", "/settings/identity-field"),
    ("PUT", "/settings/primary-fields"),
]

#: Two-id routes, each needing its own test because both ids must be checked.
M2_TWO_ID_ROUTES: list[tuple[str, str]] = [
    ("PATCH", "/settings/lead-fields/{field_id}/options/{option_id}"),
    ("DELETE", "/settings/lead-fields/{field_id}/options/{option_id}"),
    ("POST", "/settings/lead-fields/{field_id}/options/copy-from/{source_field_id}"),
]

# --- M3: pipeline and taxonomy -----------------------------------------------

M3_COLLECTION_ROUTES: list[str] = [
    "/settings/stages",
    "/settings/lost-reasons",
    "/settings/call-dispositions",
    "/settings/custom-actions",
    "/settings/preferences",
    "/settings/voice-outcome-stages",
]

#: Routes taking a taxonomy id, tagged with which kind so the probe can aim a
#: genuine, existing id from workspace B at workspace A.
M3_ID_ROUTES: list[tuple[str, str, str]] = [
    ("PATCH", "/settings/stages/{stage_id}", "stage"),
    ("DELETE", "/settings/stages/{stage_id}", "stage"),
    ("PATCH", "/settings/lost-reasons/{reason_id}", "lost_reason"),
    ("DELETE", "/settings/lost-reasons/{reason_id}", "lost_reason"),
    ("PATCH", "/settings/call-dispositions/{disposition_id}", "disposition"),
    ("POST", "/settings/call-dispositions/{disposition_id}/set-default", "disposition"),
    ("POST", "/settings/call-dispositions/{disposition_id}/archive", "disposition"),
]

M3_COLLECTION_WRITE_ROUTES: list[tuple[str, str]] = [
    ("POST", "/settings/stages"),
    ("PATCH", "/settings/stages/reorder"),
    ("POST", "/settings/lost-reasons"),
    ("POST", "/settings/call-dispositions"),
    ("PATCH", "/settings/preferences"),
    ("POST", "/settings/custom-actions"),
    # Which stages a finished AI call moves a lead to. A single-document
    # setting rather than a collection, so set and clear sit here beside the
    # other taxonomy writes; the GET is in `M3_COLLECTION_ROUTES`.
    ("PUT", "/settings/voice-outcome-stages"),
    ("DELETE", "/settings/voice-outcome-stages"),
]

#: Custom-action routes, which additionally sit behind a feature flag.
M3_ACTION_ROUTES: list[tuple[str, str]] = [
    ("GET", "/settings/custom-actions/{type_id}"),
    ("PATCH", "/settings/custom-actions/{type_id}"),
    ("POST", "/settings/custom-actions/{type_id}/archive"),
    ("GET", "/settings/custom-actions/{type_id}/fields"),
    ("POST", "/settings/custom-actions/{type_id}/fields"),
    ("DELETE", "/settings/custom-actions/{type_id}/fields/{field_id}"),
]

_M3_BODIES: dict[str, dict[str, object]] = {
    "/settings/stages/{stage_id}": {"label": "Renamed"},
    "/settings/lost-reasons/{reason_id}": {"label": "Renamed"},
    "/settings/call-dispositions/{disposition_id}": {"label": "Renamed"},
    "/settings/custom-actions/{type_id}": {"name": "Renamed"},
    "/settings/custom-actions/{type_id}/fields": {"label": "Smuggled", "field_type": "TEXT"},
}

# --- M4: field-level permissions ---------------------------------------------

M4_COLLECTION_ROUTES: list[str] = [
    "/settings/permission-templates",
    # A product constant like the type registries: the 13 group definitions are
    # identical for every workspace.
    "/settings/permission-templates/capability-schema",
]

M4_TENANT_FREE_ROUTES: frozenset[str] = frozenset(
    {"/settings/permission-templates/capability-schema"}
)

M4_TEMPLATE_ROUTES: list[tuple[str, str]] = [
    ("GET", "/settings/permission-templates/{template_id}"),
    ("PATCH", "/settings/permission-templates/{template_id}"),
    ("DELETE", "/settings/permission-templates/{template_id}"),
    ("GET", "/settings/permission-templates/{template_id}/field-grants"),
    ("PUT", "/settings/permission-templates/{template_id}/field-grants"),
    ("PUT", "/settings/permission-templates/{template_id}/field-grants/bulk"),
    ("GET", "/settings/permission-templates/{template_id}/lead-view"),
    ("PUT", "/settings/permission-templates/{template_id}/lead-view"),
    ("GET", "/settings/permission-templates/{template_id}/assignees"),
]

M4_COLLECTION_WRITE_ROUTES: list[tuple[str, str]] = [
    ("POST", "/settings/permission-templates"),
]

_M4_BODIES: dict[str, dict[str, object]] = {
    "/settings/permission-templates/{template_id}": {"name": "Renamed By An Outsider"},
    "/settings/permission-templates/{template_id}/field-grants": {"grants": []},
    "/settings/permission-templates/{template_id}/field-grants/bulk": {
        "grant": "VIEW",
        "value": True,
    },
    "/settings/permission-templates/{template_id}/lead-view": {"layout": []},
}

# --- M5: leads, actions, changesets, templates --------------------------------

M5_COLLECTION_ROUTES: list[str] = ["/leads", "/changesets", "/templates"]

M5_LEAD_ROUTES: list[tuple[str, str]] = [
    ("GET", "/leads/{lead_id}"),
    ("PATCH", "/leads/{lead_id}"),
    ("DELETE", "/leads/{lead_id}"),
    ("GET", "/leads/{lead_id}/actions"),
    ("POST", "/leads/{lead_id}/notes"),
    ("POST", "/leads/{lead_id}/calls"),
    ("POST", "/leads/{lead_id}/custom-actions"),
    ("POST", "/leads/{lead_id}/messages"),
]

M5_COLLECTION_WRITE_ROUTES: list[tuple[str, str]] = [
    ("POST", "/leads"),
    ("POST", "/templates"),
]

M5_TEMPLATE_ROUTES: list[tuple[str, str]] = [
    ("DELETE", "/templates/{template_id}"),
    ("POST", "/templates/{template_id}/render"),
]

_M5_BODIES: dict[str, dict[str, object]] = {
    "/leads/{lead_id}": {"values": {"name": "Renamed By An Outsider"}},
    "/leads/{lead_id}/notes": {"body": "smuggled note"},
    "/leads/{lead_id}/calls": {
        "direction": "OUTGOING",
        "disposition_id": "00000000-0000-0000-0000-000000000000",
        "duration_seconds": 1,
    },
    "/leads/{lead_id}/custom-actions": {
        "action_type_id": "00000000-0000-0000-0000-000000000000",
        "values": {},
    },
    "/leads/{lead_id}/messages": {"channel": "SMS", "body": "smuggled"},
}

#: Bodies for the M2 field routes, by route template. A write probe must send
#: a *valid* body: a 422 for a malformed payload would mask whether the scoping
#: check fired at all.
_FIELD_BODIES: dict[str, dict[str, object]] = {
    "/settings/lead-fields/{field_id}": {"label": "Renamed By An Outsider"},
    "/settings/lead-fields/{field_id}/options": {"label": "Smuggled Option"},
    "/settings/lead-fields/{field_id}/options/bulk": {"labels": ["Smuggled"]},
    "/settings/lead-fields/{field_id}/options/reorder": {"ordered_ids": []},
}

_BODIES: dict[str, dict[str, object]] = {
    "PATCH": {"full_name": "Renamed By An Outsider"},
    "PUT": {"status": "ON_LEAVE", "note": "set by an outsider"},
    "POST": {},
}


# --- M6: saved filters, layouts, filtered search ------------------------------

M6_COLLECTION_ROUTES: list[str] = ["/filters", "/layouts"]

M6_COLLECTION_WRITE_ROUTES: list[tuple[str, str]] = [
    ("POST", "/filters"),
    ("PATCH", "/filters/reorder"),
    ("PUT", "/layouts"),
    ("POST", "/leads/search"),
]

M6_FILTER_ROUTES: list[tuple[str, str]] = [
    ("GET", "/filters/{filter_id}"),
    ("PATCH", "/filters/{filter_id}"),
    ("DELETE", "/filters/{filter_id}"),
    ("POST", "/filters/{filter_id}/duplicate"),
    ("GET", "/filters/{filter_id}/stats"),
]

_M6_BODIES: dict[str, dict[str, object]] = {
    "/filters/{filter_id}": {"name": "Renamed By An Outsider"},
    "/filters": {
        "name": "Smuggled",
        "definition": {"type": "group", "op": "AND", "children": []},
    },
    "/filters/reorder": {"filter_ids": []},
    "/layouts": {"columns": ["identity_value"]},
    "/leads/search": {"filter": {"type": "group", "op": "AND", "children": []}},
}


# --- M7: bulk edit and undo ---------------------------------------------------

M7_CHANGESET_ROUTES: list[tuple[str, str]] = [
    ("GET", "/changesets/{changeset_id}"),
    ("POST", "/changesets/{changeset_id}/preview-undo"),
    ("POST", "/changesets/{changeset_id}/undo"),
]

M7_COLLECTION_WRITE_ROUTES: list[tuple[str, str]] = [
    ("POST", "/leads/bulk"),
    ("POST", "/leads/export"),
    ("POST", "/leads/merge"),
]

#: An export job holds a file of one workspace's customer data. Reaching one
#: across the boundary would download it.
M7_EXPORT_ROUTES: list[tuple[str, str]] = [
    ("GET", "/leads/export/{job_id}"),
]

M7_TASK_ROUTES: list[tuple[str, str]] = [
    ("GET", "/tasks/{task_id}"),
    ("PATCH", "/tasks/{task_id}"),
    ("POST", "/tasks/{task_id}/complete"),
    ("POST", "/tasks/{task_id}/reopen"),
]

M7_LABEL_ROUTES: list[tuple[str, str]] = [
    ("PATCH", "/labels/{label_id}"),
    ("DELETE", "/labels/{label_id}"),
]

M7_JOB_ROUTES: list[tuple[str, str]] = [
    ("GET", "/imports/{job_id}"),
    ("PUT", "/imports/{job_id}/mapping"),
    ("POST", "/imports/{job_id}/preview"),
    ("POST", "/imports/{job_id}/commit"),
]

#: Routes taking both a lead id and a label id — the two-id shape that a naive
#: scoping check passes and a real one has to resolve twice.
M7_LEAD_LABEL_ROUTES: list[tuple[str, str]] = [
    ("POST", "/leads/{lead_id}/labels/{label_id}"),
    ("DELETE", "/leads/{lead_id}/labels/{label_id}"),
]

M7_LEAD_CHILD_ROUTES: list[str] = [
    "/leads/{lead_id}/tasks",
    "/leads/{lead_id}/labels",
]

M7_COLLECTION_ROUTES: list[str] = [
    "/tasks",
    "/labels",
    "/imports",
    "/imports/fields",
    "/tasks/counts",
]

# M8 — routing configuration and the distribution write. Both id-bearing
# resources are settings, so a leaked id would let one workspace rewrite
# another's lead routing; `/leads/distribute` is the write that would act on it.
M8_GROUP_ROUTES: list[tuple[str, str]] = [
    ("PATCH", "/settings/sales-groups/{group_id}"),
    ("DELETE", "/settings/sales-groups/{group_id}"),
    ("GET", "/settings/sales-groups/{group_id}/members"),
    ("PUT", "/settings/sales-groups/{group_id}/members"),
]

M8_RULE_ROUTES: list[tuple[str, str]] = [
    ("PATCH", "/settings/assignment-rules/{rule_id}"),
    ("DELETE", "/settings/assignment-rules/{rule_id}"),
]

M8_COLLECTION_ROUTES: list[str] = [
    "/settings/sales-groups",
    "/settings/assignment-rules",
    "/scheduled-reports",
]

# A schedule mails a rendered report to arbitrary addresses, so a leaked id is
# an exfiltration primitive rather than merely a config leak.
M8_SCHEDULE_ROUTES: list[tuple[str, str]] = [
    ("PATCH", "/scheduled-reports/{report_id}"),
    ("DELETE", "/scheduled-reports/{report_id}"),
    ("POST", "/scheduled-reports/{report_id}/run-now"),
]

M8_COLLECTION_WRITE_ROUTES: list[tuple[str, str]] = [
    ("POST", "/settings/sales-groups"),
    ("POST", "/settings/assignment-rules"),
    ("PATCH", "/settings/assignment-rules/reorder"),
    ("POST", "/settings/assignment-rules/preview"),
    ("POST", "/leads/distribute"),
    ("POST", "/scheduled-reports"),
]

_M8_BODIES: dict[str, dict[str, object]] = {
    "/settings/sales-groups/{group_id}": {"name": "Renamed By An Outsider"},
    "/settings/sales-groups": {"name": "Smuggled"},
    "/settings/assignment-rules/{rule_id}": {"name": "Renamed By An Outsider"},
    "/settings/assignment-rules": {
        "name": "Smuggled",
        "strategy": "UNASSIGNED",
        "config": {},
    },
    "/settings/assignment-rules/reorder": {"order": ["00000000-0000-4000-8000-000000000001"]},
    "/settings/assignment-rules/preview": {},
    "/leads/distribute": {
        "lead_ids": ["00000000-0000-4000-8000-000000000001"],
        "strategy": "UNASSIGNED",
        "config": {},
    },
    "/scheduled-reports/{report_id}": {"name": "Renamed By An Outsider"},
    "/scheduled-reports": {
        "name": "Smuggled",
        "report_type": "leads",
        "cron": "0 9 * * *",
        "recipients": ["outsider@example.com"],
    },
}

# M9 — reports and dashboards. Every one of these is a *lead read* wearing an
# aggregate's clothing, so a leak here is a leak of the numbers themselves —
# and a total is often all somebody wanted.
M9_COLLECTION_ROUTES: list[str] = [
    "/dashboard/follow-ups",
    "/dashboard/leads-by-stage",
    "/dashboard/filter-stats",
    "/reports/funnel",
    "/reports/activity",
    "/reports/leaderboard",
    "/dashboards",
    "/dashboards/widgets",
]

M9_ID_ROUTES: list[tuple[str, str]] = [
    ("GET", "/dashboards/{dashboard_id}"),
    ("PATCH", "/dashboards/{dashboard_id}"),
    ("DELETE", "/dashboards/{dashboard_id}"),
    ("PUT", "/dashboards/{dashboard_id}/default"),
]

_M9_BODIES: dict[str, dict[str, object]] = {
    "/dashboards/{dashboard_id}": {"name": "Renamed By An Outsider"},
    "/dashboards": {"name": "Smuggled", "layout": []},
}

# M10 — the integration surface. Every id here is a capability: a webhook id
# lets you redirect another workspace's events, an outbox id lets you replay
# them, and an API key id lets you revoke somebody else's live integration.
M10_ID_ROUTES: list[tuple[str, str]] = [
    ("DELETE", "/settings/api-keys/{key_id}"),
    ("PATCH", "/settings/webhooks/{endpoint_id}"),
    ("DELETE", "/settings/webhooks/{endpoint_id}"),
    ("POST", "/settings/webhooks/{endpoint_id}/test"),
    ("POST", "/settings/outbox/{event_id}/retry"),
]

M10_COLLECTION_ROUTES: list[str] = [
    "/settings/api-keys",
    "/settings/webhooks",
    "/settings/webhooks/events",
    "/settings/outbox",
    "/settings/intake-log",
]

# docs/14 — the wallet. Read-only for members; their isolation (404 for another
# workspace's wallet, no foreign figures in a statement) is asserted in
# `tests/test_wallet.py::test_another_workspaces_wallet_is_a_404_and_never_leaks`,
# where the wallets those checks need are set up.
WALLET_COLLECTION_ROUTES: list[str] = ["/wallet", "/wallet/transactions"]

M10_COLLECTION_WRITE_ROUTES: list[tuple[str, str]] = [
    ("POST", "/settings/api-keys"),
    ("POST", "/settings/webhooks"),
]

_M10_BODIES: dict[str, dict[str, object]] = {
    "/settings/webhooks/{endpoint_id}": {"name": "Renamed By An Outsider"},
    "/settings/webhooks": {
        "name": "Smuggled",
        "url": "https://attacker.example.com/hook",
        "permission_template_id": "00000000-0000-4000-8000-000000000001",
    },
    "/settings/api-keys": {
        "name": "Smuggled",
        "permission_template_id": "00000000-0000-4000-8000-000000000001",
    },
}

# Voice — the CRM side of the Bolna integration (docs/09-13). Not an M-numbered
# milestone, and missing from this matrix until the coverage guard below caught
# it. A call row holds a transcript of a customer talking, so a leaked call id
# is a leaked conversation. The probes live in `test_cross_workspace_voice.py`.
VOICE_ID_ROUTES: list[tuple[str, str]] = [
    ("GET", "/voice/context/{lead_id}"),
    ("PUT", "/voice/context/{lead_id}/summary"),
    # docs/15: approving a lead for AI calling decides who gets dialled.
    ("PUT", "/voice/approvals/{lead_id}"),
    # docs/16: fulfilling or cancelling another workspace's number request.
    ("POST", "/voice/number-requests/{request_id}/fulfil"),
    ("POST", "/voice/number-requests/{request_id}/cancel"),
    ("GET", "/voice/calls/{call_id}"),
    # docs/20: another workspace's call recording.
    ("GET", "/voice/calls/{call_id}/recording"),
    ("GET", "/voice/extraction-mappings/{mapping_id}"),
    ("PATCH", "/voice/extraction-mappings/{mapping_id}"),
    ("DELETE", "/voice/extraction-mappings/{mapping_id}"),
]

VOICE_COLLECTION_ROUTES: list[str] = [
    "/voice/calls",
    "/voice/extraction-mappings",
    "/voice/number-requests",
]

#: `POST /voice/calls` takes its lead by reference (an id or a phone number) in
#: the body, so it is probed both under B's path and with B's lead aimed at A.
VOICE_COLLECTION_WRITE_ROUTES: list[tuple[str, str]] = [
    ("POST", "/voice/calls"),
    # docs/15: what a call would send, built for one lead. Takes the lead by
    # reference exactly like placing a call, so it is probed the same way.
    ("POST", "/voice/calls/preview"),
    ("POST", "/voice/extraction-mappings"),
    # docs/16: setting another workspace's calling numbers.
    ("PUT", "/voice/calling-numbers"),
    # docs/16: requesting a number on another workspace's wallet.
    ("POST", "/voice/number-requests"),
]

#: docs/15: the variable catalogue — the workspace's own field names plus the
#: reserved ones. No ids to leak, but another workspace's field names would be.
VOICE_CATALOGUE_ROUTES: list[tuple[str, str]] = [
    ("GET", "/voice/variables"),
    # docs/16: which numbers this workspace calls from.
    ("GET", "/voice/calling-numbers"),
]

#: A lookup by natural key rather than id: B's phone number, asked of A.
VOICE_LOOKUP_ROUTES: list[tuple[str, str]] = [("GET", "/voice/context")]

#: The completion webhook. Authenticated by an API key rather than a member,
#: so its probes are a key from the wrong workspace and a payload naming
#: another workspace's execution or lead.
VOICE_MACHINE_ROUTES: list[tuple[str, str]] = [("POST", "/voice/executions")]

_VOICE_BODIES: dict[str, dict[str, object]] = {
    "/voice/approvals/{lead_id}": {"approved": True},
    "/voice/calling-numbers": {"numbers": ["+919000000999"]},
    "/voice/number-requests": {"region": "Smuggled"},
    "/voice/number-requests/{request_id}/fulfil": {"phone_number": "+919000000998"},
    "/voice/context/{lead_id}/summary": {
        "summary": "Smuggled by an outsider.",
        "external_id": "smuggled-1",
    },
    "/voice/extraction-mappings/{mapping_id}": {"min_confidence": 0.1},
    "/voice/extraction-mappings": {
        "disposition_name": "Smuggled",
        "target_field_key": "name",
    },
}

_M7_BODIES: dict[str, dict[str, object]] = {
    "/changesets/{changeset_id}/undo": {"skip_conflicts": True},
    "/changesets/{changeset_id}/preview-undo": {},
    "/leads/bulk": {
        "lead_ids": ["00000000-0000-4000-8000-000000000001"],
        "values": {"name": "Smuggled"},
    },
    "/tasks/{task_id}": {"title": "Renamed By An Outsider"},
    "/tasks": {"title": "Smuggled", "due_at": "2026-09-01T09:00:00Z"},
    "/labels/{label_id}": {"name": "Renamed By An Outsider"},
    "/labels": {"name": "Smuggled"},
    "/imports/{job_id}/mapping": {"mapping": {"Phone": "phone"}},
    "/leads/export": {},
    "/leads/merge": {
        "primary_id": "00000000-0000-4000-8000-000000000001",
        "merge_ids": ["00000000-0000-4000-8000-000000000002"],
    },
}


# --- 1. by direct id ---------------------------------------------------------


@pytest.mark.parametrize(("method", "template"), M1_MEMBERSHIP_ROUTES)
async def test_member_route_in_another_workspace_returns_404(
    api: AsyncClient,
    tenants: TenantPair,
    method: str,
    template: str,
) -> None:
    """A's admin, using A's workspace path, cannot address B's membership.

    This is the common case: the workspace id is right, the resource id belongs
    to someone else. `ScopedSession.get` returns None, and the service raises
    the same 404 it would for a uuid that never existed.
    """
    path = tenants.a.path(template.format(membership_id=tenants.b.members["rep"].membership.id))
    response = await api.request(
        method,
        path,
        headers=tenants.a.owner.auth,
        json=_BODIES.get(method),
    )

    assert response.status_code == 404, (
        f"{method} {template} leaked workspace B's membership: {response.status_code} "
        f"{response.text}"
    )
    assert response.json()["detail"]["code"] == "not_found"


@pytest.mark.parametrize(("method", "template"), M1_MEMBERSHIP_ROUTES)
async def test_member_route_under_foreign_workspace_path_returns_404(
    api: AsyncClient,
    tenants: TenantPair,
    method: str,
    template: str,
) -> None:
    """A's admin cannot address B's membership under B's own workspace path.

    Here the ids are mutually consistent — only the caller is wrong. The
    scoping dependency refuses at the membership check, before any handler
    runs, and reports 404 for the *workspace*, not 403.
    """
    path = tenants.b.path(template.format(membership_id=tenants.b.members["rep"].membership.id))
    response = await api.request(
        method,
        path,
        headers=tenants.a.owner.auth,
        json=_BODIES.get(method),
    )

    assert response.status_code == 404, (
        f"{method} {template} let a non-member into workspace B: {response.status_code} "
        f"{response.text}"
    )


async def test_workspace_detail_of_another_tenant_returns_404(
    api: AsyncClient,
    tenants: TenantPair,
) -> None:
    response = await api.get(tenants.b.path(), headers=tenants.a.owner.auth)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"


async def test_workspace_patch_of_another_tenant_returns_404(
    api: AsyncClient,
    tenants: TenantPair,
) -> None:
    response = await api.patch(
        tenants.b.path(),
        headers=tenants.a.owner.auth,
        json={"name": "Renamed by an outsider"},
    )
    assert response.status_code == 404


async def test_permissions_for_a_workspace_you_are_not_in_returns_404(
    api: AsyncClient,
    tenants: TenantPair,
) -> None:
    """`/me/permissions` is unscoped by path, so it needs its own check."""
    response = await api.get(
        "/api/v1/me/permissions",
        params={"workspace_id": str(tenants.b.id)},
        headers=tenants.a.owner.auth,
    )
    assert response.status_code == 404


async def test_nonexistent_and_foreign_ids_are_indistinguishable(
    api: AsyncClient,
    tenants: TenantPair,
) -> None:
    """The whole point of 404-not-403: the two responses must be identical.

    If they differ in status, code, or message, an attacker can enumerate which
    membership ids are real in workspaces they cannot read.
    """
    foreign = await api.get(
        tenants.a.path(f"/members/{tenants.b.members['rep'].membership.id}"),
        headers=tenants.a.owner.auth,
    )
    invented = await api.get(
        tenants.a.path(f"/members/{uuid.uuid4()}"),
        headers=tenants.a.owner.auth,
    )

    assert foreign.status_code == invented.status_code == 404
    assert foreign.json() == invented.json()


# --- 2. by list --------------------------------------------------------------


@pytest.mark.parametrize("route", M1_COLLECTION_ROUTES)
async def test_collections_never_contain_another_workspaces_ids(
    api: AsyncClient,
    tenants: TenantPair,
    route: str,
) -> None:
    """No id belonging to B may appear anywhere in a response A receives.

    Checked against the raw response text rather than a parsed field, so a leak
    through a field the assertion did not anticipate still fails the test.
    """
    response = await api.get(tenants.a.path(route), headers=tenants.a.owner.auth)
    assert response.status_code == 200

    body = response.text
    foreign_ids = [
        str(tenants.b.id),
        str(tenants.b.owner.membership.id),
        str(tenants.b.owner.user.id),
        *(str(actor.membership.id) for actor in tenants.b.members.values()),
        *(str(actor.user.id) for actor in tenants.b.members.values()),
        *(str(template.id) for template in tenants.b.templates.values()),
    ]
    for leaked in foreign_ids:
        assert leaked not in body, f"{route} leaked {leaked} from workspace B"


async def test_member_list_contains_only_own_workspace(
    api: AsyncClient,
    tenants: TenantPair,
) -> None:
    response = await api.get(tenants.a.path("/members"), headers=tenants.a.owner.auth)
    assert response.status_code == 200

    payload = response.json()
    assert payload["total"] == 3  # owner, manager, rep
    assert all(item["workspace_id"] == str(tenants.a.id) for item in payload["items"])


async def test_permission_templates_are_only_the_callers_workspaces(
    api: AsyncClient,
    tenants: TenantPair,
) -> None:
    """Both workspaces were provisioned with identically *named* templates.

    Names matching is expected — they are product roles. What must not happen
    is A receiving B's template *ids*, which would let A assign a member to a
    template belonging to another tenant.
    """
    response = await api.get(
        tenants.a.path("/settings/permission-templates"),
        headers=tenants.a.owner.auth,
    )
    assert response.status_code == 200

    returned = {item["id"] for item in response.json()}
    assert returned == {str(template.id) for template in tenants.a.templates.values()}
    assert returned.isdisjoint({str(t.id) for t in tenants.b.templates.values()})


async def test_workspace_list_shows_only_the_callers_workspaces(
    api: AsyncClient,
    tenants: TenantPair,
) -> None:
    response = await api.get("/api/v1/workspaces", headers=tenants.a.owner.auth)
    assert response.status_code == 200

    ids = {item["id"] for item in response.json()}
    assert ids == {str(tenants.a.id)}


async def test_me_exposes_only_the_callers_memberships(
    api: AsyncClient,
    tenants: TenantPair,
) -> None:
    response = await api.get("/api/v1/me", headers=tenants.a.owner.auth)
    assert response.status_code == 200

    workspace_ids = {m["workspace"]["id"] for m in response.json()["memberships"]}
    assert workspace_ids == {str(tenants.a.id)}


# --- 3. by filter / foreign reference ----------------------------------------


async def test_cannot_set_a_foreign_membership_as_manager(
    api: AsyncClient,
    tenants: TenantPair,
) -> None:
    """A foreign id passed as a *parameter*, not a path segment.

    The read is scoped, but nothing stops a caller sending B's membership id as
    `manager_id` unless the write validates it through the same scope. If this
    ever returns 200, A's hierarchy contains a pointer into B.
    """
    response = await api.patch(
        tenants.a.path(f"/members/{tenants.a.members['rep'].membership.id}"),
        headers=tenants.a.owner.auth,
        json={"manager_id": str(tenants.b.members["manager"].membership.id)},
    )
    assert response.status_code == 404


async def test_cannot_assign_a_foreign_permission_template(
    api: AsyncClient,
    tenants: TenantPair,
) -> None:
    response = await api.patch(
        tenants.a.path(f"/members/{tenants.a.members['rep'].membership.id}"),
        headers=tenants.a.owner.auth,
        json={"template_id": str(tenants.b.templates["Admin"].id)},
    )
    assert response.status_code == 404


async def test_bulk_upload_under_a_foreign_workspace_path_returns_404(
    api: AsyncClient,
    tenants: TenantPair,
) -> None:
    """A's admin cannot import members into workspace B.

    The highest-impact collection write in M1: a successful call would create
    real memberships inside another tenant. The workbook is valid and names one
    of B's own template names, so nothing but the scoping check stands between
    this request and a write.
    """
    response = await api.post(
        tenants.b.path("/members/bulk-upload"),
        headers=tenants.a.owner.auth,
        params={"dry_run": False},
        files={"file": ("members.xlsx", _members_workbook(), _XLSX_MIME)},
    )

    assert response.status_code == 404, (
        f"bulk upload wrote into workspace B: {response.status_code} {response.text}"
    )

    # Prove the refusal, not just the status code: B's roster is untouched.
    roster = await api.get(tenants.b.path("/members"), headers=tenants.b.owner.auth)
    assert roster.status_code == 200
    emails = {item["user"]["email"] for item in roster.json()["items"]}
    assert "smuggled@example.com" not in emails


async def test_bulk_upload_dry_run_into_another_workspace_returns_404(
    api: AsyncClient,
    tenants: TenantPair,
) -> None:
    """The dry run must refuse too.

    It writes nothing, but it *reads* — an accepted preview would report which
    of B's members already exist and which template names B uses.
    """
    response = await api.post(
        tenants.b.path("/members/bulk-upload"),
        headers=tenants.a.owner.auth,
        params={"dry_run": True},
        files={"file": ("members.xlsx", _members_workbook(), _XLSX_MIME)},
    )
    assert response.status_code == 404


async def test_cannot_invite_against_a_foreign_template(
    api: AsyncClient,
    tenants: TenantPair,
) -> None:
    response = await api.post(
        tenants.a.path("/members"),
        headers=tenants.a.owner.auth,
        json={
            "email": "newcomer@example.com",
            "full_name": "Newcomer",
            "template_id": str(tenants.b.templates["Caller"].id),
        },
    )
    assert response.status_code == 404


async def test_cannot_reassign_leads_to_a_foreign_membership(
    api: AsyncClient,
    tenants: TenantPair,
) -> None:
    """The reassignment target is the highest-value foreign reference here.

    In M1 nobody holds leads, so the call succeeds without consulting the
    target — the assertion is that it does *not* resolve B's membership. From
    M5, when the target is actually used, this same test proves a deactivation
    in A cannot hand a pipeline to a member of B.
    """
    response = await api.post(
        tenants.a.path(f"/members/{tenants.a.members['rep'].membership.id}/deactivate"),
        headers=tenants.a.owner.auth,
        json={"reassign_to_membership_id": str(tenants.b.members["manager"].membership.id)},
    )

    assert response.status_code in (200, 404)
    if response.status_code == 200:
        # No leads existed, so the target was never dereferenced. Prove the
        # membership in B is untouched rather than accepting the 200 blindly.
        check = await api.get(
            tenants.b.path(f"/members/{tenants.b.members['manager'].membership.id}"),
            headers=tenants.b.owner.auth,
        )
        assert check.status_code == 200
        assert check.json()["is_active"] is True


async def test_availability_log_of_a_foreign_member_returns_404_not_empty(
    api: AsyncClient,
    tenants: TenantPair,
) -> None:
    """An empty page would read as "that member has no history".

    404 is the only honest answer — the member is not addressable from here.
    """
    response = await api.get(
        tenants.a.path(f"/members/{tenants.b.members['rep'].membership.id}/availability-log"),
        headers=tenants.a.owner.auth,
    )
    assert response.status_code == 404


async def test_pagination_offset_cannot_walk_into_another_workspace(
    api: AsyncClient,
    tenants: TenantPair,
) -> None:
    """Paging past the end returns an empty page, not the next tenant's rows."""
    response = await api.get(
        tenants.a.path("/members"),
        headers=tenants.a.owner.auth,
        params={"limit": 100, "offset": 3},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["items"] == []
    assert payload["total"] == 3


# --- 4. the matrix stays complete -------------------------------------------


# Call facts (docs/17). A fact set decides what the voice agent tells a lead,
# so a leaked id would let one workspace put words in another's calls. The
# probes live in `test_cross_workspace_call_facts.py`.
CALL_FACT_ID_ROUTES: list[tuple[str, str]] = [
    ("PATCH", "/settings/call-facts/{fact_set_id}"),
    ("DELETE", "/settings/call-facts/{fact_set_id}"),
]
CALL_FACT_COLLECTION_ROUTES: list[str] = ["/settings/call-facts"]
CALL_FACT_COLLECTION_WRITE_ROUTES: list[tuple[str, str]] = [("POST", "/settings/call-facts")]


# Connected apps and workflows (docs/18). A workflow changes leads with the
# workspace's own authority and an app's event log holds customers' payments,
# so a leaked id would let one workspace read or rewire another's money flow.
# The probes live in `test_cross_workspace_automation.py`.
WORKFLOW_ID_ROUTES: list[tuple[str, str]] = [
    ("GET", "/workflows/{workflow_id}"),
    ("PATCH", "/workflows/{workflow_id}"),
    ("DELETE", "/workflows/{workflow_id}"),
    ("POST", "/workflows/{workflow_id}/test"),
    ("GET", "/workflows/{workflow_id}/runs"),
]
AUTOMATION_COLLECTION_ROUTES: list[str] = [
    "/workflows",
    "/apps",
    "/apps/{provider}/events",
    "/automation/catalogue",
    "/settings/voice-never-call",
]
AUTOMATION_COLLECTION_WRITE_ROUTES: list[tuple[str, str]] = [
    ("POST", "/workflows"),
    ("POST", "/apps/{provider}/connect"),
    ("PATCH", "/apps/{provider}"),
    ("POST", "/apps/{provider}/rotate-url"),
    ("PUT", "/settings/voice-never-call"),
]


def test_the_matrix_covers_every_workspace_scoped_route(app: FastAPI) -> None:
    """Every tenant-scoped operation appears in one of the route lists above.

    The suite's promise is "for every endpoint that exists", and a promise
    checked by hand is a promise that quietly lapses — `POST /members/bulk-upload`
    was added in M1 and missed by exactly that failure.

    So the claim is derived from the application's own OpenAPI schema rather
    than asserted. A milestone that mounts a new tenant route fails here until
    it is listed, which is the point: the reminder arrives with the route, not
    after an audit.
    """
    declared: set[tuple[str, str]] = set()
    declared.update(CALL_FACT_ID_ROUTES)
    declared.update(("GET", route) for route in CALL_FACT_COLLECTION_ROUTES)
    declared.update(CALL_FACT_COLLECTION_WRITE_ROUTES)
    declared.update(WORKFLOW_ID_ROUTES)
    declared.update(("GET", route) for route in AUTOMATION_COLLECTION_ROUTES)
    declared.update(AUTOMATION_COLLECTION_WRITE_ROUTES)
    declared.update(M1_MEMBERSHIP_ROUTES)
    declared.update(("GET", route) for route in M1_COLLECTION_ROUTES)
    declared.update(M1_COLLECTION_WRITE_ROUTES)
    declared.update(M1_WORKSPACE_ROOT_ROUTES)
    declared.update(M2_FIELD_ROUTES)
    declared.update(("GET", route) for route in M2_COLLECTION_ROUTES)
    declared.update(M2_COLLECTION_WRITE_ROUTES)
    declared.update(M2_TWO_ID_ROUTES)
    declared.update((method, route) for method, route, _ in M3_ID_ROUTES)
    declared.update(("GET", route) for route in M3_COLLECTION_ROUTES)
    declared.update(M3_COLLECTION_WRITE_ROUTES)
    declared.update(M3_ACTION_ROUTES)
    declared.update(M4_TEMPLATE_ROUTES)
    declared.update(("GET", route) for route in M4_COLLECTION_ROUTES)
    declared.update(M4_COLLECTION_WRITE_ROUTES)
    declared.update(M5_LEAD_ROUTES)
    declared.update(("GET", route) for route in M5_COLLECTION_ROUTES)
    declared.update(M5_COLLECTION_WRITE_ROUTES)
    declared.update(M5_TEMPLATE_ROUTES)
    declared.update(M6_FILTER_ROUTES)
    declared.update(("GET", route) for route in M6_COLLECTION_ROUTES)
    declared.update(M6_COLLECTION_WRITE_ROUTES)
    declared.update(M7_CHANGESET_ROUTES)
    declared.update(M7_COLLECTION_WRITE_ROUTES)
    declared.update(M7_TASK_ROUTES)
    declared.update(M7_LABEL_ROUTES)
    declared.update(M7_JOB_ROUTES)
    declared.update(M7_EXPORT_ROUTES)
    declared.update({("GET", "/leads/duplicates")})
    declared.update(M7_LEAD_LABEL_ROUTES)
    declared.update(("GET", route) for route in M7_LEAD_CHILD_ROUTES)
    declared.update(("GET", route) for route in M7_COLLECTION_ROUTES)
    declared.update({("POST", "/tasks"), ("POST", "/labels"), ("POST", "/imports")})
    declared.update(M8_GROUP_ROUTES)
    declared.update(M8_RULE_ROUTES)
    declared.update(("GET", route) for route in M8_COLLECTION_ROUTES)
    declared.update(M8_COLLECTION_WRITE_ROUTES)
    declared.update(M8_SCHEDULE_ROUTES)
    declared.update({("GET", "/recurring-dates/occurrences")})
    declared.update(M10_ID_ROUTES)
    declared.update(("GET", route) for route in M10_COLLECTION_ROUTES)
    declared.update(M10_COLLECTION_WRITE_ROUTES)
    declared.update(M9_ID_ROUTES)
    declared.update(("GET", route) for route in M9_COLLECTION_ROUTES)
    declared.update({("GET", "/reports/breakdown"), ("POST", "/dashboards")})
    declared.update(VOICE_ID_ROUTES)
    declared.update(("GET", route) for route in VOICE_COLLECTION_ROUTES)
    declared.update(VOICE_COLLECTION_WRITE_ROUTES)
    declared.update(VOICE_LOOKUP_ROUTES)
    declared.update(VOICE_MACHINE_ROUTES)
    declared.update(VOICE_CATALOGUE_ROUTES)
    declared.update(("GET", route) for route in WALLET_COLLECTION_ROUTES)

    mounted: set[tuple[str, str]] = set()
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith(_TENANT_PREFIX):
            continue
        suffix = path[len(_TENANT_PREFIX) :]
        mounted.update((method.upper(), suffix) for method in operations)

    assert mounted, "No tenant-scoped routes found — has the mount prefix changed?"

    uncovered = mounted - declared
    assert not uncovered, (
        "Tenant-scoped routes missing from the isolation matrix: "
        f"{sorted(uncovered)}. Add them to the route lists in this module."
    )

    # Also catch the reverse: a list entry for a route that no longer exists,
    # which would leave a test passing against nothing.
    stale = declared - mounted
    assert not stale, f"Isolation matrix lists routes that are not mounted: {sorted(stale)}"


# --- 5. M2: the field definition engine --------------------------------------


@pytest.mark.parametrize(("method", "template"), M2_FIELD_ROUTES)
async def test_field_route_in_another_workspace_returns_404(
    api: AsyncClient,
    tenants: TenantPair,
    method: str,
    template: str,
) -> None:
    """A's admin, on A's own path, cannot address B's field by id.

    Every workspace is provisioned with a `phone` field, so B always has one to
    aim at — and its id is a real uuid, not a fabricated one, which is what
    makes this a genuine probe rather than a 404-for-nonexistence.
    """
    path = tenants.a.path(template.format(field_id=tenants.b.builtin_field_id))
    response = await api.request(
        method,
        path,
        headers=tenants.a.owner.auth,
        json=_FIELD_BODIES.get(template),
    )

    assert response.status_code == 404, (
        f"{method} {template} reached workspace B's field: {response.status_code} {response.text}"
    )
    assert response.json()["detail"]["code"] == "not_found"


@pytest.mark.parametrize(("method", "template"), M2_FIELD_ROUTES)
async def test_field_route_under_foreign_workspace_path_returns_404(
    api: AsyncClient,
    tenants: TenantPair,
    method: str,
    template: str,
) -> None:
    """Consistent ids, wrong caller: refused by the scoping dependency."""
    path = tenants.b.path(template.format(field_id=tenants.b.builtin_field_id))
    response = await api.request(
        method,
        path,
        headers=tenants.a.owner.auth,
        json=_FIELD_BODIES.get(template),
    )
    assert response.status_code == 404, (
        f"{method} {template} let a non-member into workspace B: {response.text}"
    )


@pytest.mark.parametrize("route", M2_COLLECTION_ROUTES)
async def test_settings_collections_never_contain_another_workspaces_ids(
    api: AsyncClient,
    tenants: TenantPair,
    route: str,
) -> None:
    """Checked against the raw response text, so a leak through an
    unanticipated field still fails."""
    response = await api.get(tenants.a.path(route), headers=tenants.a.owner.auth)
    assert response.status_code == 200, response.text

    if route in M2_TENANT_FREE_ROUTES:
        # A product constant: identical for every workspace, nothing to leak.
        return

    body = response.text
    assert str(tenants.b.id) not in body
    assert str(tenants.b.builtin_field_id) not in body


@pytest.mark.parametrize(("method", "route"), M2_COLLECTION_WRITE_ROUTES)
async def test_settings_writes_under_a_foreign_workspace_path_return_404(
    api: AsyncClient,
    tenants: TenantPair,
    method: str,
    route: str,
) -> None:
    """A's admin cannot create schema inside workspace B."""
    bodies: dict[str, dict[str, object]] = {
        "/settings/lead-fields": {"label": "Smuggled Field", "field_type": "TEXT"},
        "/settings/indexed-fields": {"field_id": str(tenants.b.builtin_field_id)},
        "/settings/identity-field": {"field_id": str(tenants.b.builtin_field_id)},
        "/settings/primary-fields": {"h1_field_id": str(tenants.b.builtin_field_id)},
    }
    response = await api.request(
        method, tenants.b.path(route), headers=tenants.a.owner.auth, json=bodies[route]
    )
    assert response.status_code == 404, response.text


async def test_a_foreign_field_cannot_become_the_identity_field(
    api: AsyncClient,
    tenants: TenantPair,
) -> None:
    """The by-reference leak: the read is scoped but the foreign key might not
    be. A's workspace must not be able to point its identity at B's field."""
    response = await api.put(
        tenants.a.path("/settings/identity-field"),
        headers=tenants.a.owner.auth,
        json={"field_id": str(tenants.b.builtin_field_id)},
    )
    assert response.status_code == 404


async def test_a_foreign_field_cannot_become_a_primary_field(
    api: AsyncClient,
    tenants: TenantPair,
) -> None:
    response = await api.put(
        tenants.a.path("/settings/primary-fields"),
        headers=tenants.a.owner.auth,
        json={"h1_field_id": str(tenants.b.builtin_field_id)},
    )
    assert response.status_code == 404


async def test_options_cannot_be_copied_across_a_workspace_boundary(
    api: AsyncClient,
    tenants: TenantPair,
) -> None:
    """ "Copy options" reads a whole option set — the highest-value read in the
    settings surface to get wrong."""
    own = await api.post(
        tenants.a.path("/settings/lead-fields"),
        headers=tenants.a.owner.auth,
        json={"label": "Target", "field_type": "DROPDOWN"},
    )
    assert own.status_code == 201
    target_id = own.json()["id"]

    response = await api.post(
        tenants.a.path(
            f"/settings/lead-fields/{target_id}/options/copy-from/{tenants.b.builtin_field_id}"
        ),
        headers=tenants.a.owner.auth,
    )
    assert response.status_code == 404


async def test_a_foreign_option_cannot_be_edited_through_an_owned_field(
    api: AsyncClient,
    tenants: TenantPair,
) -> None:
    """Both ids are checked, not just the one in the outer path segment."""
    field = await api.post(
        tenants.a.path("/settings/lead-fields"),
        headers=tenants.a.owner.auth,
        json={"label": "Mine", "field_type": "DROPDOWN"},
    )
    field_id = field.json()["id"]

    foreign_field = await api.post(
        tenants.b.path("/settings/lead-fields"),
        headers=tenants.b.owner.auth,
        json={"label": "Theirs", "field_type": "DROPDOWN"},
    )
    foreign_option = await api.post(
        tenants.b.path(f"/settings/lead-fields/{foreign_field.json()['id']}/options"),
        headers=tenants.b.owner.auth,
        json={"label": "Theirs Only"},
    )
    assert foreign_option.status_code == 201

    response = await api.patch(
        tenants.a.path(f"/settings/lead-fields/{field_id}/options/{foreign_option.json()['id']}"),
        headers=tenants.a.owner.auth,
        json={"label": "Renamed By An Outsider"},
    )
    assert response.status_code == 404
