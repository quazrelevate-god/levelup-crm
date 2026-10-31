"""Integration tests against a real Postgres 16 container."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import Base, create_session_factory

pytestmark = pytest.mark.integration


async def test_engine_connects_to_postgres(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT 1"))

    assert result.scalar_one() == 1


async def test_server_is_postgres_16_or_newer(engine: AsyncEngine) -> None:
    """The data model targets Postgres 16.

    CI pins 16 through testcontainers. A developer pointing `TEST_DATABASE_URL`
    at a newer local server is fine — what must not pass silently is an older
    one, which would lack features M2 onward depends on.
    """
    async with engine.connect() as connection:
        version = await connection.scalar(text("SHOW server_version_num"))

    assert int(str(version)) >= 160000, f"Postgres 16+ required, found {version}"


async def test_session_factory_yields_a_working_session(engine: AsyncEngine) -> None:
    session_factory = create_session_factory(engine)

    async with session_factory() as session:
        value = await session.scalar(text("SELECT 42"))

    assert value == 42


#: Tables each milestone is allowed to define. A table appearing before its
#: milestone is the "building ahead" failure mode CLAUDE.md warns about — and
#: if it is a taxonomy table, the worse one. Extend this deliberately, one
#: milestone at a time; never to make a red test green.
M1_TABLES = {
    "users",
    "refresh_tokens",
    "workspaces",
    "permission_templates",
    "memberships",
    "availability_log",
}

M2_TABLES = {
    "lead_fields",
    "field_options",
    # `custom_action_types` lands with M2 because `action_fields` holds a
    # foreign key to it; M3 adds its behaviour, not its table.
    "custom_action_types",
    "action_fields",
    "action_field_options",
    "indexed_fields",
}


M3_TABLES = {
    "stages",
    "lost_reasons",
    "call_dispositions",
}

M4_TABLES = {
    "template_field_grants",
    "template_lead_views",
}

M5_TABLES = {
    "leads",
    "actions",
    "action_attachments",
    "changesets",
    "message_templates",
}

# M6 stores what a list *view* is — the question and the columns — and nothing
# about results. `labels`, `lead_labels` and `tasks` share §6 of the data model
# but belong to M7, so they are deliberately absent here.
M6_TABLES = {
    "saved_filters",
    "table_layouts",
}

# M7 owns the rest of §6's list plus the job table every import and export
# answers with. `outbox_events`, `webhook_endpoints`, `api_keys` and
# `intake_log` share that section but belong to M10.
M7_TABLES = {
    "tasks",
    "labels",
    "lead_labels",
    "import_jobs",
}

# M8 owns §5.2, §5.3 and the scheduled half of §5.5. `dashboards` shares §5.5
# but belongs to M9 — a dashboard is composed and read, a scheduled report is
# rendered and mailed, and only the second needs a scheduler.
M8_TABLES = {
    "sales_groups",
    "sales_group_members",
    "assignment_rules",
    "assignment_cursors",
    "scheduled_reports",
}

# M10 completes §6's list. Landed before M9 because the event bus is the seam
# the planned voice-agent integration attaches to, and dashboards depend on
# nothing here.
M10_TABLES = {
    "api_keys",
    "webhook_endpoints",
    "outbox_events",
    "intake_log",
}

# M9 closes §5.5. `scheduled_reports` shares that section but landed in M8,
# because a schedule needs a scheduler and a dashboard does not. Built after
# M10 — the event bus was the voice integration's dependency and dashboards
# were nobody's.
M9_TABLES = {"dashboards"}

# M11. The workspace model is invite-only, so this table is *the* account
# creation path — and like `users` and `refresh_tokens` it is global rather
# than tenant data, because a person exists across workspaces.
M11_TABLES = {"password_reset_tokens"}

# The voice integration (docs/09-13). Not an M-numbered milestone, and missing
# from this list until the wallet work found it: the per-lead call digest, one
# row per call attempt, and the admin's disposition-to-field mappings.
VOICE_TABLES = {"voice_call_contexts", "voice_call_executions", "voice_extraction_mappings"}

# Prepaid AI-call wallets (docs/14): the balance and its append-only ledger.
BILLING_TABLES = {"wallets", "wallet_transactions", "voice_number_requests"}
#: docs/17: reference details the voice agent is told, chosen by rule.
CALL_FACT_TABLES = {"call_fact_sets"}
#: docs/18: connected apps, what they sent, workflows and their runs.
AUTOMATION_TABLES = {"app_connections", "app_events", "workflows", "workflow_runs"}
#: docs/19: what the platform operator did, across companies. Global, not tenant.
OPERATOR_TABLES = {"operator_actions"}


def test_the_schema_defines_exactly_the_tables_the_landed_milestones_own() -> None:
    """No table exists before the milestone that owns it."""
    assert set(Base.metadata.tables) == (
        M1_TABLES
        | M2_TABLES
        | M3_TABLES
        | M4_TABLES
        | M5_TABLES
        | M6_TABLES
        | M7_TABLES
        | M8_TABLES
        | M10_TABLES
        | M9_TABLES
        | M11_TABLES
        | VOICE_TABLES
        | BILLING_TABLES
        | CALL_FACT_TABLES
        | AUTOMATION_TABLES
        | OPERATOR_TABLES
    )


def test_no_table_name_encodes_a_business_concept() -> None:
    """Tables are product concepts. A customer's taxonomy lives in rows.

    A `courses` or `applications` table would mean the schema had learned
    something about one customer's business.
    """
    forbidden = {"course", "student", "application", "product", "enquiry", "admission"}
    for name in Base.metadata.tables:
        assert not (forbidden & set(name.split("_"))), f"{name} names a business concept"


def test_every_tenant_table_carries_workspace_id() -> None:
    """Architecture rule 1, asserted structurally rather than by review.

    `users`, `workspaces` and `refresh_tokens` are the only legitimate
    exceptions: a user is global, a workspace *is* the tenant, and a refresh
    token belongs to a user across every workspace they hold.
    """
    # Deliberately global, not an oversight: a person and their credentials
    # exist across workspaces, so there is no tenant to scope them to. Adding
    # to this set is a decision — anything holding *customer* data belongs on
    # the other side of it (architecture rule 1).
    global_tables = {
        "users",
        "workspaces",
        "refresh_tokens",
        "password_reset_tokens",
    }

    for name, table in Base.metadata.tables.items():
        if name in global_tables:
            continue
        assert "workspace_id" in table.columns, f"{name} holds tenant data without workspace_id"
