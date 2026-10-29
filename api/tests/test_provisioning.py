"""Provisioning creates structure, never taxonomy.

The first two tests are the guardrails that matter most in this codebase: they
fail if anyone ever seeds an industry vocabulary into a new workspace. That is
"the #1 mistake here" per CLAUDE.md, and it is much easier to prevent than to
unpick once customers have data hanging off it.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import build_workspace, login

from app.auth.passwords import PasswordHasherService
from app.models import Membership, PermissionTemplate
from app.services.provisioning import (
    EXPECTED_STEPS,
    ProvisioningRegistry,
    ProvisioningStep,
    provisioning_registry,
    slugify,
)

pytestmark = pytest.mark.integration

# The five product roles from 03-configuration-model.md §6.1. Product concepts —
# the shape of a sales team, not one customer's vocabulary.
EXPECTED_TEMPLATES = {"Root", "Admin", "Manager", "Caller", "Marketing"}


@pytest.fixture
def hasher(wired_app: FastAPI) -> PasswordHasherService:
    hasher = wired_app.state.password_hasher
    assert isinstance(hasher, PasswordHasherService)
    return hasher


async def test_provisioning_creates_exactly_the_five_default_templates(
    db_session: AsyncSession,
    hasher: PasswordHasherService,
) -> None:
    fixture = await build_workspace(
        db_session,
        hasher,
        name="Fresh Start",
        owner_email="fresh@example.com",
    )

    rows = await db_session.execute(
        select(PermissionTemplate).where(PermissionTemplate.workspace_id == fixture.id)
    )
    templates = list(rows.scalars().all())

    assert {template.name for template in templates} == EXPECTED_TEMPLATES
    assert all(template.is_system for template in templates)

    root = next(template for template in templates if template.name == "Root")
    assert root.is_readonly, "Root must not be editable into uselessness"
    assert all(not template.is_readonly for template in templates if template.name != "Root"), (
        "Only Root is read-only"
    )


async def test_a_new_workspace_contains_no_business_taxonomy(
    db_session: AsyncSession,
    hasher: PasswordHasherService,
) -> None:
    """No industry term may appear anywhere in a freshly provisioned workspace.

    Scanned against a list of terms drawn from the legacy workspace the docs
    describe. If provisioning ever seeds a vertical's vocabulary, this fails —
    and it should, because that vocabulary is customer data.
    """
    fixture = await build_workspace(
        db_session,
        hasher,
        name="Blank Slate",
        owner_email="blank@example.com",
    )

    rows = await db_session.execute(
        select(PermissionTemplate).where(PermissionTemplate.workspace_id == fixture.id)
    )
    corpus = " ".join(
        f"{template.name} {template.capabilities}" for template in rows.scalars().all()
    ).casefold()

    forbidden = [
        "forge",
        "interview",
        "application_status",
        "mql",
        "sql",
        "tuition",
        "admission",
        "enrolment",
        "enrollment",
        "course",
        "batch",
        "demo_booked",
        "hot",
        "warm",
        "cold",
    ]
    leaked = [term for term in forbidden if term in corpus]
    assert not leaked, f"Provisioning seeded business taxonomy: {leaked}"


def test_the_provisioning_registry_matches_the_spec() -> None:
    """Every registered step is one §7 names, and the outstanding list is honest.

    §7 lists five things to provision. M1 owns one of them; the other four
    belong to milestones whose tables do not exist yet. This asserts the
    registry knows the difference rather than quietly shipping a partial
    provisioner that looks complete.
    """
    assert provisioning_registry.registered_names <= EXPECTED_STEPS
    assert "permission_templates" in provisioning_registry.registered_names
    # M3 closed the last three. Every item docs/01-data-model.md §7 names is
    # now provisioned, so this set is empty — and must stay empty unless the
    # spec itself grows.
    assert provisioning_registry.outstanding_names == frozenset()
    assert provisioning_registry.registered_names == EXPECTED_STEPS


def test_the_registry_refuses_a_step_the_spec_does_not_name() -> None:
    """A step nobody wrote into §7 is how taxonomy gets in the back door."""
    registry = ProvisioningRegistry()

    async def seed_products(session: object, workspace: object) -> None:  # pragma: no cover
        raise AssertionError("never runs")

    with pytest.raises(ValueError, match="Unknown provisioning step"):
        registry.register(ProvisioningStep(name="product_catalogue", run=seed_products))  # type: ignore[arg-type]


async def test_the_creator_becomes_a_licensed_root_member(
    db_session: AsyncSession,
    hasher: PasswordHasherService,
) -> None:
    fixture = await build_workspace(
        db_session,
        hasher,
        name="Solo",
        owner_email="solo@example.com",
    )

    rows = await db_session.execute(select(Membership).where(Membership.workspace_id == fixture.id))
    memberships = list(rows.scalars().all())

    assert len(memberships) == 1
    membership = memberships[0]
    assert membership.has_license, "The creator must be able to log back in"
    assert membership.is_active
    assert membership.template_id == fixture.templates["Root"].id


async def test_create_workspace_endpoint_provisions_and_defaults_locale(
    api: AsyncClient,
    db_session: AsyncSession,
    hasher: PasswordHasherService,
    make_operator: Callable[..., None],
) -> None:
    seed = await build_workspace(
        db_session,
        hasher,
        name="Seed",
        owner_email="creator@example.com",
    )
    make_operator("creator@example.com")
    await login(api, seed.owner)

    response = await api.post(
        "/api/v1/workspaces",
        headers=seed.owner.auth,
        json={
            "name": "Second Workspace",
            "timezone": "America/New_York",
            "currency": "usd",
            "default_country_code": "1",
            "seat_limit": 10,
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["timezone"] == "America/New_York"
    assert body["currency"] == "USD"
    assert body["default_country_code"] == "1"
    assert body["seat_limit"] == 10
    assert body["seats_used"] == 1
    # M2 provisions the four built-in fields and points the workspace at them:
    # Phone identifies a lead, Name and Phone are the H1/H2 headline fields.
    assert body["identity_field_id"] is not None
    assert body["primary_field_1_id"] is not None


async def test_create_workspace_rejects_an_unknown_timezone(
    api: AsyncClient,
    db_session: AsyncSession,
    hasher: PasswordHasherService,
    make_operator: Callable[..., None],
) -> None:
    """The scheduler evaluates cron in this zone from M8; an unknown one would
    fail at 3am rather than at signup."""
    seed = await build_workspace(
        db_session,
        hasher,
        name="Seed TZ",
        owner_email="tz@example.com",
    )
    make_operator("tz@example.com")
    await login(api, seed.owner)

    response = await api.post(
        "/api/v1/workspaces",
        headers=seed.owner.auth,
        json={"name": "Bad TZ", "timezone": "Mars/Olympus_Mons"},
    )
    assert response.status_code == 422


async def test_duplicate_names_get_distinct_slugs(
    api: AsyncClient,
    db_session: AsyncSession,
    hasher: PasswordHasherService,
    make_operator: Callable[..., None],
) -> None:
    """Two customers may legitimately be called the same thing."""
    seed = await build_workspace(
        db_session,
        hasher,
        name="Seed Slug",
        owner_email="slug@example.com",
    )
    make_operator("slug@example.com")
    await login(api, seed.owner)

    first = await api.post(
        "/api/v1/workspaces", headers=seed.owner.auth, json={"name": "Northwind"}
    )
    second = await api.post(
        "/api/v1/workspaces", headers=seed.owner.auth, json={"name": "Northwind"}
    )

    assert first.status_code == second.status_code == 201
    assert first.json()["slug"] == "northwind"
    assert second.json()["slug"] != first.json()["slug"]


def test_slugify_handles_accents_and_punctuation() -> None:
    assert slugify("Açme Corp.") == "acme-corp"
    assert slugify("  Multiple   Spaces  ") == "multiple-spaces"
    assert slugify("!!!").startswith("workspace-")


async def test_creating_a_workspace_requires_authentication(api: AsyncClient) -> None:
    response = await api.post("/api/v1/workspaces", json={"name": "Anonymous"})
    assert response.status_code == 401


def test_root_names_every_access_group() -> None:
    """Root must name all 10 Access groups — a missing one is unfixable.

    `admin_access` grants everything in a group *including capabilities added
    later*, but only for groups the template names at all. Root is
    `is_readonly`, so if it omits a group, no admin can grant it from the UI and
    the endpoints behind it are dead in every workspace ever created.

    That is not hypothetical. M7 shipped task endpoints Root could not call, and
    when M8 looked, `automations`, `reports`, `calling`, `salesform`, `billings`
    and `integrations` were all still missing with M8, M9 and M10 endpoints
    queued behind them.

    This test is the fix. The defaults dict is easy to forget; a red test is not.
    """
    from app.permissions import ACCESS_GROUPS
    from app.services.provisioning import _ROOT_CAPS

    assert set(_ROOT_CAPS) == set(ACCESS_GROUPS), (
        "Root is missing access groups: "
        f"{sorted(set(ACCESS_GROUPS) - set(_ROOT_CAPS))}. Every endpoint gated "
        "on one of these would 403 for Root, in every workspace, unfixably."
    )
    for group, flags in _ROOT_CAPS.items():
        assert flags.get("admin_access") is True, f"Root must hold admin_access on {group}"


def test_admin_names_every_access_group_except_billing() -> None:
    """Admin is the same rule, with one deliberate exception.

    Admin is an editable template, so a workspace that wants its admins in the
    billing screens grants it there. Root always holds billing. Spending money
    is the one capability worth making somebody opt into.
    """
    from app.permissions import ACCESS_GROUPS
    from app.services.provisioning import _ADMIN_CAPS

    assert set(_ADMIN_CAPS) == set(ACCESS_GROUPS) - {"billings"}
