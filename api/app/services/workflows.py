"""Workflows: when something happens, check things, then act (docs/18).

A workflow is a graph the admin draws on a canvas:

    [Trigger: payment received] ─▶ [If amount equals 400] ─yes─▶ [Change stage]
                                                          └─no──▶ [If amount equals 8000] ─▶ …

**What is product code here, and what is not.** The kinds of trigger, check and
action are the product's — like the 13 field types, a closed list the engine
knows how to run. Everything inside a node — which stage, which field, which
amount — is the workspace's data, stored in its graph. Nothing about any
customer's business appears in this file.

**How a run goes.** Start at the trigger, follow its edges, run each node once.
A check takes its "yes" or "no" exit; an action runs and takes its one exit. An
exit wired to several nodes runs all of them, top of the canvas first. A step
that cannot be done (a value the field refuses) is recorded as failed and its
branch stops; other branches carry on, and the run is marked FAILED so it shows
up in red.

**Every change is an ordinary lead change.** Field and stage updates go through
`LeadService` — validation, the timeline, one AUTOMATION changeset per run — so
a workflow's work is visible on the lead and undoable like anyone else's.

**A test run changes nothing.** It runs in a savepoint that is always rolled
back, so the admin sees exactly what would happen to a real lead, safely.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.errors import api_error, conflict, not_found
from app.models.automation import Workflow, WorkflowRun
from app.models.enums import ChangesetSource, StageKind
from app.models.field import LeadField
from app.models.lead import Lead
from app.models.pipeline import LostReason, Stage
from app.models.workspace import Workspace
from app.services.actions import ActionWriter
from app.services.leads import LeadService
from app.services.system_filters import lead_matches, parse_rule, system_lead_service
from app.tenancy.session import ScopedSession

__all__ = [
    "NODE_TYPES",
    "OPERATORS",
    "TRIGGERS",
    "RunResult",
    "TriggerSpec",
    "Variable",
    "WorkflowEngine",
    "WorkflowService",
    "WorkflowStats",
    "check_graph",
    "render",
]

# --- the product's vocabulary -------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class Variable:
    key: str
    label: str
    #: `number` compares numerically; `text` case-insensitively.
    kind: str = "text"


@dataclasses.dataclass(frozen=True, slots=True)
class TriggerSpec:
    key: str
    label: str
    #: The connected app it comes from, or `None` for the CRM's own events.
    app: str | None
    description: str
    variables: tuple[Variable, ...]


_PAYMENT_VARIABLES = (
    Variable("amount", "Amount", "number"),
    Variable("currency", "Currency"),
    Variable("phone", "Customer phone"),
    Variable("email", "Customer email"),
    Variable("name", "Customer name"),
    Variable("payment_id", "Payment ID"),
    Variable("order_id", "Order ID"),
    Variable("method", "Payment method"),
    Variable("description", "Description"),
)

TRIGGERS: dict[str, TriggerSpec] = {
    spec.key: spec
    for spec in (
        TriggerSpec(
            key="razorpay.payment_captured",
            label="Payment received",
            app="razorpay",
            description="Razorpay captured a payment from a lead.",
            variables=_PAYMENT_VARIABLES,
        ),
        TriggerSpec(
            key="razorpay.payment_failed",
            label="Payment failed",
            app="razorpay",
            description="A lead tried to pay through Razorpay and the payment failed.",
            variables=(*_PAYMENT_VARIABLES, Variable("error_reason", "Failure reason")),
        ),
    )
}

#: Node type → its exits. A check has two; everything else has one.
NODE_TYPES: dict[str, tuple[str, ...]] = {
    "trigger": ("out",),
    "condition": ("yes", "no"),
    "lead_condition": ("yes", "no"),
    "update_fields": ("out",),
    "update_stage": ("out",),
    "add_note": ("out",),
}
ACTION_TYPES = frozenset({"update_fields", "update_stage", "add_note"})

OPERATORS: dict[str, str] = {
    "equals": "equals",
    "not_equals": "does not equal",
    "greater_than": "is more than",
    "less_than": "is less than",
    "contains": "contains",
    "not_contains": "does not contain",
    "is_empty": "is empty",
    "is_not_empty": "is not empty",
}
_NO_VALUE_OPERATORS = frozenset({"is_empty", "is_not_empty"})

MAX_NODES = 100
MAX_EDGES = 300
#: Generous for any graph a person draws; a hard stop if something loops.
MAX_STEPS = 200

_TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")


# --- reading the trigger's data -------------------------------------------------


def lookup(data: Mapping[str, Any], variable: str) -> Any:
    """`amount`, or `notes.course` for a key inside the payment's notes."""
    if variable.startswith("notes."):
        notes = data.get("notes")
        return notes.get(variable[6:]) if isinstance(notes, Mapping) else None
    return data.get(variable)


def render(template: str, data: Mapping[str, Any]) -> str:
    """`{{amount}}` → the value. Unknown names become empty, never a literal
    `{{...}}` stored on a lead (CLAUDE.md's "resolve at write time")."""

    def one(match: re.Match[str]) -> str:
        value = lookup(data, match.group(1))
        return "" if value is None else str(value)

    return _TEMPLATE_RE.sub(one, template)


def _number(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def compare(actual: Any, operator: str, expected: Any) -> bool:
    """One rule. Numbers compare as numbers ("400" equals "400.00"); text
    ignores case and surrounding spaces."""
    if operator == "is_empty":
        return actual in (None, "", [], {})
    if operator == "is_not_empty":
        return actual not in (None, "", [], {})
    left = "" if actual is None else str(actual).strip()
    right = "" if expected is None else str(expected).strip()
    left_n, right_n = _number(left), _number(right)
    both_numbers = left_n is not None and right_n is not None
    if operator in ("equals", "not_equals"):
        same = left_n == right_n if both_numbers else left.lower() == right.lower()
        return same if operator == "equals" else not same
    if operator in ("greater_than", "less_than"):
        if not both_numbers:
            return False
        assert left_n is not None and right_n is not None
        return left_n > right_n if operator == "greater_than" else left_n < right_n
    if operator in ("contains", "not_contains"):
        found = right.lower() in left.lower()
        return found if operator == "contains" else not found
    return False


# --- the graph ------------------------------------------------------------------


@dataclasses.dataclass(slots=True)
class GraphProblem:
    node_id: str | None
    message: str


def _nodes(graph: Mapping[str, Any]) -> list[dict[str, Any]]:
    nodes = graph.get("nodes")
    return [n for n in nodes if isinstance(n, dict)] if isinstance(nodes, list) else []


def _edges(graph: Mapping[str, Any]) -> list[dict[str, Any]]:
    edges = graph.get("edges")
    return [e for e in edges if isinstance(e, dict)] if isinstance(edges, list) else []


def _data(node: Mapping[str, Any]) -> dict[str, Any]:
    data = node.get("data")
    return data if isinstance(data, dict) else {}


def check_graph(
    graph: Mapping[str, Any],
    *,
    complete: bool,
    field_keys: frozenset[str] = frozenset(),
    stages: Mapping[uuid.UUID, Stage] | None = None,
    lost_reasons: frozenset[uuid.UUID] = frozenset(),
) -> tuple[str | None, list[GraphProblem]]:
    """The trigger key and what is wrong with the graph.

    The shape is always checked — a broken graph is never saved. Whether every
    node is filled in (`complete`) is checked only for a workflow being
    switched on: a half-drawn draft can be saved and finished later.
    """
    problems: list[GraphProblem] = []
    nodes, edges = _nodes(graph), _edges(graph)
    if len(nodes) > MAX_NODES:
        problems.append(GraphProblem(None, f"A workflow can have at most {MAX_NODES} blocks"))
    if len(edges) > MAX_EDGES:
        problems.append(GraphProblem(None, f"A workflow can have at most {MAX_EDGES} links"))

    by_id: dict[str, dict[str, Any]] = {}
    for node in nodes:
        node_id, kind = node.get("id"), node.get("type")
        if not isinstance(node_id, str) or not node_id or node_id in by_id:
            problems.append(GraphProblem(None, "Every block needs its own id"))
            continue
        if kind not in NODE_TYPES:
            problems.append(GraphProblem(node_id, f"Unknown block type {kind!r}"))
            continue
        by_id[node_id] = node

    triggers = [n for n in by_id.values() if n["type"] == "trigger"]
    if len(triggers) != 1:
        problems.append(GraphProblem(None, "A workflow has exactly one trigger"))
    trigger_key = _data(triggers[0]).get("trigger") if len(triggers) == 1 else None
    if trigger_key is not None and trigger_key not in TRIGGERS:
        problems.append(GraphProblem(triggers[0]["id"], f"Unknown trigger {trigger_key!r}"))
        trigger_key = None

    for edge in edges:
        source, target = by_id.get(str(edge.get("source"))), by_id.get(str(edge.get("target")))
        if source is None or target is None:
            problems.append(GraphProblem(None, "A link points at a block that does not exist"))
            continue
        handle = edge.get("sourceHandle") or "out"
        if handle not in NODE_TYPES[source["type"]]:
            problems.append(GraphProblem(source["id"], f"This block has no {handle!r} exit"))
        if target["type"] == "trigger":
            problems.append(GraphProblem(target["id"], "Nothing can link into the trigger"))

    if complete:
        if trigger_key is None:
            problems.append(GraphProblem(None, "Choose what starts this workflow"))
        spec = TRIGGERS.get(trigger_key) if trigger_key else None
        known = {v.key for v in spec.variables} if spec else set()
        for node in by_id.values():
            problems.extend(
                _check_node(
                    node,
                    known_variables=known,
                    field_keys=field_keys,
                    stages=stages or {},
                    lost_reasons=lost_reasons,
                )
            )
        if trigger_key and not any(str(e.get("source")) == triggers[0]["id"] for e in edges):
            problems.append(GraphProblem(triggers[0]["id"], "Link the trigger to a first step"))
    return (str(trigger_key) if trigger_key else None), problems


def _uuid(raw: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError):
        return None


def _check_node(
    node: Mapping[str, Any],
    *,
    known_variables: set[str],
    field_keys: frozenset[str],
    stages: Mapping[uuid.UUID, Stage],
    lost_reasons: frozenset[uuid.UUID],
) -> list[GraphProblem]:
    node_id, kind, data = str(node["id"]), node["type"], _data(node)
    out: list[GraphProblem] = []
    if kind == "condition":
        rules = data.get("rules")
        if not isinstance(rules, list) or not rules:
            return [GraphProblem(node_id, "Add at least one check")]
        for rule in rules:
            if not isinstance(rule, dict):
                out.append(GraphProblem(node_id, "A check is malformed"))
                continue
            variable, operator = str(rule.get("variable") or ""), rule.get("operator")
            if variable not in known_variables and not variable.startswith("notes."):
                out.append(GraphProblem(node_id, "Choose what to check"))
            if operator not in OPERATORS:
                out.append(GraphProblem(node_id, "Choose how to compare"))
            elif operator not in _NO_VALUE_OPERATORS and str(rule.get("value") or "") == "":
                out.append(GraphProblem(node_id, "Enter the value to compare with"))
    elif kind == "lead_condition":
        try:
            if parse_rule(data.get("conditions"), code="invalid_workflow") is None:
                out.append(GraphProblem(node_id, "Add at least one lead condition"))
        except HTTPException:
            out.append(GraphProblem(node_id, "The lead condition is not valid"))
    elif kind == "update_fields":
        values = data.get("values")
        if not isinstance(values, list) or not values:
            return [GraphProblem(node_id, "Choose at least one field to update")]
        for item in values:
            key = item.get("field_key") if isinstance(item, dict) else None
            if key not in field_keys:
                out.append(GraphProblem(node_id, "Choose a field that exists"))
    elif kind == "update_stage":
        stage_id = _uuid(data.get("stage_id"))
        stage = stages.get(stage_id) if stage_id else None
        if stage is None:
            out.append(GraphProblem(node_id, "Choose the stage to move the lead to"))
        elif stage.kind == StageKind.LOST and _uuid(data.get("lost_reason_id")) not in lost_reasons:
            out.append(GraphProblem(node_id, "Moving a lead to Lost needs a lost reason"))
    elif kind == "add_note" and not str(data.get("text") or "").strip():
        out.append(GraphProblem(node_id, "Write the note"))
    return out


# --- running ----------------------------------------------------------------------


@dataclasses.dataclass(slots=True)
class RunResult:
    status: str
    steps: list[dict[str, Any]]
    duration_ms: int
    error: str | None


def _message(exc: HTTPException) -> str:
    detail: Any = exc.detail
    if isinstance(detail, dict):
        text = str(detail.get("message") or detail.get("code") or "Could not be done")
        fields = detail.get("fields")
        if isinstance(fields, dict) and fields:
            text += ": " + "; ".join(f"{k} — {v}" for k, v in fields.items())
        return text
    return str(detail)


class WorkflowEngine:
    """Runs workflows for one workspace, one lead at a time."""

    def __init__(self, session: ScopedSession, *, workspace: Workspace) -> None:
        self._session = session
        self._workspace = workspace
        self._leads: LeadService | None = None
        self._stages: dict[uuid.UUID, Stage] | None = None
        self._fields: dict[str, LeadField] | None = None

    async def _lead_service(self) -> LeadService:
        if self._leads is None:
            self._leads = await system_lead_service(self._session, self._workspace)
        return self._leads

    async def _stage_map(self) -> dict[uuid.UUID, Stage]:
        if self._stages is None:
            rows = await self._session.execute(self._session.select(Stage))
            self._stages = {s.id: s for s in rows.scalars().all()}
        return self._stages

    async def _field_map(self) -> dict[str, LeadField]:
        if self._fields is None:
            rows = await self._session.execute(
                self._session.select(LeadField).options(selectinload(LeadField.options))
            )
            self._fields = {f.key: f for f in rows.scalars().all()}
        return self._fields

    async def run_trigger(
        self,
        trigger: str,
        lead: Lead,
        data: Mapping[str, Any],
        *,
        app_event_id: uuid.UUID | None = None,
    ) -> list[WorkflowRun]:
        """Every switched-on workflow that starts on `trigger`, for this lead."""
        rows = await self._session.execute(
            self._session.select(Workflow)
            .where(
                Workflow.trigger == trigger,
                Workflow.is_active.is_(True),
                Workflow.is_archived.is_(False),
            )
            .order_by(Workflow.created_at)
        )
        runs: list[WorkflowRun] = []
        for workflow in rows.scalars().all():
            result = await self.run(workflow, lead, data)
            run = WorkflowRun(
                workflow_id=workflow.id,
                lead_id=lead.id,
                trigger=trigger,
                app_event_id=app_event_id,
                status=result.status,
                steps=result.steps,
                data=dict(data),
                duration_ms=result.duration_ms,
                error=result.error,
            )
            self._session.add(run)
            runs.append(run)
        await self._session.flush()
        return runs

    async def test(
        self, workflow_graph: Mapping[str, Any], name: str, lead: Lead, data: Mapping[str, Any]
    ) -> RunResult:
        """What the workflow would do to this lead. Nothing is kept."""
        savepoint = await self._session.raw.begin_nested()
        try:
            return await self._run_graph(workflow_graph, name, lead, data)
        finally:
            await savepoint.rollback()
            # Reload what the rollback expired; async sessions cannot lazy-load.
            await self._session.refresh(lead)

    async def run(self, workflow: Workflow, lead: Lead, data: Mapping[str, Any]) -> RunResult:
        return await self._run_graph(workflow.graph, workflow.name, lead, data)

    async def _run_graph(
        self, graph: Mapping[str, Any], name: str, lead: Lead, data: Mapping[str, Any]
    ) -> RunResult:
        started = time.monotonic()
        nodes = {str(n["id"]): n for n in _nodes(graph) if n.get("type") in NODE_TYPES}
        edges = _edges(graph)

        def targets(node_id: str, handle: str) -> list[str]:
            found = [
                str(e["target"])
                for e in edges
                if str(e.get("source")) == node_id
                and (e.get("sourceHandle") or "out") == handle
                and str(e.get("target")) in nodes
            ]
            # Top of the canvas first, so a fan-out runs in the order it reads.
            return sorted(found, key=lambda nid: _y(nodes[nid]))

        steps: list[dict[str, Any]] = []
        failed = False
        writer: ActionWriter | None = None
        start = next((n for n in nodes.values() if n["type"] == "trigger"), None)
        queue = targets(str(start["id"]), "out") if start else []
        seen: set[str] = set()

        while queue and len(steps) < MAX_STEPS:
            node_id = queue.pop(0)
            if node_id in seen:
                continue
            seen.add(node_id)
            node = nodes[node_id]
            kind = node["type"]
            if kind in ("condition", "lead_condition"):
                passed, detail = await self._check(node, lead, data)
                steps.append(self._step(node, "yes" if passed else "no", detail))
                queue.extend(targets(node_id, "yes" if passed else "no"))
                continue
            if kind not in ACTION_TYPES:
                continue
            if writer is None:
                writer = ActionWriter(self._session, actor_id=None)
                await writer.open_changeset(
                    source=ChangesetSource.AUTOMATION,
                    summary=f"Workflow “{name}” for {lead.identity_value}"[:200],
                )
            try:
                async with self._session.raw.begin_nested():
                    detail = await self._act(node, lead, data, writer)
                    await self._session.flush()
            except HTTPException as exc:
                failed = True
                steps.append(self._step(node, "failed", _message(exc)))
                # The savepoint's rollback expired the lead; reload it here,
                # since an async session cannot lazy-load it later.
                await self._session.refresh(lead)
                continue
            steps.append(self._step(node, "done", detail))
            queue.extend(targets(node_id, "out"))

        error = next((s["detail"] for s in steps if s["result"] == "failed"), None)
        return RunResult(
            status="FAILED" if failed else "SUCCESS",
            steps=steps,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=error,
        )

    @staticmethod
    def _step(node: Mapping[str, Any], result: str, detail: str) -> dict[str, Any]:
        return {
            "node_id": str(node["id"]),
            "type": node["type"],
            "label": str(_data(node).get("title") or _DEFAULT_TITLES.get(node["type"], "")),
            "result": result,
            "detail": detail,
        }

    async def _check(
        self, node: Mapping[str, Any], lead: Lead, data: Mapping[str, Any]
    ) -> tuple[bool, str]:
        config = _data(node)
        if node["type"] == "lead_condition":
            lead_rule = parse_rule(config.get("conditions"), code="invalid_workflow")
            if lead_rule is None:
                return True, "No lead condition set, so every lead passes"
            await self._session.flush()
            passed = await lead_matches(self._session, self._workspace, lead_rule, lead.id)
            return passed, "The lead matches" if passed else "The lead does not match"

        rules = [r for r in (config.get("rules") or []) if isinstance(r, dict)]
        results: list[bool] = []
        parts: list[str] = []
        for rule in rules:
            variable = str(rule.get("variable") or "")
            operator = str(rule.get("operator") or "equals")
            actual = lookup(data, variable)
            ok = compare(actual, operator, rule.get("value"))
            results.append(ok)
            shown = "empty" if actual in (None, "") else str(actual)
            target = "" if operator in _NO_VALUE_OPERATORS else f" {rule.get('value')}"
            parts.append(
                f"{variable} is {shown} — {OPERATORS.get(operator, operator)}{target}: "
                f"{'yes' if ok else 'no'}"
            )
        match_any = config.get("match") == "any"
        passed = bool(results) and (any(results) if match_any else all(results))
        return passed, "; ".join(parts) or "Nothing to check"

    async def _act(
        self,
        node: Mapping[str, Any],
        lead: Lead,
        data: Mapping[str, Any],
        writer: ActionWriter,
    ) -> str:
        config = _data(node)
        kind = node["type"]
        leads = await self._lead_service()
        if kind == "add_note":
            body = render(str(config.get("text") or ""), data).strip()
            if not body:
                raise api_error(422, "empty_note", "The note came out empty")
            writer.record_note(lead, body=body[:4000])
            return f"Added a note: {body[:120]}"

        if kind == "update_stage":
            stages = await self._stage_map()
            stage_id = _uuid(config.get("stage_id"))
            stage = stages.get(stage_id) if stage_id else None
            if stage is None or stage.is_archived:
                raise api_error(422, "stage_missing", "The chosen stage no longer exists")
            if lead.stage_id == stage.id:
                return f"Already in {stage.label}"
            await leads.apply_update(
                lead,
                writer,
                stage_id=stage.id,
                lost_reason_id=_uuid(config.get("lost_reason_id")),
            )
            return f"Stage → {stage.label}"

        # update_fields
        fields = await self._field_map()
        values: dict[str, Any] = {}
        skipped: list[str] = []
        for item in config.get("values") or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("field_key") or "")
            if key not in fields:
                raise api_error(422, "field_missing", f"The field {key!r} no longer exists")
            value = render(str(item.get("value") if item.get("value") is not None else ""), data)
            if value.strip() == "":
                # A blank (or a variable this event did not carry) never wipes
                # what the lead already has.
                skipped.append(fields[key].label)
                continue
            values[key] = value.strip()
        if values:
            await leads.apply_update(lead, writer, values=values)
        def shown(key: str, value: str) -> str:
            option = next((o for o in fields[key].options if o.code == value), None)
            return option.label if option else value

        changed = ", ".join(f"{fields[k].label} → {shown(k, v)}" for k, v in values.items())
        note = f" (left blank: {', '.join(skipped)})" if skipped else ""
        return (changed or "Nothing to update") + note


def _y(node: Mapping[str, Any]) -> float:
    position = node.get("position")
    if isinstance(position, dict):
        try:
            return float(position.get("y") or 0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


_DEFAULT_TITLES = {
    "trigger": "Trigger",
    "condition": "If the payment…",
    "lead_condition": "If the lead…",
    "update_fields": "Update lead fields",
    "update_stage": "Change stage",
    "add_note": "Add a note",
}


# --- managing workflows -------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class WorkflowStats:
    total_runs: int = 0
    runs_24h: int = 0
    failed_24h: int = 0
    last_run_at: dt.datetime | None = None


def _blank_graph(trigger: str | None) -> dict[str, Any]:
    return {
        "nodes": [
            {
                "id": "trigger",
                "type": "trigger",
                "position": {"x": 60, "y": 160},
                "data": {"trigger": trigger},
            }
        ],
        "edges": [],
    }


class WorkflowService:
    def __init__(
        self, session: ScopedSession, *, workspace: Workspace, actor_id: uuid.UUID | None
    ) -> None:
        self._session = session
        self._workspace = workspace
        self._actor_id = actor_id

    async def list_workflows(self) -> Sequence[Workflow]:
        rows = await self._session.execute(
            self._session.select(Workflow)
            .where(Workflow.is_archived.is_(False))
            .order_by(Workflow.created_at)
        )
        found: Sequence[Workflow] = rows.scalars().all()
        return found

    async def get(self, workflow_id: uuid.UUID) -> Workflow:
        row = await self._session.get(Workflow, workflow_id)
        if row is None or row.is_archived:
            raise not_found("Workflow")
        return row

    async def stats(self) -> dict[uuid.UUID, WorkflowStats]:
        since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=24)
        rows = await self._session.execute(
            select(
                WorkflowRun.workflow_id,
                func.count(),
                func.count().filter(WorkflowRun.created_at >= since),
                func.count().filter(
                    WorkflowRun.created_at >= since, WorkflowRun.status == "FAILED"
                ),
                func.max(WorkflowRun.created_at),
            )
            .where(WorkflowRun.workspace_id == self._session.workspace_id)
            .group_by(WorkflowRun.workflow_id)
        )
        return {
            row[0]: WorkflowStats(
                total_runs=row[1], runs_24h=row[2], failed_24h=row[3], last_run_at=row[4]
            )
            for row in rows.all()
        }

    async def create(
        self, *, name: str, graph: Mapping[str, Any] | None, trigger: str | None
    ) -> Workflow:
        await self._ensure_name_free(name)
        if trigger is not None and trigger not in TRIGGERS:
            raise api_error(422, "unknown_trigger", f"No trigger called {trigger!r}")
        body = dict(graph) if graph else _blank_graph(trigger)
        resolved, _ = await self.validate(body, complete=False)
        row = Workflow(
            name=name.strip(),
            graph=body,
            trigger=resolved,
            is_active=False,
            created_by_id=self._actor_id,
            updated_by_id=self._actor_id,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def update(
        self,
        workflow_id: uuid.UUID,
        *,
        name: str | None = None,
        graph: Mapping[str, Any] | None = None,
        is_active: bool | None = None,
    ) -> Workflow:
        row = await self.get(workflow_id)
        if name is not None and name.strip() != row.name:
            await self._ensure_name_free(name)
            row.name = name.strip()
        body = dict(graph) if graph is not None else row.graph
        active = row.is_active if is_active is None else is_active
        resolved, _ = await self.validate(body, complete=active)
        row.graph = body
        row.trigger = resolved
        row.is_active = active
        row.updated_by_id = self._actor_id
        await self._session.flush()
        return row

    async def archive(self, workflow_id: uuid.UUID) -> Workflow:
        row = await self.get(workflow_id)
        row.is_archived = True
        row.is_active = False
        await self._session.flush()
        return row

    async def problems(self, row: Workflow) -> list[GraphProblem]:
        """What still has to be filled in before it can be switched on."""
        _, found = await self._check(row.graph, complete=True)
        return found

    async def runs(
        self, workflow_id: uuid.UUID, *, limit: int, offset: int, status: str | None
    ) -> tuple[list[tuple[WorkflowRun, Lead | None]], int]:
        await self.get(workflow_id)
        base = self._session.select(WorkflowRun).where(WorkflowRun.workflow_id == workflow_id)
        if status:
            base = base.where(WorkflowRun.status == status)
        total = (
            await self._session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        rows = await self._session.execute(
            base.order_by(WorkflowRun.created_at.desc()).limit(limit).offset(offset)
        )
        runs = list(rows.scalars().all())
        lead_ids = {r.lead_id for r in runs if r.lead_id}
        leads: dict[uuid.UUID, Lead] = {}
        if lead_ids:
            found = await self._session.execute(
                self._session.select(Lead).where(Lead.id.in_(lead_ids))
            )
            leads = {lead.id: lead for lead in found.scalars().all()}
        return [(r, leads.get(r.lead_id) if r.lead_id else None) for r in runs], int(total)

    async def _check(
        self, graph: Mapping[str, Any], *, complete: bool
    ) -> tuple[str | None, list[GraphProblem]]:
        fields = await self._session.execute(self._session.select(LeadField))
        stages = await self._session.execute(self._session.select(Stage))
        reasons = await self._session.execute(
            self._session.select(LostReason).where(LostReason.is_archived.is_(False))
        )
        return check_graph(
            graph,
            complete=complete,
            field_keys=frozenset(f.key for f in fields.scalars().all()),
            stages={s.id: s for s in stages.scalars().all() if not s.is_archived},
            lost_reasons=frozenset(r.id for r in reasons.scalars().all()),
        )

    async def validate(
        self, graph: Mapping[str, Any], *, complete: bool
    ) -> tuple[str | None, list[GraphProblem]]:
        trigger, problems = await self._check(graph, complete=complete)
        if problems:
            raise api_error(
                422,
                "workflow_incomplete" if complete else "invalid_workflow",
                problems[0].message
                if len(problems) == 1
                else f"{problems[0].message} (and {len(problems) - 1} more)",
                problems=[dataclasses.asdict(p) for p in problems],
            )
        return trigger, problems

    async def _ensure_name_free(self, name: str) -> None:
        if not name.strip():
            raise api_error(422, "name_required", "A workflow needs a name")
        clash = await self._session.execute(
            self._session.select(Workflow).where(
                Workflow.name == name.strip(), Workflow.is_archived.is_(False)
            )
        )
        if clash.scalars().first() is not None:
            raise conflict("workflow_name_taken", f"A workflow named {name.strip()!r} exists")
