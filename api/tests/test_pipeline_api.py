"""Pipeline, taxonomy and preferences (M3).

The rules under test are the ones the source system enforces and the ones
CLAUDE.md flags as easy to get wrong: stage cardinality, the 25-reason cap, the
single default disposition, the system/custom tier, and feature flags that gate
endpoints rather than menus.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import WorkspaceFixture, add_member, build_workspace, login

from app.auth.passwords import PasswordHasherService

pytestmark = pytest.mark.integration


@pytest.fixture
def hasher(wired_app: FastAPI) -> PasswordHasherService:
    hasher = wired_app.state.password_hasher
    assert isinstance(hasher, PasswordHasherService)
    return hasher


@pytest.fixture
async def workspace(db_session: AsyncSession, hasher: PasswordHasherService) -> WorkspaceFixture:
    fixture = await build_workspace(
        db_session, hasher, name="Acme", owner_email="owner@example.com"
    )
    await add_member(
        db_session, hasher, fixture, key="rep", email="rep@example.com", template_name="Caller"
    )
    return fixture


async def _admin(api: AsyncClient, workspace: WorkspaceFixture) -> dict[str, str]:
    await login(api, workspace.owner)
    return workspace.owner.auth


# --- provisioning ------------------------------------------------------------


async def test_a_new_workspace_gets_the_documented_pipeline(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """§7: 4 stages — one initial, one active, one won, one lost."""
    headers = await _admin(api, workspace)
    body = (await api.get(workspace.path("/settings/stages"), headers=headers)).json()

    assert body["initial"]["label"] == "New"
    assert [s["label"] for s in body["active"]] == ["Contacted"]
    assert body["won"]["label"] == "Won"
    assert body["lost"]["label"] == "Lost"
    assert body["archived"] == []


async def test_a_new_workspace_gets_five_lost_reasons_and_seven_dispositions(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)

    reasons = (await api.get(workspace.path("/settings/lost-reasons"), headers=headers)).json()
    assert len(reasons) == 5
    assert sum(1 for r in reasons if r["is_default"]) == 1

    dispositions = (
        await api.get(workspace.path("/settings/call-dispositions"), headers=headers)
    ).json()
    assert len(dispositions) == 7
    assert all(d["is_system"] for d in dispositions)
    assert sum(1 for d in dispositions if d["is_default"]) == 1


async def test_provisioned_taxonomy_names_no_industry(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """The #1 trap in CLAUDE.md. A new workspace gets structure, not a process."""
    headers = await _admin(api, workspace)
    labels = set()
    for route in ("/settings/lost-reasons", "/settings/call-dispositions"):
        labels |= {
            item["label"].lower()
            for item in (await api.get(workspace.path(route), headers=headers)).json()
        }
    stages = (await api.get(workspace.path("/settings/stages"), headers=headers)).json()
    labels |= {s["label"].lower() for s in stages["active"]}

    forbidden = {
        "forge writing",
        "interview scheduled",
        "admission",
        "course",
        "batch",
        "enrolled",
        "demo booked",
    }
    assert not (forbidden & labels)


# --- stage cardinality -------------------------------------------------------


async def test_only_active_stages_can_be_created(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """`kind` is not offered, so a second WON cannot even be requested."""
    headers = await _admin(api, workspace)
    created = await api.post(
        workspace.path("/settings/stages"),
        headers=headers,
        json={"label": "Qualified", "color": "#22c55e", "kind": "WON"},
    )
    assert created.status_code == 201
    assert created.json()["kind"] == "ACTIVE", "the ignored `kind` must not take effect"


async def test_the_singleton_stages_cannot_be_archived(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """A pipeline with no won state is not a pipeline."""
    headers = await _admin(api, workspace)
    stages = (await api.get(workspace.path("/settings/stages"), headers=headers)).json()

    for key in ("initial", "won", "lost"):
        response = await api.delete(
            workspace.path(f"/settings/stages/{stages[key]['id']}"), headers=headers
        )
        assert response.status_code == 409, key
        assert response.json()["detail"]["code"] == "stage_cardinality"


async def test_an_active_stage_archives_rather_than_deletes(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)
    stages = (await api.get(workspace.path("/settings/stages"), headers=headers)).json()
    active = stages["active"][0]

    archived = await api.delete(workspace.path(f"/settings/stages/{active['id']}"), headers=headers)
    assert archived.status_code == 200

    after = (await api.get(workspace.path("/settings/stages"), headers=headers)).json()
    assert active["id"] not in {s["id"] for s in after["active"]}
    assert active["id"] in {s["id"] for s in after["archived"]}


async def test_a_singleton_stage_can_be_renamed(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """Renaming is how a workspace turns "Won" into its own vocabulary — the
    structure is fixed, the label never is."""
    headers = await _admin(api, workspace)
    stages = (await api.get(workspace.path("/settings/stages"), headers=headers)).json()

    renamed = await api.patch(
        workspace.path(f"/settings/stages/{stages['won']['id']}"),
        headers=headers,
        json={"label": "Signed Up"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["label"] == "Signed Up"
    assert renamed.json()["kind"] == "WON"


async def test_a_stage_label_is_capped_at_28_characters(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """§2.2 — the edit dialog's live counter stops there."""
    headers = await _admin(api, workspace)
    response = await api.post(
        workspace.path("/settings/stages"), headers=headers, json={"label": "x" * 29}
    )
    assert response.status_code == 422


async def test_only_active_stages_can_be_reordered(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)
    stages = (await api.get(workspace.path("/settings/stages"), headers=headers)).json()
    response = await api.patch(
        workspace.path("/settings/stages/reorder"),
        headers=headers,
        json={"ordered_ids": [stages["won"]["id"]]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "stage_not_reorderable"


# --- lost reasons ------------------------------------------------------------


async def test_the_twenty_sixth_lost_reason_is_refused(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """§2.1 — a clear 409, not a constraint violation."""
    headers = await _admin(api, workspace)
    # Five are provisioned; twenty more reach the cap.
    for n in range(20):
        created = await api.post(
            workspace.path("/settings/lost-reasons"),
            headers=headers,
            json={"label": f"Reason {n}"},
        )
        assert created.status_code == 201, created.text

    refused = await api.post(
        workspace.path("/settings/lost-reasons"), headers=headers, json={"label": "One too many"}
    )
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["code"] == "lost_reason_limit"
    assert detail["limit"] == 25


async def test_archiving_a_lost_reason_frees_a_slot(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)
    reasons = (await api.get(workspace.path("/settings/lost-reasons"), headers=headers)).json()

    archived = await api.delete(
        workspace.path(f"/settings/lost-reasons/{reasons[0]['id']}"), headers=headers
    )
    assert archived.status_code == 200
    assert archived.json()["is_archived"] is True

    live = (await api.get(workspace.path("/settings/lost-reasons"), headers=headers)).json()
    assert len(live) == 4

    with_archived = (
        await api.get(
            workspace.path("/settings/lost-reasons"),
            headers=headers,
            params={"include_archived": True},
        )
    ).json()
    assert len(with_archived) == 5


# --- call dispositions -------------------------------------------------------


async def test_a_system_disposition_cannot_be_renamed(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """Observed verbatim in the source system: "can't edit system generated"."""
    headers = await _admin(api, workspace)
    dispositions = (
        await api.get(workspace.path("/settings/call-dispositions"), headers=headers)
    ).json()

    response = await api.patch(
        workspace.path(f"/settings/call-dispositions/{dispositions[0]['id']}"),
        headers=headers,
        json={"label": "Renamed"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "system_disposition"


async def test_a_system_disposition_can_be_archived(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """§3 lists Archive as available on system entries — a workspace hides what
    it does not use rather than being stuck with it."""
    headers = await _admin(api, workspace)
    dispositions = (
        await api.get(workspace.path("/settings/call-dispositions"), headers=headers)
    ).json()
    non_default = next(d for d in dispositions if not d["is_default"])

    response = await api.post(
        workspace.path(f"/settings/call-dispositions/{non_default['id']}/archive"),
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["is_archived"] is True


async def test_exactly_one_disposition_is_default_at_a_time(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)
    dispositions = (
        await api.get(workspace.path("/settings/call-dispositions"), headers=headers)
    ).json()
    original = next(d for d in dispositions if d["is_default"])
    other = next(d for d in dispositions if not d["is_default"])

    promoted = await api.post(
        workspace.path(f"/settings/call-dispositions/{other['id']}/set-default"), headers=headers
    )
    assert promoted.status_code == 200

    after = (await api.get(workspace.path("/settings/call-dispositions"), headers=headers)).json()
    defaults = [d["id"] for d in after if d["is_default"]]
    assert defaults == [other["id"]]
    assert original["id"] not in defaults


async def test_the_default_disposition_cannot_be_archived(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """A workspace with no default has nothing to preselect on the call form."""
    headers = await _admin(api, workspace)
    dispositions = (
        await api.get(workspace.path("/settings/call-dispositions"), headers=headers)
    ).json()
    default = next(d for d in dispositions if d["is_default"])

    response = await api.post(
        workspace.path(f"/settings/call-dispositions/{default['id']}/archive"), headers=headers
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "default_disposition"


async def test_a_custom_disposition_is_fully_editable(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)
    created = await api.post(
        workspace.path("/settings/call-dispositions"),
        headers=headers,
        json={"label": "Left Voicemail"},
    )
    assert created.status_code == 201
    assert created.json()["is_system"] is False

    renamed = await api.patch(
        workspace.path(f"/settings/call-dispositions/{created.json()['id']}"),
        headers=headers,
        json={"label": "Voicemail"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["label"] == "Voicemail"


# --- custom actions ----------------------------------------------------------


async def test_custom_action_codes_are_workspace_sequential_from_1001(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """§4.1."""
    headers = await _admin(api, workspace)
    codes = []
    for n in range(3):
        created = await api.post(
            workspace.path("/settings/custom-actions"),
            headers=headers,
            json={"name": f"Action {n}", "score": 10, "direction": "OUTBOUND"},
        )
        assert created.status_code == 201, created.text
        codes.append(created.json()["code"])

    assert codes == [1001, 1002, 1003]


async def test_every_custom_action_starts_with_a_required_notes_field(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """§4.2 — "Every action starts with one field: Notes (Text, required)"."""
    headers = await _admin(api, workspace)
    created = await api.post(
        workspace.path("/settings/custom-actions"), headers=headers, json={"name": "Site Visit"}
    )
    fields = created.json()["fields"]
    assert len(fields) == 1
    assert fields[0]["label"] == "Notes"
    assert fields[0]["field_type"] == "TEXT"
    assert fields[0]["is_required"] is True


async def test_the_score_range_is_enforced(api: AsyncClient, workspace: WorkspaceFixture) -> None:
    """§4.2 — min -1000, max 1000."""
    headers = await _admin(api, workspace)
    for score in (-1001, 1001):
        response = await api.post(
            workspace.path("/settings/custom-actions"),
            headers=headers,
            json={"name": "Out of range", "score": score},
        )
        assert response.status_code == 422

    ok = await api.post(
        workspace.path("/settings/custom-actions"),
        headers=headers,
        json={"name": "At the edge", "score": 1000},
    )
    assert ok.status_code == 201


async def test_an_action_field_reuses_the_m2_builder(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """Same slug derivation and the same tables — one field engine, two
    registries (§4.3)."""
    headers = await _admin(api, workspace)
    action = (
        await api.post(
            workspace.path("/settings/custom-actions"), headers=headers, json={"name": "Meeting"}
        )
    ).json()

    field = await api.post(
        workspace.path(f"/settings/custom-actions/{action['id']}/fields"),
        headers=headers,
        json={
            "label": "Outcome Summary",
            "field_type": "DROPDOWN",
            "options": ["Positive", "Neutral"],
        },
    )
    assert field.status_code == 201
    body = field.json()
    assert body["key"] == "outcome_summary"
    assert {o["label"] for o in body["options"]} == {"Positive", "Neutral"}


async def test_an_action_field_rejects_a_lead_only_type(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """The action registry is a different, smaller set — MONEY is not in it."""
    headers = await _admin(api, workspace)
    action = (
        await api.post(
            workspace.path("/settings/custom-actions"), headers=headers, json={"name": "Deal"}
        )
    ).json()

    response = await api.post(
        workspace.path(f"/settings/custom-actions/{action['id']}/fields"),
        headers=headers,
        json={"label": "Amount", "field_type": "MONEY"},
    )
    assert response.status_code == 422


# --- feature flags -----------------------------------------------------------


async def test_a_disabled_feature_refuses_at_the_endpoint(
    api: AsyncClient, workspace: WorkspaceFixture, make_operator: Callable[..., None]
) -> None:
    """CLAUDE.md: "A disabled feature returns 403, it doesn't merely hide a menu
    item." This is the test that proves the flag is a boundary, not decoration.
    """
    headers = await _admin(api, workspace)
    make_operator(workspace.owner.user.email)

    enabled = await api.get(workspace.path("/settings/custom-actions"), headers=headers)
    assert enabled.status_code == 200

    turned_off = await api.patch(
        workspace.path("/settings/preferences"),
        headers=headers,
        json={"features": {"custom_actions": False}},
    )
    assert turned_off.status_code == 200

    for method, path, body in (
        ("GET", "/settings/custom-actions", None),
        ("POST", "/settings/custom-actions", {"name": "Blocked"}),
    ):
        response = await api.request(method, workspace.path(path), headers=headers, json=body)
        assert response.status_code == 403, f"{method} {path}"
        assert response.json()["detail"]["code"] == "feature_disabled"


async def test_an_unknown_feature_flag_is_rejected(
    api: AsyncClient, workspace: WorkspaceFixture, make_operator: Callable[..., None]
) -> None:
    headers = await _admin(api, workspace)
    make_operator(workspace.owner.user.email)
    response = await api.patch(
        workspace.path("/settings/preferences"),
        headers=headers,
        json={"features": {"teleportation": True}},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unknown_feature"


# --- preferences -------------------------------------------------------------


async def test_preferences_are_the_localisation_seam(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """§5: "A US customer must work without a code change"."""
    headers = await _admin(api, workspace)
    response = await api.patch(
        workspace.path("/settings/preferences"),
        headers=headers,
        json={
            "default_country_code": "+1",
            "timezone": "America/Chicago",
            "currency": "usd",
            "connected_call_min_seconds": 30,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["default_country_code"] == "1"
    assert body["timezone"] == "America/Chicago"
    assert body["currency"] == "USD"
    assert body["connected_call_min_seconds"] == 30


async def test_an_unknown_timezone_is_refused(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """The scheduler evaluates cron in this zone from M8."""
    headers = await _admin(api, workspace)
    response = await api.patch(
        workspace.path("/settings/preferences"),
        headers=headers,
        json={"timezone": "Mars/Olympus_Mons"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unknown_timezone"


async def test_a_rep_cannot_change_the_taxonomy(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    rep = workspace.members["rep"]
    await login(api, rep)

    assert (await api.get(workspace.path("/settings/stages"), headers=rep.auth)).status_code == 200

    for method, path, body in (
        ("POST", "/settings/stages", {"label": "Sneaky"}),
        ("POST", "/settings/lost-reasons", {"label": "Sneaky"}),
        ("POST", "/settings/call-dispositions", {"label": "Sneaky"}),
        ("PATCH", "/settings/preferences", {"currency": "EUR"}),
    ):
        response = await api.request(method, workspace.path(path), headers=rep.auth, json=body)
        assert response.status_code == 403, f"{method} {path}"
