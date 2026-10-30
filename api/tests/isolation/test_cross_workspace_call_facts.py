"""Call facts never cross a workspace boundary (docs/17).

A fact set decides what the voice agent tells a lead. Another workspace must
not read B's sets, edit or archive them by id, or see them in a list.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from tests.factories import WorkspaceFixture
from tests.isolation.conftest import TenantPair
from tests.isolation.test_cross_workspace import (
    CALL_FACT_COLLECTION_ROUTES,
    CALL_FACT_COLLECTION_WRITE_ROUTES,
    CALL_FACT_ID_ROUTES,
)

B_SECRET = "B's batch starts on the 3rd"


async def _set_in(api: AsyncClient, fixture: WorkspaceFixture, name: str, value: str) -> str:
    response = await api.post(
        fixture.path("/settings/call-facts"),
        headers=fixture.owner.auth,
        json={"name": name, "facts": [{"label": "Batch start date", "value": value}]},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


@pytest.mark.parametrize(("method", "template"), CALL_FACT_ID_ROUTES)
async def test_another_workspace_s_fact_set_id_is_a_404(
    api: AsyncClient, tenants: TenantPair, method: str, template: str
) -> None:
    b_id = await _set_in(api, tenants.b, "B set", B_SECRET)

    response = await api.request(
        method,
        tenants.a.path(template.format(fact_set_id=b_id)),
        headers=tenants.a.owner.auth,
        json={"name": "Renamed By An Outsider"} if method == "PATCH" else None,
    )

    assert response.status_code == 404, f"{method} {template} leaked: {response.text}"
    assert B_SECRET not in response.text
    listed = await api.get(tenants.b.path("/settings/call-facts"), headers=tenants.b.owner.auth)
    [row] = listed.json()
    assert (row["name"], row["is_active"]) == ("B set", True)


@pytest.mark.parametrize("route", CALL_FACT_COLLECTION_ROUTES)
async def test_a_fact_set_list_shows_only_its_own_workspace(
    api: AsyncClient, tenants: TenantPair, route: str
) -> None:
    a_id = await _set_in(api, tenants.a, "A set", "A's date")
    await _set_in(api, tenants.b, "B set", B_SECRET)

    response = await api.get(tenants.a.path(route), headers=tenants.a.owner.auth)

    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()] == [a_id]
    assert B_SECRET not in response.text


@pytest.mark.parametrize(("method", "route"), CALL_FACT_COLLECTION_WRITE_ROUTES)
async def test_a_non_member_cannot_write_another_workspace_s_facts(
    api: AsyncClient, tenants: TenantPair, method: str, route: str
) -> None:
    response = await api.request(
        method,
        tenants.b.path(route),
        headers=tenants.a.owner.auth,
        json={"name": "Smuggled", "facts": [{"label": "Mentors", "value": "Nobody"}]},
    )

    assert response.status_code == 404, response.text
    listed = await api.get(tenants.b.path(route), headers=tenants.b.owner.auth)
    assert listed.json() == []
