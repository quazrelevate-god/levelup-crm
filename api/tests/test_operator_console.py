"""The operator console (docs/19).

1. **Only the operator** reaches it; a client's own Root does not.
2. **Creating a company** makes its wallet, its owner (Root) and a sign-in link
   the owner uses to choose their own password — the operator is not a member.
3. **People** at any role, under any manager; seats are enforced.
4. **Editing**: seats (never below those in use), features, suspending.
5. **Money**: add, free credit, corrections, price per minute — all recorded.
6. **Support access**: the operator goes inside as Root without taking a seat.
7. **History**: every action is written down.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import WorkspaceFixture, build_workspace, login

from app.auth.passwords import PasswordHasherService

BASE = "/api/v1/operator"
NEW_PASSWORD = "a-fresh-password-1234"


@pytest.fixture
def hasher(wired_app: FastAPI) -> PasswordHasherService:
    hasher = wired_app.state.password_hasher
    assert isinstance(hasher, PasswordHasherService)
    return hasher


@pytest.fixture
async def home(
    api: AsyncClient,
    db_session: AsyncSession,
    hasher: PasswordHasherService,
    make_operator: Callable[..., None],
) -> WorkspaceFixture:
    """The operator's own company; its owner is the operator."""
    fixture = await build_workspace(
        db_session, hasher, name="Operator Co", owner_email="operator@platform.example"
    )
    make_operator("operator@platform.example")
    await login(api, fixture.owner)
    return fixture


def _auth(home: WorkspaceFixture) -> dict[str, str]:
    return home.owner.auth


async def _create(api: AsyncClient, home: WorkspaceFixture, **overrides: Any) -> dict[str, Any]:
    body = {
        "name": "Acme Academy",
        "seat_limit": 3,
        "features": {"automations": True, "campaign": False},
        "opening_balance": "1000",
        "rate_per_minute": "5",
        "owner_name": "Asha Owner",
        "owner_email": "asha@acme.example",
        **overrides,
    }
    response = await api.post(f"{BASE}/companies", headers=_auth(home), json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def me_support_id(me: dict[str, Any], workspace_id: str) -> str:
    return str(next(m["id"] for m in me["memberships"] if m["workspace"]["id"] == workspace_id))


def _token(link: str) -> str:
    return parse_qs(urlparse(link).query)["token"][0]


# --- 1. only the operator -------------------------------------------------------------


async def test_a_client_root_cannot_open_the_console(
    api: AsyncClient,
    db_session: AsyncSession,
    hasher: PasswordHasherService,
    make_operator: Callable[..., None],
) -> None:
    client = await build_workspace(
        db_session, hasher, name="Client Co", owner_email="root@client.example"
    )
    make_operator("someone-else@platform.example")
    await login(api, client.owner)

    for path in ("/overview", "/companies", "/history"):
        response = await api.get(f"{BASE}{path}", headers=client.owner.auth)
        assert response.status_code == 403, path


async def test_login_says_who_the_operator_is(api: AsyncClient, home: WorkspaceFixture) -> None:
    response = await api.post(
        "/api/v1/auth/login",
        json={"email": home.owner.user.email, "password": "correct-horse-battery-staple"},
    )
    assert response.json()["is_platform_operator"] is True


# --- 2. creating a company ---------------------------------------------------------------


async def test_creating_a_company_makes_its_wallet_owner_and_sign_in_link(
    api: AsyncClient, home: WorkspaceFixture
) -> None:
    created = await _create(api, home)
    company = created["company"]

    assert company["name"] == "Acme Academy"
    assert company["owner_email"] == "asha@acme.example"
    assert (company["seats_used"], company["seat_limit"]) == (1, 3)
    assert company["balance"] == "1000.00"
    assert company["features"]["automations"] is True
    assert company["features"]["campaign"] is False

    # The owner chooses their own password through the link, then signs in.
    link = created["owner"]["link"]
    redeemed = await api.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": _token(link), "new_password": NEW_PASSWORD},
    )
    assert redeemed.status_code in (200, 204), redeemed.text
    signed_in = await api.post(
        "/api/v1/auth/login", json={"email": "asha@acme.example", "password": NEW_PASSWORD}
    )
    assert signed_in.status_code == 200
    [membership] = signed_in.json()["memberships"]
    assert (membership["workspace"]["name"], membership["template_name"]) == (
        "Acme Academy",
        "Root",
    )
    assert signed_in.json()["is_platform_operator"] is False

    people = (await api.get(f"{BASE}/companies/{company['id']}/people", headers=_auth(home))).json()
    assert [p["email"] for p in people] == ["asha@acme.example"]


async def test_the_ledger_lists_every_company_with_its_numbers(
    api: AsyncClient, home: WorkspaceFixture
) -> None:
    await _create(api, home, opening_balance="200")
    rows = (await api.get(f"{BASE}/companies", headers=_auth(home))).json()

    names = {row["name"]: row for row in rows}
    assert set(names) == {"Operator Co", "Acme Academy"}
    assert names["Acme Academy"]["low_balance"] is True
    overview = (await api.get(f"{BASE}/overview", headers=_auth(home))).json()
    assert overview["companies"] == 2
    assert [c["name"] for c in overview["low_balance"]] == ["Acme Academy"]
    assert overview["low_balance_at"] == "500.00"


# --- 3. people ---------------------------------------------------------------------------


async def test_people_are_added_at_any_role_under_any_manager_within_the_seats(
    api: AsyncClient, home: WorkspaceFixture
) -> None:
    company = (await _create(api, home))["company"]
    path = f"{BASE}/companies/{company['id']}"
    roles = {r["name"]: r for r in (await api.get(f"{path}/roles", headers=_auth(home))).json()}
    assert roles["Root"]["is_locked"] is True
    assert roles["Admin"]["is_admin"] is True

    manager = await api.post(
        f"{path}/people",
        headers=_auth(home),
        json={
            "full_name": "Mo Manager",
            "email": "mo@acme.example",
            "role_id": roles["Manager"]["id"],
        },
    )
    assert manager.status_code == 201, manager.text
    assert manager.json()["sign_in"]["link"]
    caller = await api.post(
        f"{path}/people",
        headers=_auth(home),
        json={
            "full_name": "Cal Caller",
            "email": "cal@acme.example",
            "role_id": roles["Caller"]["id"],
            "manager_id": manager.json()["person"]["membership_id"],
        },
    )
    assert caller.status_code == 201, caller.text
    assert caller.json()["person"]["manager_id"] == manager.json()["person"]["membership_id"]

    full = await api.post(
        f"{path}/people",
        headers=_auth(home),
        json={
            "full_name": "One Too Many",
            "email": "x@acme.example",
            "role_id": roles["Caller"]["id"],
        },
    )
    assert full.status_code == 409
    assert full.json()["detail"]["code"] == "seat_limit_reached"


async def test_a_person_can_be_promoted_and_given_a_fresh_link(
    api: AsyncClient, home: WorkspaceFixture
) -> None:
    company = (await _create(api, home))["company"]
    path = f"{BASE}/companies/{company['id']}"
    roles = {r["name"]: r for r in (await api.get(f"{path}/roles", headers=_auth(home))).json()}
    added = await api.post(
        f"{path}/people",
        headers=_auth(home),
        json={"full_name": "Pat", "email": "pat@acme.example", "role_id": roles["Caller"]["id"]},
    )
    person = added.json()["person"]["membership_id"]

    promoted = await api.patch(
        f"{path}/people/{person}", headers=_auth(home), json={"role_id": roles["Admin"]["id"]}
    )
    link = await api.post(f"{path}/people/{person}/sign-in-link", headers=_auth(home))

    assert promoted.json()["role_name"] == "Admin"
    assert link.status_code == 200 and link.json()["link"]


# --- 4. editing ----------------------------------------------------------------------------


async def test_seats_never_go_below_those_in_use_and_suspending_locks_the_company_out(
    api: AsyncClient, home: WorkspaceFixture
) -> None:
    created = await _create(api, home, seat_limit=2)
    company = created["company"]
    path = f"{BASE}/companies/{company['id']}"
    await api.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": _token(created["owner"]["link"]), "new_password": NEW_PASSWORD},
    )
    roles = {r["name"]: r for r in (await api.get(f"{path}/roles", headers=_auth(home))).json()}
    await api.post(
        f"{path}/people",
        headers=_auth(home),
        json={"full_name": "B", "email": "b@acme.example", "role_id": roles["Caller"]["id"]},
    )

    too_few = await api.patch(path, headers=_auth(home), json={"seat_limit": 1})
    assert too_few.status_code == 409

    more = await api.patch(
        path, headers=_auth(home), json={"seat_limit": 10, "features": {"campaign": True}}
    )
    assert (more.json()["seat_limit"], more.json()["features"]["campaign"]) == (10, True)

    owner = await api.post(
        "/api/v1/auth/login", json={"email": "asha@acme.example", "password": NEW_PASSWORD}
    )
    owner_auth = {"Authorization": f"Bearer {owner.json()['access_token']}"}
    assert (
        await api.get(f"/api/v1/workspaces/{company['id']}/leads", headers=owner_auth)
    ).status_code == 200

    suspended = await api.patch(path, headers=_auth(home), json={"is_active": False})
    assert suspended.json()["is_active"] is False
    assert (
        await api.get(f"/api/v1/workspaces/{company['id']}/leads", headers=owner_auth)
    ).status_code == 404


# --- 5. money -------------------------------------------------------------------------------


async def test_money_in_free_credit_and_corrections_are_all_recorded(
    api: AsyncClient, home: WorkspaceFixture
) -> None:
    company = (await _create(api, home))["company"]
    path = f"{BASE}/companies/{company['id']}/wallet"

    topped = await api.post(
        f"{path}/entries",
        headers=_auth(home),
        json={"kind": "TOPUP", "amount": "5000", "note": "UPI ref 42"},
    )
    assert topped.status_code == 201, topped.text
    assert topped.json()["balance"] == "6000.00"

    no_note = await api.post(
        f"{path}/entries", headers=_auth(home), json={"kind": "ADJUSTMENT", "amount": "-100"}
    )
    assert no_note.status_code == 422

    corrected = await api.post(
        f"{path}/entries",
        headers=_auth(home),
        json={"kind": "ADJUSTMENT", "amount": "-100", "note": "typed one zero too many"},
    )
    rate = await api.put(f"{path}/rate", headers=_auth(home), json={"rate_per_minute": "6.50"})

    assert corrected.json()["balance"] == "5900.00"
    assert rate.json()["rate_per_minute"] == "6.50"
    kinds = [e["kind"] for e in rate.json()["entries"]]
    assert kinds == ["ADJUSTMENT", "TOPUP", "TOPUP"]
    assert rate.json()["entries"][1]["recorded_by"] == "operator@platform.example"


# --- 6. support access ---------------------------------------------------------------------


async def test_going_inside_to_help_takes_no_seat_and_can_be_ended(
    api: AsyncClient, home: WorkspaceFixture
) -> None:
    company = (await _create(api, home, seat_limit=1))["company"]
    path = f"{BASE}/companies/{company['id']}"
    leads = f"/api/v1/workspaces/{company['id']}/leads"

    assert (await api.get(leads, headers=_auth(home))).status_code == 404
    started = await api.post(f"{path}/support", headers=_auth(home))
    assert started.status_code == 204, started.text

    assert (await api.get(leads, headers=_auth(home))).status_code == 200
    after = (await api.get(path, headers=_auth(home))).json()
    assert (after["seats_used"], after["support_active"]) == (1, True)
    me = (await api.get("/api/v1/me", headers=_auth(home))).json()
    support = [m for m in me["memberships"] if m["workspace"]["id"] == company["id"]]
    assert support[0]["is_support"] is True

    # Support access is never routed a lead — not even by a rule that names it
    # directly and ignores availability.
    support_id = me_support_id(me, company["id"])
    rule = await api.post(
        f"/api/v1/workspaces/{company['id']}/settings/assignment-rules",
        headers=_auth(home),
        json={
            "name": "Straight to support",
            "strategy": "FIXED",
            "config": {"membership_id": support_id},
            "conditions": {},
            "skip_unavailable": False,
        },
    )
    assert rule.status_code == 201, rule.text
    lead = await api.post(
        leads, headers=_auth(home), json={"values": {"name": "L", "phone": "+919000004001"}}
    )
    assert lead.status_code == 201, lead.text
    assert lead.json()["assignee_id"] != support_id

    await api.delete(f"{path}/support", headers=_auth(home))
    assert (await api.get(leads, headers=_auth(home))).status_code in (403, 404)


# --- 7. history ------------------------------------------------------------------------------


async def test_every_console_action_is_written_down(
    api: AsyncClient, home: WorkspaceFixture
) -> None:
    company = (await _create(api, home))["company"]
    path = f"{BASE}/companies/{company['id']}"
    await api.patch(path, headers=_auth(home), json={"seat_limit": 7})
    await api.post(
        f"{path}/wallet/entries", headers=_auth(home), json={"kind": "TOPUP", "amount": "250"}
    )

    history = (
        await api.get(
            f"{BASE}/history", headers=_auth(home), params={"workspace_id": company["id"]}
        )
    ).json()["items"]

    assert [h["action"] for h in history] == ["wallet.topup", "company.updated", "company.created"]
    assert "Seats 3 → 7" in history[1]["summary"]
    assert all(h["actor_email"] == "operator@platform.example" for h in history)


async def test_an_operator_address_gets_a_sign_in_link_and_others_do_not(
    api: AsyncClient, home: WorkspaceFixture, make_operator: Callable[..., None]
) -> None:
    make_operator("operator@platform.example", "second@platform.example")

    accounts = (await api.get(f"{BASE}/operators", headers=_auth(home))).json()
    assert {(a["email"], a["has_account"]) for a in accounts} == {
        ("operator@platform.example", True),
        ("second@platform.example", False),
    }
    link = await api.post(
        f"{BASE}/operators/sign-in-link",
        headers=_auth(home),
        json={"email": "second@platform.example"},
    )
    refused = await api.post(
        f"{BASE}/operators/sign-in-link", headers=_auth(home), json={"email": "nobody@else.example"}
    )
    assert link.status_code == 200 and link.json()["link"]
    assert refused.status_code == 403
