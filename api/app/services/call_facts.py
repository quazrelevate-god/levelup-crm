"""Call facts: which reference details the agent is told about a lead (docs/17).

A workspace keeps sets of facts — "VE Batch 6: starts 12 December, mentors A
and B" — each with a rule in the filter DSL. On every call the first active set
whose rule matches the lead, lowest priority number first, supplies its facts.
The rule check is the assignment engine's: every set's rule is one boolean in a
single query over the lead's row, so twenty sets cost one round trip.

Fact keys are the names the agent's script can use (`{{batch_start_date}}`).
They come from the label and must not collide with a lead field's key or the
CRM's reserved `crm_*` names, since all of them share one namespace in a call.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Sequence
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.sql.elements import ColumnElement

from app.errors import api_error, conflict, not_found
from app.fields.values import slugify_key
from app.filters.compiler import FilterCompiler
from app.filters.dsl import FilterNode, describe, validate_shape
from app.models.call_facts import CallFactSet
from app.models.field import LeadField
from app.models.lead import Lead
from app.models.workspace import Workspace
from app.permissions import FieldGrants, FieldProjectionService
from app.tenancy.session import ScopedSession

__all__ = ["MAX_FACTS", "CallFactService", "FactInput", "MatchedFacts"]

_FILTER_ADAPTER: TypeAdapter[FilterNode] = TypeAdapter(FilterNode)
MAX_FACTS = 30
#: Names the CRM already sends on every call; a fact cannot take them.
_RESERVED_EXACT = frozenset({"call_opening"})


@dataclasses.dataclass(frozen=True, slots=True)
class FactInput:
    label: str
    value: str


@dataclasses.dataclass(frozen=True, slots=True)
class MatchedFacts:
    """The set a lead matched, and its facts as `{key: value}` in order."""

    fact_set: CallFactSet
    values: dict[str, str]
    #: `(label, value)` pairs, for the agent's readable lead notes.
    lines: list[tuple[str, str]]


class CallFactService:
    def __init__(self, session: ScopedSession, *, workspace: Workspace) -> None:
        self._session = session
        self._workspace = workspace
        self._fields: list[LeadField] | None = None

    # --- reading ---------------------------------------------------------------

    async def list_sets(self, *, include_archived: bool = False) -> Sequence[CallFactSet]:
        statement = self._session.select(CallFactSet).order_by(CallFactSet.priority, CallFactSet.id)
        if not include_archived:
            statement = statement.where(CallFactSet.is_active.is_(True))
        rows = await self._session.execute(statement)
        sets: Sequence[CallFactSet] = rows.scalars().all()
        return sets

    async def get(self, fact_set_id: uuid.UUID) -> CallFactSet:
        row = await self._session.get(CallFactSet, fact_set_id)
        if row is None:
            raise not_found("Call fact set")
        return row

    def describe_rule(self, fact_set: CallFactSet) -> str:
        """The rule in words, e.g. `program is "VE"`, or "Every lead"."""
        node = self._parse(fact_set.conditions)
        return describe(node) if node is not None else "Every lead"

    async def all_keys(self) -> list[str]:
        """Every fact key in any active set, so a call can send each one blank
        when the lead's set does not have it, never as a raw `{{key}}`."""
        keys: list[str] = []
        for fact_set in await self.list_sets():
            for fact in fact_set.facts:
                if fact["key"] not in keys:
                    keys.append(fact["key"])
        return keys

    async def catalogue(self) -> list[tuple[str, str]]:
        """`(key, label)` for every fact in any active set, first label wins."""
        seen: dict[str, str] = {}
        for fact_set in await self.list_sets():
            for fact in fact_set.facts:
                seen.setdefault(fact["key"], fact["label"])
        return list(seen.items())

    # --- writing ---------------------------------------------------------------

    async def create(
        self,
        *,
        name: str,
        priority: int,
        conditions: dict[str, Any],
        facts: Sequence[FactInput],
    ) -> CallFactSet:
        await self._ensure_name_free(name)
        self._parse(conditions)
        row = CallFactSet(
            name=name.strip(),
            priority=priority,
            conditions=conditions,
            facts=await self._normalise_facts(facts),
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def update(
        self,
        fact_set_id: uuid.UUID,
        *,
        name: str | None = None,
        priority: int | None = None,
        conditions: dict[str, Any] | None = None,
        facts: Sequence[FactInput] | None = None,
        is_active: bool | None = None,
    ) -> CallFactSet:
        row = await self.get(fact_set_id)
        if name is not None and name.strip() != row.name:
            await self._ensure_name_free(name)
            row.name = name.strip()
        if priority is not None:
            row.priority = priority
        if conditions is not None:
            self._parse(conditions)
            row.conditions = conditions
        if facts is not None:
            row.facts = await self._normalise_facts(facts)
        if is_active is not None:
            row.is_active = is_active
        await self._session.flush()
        return row

    # --- matching ----------------------------------------------------------------

    async def match(self, lead_id: uuid.UUID) -> MatchedFacts | None:
        """The facts for this lead: the first active set whose rule it meets."""
        sets = list(await self.list_sets())
        if not sets:
            return None
        compiler = await self._compiler()
        columns: list[ColumnElement[bool]] = []
        conditional: list[CallFactSet] = []
        catch_all: CallFactSet | None = None
        for fact_set in sets:
            node = self._parse(fact_set.conditions)
            if node is None:
                if catch_all is None:
                    catch_all = fact_set
                continue
            conditional.append(fact_set)
            columns.append(compiler.compile(node).label(f"s{len(columns)}"))

        matched_ids: set[uuid.UUID] = set()
        if columns:
            row = (
                await self._session.execute(
                    select(*columns)
                    .select_from(Lead)
                    .where(Lead.id == lead_id, Lead.workspace_id == self._session.workspace_id)
                )
            ).one_or_none()
            if row is not None:
                matched_ids = {s.id for s, hit in zip(conditional, row, strict=True) if hit}

        for fact_set in sets:  # priority order, so a catch-all competes fairly
            if fact_set.id in matched_ids or (catch_all is not None and fact_set is catch_all):
                return MatchedFacts(
                    fact_set=fact_set,
                    values={f["key"]: f["value"] for f in fact_set.facts},
                    lines=[(f["label"], f["value"]) for f in fact_set.facts if f["value"]],
                )
        return None

    # --- helpers -------------------------------------------------------------------

    def _parse(self, conditions: Any) -> FilterNode | None:
        if not conditions:
            return None
        try:
            node = _FILTER_ADAPTER.validate_python(conditions)
            validate_shape(node)
        except (ValidationError, ValueError) as exc:
            raise api_error(
                422, "invalid_call_fact_rule", f"The rule is not a valid filter: {exc}"
            ) from exc
        return node

    async def _load_fields(self) -> list[LeadField]:
        if self._fields is None:
            rows = await self._session.execute(
                self._session.select(LeadField).order_by(LeadField.sort_order)
            )
            self._fields = list(rows.scalars().all())
        return self._fields

    async def _compiler(self) -> FilterCompiler:
        """System grants, as for assignment rules: a rule is the workspace's own
        configuration, not a read on behalf of whoever placed the call."""
        fields = await self._load_fields()
        every = frozenset(field.key for field in fields)
        grants = FieldGrants(view=every, edit=every, import_=every, export=every, is_admin=True)
        return FilterCompiler(
            fields=fields,
            projection=FieldProjectionService(grants),
            timezone=self._workspace.timezone,
        )

    async def _ensure_name_free(self, name: str) -> None:
        if not name.strip():
            raise api_error(422, "name_required", "A call fact set needs a name")
        clash = await self._session.execute(
            self._session.select(CallFactSet).where(CallFactSet.name == name.strip())
        )
        if clash.scalars().first() is not None:
            raise conflict("call_fact_set_name_taken", f"A set named {name.strip()!r} exists")

    async def _normalise_facts(self, facts: Sequence[FactInput]) -> list[dict[str, str]]:
        if len(facts) > MAX_FACTS:
            raise api_error(422, "too_many_facts", f"A set holds at most {MAX_FACTS} facts")
        field_keys = {field.key for field in await self._load_fields()}
        out: list[dict[str, str]] = []
        taken: set[str] = set()
        for fact in facts:
            label = fact.label.strip()
            if not label:
                raise api_error(422, "fact_label_required", "Every fact needs a label")
            key = slugify_key(label)
            if key in taken:
                raise api_error(422, "duplicate_fact", f"Two facts are both called {label!r}")
            if key in field_keys or key.startswith("crm_") or key in _RESERVED_EXACT:
                raise api_error(
                    422,
                    "fact_name_in_use",
                    f"{label!r} is already the name of a lead field or a CRM variable. "
                    "Choose a different label.",
                    key=key,
                )
            taken.add(key)
            out.append({"key": key, "label": label, "value": fact.value.strip()})
        return out
