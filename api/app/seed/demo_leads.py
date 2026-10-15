"""Fill an existing workspace's lead table for a demo.

Unlike `app.seed.demo`, this invents **no taxonomy at all**. It reads the
workspace's own field definitions, stages, lost reasons and members, and
generates values that fit them — so a workspace whose admin called a stage
"Busy" gets leads in "Busy", and a field the admin never created is never
populated. That is the rule CLAUDE.md gives its own section, and it is the
whole reason this is a separate module from the one that builds Northwind.

Purely additive: it inserts new leads and touches no existing row. The leads it
writes carry no timeline, because an action belongs to a changeset and inventing
history somebody did not perform is a different claim from "here are some leads".

    python -m app.seed.demo_leads --workspace-name 'LevelUp Learning' --count 250
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import random
import sys
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.config import Settings
from app.db import create_engine, create_session_factory
from app.models.field import FieldOption, LeadField
from app.models.lead import Lead
from app.models.pipeline import LostReason, Stage
from app.models.workspace import Membership, Workspace
from app.tenancy.session import bind_scope

__all__ = ["DemoLeadSeeder", "LeadSeedResult"]

_BATCH = 2_000
_HISTORY_DAYS = 240
#: A fixture where every field is populated hides every null-handling bug.
_SPARSITY = 0.28

_FIRST = (
    "Aarav",
    "Ananya",
    "Rohan",
    "Meera",
    "Vikram",
    "Priya",
    "Karthik",
    "Divya",
    "Arjun",
    "Nithya",
    "Sanjay",
    "Lakshmi",
    "Harish",
    "Swetha",
    "Imran",
    "Fatima",
    "Joel",
    "Rhea",
    "Mahesh",
    "Kavya",
    "Sundar",
    "Anita",
    "Varun",
    "Pooja",
)
_LAST = (
    "Sharma",
    "Iyer",
    "Fernandes",
    "Menon",
    "Reddy",
    "Nair",
    "Gupta",
    "Pillai",
    "Rao",
    "Banerjee",
    "Desai",
    "Krishnan",
    "Joseph",
    "Shetty",
    "Das",
    "Varma",
)
_AREAS = ("Adyar", "Velachery", "Anna Nagar", "T Nagar", "Mylapore", "Guindy")

_LEAD_COLUMNS = (
    "id",
    "workspace_id",
    "identity_value",
    "values",
    "stage_id",
    "lost_reason_id",
    "assignee_id",
    "rating",
    "score",
    "last_action_at",
    "created_at",
    "updated_at",
    "created_by_id",
    "deleted_at",
)


@dataclass(frozen=True, slots=True)
class LeadSeedResult:
    workspace_id: uuid.UUID
    workspace_name: str
    created: int
    stages_used: int
    fields_filled: int


class DemoLeadSeeder:
    def __init__(
        self,
        session: AsyncSession,
        engine: AsyncEngine,
        *,
        workspace_name: str,
        count: int = 250,
        seed: int = 7,
    ) -> None:
        self._session = session
        self._engine = engine
        self._workspace_name = workspace_name
        self._count = count
        self._random = random.Random(seed)
        self._now = dt.datetime.now(dt.UTC)

    async def run(self) -> LeadSeedResult:
        workspace = await self._workspace()
        bind_scope(self._session, workspace.id)
        try:
            fields = list(
                (
                    await self._session.execute(
                        # Every field this workspace defines. A hidden one is
                        # still part of its schema, and the projection layer is
                        # what decides who sees it.
                        select(LeadField).where(LeadField.workspace_id == workspace.id)
                    )
                ).scalars()
            )
            options: dict[uuid.UUID, list[FieldOption]] = {}
            for option in (
                await self._session.execute(
                    select(FieldOption).where(FieldOption.workspace_id == workspace.id)
                )
            ).scalars():
                options.setdefault(option.field_id, []).append(option)

            stages = list(
                (
                    await self._session.execute(
                        select(Stage).where(
                            Stage.workspace_id == workspace.id,
                            Stage.is_archived.is_(False),
                        )
                    )
                ).scalars()
            )
            if not stages:
                raise RuntimeError(f"{workspace.name!r} has no stages to put leads in")

            lost = list(
                (
                    await self._session.execute(
                        select(LostReason).where(LostReason.workspace_id == workspace.id)
                    )
                ).scalars()
            )
            members = list(
                (
                    await self._session.execute(
                        select(Membership).where(Membership.workspace_id == workspace.id)
                    )
                ).scalars()
            )
            if not members:
                raise RuntimeError(f"{workspace.name!r} has no members to assign leads to")

            identity_key = await self._identity_key(workspace, fields)
            start = await self._next_index(workspace.id)
            created = await self._generate(
                workspace, fields, options, stages, lost, members, identity_key, start
            )
        finally:
            bind_scope(self._session, None)

        return LeadSeedResult(
            workspace_id=workspace.id,
            workspace_name=workspace.name,
            created=created,
            stages_used=len(stages),
            fields_filled=len([f for f in fields if not f.is_builtin]),
        )

    async def _workspace(self) -> Workspace:
        rows = await self._session.execute(select(Workspace))
        workspaces = list(rows.scalars())
        match = [w for w in workspaces if w.name == self._workspace_name]
        if not match:
            names = ", ".join(repr(w.name) for w in workspaces) or "(none)"
            raise RuntimeError(
                f"no workspace named {self._workspace_name!r}; this database has {names}"
            )
        return match[0]

    async def _identity_key(self, workspace: Workspace, fields: list[LeadField]) -> str:
        """Whichever field this workspace designated unique — often phone."""
        for field in fields:
            if getattr(field, "id", None) == getattr(workspace, "identity_field_id", None):
                return field.key
        return "phone"

    async def _next_index(self, workspace_id: uuid.UUID) -> int:
        """Start past anything already seeded, so identity values stay unique."""
        rows = await self._session.execute(
            select(func.count()).select_from(Lead).where(Lead.workspace_id == workspace_id)
        )
        return int(rows.scalar_one()) + 1000

    def _maybe(self, value: Any) -> Any:
        return None if self._random.random() < _SPARSITY else value

    def _values_for(
        self,
        fields: list[LeadField],
        options: dict[uuid.UUID, list[FieldOption]],
        name: str,
        phone: str,
        identity_key: str,
    ) -> dict[str, Any]:
        """Shapes match `app.fields.registry`, not guesses — a fixture with the
        wrong sub-keys makes the filter compiler look broken when it is reading
        exactly what it was told to."""
        rng = self._random
        first, _, last = name.partition(" ")
        values: dict[str, Any] = {}

        for field in fields:
            key = field.key
            if key == identity_key:
                values[key] = phone
                continue
            choices = options.get(field.id, [])
            match field.field_type.value:
                case "TEXT":
                    values[key] = self._maybe(name if key == "name" else f"{first} {last}")
                case "DROPDOWN":
                    values[key] = self._maybe(rng.choice(choices).code) if choices else None
                case "TAGS":
                    if choices:
                        picked = rng.sample(choices, k=rng.randint(1, min(3, len(choices))))
                        values[key] = self._maybe([o.code for o in picked])
                case "EMAIL":
                    values[key] = self._maybe(f"{first.lower()}.{last.lower()}@example.com")
                case "PHONE":
                    values[key] = self._maybe(f"+9198{rng.randint(0, 99_999_999):08d}")
                case "CHECKBOX":
                    values[key] = self._maybe(rng.random() < 0.35)
                case "DATE":
                    day = self._now.date() - dt.timedelta(days=rng.randint(-90, _HISTORY_DAYS))
                    values[key] = self._maybe(day.isoformat())
                case "MONEY":
                    values[key] = self._maybe(
                        {"amount": f"{rng.randrange(5_000, 120_000, 500)}.00", "currency": "INR"}
                    )
                case "NUMBER":
                    values[key] = self._maybe(rng.randint(6, 12))
                case "WEBSITE":
                    values[key] = self._maybe("https://example.com/enquiry")
                case "DEPENDENT_DROPDOWN":
                    children = [o for o in choices if o.parent_option_id is not None]
                    if children:
                        child = rng.choice(children)
                        parent = next((o for o in choices if o.id == child.parent_option_id), None)
                        values[key] = self._maybe(
                            {"value": child.code, "parent": parent.code if parent else None}
                        )
                case "RECURRING_DATE":
                    values[key] = self._maybe(
                        {
                            "start": (
                                self._now.date() - dt.timedelta(days=rng.randint(0, 300))
                            ).isoformat(),
                            "frequency": "MONTHLY",
                            "interval": 1,
                            "next": (
                                self._now.date() + dt.timedelta(days=rng.randint(0, 60))
                            ).isoformat(),
                        }
                    )
                case "LOCATION":
                    values[key] = self._maybe(
                        {"city": "Chennai", "state": "Tamil Nadu", "line1": rng.choice(_AREAS)}
                    )

        if "name" not in values:
            values["name"] = name
        return {k: v for k, v in values.items() if v is not None}

    async def _generate(
        self,
        workspace: Workspace,
        fields: list[LeadField],
        options: dict[uuid.UUID, list[FieldOption]],
        stages: list[Stage],
        lost: list[LostReason],
        members: list[Membership],
        identity_key: str,
        start: int,
    ) -> int:
        rng = self._random
        lost_stage_ids = {s.id for s in stages if getattr(s.kind, "value", "") == "LOST"}
        rows: list[tuple[Any, ...]] = []
        written = 0

        for offset in range(self._count):
            index = start + offset
            phone = f"+9197{index:08d}"
            name = f"{rng.choice(_FIRST)} {rng.choice(_LAST)}"
            created_at = self._now - dt.timedelta(
                days=rng.randint(0, _HISTORY_DAYS), minutes=rng.randint(0, 1440)
            )
            last_at = created_at + dt.timedelta(hours=rng.randint(1, 72))
            if last_at > self._now:
                last_at = self._now
            stage = rng.choice(stages)
            # Unassigned sometimes, so the "Unassigned" quick filter has rows.
            assignee = rng.choice(members).id if rng.random() < 0.82 else None

            rows.append(
                (
                    uuid.uuid4(),
                    workspace.id,
                    phone,
                    json.dumps(self._values_for(fields, options, name, phone, identity_key)),
                    stage.id,
                    rng.choice(lost).id if stage.id in lost_stage_ids and lost else None,
                    assignee,
                    rng.randint(1, 5) if rng.random() < 0.55 else None,
                    0,
                    last_at,
                    created_at,
                    last_at,
                    members[0].id,
                    None,
                )
            )
            written += 1

            if len(rows) >= _BATCH:
                await self._copy(rows)
                rows = []

        if rows:
            await self._copy(rows)
        return written

    async def _copy(self, rows: list[tuple[Any, ...]]) -> None:
        """`COPY` reaches past SQLAlchemy to asyncpg: it is a protocol feature,
        not a statement, so there is no ORM equivalent."""
        async with self._engine.begin() as connection:
            raw = await connection.get_raw_connection()
            driver = raw.driver_connection
            assert driver is not None, "COPY needs the asyncpg connection"
            await driver.copy_records_to_table("leads", records=rows, columns=list(_LEAD_COLUMNS))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.seed.demo_leads",
        description="Add demo leads to an existing workspace, using its own schema.",
    )
    parser.add_argument("--workspace-name", required=True, help="Named exactly, to avoid mistakes")
    parser.add_argument("--count", type=int, default=250)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--allow-non-local",
        action="store_true",
        help="Run against a non-local deployment.",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    settings = Settings()
    if not settings.is_local and not args.allow_non_local:
        print(
            f"refusing: ENVIRONMENT is {settings.environment!r}. "
            "Pass --allow-non-local if you mean it.",
            file=sys.stderr,
        )
        return 2

    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            result = await DemoLeadSeeder(
                session,
                engine,
                workspace_name=args.workspace_name,
                count=args.count,
                seed=args.seed,
            ).run()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()

    print(
        f"added {result.created} leads to {result.workspace_name!r} "
        f"across {result.stages_used} stages, filling {result.fields_filled} custom fields"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
