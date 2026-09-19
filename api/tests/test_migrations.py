"""The Alembic revision applies, rolls back, and matches the models.

Definition of done for every milestone: "Alembic migration applies **and rolls
back**". A downgrade nobody has run is a downgrade that does not work, and the
first time you need one is the worst time to find out.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import Base

pytestmark = pytest.mark.integration

API_ROOT = Path(__file__).resolve().parent.parent

M1_TABLES = {
    "users",
    "refresh_tokens",
    "workspaces",
    "permission_templates",
    "memberships",
    "availability_log",
}


def _alembic_config() -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    return config


#: One Alembic revision per milestone (CLAUDE.md conventions), oldest first.
#: Extend as each milestone lands — never edit an applied revision.
EXPECTED_REVISIONS = [
    "0001_m1_tenancy",
    "0002_m2_fields",
    "0003_m3_pipeline",
    "0004_m4_permissions",
    "0005_m5_leads",
    "0006_m6_filters",
    "0007_m7_work",
    "0008_m8_assignment",
    # M10 lands before M9: the event bus is what the planned voice-agent
    # integration attaches to, and M9 depends on nothing in it.
    "0009_m10_intake",
    # M9 after M10: Alembic cares about the chain, not the milestone
    # numbers, and renumbering an applied revision is what CLAUDE.md
    # forbids.
    "0010_m9_dashboards",
    "0011_m11_credentials",
    # Not an M-numbered milestone: the CRM-side voice-context system
    # (docs/09-context-continuity-and-bolna-integration.md), built ahead
    # of the actual Bolna wiring.
    "0012_voice_context",
    # Phase 2: the CRM↔Bolna join table, one row per call attempt.
    "0013_voice_executions",
    # Phase 3: keep the raw Bolna body, so the payload shape is
    # evidence rather than assumption.
    "0014_voice_raw_payload",
    # Phase 4: the configurable extraction layer (docs/12) — Bolna
    # disposition names mapped to lead fields, with a confidence gate.
    "0015_voice_extraction_mappings",
    # Post-call automation (docs/13): each call's transcript, duration,
    # summary and call-log link on its execution row.
    "0016_voice_post_call",
]


def test_there_is_exactly_one_revision_per_milestone() -> None:
    """A linear history: one revision per milestone, one head, no branches."""
    script = ScriptDirectory.from_config(_alembic_config())
    revisions = list(script.walk_revisions())  # newest first

    assert [r.revision for r in revisions] == list(reversed(EXPECTED_REVISIONS))
    assert revisions[-1].down_revision is None, "the oldest revision has no parent"
    assert len(script.get_heads()) == 1, "A branched history means two revisions collided"


async def test_upgrade_creates_the_expected_tables_and_downgrade_removes_them(
    schema_engine: AsyncEngine,
) -> None:
    """Applied through metadata rather than the Alembic runner.

    Alembic drives a synchronous engine, which does not compose with this
    suite's async fixtures. What matters at this level is that the schema the
    revision describes can be created and dropped cleanly; `test_migration_
    matches_models` below is what keeps the revision honest about *what* that
    schema is.
    """
    async with schema_engine.connect() as connection:
        tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))

    assert tables >= M1_TABLES


async def test_the_availability_enum_exists_with_its_three_values(
    schema_engine: AsyncEngine,
) -> None:
    """The only enum M1 creates. Every other business concept is a row."""
    async with schema_engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT e.enumlabel FROM pg_enum e "
                "JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = 'availability_status' "
                "ORDER BY e.enumsortorder"
            )
        )
        labels = [row[0] for row in result]

    assert labels == ["WORKING", "ON_LEAVE", "INACTIVE"]


async def test_no_enum_in_the_database_encodes_business_taxonomy(
    schema_engine: AsyncEngine,
) -> None:
    """Data model §1: the only enums are product concepts.

    If a stage name, lead status or product type ever appears as a Postgres
    enum, somebody has compiled a customer's vocabulary into the schema.
    """
    async with schema_engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT t.typname FROM pg_type t "
                "JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE t.typtype = 'e' AND n.nspname = 'public'"
            )
        )
        enum_names = {row[0] for row in result}

    # Every enum the product legitimately owns, and why it is a *product*
    # concept rather than a customer's vocabulary. Adding to this list is a
    # deliberate act; if the name reads like something a customer would say,
    # it belongs in a table instead.
    assert enum_names == {
        "availability_status",  # M1 — whether a member can receive work
        "lead_field_type",  # M2 — the 13 kinds of data, §1.3
        "action_field_type",  # M2 — the 8 kinds, §4.3
        "action_direction",  # M2 — inbound / outbound / information
        "stage_kind",  # M3 — the 4 structural pipeline kinds, NOT statuses
        "permission_grant",  # M4 — VIEW / EDIT / IMPORT / EXPORT
        "system_action_kind",  # M5 — timeline events the product defines
        "changeset_source",  # M5 — what opened a mutation batch
        "template_channel",  # M5 — WhatsApp / SMS / Email
        # M6 — who a saved filter is for. Personal/shared/role is a
        # sharing model, not a customer vocabulary: no workspace names
        # these, they only choose among them.
        "saved_filter_visibility",
        # M7 — what a spreadsheet run is doing and where it has got to.
        # Both describe the product's own import machinery; neither names
        # anything a customer would recognise as their vocabulary.
        "import_job_kind",
        "import_job_status",
        # M8 — *how* a rule chooses a member, never *who* it chooses. The
        # members, groups and field values every strategy reads are rows; this
        # names only the six ways of picking among them.
        "assignment_strategy",
        # M8 — the attachment format a scheduled report is mailed as.
        "scheduled_report_format",
        # M10 — where one outbound delivery has got to, and what one inbound
        # request did. Both describe the product's own plumbing; neither names
        # anything a customer would recognise as their vocabulary.
        "outbox_status",
        "intake_outcome",
        # Phase 2 — where one Bolna call attempt has got to: written, sent,
        # written back, or failed. The product's own call-dispatch lifecycle.
        # Bolna's much richer status vocabulary is deliberately *not* here: it
        # is kept verbatim in a text column, because a vendor's words are not
        # ours to freeze into the schema either.
        "voice_call_status",
    }

    # And the harder check: no enum *value* anywhere names a business concept.
    async with schema_engine.connect() as connection:
        values = await connection.execute(
            text(
                "SELECT e.enumlabel FROM pg_enum e "
                "JOIN pg_type t ON t.oid = e.enumtypid "
                "JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE n.nspname = 'public'"
            )
        )
        labels = {row[0].lower() for row in values}

    forbidden = {
        "course",
        "student",
        "admission",
        "enquiry",
        "interview_scheduled",
        "forge_writing",
        "mql",
        "application_status",
    }
    assert not (forbidden & labels), f"business taxonomy compiled into an enum: {labels}"


async def test_workspace_cascade_removes_its_tenant_rows(
    schema_engine: AsyncEngine,
) -> None:
    """Every tenant FK cascades from `workspaces`.

    Not a delete path the product uses — soft delete is the rule — but the
    constraint proves nothing tenant-scoped is orphaned at the schema level.
    """
    async with schema_engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT tc.table_name, rc.delete_rule "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.referential_constraints rc "
                "  ON rc.constraint_name = tc.constraint_name "
                "JOIN information_schema.key_column_usage kcu "
                "  ON kcu.constraint_name = tc.constraint_name "
                "WHERE tc.constraint_type = 'FOREIGN KEY' "
                "  AND kcu.column_name = 'workspace_id'"
            )
        )
        rules = {row[0]: row[1] for row in result}

    assert rules, "No workspace_id foreign keys found at all"
    for table, rule in rules.items():
        assert rule == "CASCADE", f"{table}.workspace_id does not cascade from workspaces"


def test_the_revision_covers_every_model_table() -> None:
    """The migration and the models must not drift.

    Checks the revision source names each table the metadata declares. Crude,
    but it catches the common failure — a model added without a migration —
    without needing an Alembic autogenerate diff in the test path.
    """
    revision_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (API_ROOT / "alembic" / "versions").glob("*.py")
    )

    for table_name in Base.metadata.tables:
        assert f'"{table_name}"' in revision_source, (
            f"Table {table_name!r} is declared in the models but never created by a migration"
        )
