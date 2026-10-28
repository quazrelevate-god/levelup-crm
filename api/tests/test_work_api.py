"""Tasks, labels and spreadsheet imports (M7).

The import half carries the weight here. `04-feature-coverage.md` found four
distinct flows behind "upload an Excel" and marked three of them missing:

- **Excel Advance Distribution** — share an imported batch across reps
- **Owner Specific Assignment** — the owner comes from a column in the sheet
- **Excel Bulk Update** — update-by-identity, distinct from create
- **Importing existing Actions** — historical timeline migration

Each has a test below, plus the dry-run preview the audit calls the answer to
"Pitfalls of Excel Upload", and the property that makes a bad import survivable:
one changeset per run, so it undoes as a unit.
"""

from __future__ import annotations

import datetime as dt
import io
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from openpyxl import Workbook
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import WorkspaceFixture, add_member, build_workspace, login

from app.auth.passwords import PasswordHasherService

pytestmark = pytest.mark.integration

_CSV = "text/csv"


@pytest.fixture
def hasher(wired_app: FastAPI) -> PasswordHasherService:
    hasher = wired_app.state.password_hasher
    assert isinstance(hasher, PasswordHasherService)
    return hasher


@pytest.fixture
def storage(wired_app: FastAPI) -> None:
    """An in-memory stand-in for S3.

    The import flow is four requests, so the uploaded sheet has to survive
    between them. What matters to these tests is that the *same bytes* the
    preview read are the ones the commit reads — not which object store holds
    them.
    """

    class _Bucket:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}

        def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> None:  # noqa: N803
            self.objects[Key] = Body

        def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
            return {"Body": io.BytesIO(self.objects[Key])}

    wired_app.state.s3 = _Bucket()


@pytest.fixture
async def workspace(
    db_session: AsyncSession, hasher: PasswordHasherService, storage: None
) -> WorkspaceFixture:
    fixture = await build_workspace(
        db_session, hasher, name="Work", owner_email="work-owner@example.com"
    )
    await add_member(
        db_session,
        hasher,
        fixture,
        key="rep",
        email="work-rep@example.com",
        template_name="Caller",
    )
    await add_member(
        db_session,
        hasher,
        fixture,
        key="rep_two",
        email="work-rep-two@example.com",
        template_name="Caller",
    )
    await db_session.commit()
    return fixture


async def _admin(api: AsyncClient, workspace: WorkspaceFixture) -> dict[str, str]:
    await login(api, workspace.owner)
    return workspace.owner.auth


async def _field(
    api: AsyncClient, ws: WorkspaceFixture, headers: dict[str, str], **payload: Any
) -> dict:
    response = await api.post(ws.path("/settings/lead-fields"), headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def _lead(
    api: AsyncClient, ws: WorkspaceFixture, headers: dict[str, str], **values: Any
) -> dict:
    response = await api.post(ws.path("/leads"), headers=headers, json={"values": values})
    assert response.status_code == 201, response.text
    return response.json()


def _csv(rows: list[list[str]]) -> bytes:
    return "\n".join(",".join(cell for cell in row) for row in rows).encode()


async def _upload(
    api: AsyncClient,
    ws: WorkspaceFixture,
    headers: dict[str, str],
    content: bytes,
    *,
    kind: str = "LEAD_IMPORT",
    filename: str = "leads.csv",
) -> dict:
    response = await api.post(
        ws.path("/imports"),
        headers=headers,
        params={"kind": kind},
        files={"file": (filename, content, _CSV)},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _run(
    api: AsyncClient,
    ws: WorkspaceFixture,
    headers: dict[str, str],
    job_id: str,
    mapping: dict[str, str],
    options: dict[str, Any] | None = None,
    *,
    commit: bool = True,
) -> dict:
    mapped = await api.put(
        ws.path(f"/imports/{job_id}/mapping"),
        headers=headers,
        json={"mapping": mapping, "options": options or {}},
    )
    assert mapped.status_code == 200, mapped.text

    previewed = await api.post(ws.path(f"/imports/{job_id}/preview"), headers=headers)
    assert previewed.status_code == 200, previewed.text
    if not commit:
        return previewed.json()

    committed = await api.post(ws.path(f"/imports/{job_id}/commit"), headers=headers)
    assert committed.status_code == 200, committed.text
    return committed.json()


# --- tasks -------------------------------------------------------------------


async def test_task_buckets_are_computed_not_stored(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """`late` means "due before now and not done", and now moves.

    Storing the bucket would need a nightly job and would be wrong every
    morning until it ran.
    """
    headers = await _admin(api, workspace)
    now = dt.datetime.now(dt.UTC)

    overdue = await api.post(
        workspace.path("/tasks"),
        headers=headers,
        json={"title": "Chase", "due_at": (now - dt.timedelta(days=1)).isoformat()},
    )
    assert overdue.status_code == 201, overdue.text
    soon = await api.post(
        workspace.path("/tasks"),
        headers=headers,
        json={"title": "Call back", "due_at": (now + dt.timedelta(days=1)).isoformat()},
    )
    assert soon.status_code == 201, soon.text

    late = (
        await api.get(workspace.path("/tasks"), headers=headers, params={"bucket": "late"})
    ).json()
    upcoming = (
        await api.get(workspace.path("/tasks"), headers=headers, params={"bucket": "upcoming"})
    ).json()

    assert [t["title"] for t in late["items"]] == ["Chase"]
    assert [t["title"] for t in upcoming["items"]] == ["Call back"]

    counts = (await api.get(workspace.path("/tasks/counts"), headers=headers)).json()
    assert counts == {"upcoming": 1, "late": 1, "done": 0}


async def test_completing_a_task_moves_it_to_done_and_writes_the_timeline(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """ "Someone promised to call back on Thursday" belongs in the audit trail
    as much as the call itself does (rule 5)."""
    headers = await _admin(api, workspace)
    lead = await _lead(api, workspace, headers, name="Promised", phone="9930000001")

    created = await api.post(
        workspace.path("/tasks"),
        headers=headers,
        json={
            "title": "Send the fee structure",
            "due_at": (dt.datetime.now(dt.UTC) + dt.timedelta(days=2)).isoformat(),
            "lead_id": lead["id"],
        },
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]

    timeline = (
        await api.get(workspace.path(f"/leads/{lead['id']}/actions"), headers=headers)
    ).json()
    assert any(a["kind"] == "TASK_CREATED" for a in timeline["items"])

    done = await api.post(workspace.path(f"/tasks/{task_id}/complete"), headers=headers)
    assert done.status_code == 200, done.text
    assert done.json()["completed_at"] is not None

    counts = (await api.get(workspace.path("/tasks/counts"), headers=headers)).json()
    assert counts["done"] == 1

    after = (await api.get(workspace.path(f"/leads/{lead['id']}/actions"), headers=headers)).json()
    assert any(a["kind"] == "TASK_COMPLETED" for a in after["items"])


async def test_a_task_can_be_listed_from_its_lead(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)
    lead = await _lead(api, workspace, headers, name="Owner", phone="9930000002")
    await api.post(
        workspace.path("/tasks"),
        headers=headers,
        json={
            "title": "Follow up",
            "due_at": dt.datetime.now(dt.UTC).isoformat(),
            "lead_id": lead["id"],
        },
    )

    listed = (await api.get(workspace.path(f"/leads/{lead['id']}/tasks"), headers=headers)).json()
    assert [t["title"] for t in listed] == ["Follow up"]


async def test_a_completed_task_cannot_be_edited_until_reopened(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)
    created = await api.post(
        workspace.path("/tasks"),
        headers=headers,
        json={"title": "Done thing", "due_at": dt.datetime.now(dt.UTC).isoformat()},
    )
    task_id = created.json()["id"]
    await api.post(workspace.path(f"/tasks/{task_id}/complete"), headers=headers)

    refused = await api.patch(
        workspace.path(f"/tasks/{task_id}"), headers=headers, json={"title": "Changed"}
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "task_completed"

    await api.post(workspace.path(f"/tasks/{task_id}/reopen"), headers=headers)
    allowed = await api.patch(
        workspace.path(f"/tasks/{task_id}"), headers=headers, json={"title": "Changed"}
    )
    assert allowed.status_code == 200, allowed.text


async def test_a_caller_does_not_see_another_members_tasks(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)
    rep_two = workspace.members["rep_two"]
    created = await api.post(
        workspace.path("/tasks"),
        headers=headers,
        json={
            "title": "Someone else's",
            "due_at": dt.datetime.now(dt.UTC).isoformat(),
            "assignee_id": str(rep_two.membership.id),
        },
    )
    assert created.status_code == 201, created.text

    rep = workspace.members["rep"]
    await login(api, rep)
    listed = (await api.get(workspace.path("/tasks"), headers=rep.auth)).json()
    assert created.json()["id"] not in {t["id"] for t in listed["items"]}

    # And by direct id it is indistinguishable from absent.
    direct = await api.get(workspace.path(f"/tasks/{created.json()['id']}"), headers=rep.auth)
    assert direct.status_code == 404


# --- labels ------------------------------------------------------------------


async def test_labels_attach_and_detach(api: AsyncClient, workspace: WorkspaceFixture) -> None:
    headers = await _admin(api, workspace)
    lead = await _lead(api, workspace, headers, name="Tagged", phone="9940000001")

    created = await api.post(
        workspace.path("/labels"), headers=headers, json={"name": "Hot", "color": "#ef4444"}
    )
    assert created.status_code == 201, created.text
    label_id = created.json()["id"]

    attached = await api.post(
        workspace.path(f"/leads/{lead['id']}/labels/{label_id}"), headers=headers
    )
    assert attached.status_code == 200, attached.text
    assert [entry["name"] for entry in attached.json()] == ["Hot"]

    # Attaching twice is not an error — it is the same fact.
    again = await api.post(
        workspace.path(f"/leads/{lead['id']}/labels/{label_id}"), headers=headers
    )
    assert len(again.json()) == 1

    detached = await api.delete(
        workspace.path(f"/leads/{lead['id']}/labels/{label_id}"), headers=headers
    )
    assert detached.status_code == 200
    assert detached.json() == []


async def test_a_duplicate_label_name_is_refused(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)
    await api.post(workspace.path("/labels"), headers=headers, json={"name": "Priority"})
    repeat = await api.post(workspace.path("/labels"), headers=headers, json={"name": "priority"})
    assert repeat.status_code == 409
    assert repeat.json()["detail"]["code"] == "duplicate_label"


async def test_an_archived_label_leaves_the_list_and_cannot_be_applied(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """Archived, never deleted — deleting would strip it from every lead
    carrying it, unanswerably."""
    headers = await _admin(api, workspace)
    lead = await _lead(api, workspace, headers, name="Later", phone="9940000002")
    created = await api.post(workspace.path("/labels"), headers=headers, json={"name": "Old"})
    label_id = created.json()["id"]

    await api.delete(workspace.path(f"/labels/{label_id}"), headers=headers)

    listed = (await api.get(workspace.path("/labels"), headers=headers)).json()
    assert listed == []
    with_archived = (
        await api.get(
            workspace.path("/labels"), headers=headers, params={"include_archived": "true"}
        )
    ).json()
    assert [entry["name"] for entry in with_archived] == ["Old"]

    refused = await api.post(
        workspace.path(f"/leads/{lead['id']}/labels/{label_id}"), headers=headers
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "label_archived"


# --- import: mapping and permissions -----------------------------------------


async def test_the_mapping_offers_only_importable_fields(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """Both conditions: `show_in_import` *and* the caller's Import grant."""
    headers = await _admin(api, workspace)
    await _field(api, workspace, headers, label="City", field_type="TEXT", show_in_import=True)
    await _field(
        api, workspace, headers, label="Internal Note", field_type="TEXT", show_in_import=False
    )

    offered = (await api.get(workspace.path("/imports/fields"), headers=headers)).json()
    keys = {entry["key"] for entry in offered}
    assert "city" in keys
    assert "internal_note" not in keys, "a field the admin excluded is not offered"


async def test_a_mapping_naming_a_non_importable_field_is_refused(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)
    await _field(
        api, workspace, headers, label="Internal Note", field_type="TEXT", show_in_import=False
    )
    job = await _upload(api, workspace, headers, _csv([["Phone", "Note"], ["9000000001", "hello"]]))

    refused = await api.put(
        workspace.path(f"/imports/{job['id']}/mapping"),
        headers=headers,
        json={"mapping": {"Phone": "phone", "Note": "internal_note"}},
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "field_not_importable"


async def test_a_mapping_naming_a_column_not_in_the_file_is_refused(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)
    job = await _upload(api, workspace, headers, _csv([["Phone"], ["9000000001"]]))

    refused = await api.put(
        workspace.path(f"/imports/{job['id']}/mapping"),
        headers=headers,
        json={"mapping": {"Nonexistent": "phone"}},
    )
    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "unknown_column"


# --- import: the dry run -----------------------------------------------------


async def test_the_dry_run_shows_creates_updates_and_errors_without_writing(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """The answer to "Pitfalls of Excel Upload".

    An operator who sees the counts before committing does not have to undo
    afterwards.
    """
    headers = await _admin(api, workspace)
    await _lead(api, workspace, headers, name="Existing", phone="9000000001")

    job = await _upload(
        api,
        workspace,
        headers,
        _csv(
            [
                ["Phone", "Name"],
                ["9000000001", "Existing Renamed"],
                ["9000000002", "Brand New"],
                ["", "No Identifier"],
            ]
        ),
    )
    previewed = await _run(
        api, workspace, headers, job["id"], {"Phone": "phone", "Name": "name"}, commit=False
    )

    assert previewed["status"] == "PREVIEWED"
    assert previewed["result"]["counts"] == {"update": 1, "create": 1, "error": 1}
    assert previewed["result"]["errors"][0]["row_number"] == 4, "row numbers match Excel's gutter"

    # Nothing was written.
    leads = (await api.get(workspace.path("/leads"), headers=headers)).json()
    assert leads["total"] == 1
    assert leads["items"][0]["values"]["name"] == "Existing"


async def test_committing_an_import_writes_one_changeset_that_can_be_undone(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """A bad import is one undo — which is the whole reason a run opens exactly
    one changeset."""
    headers = await _admin(api, workspace)
    job = await _upload(
        api,
        workspace,
        headers,
        _csv([["Phone", "Name"], ["9000000010", "One"], ["9000000011", "Two"]]),
    )
    committed = await _run(api, workspace, headers, job["id"], {"Phone": "phone", "Name": "name"})

    assert committed["status"] == "COMPLETED"
    assert committed["result"]["counts"]["create"] == 2
    assert committed["changeset_id"] is not None

    leads = (await api.get(workspace.path("/leads"), headers=headers)).json()
    assert leads["total"] == 2

    report = (
        await api.get(workspace.path("/changesets"), headers=headers, params={"source": "IMPORT"})
    ).json()
    assert report["total"] == 1


# --- import: the four flows the audit found missing ---------------------------


async def test_excel_bulk_update_refuses_a_row_that_matches_nothing(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """The entire difference between LEAD_UPDATE and LEAD_IMPORT.

    In an update run a typo'd identifier is a mistake in the sheet, not an
    instruction to create somebody.
    """
    headers = await _admin(api, workspace)
    await _lead(api, workspace, headers, name="Real", phone="9000000020")

    job = await _upload(
        api,
        workspace,
        headers,
        _csv([["Phone", "Name"], ["9000000020", "Updated"], ["9000009999", "Typo"]]),
        kind="LEAD_UPDATE",
    )
    committed = await _run(api, workspace, headers, job["id"], {"Phone": "phone", "Name": "name"})

    assert committed["result"]["counts"] == {"update": 1, "error": 1}
    assert "No existing lead" in committed["result"]["errors"][0]["message"]

    leads = (await api.get(workspace.path("/leads"), headers=headers)).json()
    assert leads["total"] == 1, "an update run never creates"
    assert leads["items"][0]["values"]["name"] == "Updated"


async def test_excel_advance_distribution_spreads_a_batch_round_robin(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """ "Distributing an imported batch across reps" — the audit's words."""
    headers = await _admin(api, workspace)
    rep = workspace.members["rep"]
    rep_two = workspace.members["rep_two"]

    job = await _upload(
        api,
        workspace,
        headers,
        _csv(
            [
                ["Phone", "Name"],
                ["9000000030", "A"],
                ["9000000031", "B"],
                ["9000000032", "C"],
                ["9000000033", "D"],
            ]
        ),
    )
    await _run(
        api,
        workspace,
        headers,
        job["id"],
        {"Phone": "phone", "Name": "name"},
        {
            "strategy": "ROUND_ROBIN",
            "membership_ids": [str(rep.membership.id), str(rep_two.membership.id)],
        },
    )

    leads = (await api.get(workspace.path("/leads"), headers=headers, params={"limit": 100})).json()
    owners = [lead["assignee_id"] for lead in leads["items"]]
    assert owners.count(str(rep.membership.id)) == 2
    assert owners.count(str(rep_two.membership.id)) == 2


async def test_weighted_distribution_gives_a_bigger_share_to_a_heavier_weight(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)
    rep = workspace.members["rep"]
    rep_two = workspace.members["rep_two"]

    job = await _upload(
        api,
        workspace,
        headers,
        _csv([["Phone"], *[[f"90000004{i:02d}"] for i in range(6)]]),
    )
    await _run(
        api,
        workspace,
        headers,
        job["id"],
        {"Phone": "phone"},
        {
            "strategy": "WEIGHTED",
            "membership_ids": [str(rep.membership.id), str(rep_two.membership.id)],
            "weights": {str(rep.membership.id): 2, str(rep_two.membership.id): 1},
        },
    )

    leads = (await api.get(workspace.path("/leads"), headers=headers, params={"limit": 100})).json()
    owners = [lead["assignee_id"] for lead in leads["items"]]
    assert owners.count(str(rep.membership.id)) == 4
    assert owners.count(str(rep_two.membership.id)) == 2


async def test_availability_aware_distribution_skips_a_member_on_leave(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """M1 already models availability with a full log; the engine simply skips
    anyone not WORKING rather than queueing work for them."""
    headers = await _admin(api, workspace)
    rep = workspace.members["rep"]
    rep_two = workspace.members["rep_two"]

    away = await api.put(
        workspace.path(f"/members/{rep_two.membership.id}/availability"),
        headers=headers,
        json={"status": "ON_LEAVE", "note": "Annual leave"},
    )
    assert away.status_code == 200, away.text

    job = await _upload(api, workspace, headers, _csv([["Phone"], ["9000000050"], ["9000000051"]]))
    await _run(
        api,
        workspace,
        headers,
        job["id"],
        {"Phone": "phone"},
        {
            "strategy": "AVAILABILITY",
            "membership_ids": [str(rep.membership.id), str(rep_two.membership.id)],
        },
    )

    leads = (await api.get(workspace.path("/leads"), headers=headers, params={"limit": 100})).json()
    owners = {lead["assignee_id"] for lead in leads["items"]}
    assert owners == {str(rep.membership.id)}


async def test_owner_specific_assignment_reads_the_owner_from_a_column(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """ "Assignment driven by a column in the sheet" — the audit's words."""
    headers = await _admin(api, workspace)
    rep = workspace.members["rep"]

    job = await _upload(
        api,
        workspace,
        headers,
        _csv(
            [
                ["Phone", "Owner"],
                ["9000000060", "work-rep@example.com"],
                ["9000000061", "nobody@example.com"],
            ]
        ),
    )
    await _run(
        api,
        workspace,
        headers,
        job["id"],
        {"Phone": "phone"},
        {"strategy": "COLUMN", "owner_column": "Owner"},
    )

    leads = (await api.get(workspace.path("/leads"), headers=headers, params={"limit": 100})).json()
    by_identity = {lead["identity_value"]: lead for lead in leads["items"]}
    assert by_identity["+919000000060"]["assignee_id"] == str(rep.membership.id)
    # An unrecognised owner leaves the lead unassigned rather than failing the
    # row: the contact details are still worth having.
    assert by_identity["+919000000061"]["assignee_id"] is None


async def test_importing_existing_actions_produces_a_coherent_timeline(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """M7's second acceptance check.

    Historical timeline migration — the flow every customer switching CRMs
    needs. The events are *predated*: a migrated timeline whose entries all
    landed at import o'clock is not a timeline.
    """
    headers = await _admin(api, workspace)
    lead = await _lead(api, workspace, headers, name="Migrated", phone="9000000070")

    job = await _upload(
        api,
        workspace,
        headers,
        _csv(
            [
                ["Phone", "Kind", "When", "Body"],
                ["9000000070", "CALL", "2025-03-04T10:30:00+00:00", "First contact"],
                ["9000000070", "NOTE", "2025-03-06T09:00:00+00:00", "Wants a callback"],
                ["9000000070", "EMAIL", "2025-04-01T11:00:00+00:00", "Sent the brochure"],
                ["9999999999", "NOTE", "2025-04-02T11:00:00+00:00", "Orphan"],
            ]
        ),
        kind="ACTION_IMPORT",
        filename="history.csv",
    )
    committed = await _run(
        api,
        workspace,
        headers,
        job["id"],
        {"Phone": "identity", "Kind": "kind", "When": "performed_at", "Body": "body"},
    )

    assert committed["result"]["counts"] == {"create": 3, "error": 1}

    timeline = (
        await api.get(workspace.path(f"/leads/{lead['id']}/actions"), headers=headers)
    ).json()
    migrated = [a for a in timeline["items"] if a["payload"].get("imported")]
    assert {a["kind"] for a in migrated} == {"CALL_LOGGED", "NOTE", "EMAIL_SENT"}

    # In order, and in the past — which is the whole point.
    stamps = sorted(a["performed_at"] for a in migrated)
    assert stamps[0].startswith("2025-03-04")
    assert stamps[-1].startswith("2025-04-01")


async def test_an_imported_action_cannot_fabricate_a_field_change(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """A sheet must not be able to write a kind that undo would later replay.

    `FIELD_CHANGE` and `STAGE_CHANGE` carry old/new values; accepting them from
    a migration would let an import manufacture a history that never happened
    and then have M7's undo act on it.
    """
    headers = await _admin(api, workspace)
    await _lead(api, workspace, headers, name="Safe", phone="9000000080")

    job = await _upload(
        api,
        workspace,
        headers,
        _csv(
            [
                ["Phone", "Kind", "When"],
                ["9000000080", "FIELD_CHANGE", "2025-01-01T00:00:00+00:00"],
            ]
        ),
        kind="ACTION_IMPORT",
    )
    committed = await _run(
        api,
        workspace,
        headers,
        job["id"],
        {"Phone": "identity", "Kind": "kind", "When": "performed_at"},
    )
    assert committed["result"]["counts"] == {"error": 1}
    assert "Unknown action kind" in committed["result"]["errors"][0]["message"]


# --- the sheet reader --------------------------------------------------------


async def test_an_xlsx_upload_is_read_like_a_csv(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """One parser for both, because the flows disagree about what columns mean,
    never about how to get at them."""
    headers = await _admin(api, workspace)

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Phone", "Name"])
    sheet.append([9000000090, "From Excel"])
    buffer = io.BytesIO()
    workbook.save(buffer)

    response = await api.post(
        workspace.path("/imports"),
        headers=headers,
        files={
            "file": (
                "leads.xlsx",
                buffer.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["source_columns"] == ["Phone", "Name"]

    committed = await _run(api, workspace, headers, job["id"], {"Phone": "phone", "Name": "name"})
    assert committed["result"]["counts"]["create"] == 1

    leads = (await api.get(workspace.path("/leads"), headers=headers)).json()
    # Excel hands numbers back as floats; "9000000090.0" would be a different
    # phone number and a different lead.
    assert leads["items"][0]["identity_value"] == "+919000000090"


async def test_an_unsupported_file_type_is_refused(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)
    response = await api.post(
        workspace.path("/imports"),
        headers=headers,
        files={"file": ("leads.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unsupported_file_type"


# --- export ------------------------------------------------------------------


async def test_export_carries_only_export_granted_columns(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """A caller may read a phone number on screen and be barred from
    downloading ten thousand of them. Export is not View."""
    headers = await _admin(api, workspace)
    await _field(api, workspace, headers, label="Salary", field_type="NUMBER")
    await _lead(api, workspace, headers, name="Earner", phone="9000000100", salary=99)

    started = await api.post(workspace.path("/leads/export"), headers=headers, json={})
    assert started.status_code == 200, started.text
    assert started.json()["row_count"] == 1

    downloaded = await api.get(
        workspace.path(f"/leads/export/{started.json()['job_id']}"), headers=headers
    )
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.headers["content-type"].startswith("text/csv")
    body = downloaded.text
    assert "Earner" in body
    assert "+919000000100" in body


async def test_export_is_refused_outright_when_the_template_exports_nothing(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """An empty file looks like a bug and invites a retry.

    `Export (0) None` is the observed default and a deliberate exfiltration
    control, so saying so is better than producing a file with headers only.
    """
    headers = await _admin(api, workspace)
    await _lead(api, workspace, headers, name="Private", phone="9000000101")

    rep = workspace.members["rep"]
    await login(api, rep)
    refused = await api.post(workspace.path("/leads/export"), headers=rep.auth, json={})
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"]["code"] == "export_not_permitted"


async def test_an_export_takes_the_same_filter_as_the_list(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """ "Export what I am looking at" is one call, not a second query language."""
    headers = await _admin(api, workspace)
    await _field(api, workspace, headers, label="City", field_type="TEXT")
    await _lead(api, workspace, headers, name="Here", phone="9000000110", city="Chennai")
    await _lead(api, workspace, headers, name="There", phone="9000000111", city="Delhi")

    started = await api.post(
        workspace.path("/leads/export"),
        headers=headers,
        json={"filter": {"type": "field", "key": "city", "op": "eq", "value": "Chennai"}},
    )
    assert started.status_code == 200, started.text
    assert started.json()["row_count"] == 1

    body = (
        await api.get(workspace.path(f"/leads/export/{started.json()['job_id']}"), headers=headers)
    ).text
    assert "Here" in body
    assert "There" not in body


async def test_a_money_value_exports_readably(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """A spreadsheet full of raw JSON is not an export anybody can use."""
    headers = await _admin(api, workspace)
    await _field(api, workspace, headers, label="Fee", field_type="MONEY")
    await _lead(api, workspace, headers, name="Payer", phone="9000000120", fee=5000)

    started = await api.post(workspace.path("/leads/export"), headers=headers, json={})
    body = (
        await api.get(workspace.path(f"/leads/export/{started.json()['job_id']}"), headers=headers)
    ).text
    assert "5000.00 INR" in body
    assert '{"amount"' not in body


# --- duplicates and merge ----------------------------------------------------


async def test_duplicates_group_on_any_contact_value_not_just_the_identity(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """The identity field is unique by construction.

    `leads_identity_uq` means two live leads cannot share an identity, so a
    report grouping on that alone would always be empty. The case that really
    happens is one person entered twice — their number as the identifier on one
    record and as an alternate contact on the other.
    """
    headers = await _admin(api, workspace)
    first = await _lead(api, workspace, headers, name="Same Person", phone="9000000130")
    second = await _lead(
        api,
        workspace,
        headers,
        name="Same Person Again",
        phone="9000000131",
        alternate_phone="9000000130",
    )
    await _lead(api, workspace, headers, name="Unrelated", phone="9000000132")

    groups = (await api.get(workspace.path("/leads/duplicates"), headers=headers)).json()
    assert len(groups) == 1
    assert set(groups[0]["lead_ids"]) == {first["id"], second["id"]}


async def test_merging_keeps_the_history_and_fills_only_blanks(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """A merge that discarded history would lose calls that were actually made.

    Values fill blanks only, so the primary stays authoritative and the outcome
    is predictable rather than last-write-wins.
    """
    headers = await _admin(api, workspace)
    await _field(api, workspace, headers, label="City", field_type="TEXT")
    await _field(api, workspace, headers, label="Company", field_type="TEXT")

    primary = await _lead(
        api, workspace, headers, name="Keeper", phone="9000000140", city="Chennai"
    )
    duplicate = await _lead(
        api,
        workspace,
        headers,
        name="Dupe",
        phone="9000000141",
        city="Delhi",
        company="Northwind",
    )

    noted = await api.post(
        workspace.path(f"/leads/{duplicate['id']}/notes"),
        headers=headers,
        json={"body": "Spoke on the duplicate record"},
    )
    assert noted.status_code == 201, noted.text

    merged = await api.post(
        workspace.path("/leads/merge"),
        headers=headers,
        json={"primary_id": primary["id"], "merge_ids": [duplicate["id"]]},
    )
    assert merged.status_code == 200, merged.text
    assert merged.json()["fields_filled"] == ["company"]

    kept = (await api.get(workspace.path(f"/leads/{primary['id']}"), headers=headers)).json()
    # The primary's own value wins; only the gap is filled.
    assert kept["values"]["city"] == "Chennai"
    assert kept["values"]["company"] == "Northwind"

    timeline = (
        await api.get(workspace.path(f"/leads/{primary['id']}/actions"), headers=headers)
    ).json()
    assert any(
        a["kind"] == "NOTE" and a["body"] == "Spoke on the duplicate record"
        for a in timeline["items"]
    ), "the call that was actually made must survive the merge"

    # The merged record is soft-deleted, never dropped.
    gone = await api.get(workspace.path(f"/leads/{duplicate['id']}"), headers=headers)
    assert gone.status_code == 404


async def test_merging_a_lead_into_itself_is_refused(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)
    lead = await _lead(api, workspace, headers, name="Alone", phone="9000000150")

    response = await api.post(
        workspace.path("/leads/merge"),
        headers=headers,
        json={"primary_id": lead["id"], "merge_ids": [lead["id"]]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_merge"


async def test_a_merge_is_one_changeset(api: AsyncClient, workspace: WorkspaceFixture) -> None:
    headers = await _admin(api, workspace)
    await _field(api, workspace, headers, label="Company", field_type="TEXT")
    primary = await _lead(api, workspace, headers, name="Main", phone="9000000160")
    other = await _lead(api, workspace, headers, name="Other", phone="9000000161", company="Acme")

    merged = await api.post(
        workspace.path("/leads/merge"),
        headers=headers,
        json={"primary_id": primary["id"], "merge_ids": [other["id"]]},
    )
    changeset_id = merged.json()["changeset_id"]

    detail = (await api.get(workspace.path(f"/changesets/{changeset_id}"), headers=headers)).json()
    assert "Merged" in detail["summary"]


# --- import: when the lead was first received ----------------------------------


def _created_rows() -> bytes:
    return _csv(
        [
            ["Phone", "Name", "Created On"],
            ["9000000301", "Sumit", "24/09/2026 13:29:25"],
            ["9000000302", "Azad", "08/10/2026 12:21:37"],
        ]
    )


async def _created_at(api: AsyncClient, ws: WorkspaceFixture, headers: dict[str, str]) -> dict:
    leads = (await api.get(ws.path("/leads"), headers=headers)).json()["items"]
    return {lead["identity_value"]: lead["created_at"] for lead in leads}


@pytest.mark.integration
async def test_an_imported_lead_keeps_the_date_it_was_first_received(
    api: AsyncClient, workspace: WorkspaceFixture, db_session: AsyncSession
) -> None:
    """A lead migrated from TeleCRM applied in September, not on import day.

    Read day-first (TeleCRM's `DD/MM/YYYY`), in the workspace's own timezone.
    """
    workspace.workspace.timezone = "Asia/Kolkata"
    await db_session.commit()
    headers = await _admin(api, workspace)
    job = await _upload(api, workspace, headers, _created_rows())

    committed = await _run(
        api,
        workspace,
        headers,
        job["id"],
        {"Phone": "phone", "Name": "name"},
        {"created_at_column": "Created On", "created_at_format": "day_first"},
    )

    assert committed["result"]["counts"] == {"create": 2}
    created = await _created_at(api, workspace, headers)
    sumit = dt.datetime.fromisoformat(created["+919000000301"])
    # 13:29:25 in Kolkata is 07:59:25 UTC — the same instant, read locally.
    assert sumit.astimezone(dt.UTC) == dt.datetime(2026, 9, 24, 7, 59, 25, tzinfo=dt.UTC)
    azad = dt.datetime.fromisoformat(created["+919000000302"])
    assert (azad.year, azad.month, azad.day) == (2026, 10, 8)


@pytest.mark.integration
async def test_an_iso_created_on_needs_no_format(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)
    job = await _upload(
        api,
        workspace,
        headers,
        _csv([["Phone", "Created"], ["9000000311", "2026-08-01T09:00:00+00:00"]]),
    )
    await _run(
        api, workspace, headers, job["id"], {"Phone": "phone"}, {"created_at_column": "Created"}
    )
    created = await _created_at(api, workspace, headers)
    assert dt.datetime.fromisoformat(created["+919000000311"]) == dt.datetime(
        2026, 8, 1, 9, 0, tzinfo=dt.UTC
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("value", "reason"),
    [("31/31/2026 10:00:00", "not a date"), ("01/01/2099 10:00:00", "in the future")],
)
async def test_a_bad_created_on_is_a_row_error_in_the_preview(
    api: AsyncClient, workspace: WorkspaceFixture, value: str, reason: str
) -> None:
    """Caught before anything is written, with the row and the reason."""
    headers = await _admin(api, workspace)
    job = await _upload(
        api,
        workspace,
        headers,
        _csv([["Phone", "Created On"], ["9000000321", value], ["9000000322", "01/08/2026"]]),
    )
    previewed = await _run(
        api,
        workspace,
        headers,
        job["id"],
        {"Phone": "phone"},
        {"created_at_column": "Created On", "created_at_format": "day_first"},
        commit=False,
    )
    assert previewed["result"]["counts"] == {"error": 1, "create": 1}
    [error] = previewed["result"]["errors"]
    assert error["row_number"] == 2
    assert reason in error["message"]
    leads = (await api.get(workspace.path("/leads"), headers=headers)).json()
    assert leads["total"] == 0


@pytest.mark.integration
async def test_an_existing_lead_is_never_re_dated(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    """An update describes someone the CRM already holds; their date stays."""
    headers = await _admin(api, workspace)
    existing = await _lead(api, workspace, headers, name="Already here", phone="9000000301")
    job = await _upload(api, workspace, headers, _created_rows())

    committed = await _run(
        api,
        workspace,
        headers,
        job["id"],
        {"Phone": "phone", "Name": "name"},
        {"created_at_column": "Created On", "created_at_format": "day_first"},
    )

    assert committed["result"]["counts"] == {"update": 1, "create": 1}
    created = await _created_at(api, workspace, headers)
    assert created["+919000000301"] == existing["created_at"]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("options", "code"),
    [
        (
            {"created_at_column": "Created On", "created_at_format": "guess"},
            "invalid_created_at_format",
        ),
        ({"created_at_column": "Not A Column"}, "unknown_column"),
    ],
)
async def test_created_on_options_are_checked_when_mapping(
    api: AsyncClient, workspace: WorkspaceFixture, options: dict[str, Any], code: str
) -> None:
    headers = await _admin(api, workspace)
    job = await _upload(api, workspace, headers, _created_rows())
    response = await api.put(
        workspace.path(f"/imports/{job['id']}/mapping"),
        headers=headers,
        json={"mapping": {"Phone": "phone"}, "options": options},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == code


@pytest.mark.integration
async def test_without_the_option_an_import_is_dated_as_before(
    api: AsyncClient, workspace: WorkspaceFixture
) -> None:
    headers = await _admin(api, workspace)
    job = await _upload(api, workspace, headers, _created_rows())
    before = dt.datetime.now(dt.UTC)
    await _run(api, workspace, headers, job["id"], {"Phone": "phone", "Name": "name"})
    created = await _created_at(api, workspace, headers)
    assert all(
        dt.datetime.fromisoformat(v) >= before - dt.timedelta(seconds=5) for v in created.values()
    )


# --- the same lead twice in one file ----------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    ("identity", "rows"),
    [
        # Two rows for one phone number.
        ("phone", [["9000000401", "Rahul"], ["9000000401", "Rahul K"]]),
        # Production keyed on Name, and two different people shared a first name.
        ("name", [["9000000402", "Rahul"], ["9000000403", "Rahul"]]),
    ],
    ids=["same-phone", "same-name"],
)
async def test_the_same_lead_twice_in_one_file_is_a_row_error_not_a_crash(
    api: AsyncClient,
    workspace: WorkspaceFixture,
    db_session: AsyncSession,
    identity: str,
    rows: list[list[str]],
) -> None:
    """Regression: two rows with one identifier both previewed as "create", and
    the commit then died on the unique index with a 500, importing nothing.

    The second row is now an error that names the first, the preview says so
    before anything is written, and the rest of the file imports.
    """
    workspace.workspace.identity_field_id = workspace.fields[identity].id
    await db_session.commit()
    headers = await _admin(api, workspace)
    content = _csv([["Phone", "Name"], *rows, ["9000000404", "Selvi"]])

    job = await _upload(api, workspace, headers, content)
    previewed = await _run(
        api, workspace, headers, job["id"], {"Phone": "phone", "Name": "name"}, commit=False
    )
    assert previewed["result"]["counts"] == {"create": 2, "error": 1}
    [error] = previewed["result"]["errors"]
    assert error["row_number"] == 3
    assert "row 2" in error["message"]

    committed = await api.post(workspace.path(f"/imports/{job['id']}/commit"), headers=headers)
    assert committed.status_code == 200, committed.text
    assert committed.json()["result"]["counts"] == {"create": 2, "error": 1}
    leads = (await api.get(workspace.path("/leads"), headers=headers)).json()
    assert leads["total"] == 2
