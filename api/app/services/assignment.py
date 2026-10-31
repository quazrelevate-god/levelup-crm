"""The assignment engine (M8).

`04-feature-coverage.md`: *"A telecalling CRM without automatic distribution is
a spreadsheet."* This module is that distribution. It answers one question —
**who should this lead go to?** — and it is the only thing in the codebase that
answers it, because it is called from every create path there is.

Four decisions are worth reading before changing anything here.

**The cursor is locked, not read.** Round-robin state lives in one row per rule.
The obvious implementation reads the row, computes the next member, and writes
it back; under two concurrent inserts both transactions read position 4 and both
hand the lead to the fifth rep, and the sixth rep gets nothing. The fix is a row
lock — `SELECT ... FOR UPDATE` — which serialises the pair. This is invisible in
any single-threaded test, so `test_assignment.py` runs a genuinely concurrent
one. It is the single most likely defect in this milestone.

**Rules are the M6 filter DSL, evaluated in Postgres.** Not a second condition
language, and not a Python re-implementation of the first. The engine compiles
each rule to the same SQL a saved view compiles to and asks it about one lead
id. That means a history predicate — "no call logged in 7 days" — works in an
assignment rule for free, and means there is exactly one place where a filter
can be wrong.

**Rules compile with system grants, not the caller's.** A rule authored by an
admin routes on whatever fields the admin chose. If it compiled against the
grants of whoever happened to create the lead, a rule on Budget would silently
stop matching for a caller who cannot see Budget, and identical leads would
route differently depending on who typed them in. Routing is not a read of
customer data by that caller — nothing about the rule's fields reaches them —
so the correct projection is the system's.

**Candidates must be able to log in.** `skip_unavailable` is the documented
switch and it filters on `availability`, but an unlicensed member cannot log in
at all, so assigning to one buries the lead where nobody will ever see it.
Unlicensed members are skipped unconditionally, whatever the flag says.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.sql.elements import ColumnElement

from app.errors import api_error
from app.filters.compiler import FilterCompiler
from app.filters.dsl import FilterNode, validate_shape
from app.models import (
    AssignmentCursor,
    AssignmentRule,
    AssignmentStrategy,
    AvailabilityStatus,
    Lead,
    LeadField,
    Membership,
    SalesGroup,
    SalesGroupMember,
    Workspace,
)
from app.permissions import FieldGrants, FieldProjectionService
from app.tenancy.session import ScopedSession

__all__ = ["AssignmentEngine", "AssignmentOutcome", "validate_strategy_config"]

_FILTER_ADAPTER: TypeAdapter[FilterNode] = TypeAdapter(FilterNode)

#: Weights are bounded so a typo cannot expand the deal list without limit.
MAX_WEIGHT = 100


@dataclass(frozen=True, slots=True)
class AssignmentOutcome:
    """What the engine decided, and why.

    Carries the rule as well as the member so the preview endpoint and the
    timeline can both say *which* rule fired. "Assigned to Priya" is not
    debuggable; "rule 'Website leads' assigned to Priya" is.
    """

    rule_id: uuid.UUID | None
    rule_name: str | None
    membership_id: uuid.UUID | None
    #: Machine-readable, for the preview UI: matched | no_rule_matched |
    #: no_candidates | rule_assigns_nobody
    reason: str


def validate_strategy_config(
    strategy: AssignmentStrategy, config: dict[str, Any]
) -> dict[str, Any]:
    """Check a rule's `config` against its strategy, on write.

    The column is loose JSONB because each strategy reads different keys; that
    looseness has to be paid for somewhere, and the cheapest place is here — at
    the moment an admin saves the rule, rather than at 3am when a lead arrives
    and the engine finds `group_id` missing.
    """
    match strategy:
        case AssignmentStrategy.ROUND_ROBIN:
            members = config.get("membership_ids")
            if not isinstance(members, list) or not members:
                raise api_error(
                    422, "invalid_config", "Round-robin needs a non-empty `membership_ids` list"
                )
            return {"membership_ids": [str(m) for m in members]}

        case AssignmentStrategy.WEIGHTED:
            rows = config.get("members")
            if not isinstance(rows, list) or not rows:
                raise api_error(
                    422,
                    "invalid_config",
                    "Weighted assignment needs a non-empty `members` list of "
                    "`{membership_id, weight}`",
                )
            cleaned: list[dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, dict) or "membership_id" not in row:
                    raise api_error(422, "invalid_config", "Each member needs a `membership_id`")
                weight = int(row.get("weight", 1))
                if not 1 <= weight <= MAX_WEIGHT:
                    raise api_error(
                        422, "invalid_config", f"Weight must be between 1 and {MAX_WEIGHT}"
                    )
                cleaned.append({"membership_id": str(row["membership_id"]), "weight": weight})
            return {"members": cleaned}

        case AssignmentStrategy.SALES_GROUP:
            if not config.get("group_id"):
                raise api_error(422, "invalid_config", "Sales-group assignment needs a `group_id`")
            return {"group_id": str(config["group_id"])}

        case AssignmentStrategy.FIELD_VALUE:
            field_key = config.get("field_key")
            mapping = config.get("map")
            if not field_key or not isinstance(mapping, dict) or not mapping:
                raise api_error(
                    422,
                    "invalid_config",
                    "Field-value assignment needs a `field_key` and a non-empty `map`",
                )
            return {
                "field_key": str(field_key),
                "map": {str(k): str(v) for k, v in mapping.items()},
                # Optional: who gets a lead whose value is not in the map.
                **({"fallback": str(config["fallback"])} if config.get("fallback") else {}),
            }

        case AssignmentStrategy.FIXED:
            if not config.get("membership_id"):
                raise api_error(422, "invalid_config", "Fixed assignment needs a `membership_id`")
            return {"membership_id": str(config["membership_id"])}

        case AssignmentStrategy.UNASSIGNED:
            return {}

    raise api_error(422, "invalid_strategy", f"{strategy} is not a strategy")  # pragma: no cover


class AssignmentEngine:
    """Evaluates a workspace's rules against one lead.

    Constructed per request. Caches the rule list and the member roster, since
    an import assigning 5,000 leads would otherwise re-read both 5,000 times.
    """

    def __init__(self, session: ScopedSession, *, workspace: Workspace) -> None:
        self._session = session
        self._workspace = workspace
        self._rules: list[AssignmentRule] | None = None
        self._members: dict[uuid.UUID, Membership] | None = None
        self._compiler: FilterCompiler | None = None

    # --- loading -----------------------------------------------------------

    async def _load_rules(self) -> list[AssignmentRule]:
        if self._rules is None:
            rows = await self._session.execute(
                self._session.select(AssignmentRule)
                .where(AssignmentRule.is_active.is_(True))
                # `id` breaks ties, so two rules sharing a priority always
                # evaluate in the same order rather than whatever the planner
                # felt like — a lead must not route differently on a re-run.
                .order_by(AssignmentRule.priority, AssignmentRule.id)
            )
            self._rules = list(rows.scalars().all())
        return self._rules

    async def _load_members(self) -> dict[uuid.UUID, Membership]:
        if self._members is None:
            rows = await self._session.execute(self._session.select(Membership))
            self._members = {m.id: m for m in rows.scalars().all()}
        return self._members

    async def _load_compiler(self) -> FilterCompiler:
        """A compiler with system grants. See the module docstring."""
        if self._compiler is None:
            rows = await self._session.execute(
                self._session.select(LeadField).order_by(LeadField.sort_order)
            )
            fields = list(rows.scalars().all())
            every = frozenset(field.key for field in fields)
            grants = FieldGrants(view=every, edit=every, import_=every, export=every, is_admin=True)
            self._compiler = FilterCompiler(
                fields=fields,
                projection=FieldProjectionService(grants),
                timezone=self._workspace.timezone,
            )
        return self._compiler

    # --- matching ----------------------------------------------------------

    def _parse(self, conditions: Any) -> FilterNode | None:
        """A rule's conditions, or None for the catch-all."""
        if not conditions:
            return None
        try:
            node = _FILTER_ADAPTER.validate_python(conditions)
            validate_shape(node)
        except (ValidationError, ValueError) as exc:
            raise api_error(
                422, "invalid_rule_conditions", f"Rule conditions are not a valid filter: {exc}"
            ) from exc
        return node

    async def match(self, lead_id: uuid.UUID) -> AssignmentRule | None:
        """The first rule this lead matches, in priority order.

        Every rule's predicate is evaluated in **one** query. The alternative —
        a round trip per rule until one hits — costs a workspace with 20 rules
        up to 20 round trips per lead, which an import of 50,000 leads would
        feel. Postgres evaluates 20 booleans over one row without noticing.
        """
        rules = await self._load_rules()
        if not rules:
            return None

        compiler = await self._load_compiler()
        columns: list[ColumnElement[bool]] = []
        catch_all: AssignmentRule | None = None
        conditional: list[AssignmentRule] = []

        for rule in rules:
            node = self._parse(rule.conditions)
            if node is None:
                # A catch-all matches without asking the database. Recorded in
                # priority order so it can still lose to an earlier rule.
                if catch_all is None:
                    catch_all = rule
                continue
            conditional.append(rule)
            columns.append(compiler.compile(node).label(f"r{len(columns)}"))

        if not columns:
            return catch_all

        row = (
            await self._session.execute(
                select(*columns)
                .select_from(Lead)
                .where(Lead.id == lead_id, Lead.workspace_id == self._session.workspace_id)
            )
        ).one_or_none()
        if row is None:  # pragma: no cover - the lead was just inserted
            return catch_all

        matched = {rule.id for rule, hit in zip(conditional, row, strict=True) if hit}
        for rule in rules:  # priority order, so the catch-all competes fairly
            if rule.id in matched or (catch_all is not None and rule.id == catch_all.id):
                return rule
        return None

    # --- candidate resolution ---------------------------------------------

    async def _eligible(self, membership_id: uuid.UUID, *, skip_unavailable: bool) -> bool:
        members = await self._load_members()
        member = members.get(membership_id)
        if member is None or not member.is_active:
            return False
        # Unconditional: an unlicensed member cannot log in, so a lead assigned
        # to one is a lead nobody will ever call.
        if not member.has_license:
            return False
        # The platform operator inside to help (docs/19) is never sent work.
        if member.is_support:
            return False
        return not (skip_unavailable and member.availability is not AvailabilityStatus.WORKING)

    async def _candidates(self, rule: AssignmentRule) -> list[uuid.UUID]:
        """The deal list for a rule, weights expanded, ineligible removed."""
        config = rule.config or {}
        pairs: list[tuple[uuid.UUID, int]] = []

        if rule.strategy is AssignmentStrategy.ROUND_ROBIN:
            pairs = [(uuid.UUID(m), 1) for m in config.get("membership_ids", [])]

        elif rule.strategy is AssignmentStrategy.WEIGHTED:
            pairs = [
                (uuid.UUID(row["membership_id"]), int(row.get("weight", 1)))
                for row in config.get("members", [])
            ]

        elif rule.strategy is AssignmentStrategy.SALES_GROUP:
            group_id = config.get("group_id")
            if not group_id:
                return []
            rows = await self._session.execute(
                self._session.select(SalesGroupMember)
                .join(SalesGroup, SalesGroup.id == SalesGroupMember.group_id)
                .where(
                    SalesGroupMember.group_id == uuid.UUID(str(group_id)),
                    SalesGroup.is_archived.is_(False),
                )
                # Deterministic, so the cursor means the same thing next time.
                .order_by(SalesGroupMember.membership_id)
            )
            pairs = [(row.membership_id, row.weight) for row in rows.scalars().all()]

        deal: list[uuid.UUID] = []
        for membership_id, weight in pairs:
            if await self._eligible(membership_id, skip_unavailable=rule.skip_unavailable):
                deal.extend([membership_id] * max(1, min(int(weight), MAX_WEIGHT)))
        return deal

    async def _next_by_cursor(self, rule: AssignmentRule, deal: Sequence[uuid.UUID]) -> uuid.UUID:
        """Advance the rule's cursor under a row lock and return the pick.

        The lock is the whole point of the table. See the module docstring.
        """
        # Create the row if this rule has never fired. `ON CONFLICT DO NOTHING`
        # rather than a get-or-create, because two first-ever leads racing here
        # would otherwise both insert and one would fail the primary key.
        await self._session.execute(
            pg_insert(AssignmentCursor)
            .values(
                workspace_id=self._session.workspace_id,
                rule_id=rule.id,
                position=-1,
            )
            .on_conflict_do_nothing(index_elements=["rule_id"])
        )

        cursor = (
            await self._session.execute(
                self._session.select(AssignmentCursor)
                .where(AssignmentCursor.rule_id == rule.id)
                .with_for_update()
            )
        ).scalar_one()

        position = (cursor.position + 1) % len(deal)
        picked: uuid.UUID = deal[position]
        cursor.position = position
        cursor.last_membership_id = picked
        await self._session.flush()
        return picked

    async def _resolve(self, rule: AssignmentRule) -> uuid.UUID | None:
        """Who this rule picks, or None if it picks nobody."""
        config = rule.config or {}

        if rule.strategy is AssignmentStrategy.UNASSIGNED:
            return None

        if rule.strategy is AssignmentStrategy.FIXED:
            raw = config.get("membership_id")
            if not raw:
                return None
            membership_id = uuid.UUID(str(raw))
            # A fixed rule names one person. If that person is on leave the
            # lead is left unassigned rather than quietly given to somebody the
            # admin did not name.
            eligible = await self._eligible(membership_id, skip_unavailable=rule.skip_unavailable)
            return membership_id if eligible else None

        if rule.strategy is AssignmentStrategy.FIELD_VALUE:
            return await self._resolve_field_value(rule)

        deal = await self._candidates(rule)
        if not deal:
            return None
        return await self._next_by_cursor(rule, deal)

    async def _resolve_field_value(self, rule: AssignmentRule) -> uuid.UUID | None:
        config = rule.config or {}
        lead = self._lead_under_evaluation
        if lead is None:  # pragma: no cover - set by every caller
            return None
        raw = (lead.values or {}).get(str(config.get("field_key")))
        mapped = config.get("map", {}).get(str(raw)) if raw is not None else None
        if mapped is None:
            mapped = config.get("fallback")
        if not mapped:
            return None
        membership_id = uuid.UUID(str(mapped))
        eligible = await self._eligible(membership_id, skip_unavailable=rule.skip_unavailable)
        return membership_id if eligible else None

    _lead_under_evaluation: Lead | None = None

    # --- entry point -------------------------------------------------------

    async def decide(self, lead: Lead) -> AssignmentOutcome:
        """Who this lead should go to. Assigns nothing; the caller writes.

        Split from writing so the preview endpoint and the create path can share
        one implementation — a dry run that took a different code path would not
        be a preview of anything.
        """
        self._lead_under_evaluation = lead
        try:
            rule = await self.match(lead.id)
            if rule is None:
                return AssignmentOutcome(None, None, None, "no_rule_matched")

            membership_id = await self._resolve(rule)
            if membership_id is None:
                reason = (
                    "rule_assigns_nobody"
                    if rule.strategy is AssignmentStrategy.UNASSIGNED
                    else "no_candidates"
                )
                return AssignmentOutcome(rule.id, rule.name, None, reason)

            return AssignmentOutcome(rule.id, rule.name, membership_id, "matched")
        finally:
            self._lead_under_evaluation = None
