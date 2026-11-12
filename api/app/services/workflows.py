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
import json
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.errors import api_error, conflict, not_found
from app.integrations.whatsapp import send_template, whatsapp_number
from app.models.automation import AppConnection, Workflow, WorkflowRun
from app.models.enums import ChangesetSource, StageKind, VoiceCallPurpose
from app.models.field import LeadField
from app.models.lead import Lead
from app.models.pipeline import LostReason, Stage
from app.models.workspace import Workspace
from app.services.actions import ActionWriter
from app.services.leads import LeadService
from app.services.scheduled_calls import ScheduledCallService, clamp_to_hours
from app.services.system_filters import lead_matches, parse_rule, system_lead_service
from app.services.voice_hours import parse_calling_hours
from app.tenancy.session import ScopedSession

__all__ = [
    "CALL_PURPOSES",
    "LEAD_VARIABLES",
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

_BOOKING_VARIABLES = (
    Variable("event_name", "Meeting type"),
    Variable("start_time", "Starts at"),
    Variable("name", "Booked by (name)"),
    Variable("email", "Booked by (email)"),
    Variable("phone", "Booked by (phone)"),
    Variable("reschedule_url", "Reschedule link"),
    Variable("cancel_url", "Cancel link"),
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
        TriggerSpec(
            key="voice.call_finished",
            label="AI call finished",
            app="voice",
            description="An AI call with a lead ended — answered or not. Its summary and "
            "extracted details are already on the lead when this runs.",
            variables=(
                Variable("answered", "Answered (yes / no)"),
                Variable("duration_seconds", "Talk time (seconds)", "number"),
                Variable("summary", "Call summary"),
                Variable("status", "Bolna status"),
                Variable("hangup_by", "Who hung up (lead / agent)"),
                Variable("hangup_reason", "Why it ended"),
            ),
        ),
        TriggerSpec(
            key="calendly.invitee_created",
            label="Meeting booked",
            app="calendly",
            description="A lead booked a slot on your Calendly (for example the interview).",
            variables=_BOOKING_VARIABLES,
        ),
        TriggerSpec(
            key="calendly.invitee_canceled",
            label="Booking cancelled",
            app="calendly",
            description="A lead cancelled a Calendly booking.",
            variables=(*_BOOKING_VARIABLES, Variable("cancel_reason", "Why they cancelled")),
        ),
    )
}

#: The lead itself, usable in any message as `{{lead.name}}` — the workspace's
#: own field keys, plus these three the CRM always knows.
LEAD_VARIABLES = (
    Variable("lead.name", "Lead's name"),
    Variable("lead.phone", "Lead's phone"),
    Variable("lead.email", "Lead's email"),
)

#: Node type → its exits. A check has two; everything else has one.
NODE_TYPES: dict[str, tuple[str, ...]] = {
    "trigger": ("out",),
    "condition": ("yes", "no"),
    "lead_condition": ("yes", "no"),
    "update_fields": ("out",),
    "update_stage": ("out",),
    "add_note": ("out",),
    "wait": ("out",),
    "ai_call": ("out",),
    "send_whatsapp": ("out",),
    "call_api": ("out",),
}
ACTION_TYPES = frozenset(
    {"update_fields", "update_stage", "add_note", "ai_call", "send_whatsapp", "call_api"}
)
#: Steps that talk to the outside world. They only ever run in the worker, so
#: a payment webhook never waits on WhatsApp — the run pauses for a moment
#: and the worker picks it up within a minute.
OUTSIDE_TYPES = frozenset({"send_whatsapp", "call_api"})
WAIT_UNITS = {"minutes": 60, "hours": 3600, "days": 86400}
MAX_WAIT = dt.timedelta(days=30)

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
    """`amount`, `notes.course`, `lead.name` — a dotted path into the data."""
    current: Any = data
    for part in variable.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


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
            if variable not in known_variables and not variable.startswith(("notes.", "lead.")):
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
    elif kind == "wait":
        try:
            _delay(data)
        except ValueError as exc:
            out.append(GraphProblem(node_id, str(exc)))
    elif kind == "ai_call":
        if str(data.get("purpose") or "") not in CALL_PURPOSES:
            out.append(GraphProblem(node_id, "Choose what kind of call it is"))
    elif kind == "send_whatsapp":
        if not str(data.get("template") or "").strip():
            out.append(GraphProblem(node_id, "Enter the WhatsApp template's name"))
    elif kind == "call_api":
        url = str(data.get("url") or "").strip()
        if not url:
            out.append(GraphProblem(node_id, "Enter the address to send to"))
        elif not _safe_url(url.split("{{")[0] or url):
            out.append(GraphProblem(node_id, "The address must start with https:// and be public"))
        if str(data.get("method") or "POST").upper() not in HTTP_METHODS:
            out.append(GraphProblem(node_id, "Choose GET, POST, PUT or PATCH"))
    return out


# --- running ----------------------------------------------------------------------

#: What an "AI call" block may ask for: an ordinary call, or one of the voice
#: agent's own follow-up conversations (`VoiceCallPurpose`).
CALL_PURPOSES: dict[str, str] = {
    "": "An ordinary call",
    VoiceCallPurpose.FOLLOWUP.value: "Follow-up",
    VoiceCallPurpose.FOLLOWUP_MISSED.value: "They said they'd come back and didn't",
    VoiceCallPurpose.FOLLOWUP_BOOKING.value: "Paid but has not booked the interview",
}
HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH"})
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_PRIVATE_HOSTS = re.compile(
    r"^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.|0\.|\[?::1\]?$|"
    r".*\.internal$|.*\.local$)"
)


def _safe_url(url: str) -> bool:
    """HTTPS to a public-looking host. A workflow must not reach the CRM's own
    network, whoever drew it."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and bool(host) and not _PRIVATE_HOSTS.match(host)


def _delay(config: Mapping[str, Any]) -> dt.timedelta:
    try:
        amount = int(config.get("amount") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("Enter how long to wait") from exc
    unit = str(config.get("unit") or "minutes")
    if amount <= 0:
        raise ValueError("Enter how long to wait")
    if unit not in WAIT_UNITS:
        raise ValueError("Choose minutes, hours or days")
    delay = dt.timedelta(seconds=amount * WAIT_UNITS[unit])
    if delay > MAX_WAIT:
        raise ValueError("A wait can be at most 30 days")
    return delay


def _delay_words(config: Mapping[str, Any]) -> str:
    amount, unit = int(config.get("amount") or 0), str(config.get("unit") or "minutes")
    return f"{amount} {unit[:-1] if amount == 1 else unit}"


@dataclasses.dataclass(slots=True)
class RunResult:
    status: str
    steps: list[dict[str, Any]]
    duration_ms: int
    error: str | None
    #: Where the run continues: `[{"nodes": [...], "at": iso, "seen": [...]}]`.
    pending: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    @property
    def resume_at(self) -> dt.datetime | None:
        moments = [dt.datetime.fromisoformat(p["at"]) for p in self.pending]
        return min(moments) if moments else None


def _message(exc: HTTPException) -> str:
    detail: Any = exc.detail
    if isinstance(detail, dict):
        text = str(detail.get("message") or detail.get("code") or "Could not be done")
        fields = detail.get("fields")
        if isinstance(fields, dict) and fields:
            text += ": " + "; ".join(f"{k} — {v}" for k, v in fields.items())
        return text
    return str(detail)


def _status(steps: Sequence[Mapping[str, Any]], pending: Sequence[Any]) -> str:
    if pending:
        return "WAITING"
    return "FAILED" if any(s.get("result") == "failed" for s in steps) else "SUCCESS"


#: How a run is being executed. `live` is a webhook or a call result — no
#: outside calls, they wait for the worker. `worker` may send. `test` changes
#: nothing and sends nothing; it says what would happen.
Mode = str


class WorkflowEngine:
    """Runs workflows for one workspace, one lead at a time."""

    def __init__(
        self,
        session: ScopedSession,
        *,
        workspace: Workspace,
        http: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._session = session
        self._workspace = workspace
        self._http = http
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))
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

    def _context(self, lead: Lead, data: Mapping[str, Any]) -> dict[str, Any]:
        """The trigger's data plus `lead.<field key>` for any message."""
        values = dict(lead.values or {})
        return {**data, "lead": {"id": str(lead.id), "identity": lead.identity_value, **values}}

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
            # Written first, so a step that schedules a call can point at it.
            run = WorkflowRun(
                workflow_id=workflow.id,
                lead_id=lead.id,
                trigger=trigger,
                app_event_id=app_event_id,
                status="RUNNING",
                steps=[],
                data=dict(data),
            )
            self._session.add(run)
            await self._session.flush()
            result = await self._run_graph(
                workflow.graph, workflow.name, lead, data, mode="live", run_id=run.id
            )
            run.status = result.status
            run.steps = result.steps
            run.duration_ms = result.duration_ms
            run.error = result.error
            run.pending = result.pending
            run.resume_at = result.resume_at
            runs.append(run)
        await self._session.flush()
        return runs

    async def test(
        self, workflow_graph: Mapping[str, Any], name: str, lead: Lead, data: Mapping[str, Any]
    ) -> RunResult:
        """What the workflow would do to this lead. Nothing is kept or sent."""
        savepoint = await self._session.raw.begin_nested()
        try:
            return await self._run_graph(workflow_graph, name, lead, data, mode="test")
        finally:
            await savepoint.rollback()
            # Reload what the rollback expired; async sessions cannot lazy-load.
            await self._session.refresh(lead)

    async def run(self, workflow: Workflow, lead: Lead, data: Mapping[str, Any]) -> RunResult:
        return await self._run_graph(workflow.graph, workflow.name, lead, data, mode="live")

    async def resume(self, run: WorkflowRun) -> WorkflowRun:
        """Carry a WAITING run on from wherever its waits have run out (worker)."""
        now = self._clock()
        workflow = await self._session.get(Workflow, run.workflow_id)
        lead = await self._session.get(Lead, run.lead_id) if run.lead_id else None
        if workflow is None or workflow.is_archived or not workflow.is_active:
            run.status, run.pending, run.resume_at = "CANCELLED", [], None
            run.error = "The workflow was switched off before it could carry on"
            await self._session.flush()
            return run
        if lead is None or lead.deleted_at is not None:
            run.status, run.pending, run.resume_at = "CANCELLED", [], None
            run.error = "The lead no longer exists"
            await self._session.flush()
            return run

        due = [p for p in run.pending if dt.datetime.fromisoformat(p["at"]) <= now]
        later = [p for p in run.pending if dt.datetime.fromisoformat(p["at"]) > now]
        steps = list(run.steps or [])
        for entry in due:
            result = await self._run_graph(
                workflow.graph,
                workflow.name,
                lead,
                run.data or {},
                mode="worker",
                run_id=run.id,
                start=list(entry.get("nodes") or []),
                seen=set(entry.get("seen") or []),
            )
            steps.extend(result.steps)
            later.extend(result.pending)
        run.steps = steps
        run.pending = later
        run.resume_at = RunResult("", [], 0, None, later).resume_at
        run.status = _status(steps, later)
        run.error = next((s["detail"] for s in steps if s.get("result") == "failed"), None)
        await self._session.flush()
        return run

    async def _run_graph(
        self,
        graph: Mapping[str, Any],
        name: str,
        lead: Lead,
        data: Mapping[str, Any],
        *,
        mode: Mode,
        run_id: uuid.UUID | None = None,
        start: list[str] | None = None,
        seen: set[str] | None = None,
    ) -> RunResult:
        started = time.monotonic()
        now = self._clock()
        nodes = {str(n["id"]): n for n in _nodes(graph) if n.get("type") in NODE_TYPES}
        edges = _edges(graph)
        context = self._context(lead, data)

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
        pending: list[dict[str, Any]] = []
        writer: ActionWriter | None = None
        if start is None:
            trigger = next((n for n in nodes.values() if n["type"] == "trigger"), None)
            queue = targets(str(trigger["id"]), "out") if trigger else []
        else:
            queue = [n for n in start if n in nodes]
        visited: set[str] = set(seen or ())

        while queue and len(steps) < MAX_STEPS:
            node_id = queue.pop(0)
            if node_id in visited:
                continue
            node = nodes[node_id]
            kind = node["type"]
            config = _data(node)

            if kind == "wait":
                visited.add(node_id)
                words = _delay_words(config)
                if mode == "test":
                    steps.append(self._step(node, "done", f"Would wait {words}, then carry on"))
                    queue.extend(targets(node_id, "out"))
                    continue
                at = now + _delay(config)
                steps.append(
                    self._step(node, "waiting", f"Waiting {words}, until {self._local(at)}")
                )
                pending.append({"nodes": targets(node_id, "out"), "at": at.isoformat()})
                continue

            if kind in OUTSIDE_TYPES and mode == "live":
                # Sent by the worker within a minute; nothing outside the CRM
                # is ever called while a webhook or a call result is waiting.
                pending.append({"nodes": [node_id], "at": now.isoformat()})
                continue

            visited.add(node_id)
            if kind in ("condition", "lead_condition"):
                passed, detail = await self._check(node, lead, context)
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
                    detail = await self._act(
                        node, lead, context, writer, mode=mode, name=name, run_id=run_id
                    )
                    await self._session.flush()
            except HTTPException as exc:
                steps.append(self._step(node, "failed", _message(exc)))
                # The savepoint's rollback expired the lead; reload it here,
                # since an async session cannot lazy-load it later.
                await self._session.refresh(lead)
                continue
            steps.append(self._step(node, "done", detail))
            queue.extend(targets(node_id, "out"))

        for entry in pending:
            entry["seen"] = sorted(visited)
        error = next((s["detail"] for s in steps if s["result"] == "failed"), None)
        return RunResult(
            status=_status(steps, pending),
            steps=steps,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=error,
            pending=pending,
        )

    def _local(self, moment: dt.datetime) -> str:
        return moment.astimezone(ZoneInfo(self._workspace.timezone)).strftime("%a %d %b, %I:%M %p")

    @staticmethod
    def _step(node: Mapping[str, Any], result: str, detail: str) -> dict[str, Any]:
        return {
            "node_id": str(node["id"]),
            "type": node["type"],
            "label": str(_data(node).get("title") or _DEFAULT_TITLES.get(node["type"], "")),
            "result": result,
            "detail": detail,
            "at": dt.datetime.now(dt.UTC).isoformat(),
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
        *,
        mode: Mode,
        name: str,
        run_id: uuid.UUID | None,
    ) -> str:
        config = _data(node)
        kind = node["type"]
        if kind == "add_note":
            body = render(str(config.get("text") or ""), data).strip()
            if not body:
                raise api_error(422, "empty_note", "The note came out empty")
            writer.record_note(lead, body=body[:4000])
            return f"Added a note: {body[:120]}"
        if kind == "update_stage":
            return await self._move_stage(lead, config, writer)
        if kind == "ai_call":
            return await self._schedule_call(
                lead, config, data, mode=mode, name=name, run_id=run_id
            )
        if kind == "send_whatsapp":
            return await self._send_whatsapp(lead, config, data, writer, mode=mode)
        if kind == "call_api":
            return await self._call_api(config, data, mode=mode)
        return await self._update_fields(lead, config, data, writer)

    async def _move_stage(self, lead: Lead, config: Mapping[str, Any], writer: ActionWriter) -> str:
        stages = await self._stage_map()
        stage_id = _uuid(config.get("stage_id"))
        stage = stages.get(stage_id) if stage_id else None
        if stage is None or stage.is_archived:
            raise api_error(422, "stage_missing", "The chosen stage no longer exists")
        if lead.stage_id == stage.id:
            return f"Already in {stage.label}"
        leads = await self._lead_service()
        await leads.apply_update(
            lead, writer, stage_id=stage.id, lost_reason_id=_uuid(config.get("lost_reason_id"))
        )
        return f"Stage → {stage.label}"

    async def _update_fields(
        self,
        lead: Lead,
        config: Mapping[str, Any],
        data: Mapping[str, Any],
        writer: ActionWriter,
    ) -> str:
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
            leads = await self._lead_service()
            await leads.apply_update(lead, writer, values=values)

        def shown(key: str, value: str) -> str:
            option = next((o for o in fields[key].options if o.code == value), None)
            return option.label if option else value

        changed = ", ".join(f"{fields[k].label} → {shown(k, v)}" for k, v in values.items())
        note = f" (left blank: {', '.join(skipped)})" if skipped else ""
        return (changed or "Nothing to update") + note

    async def _schedule_call(
        self,
        lead: Lead,
        config: Mapping[str, Any],
        data: Mapping[str, Any],
        *,
        mode: Mode,
        name: str,
        run_id: uuid.UUID | None,
    ) -> str:
        """Book an AI call; the worker places it (never in this request)."""
        purpose = str(config.get("purpose") or "")
        if purpose not in CALL_PURPOSES:
            raise api_error(422, "unknown_purpose", "Choose what kind of call it is")
        why = render(str(config.get("reason") or ""), data).strip()
        service = ScheduledCallService(
            self._session,
            workspace=self._workspace,
            actor_id=None,
            hours=parse_calling_hours(get_settings().voice_calling_hours),
        )
        due = clamp_to_hours(
            self._clock(),
            timezone=self._workspace.timezone,
            hours=parse_calling_hours(get_settings().voice_calling_hours),
        )
        if mode == "test":
            return (
                f"Would book an AI call ({CALL_PURPOSES[purpose].lower()}) for {self._local(due)}"
            )
        row = await service.schedule(
            lead,
            due_at=due,
            source="WORKFLOW",
            reason=f"Workflow “{name}”" + (f": {why}" if why else ""),
            purpose=purpose or None,
            workflow_run_id=run_id,
        )
        return f"AI call booked for {self._local(row.due_at)} ({CALL_PURPOSES[purpose].lower()})"

    async def _send_whatsapp(
        self,
        lead: Lead,
        config: Mapping[str, Any],
        data: Mapping[str, Any],
        writer: ActionWriter,
        *,
        mode: Mode,
    ) -> str:
        template = str(config.get("template") or "").strip()
        language = str(config.get("language") or "en").strip() or "en"
        raw_to = render(str(config.get("to") or "{{lead.phone}}"), data)
        to = whatsapp_number(raw_to, self._workspace.default_country_code)
        params = [render(str(p), data) for p in (config.get("params") or []) if p is not None]
        if not to:
            raise api_error(
                422, "no_number", f"No WhatsApp number to send to ({raw_to or 'empty'})"
            )
        if mode == "test":
            return f"Would send WhatsApp template “{template}” to +{to}"
        connection = (
            await self._session.execute(
                self._session.select(AppConnection).where(AppConnection.provider == "whatsapp")
            )
        ).scalar_one_or_none()
        if connection is None or not connection.is_active or not connection.credential:
            raise api_error(422, "whatsapp_not_connected", "Connect WhatsApp in Apps first")
        phone_id = str((connection.settings or {}).get("phone_number_id") or "").strip()
        if not phone_id:
            raise api_error(422, "whatsapp_not_connected", "WhatsApp's phone number ID is not set")
        result = await send_template(
            phone_number_id=phone_id,
            token=connection.credential,
            to=to,
            template=template,
            language=language,
            params=params,
            transport=self._http,
        )
        if not result.ok:
            raise api_error(
                502, "whatsapp_refused", f"WhatsApp said no ({result.status}): {result.error}"
            )
        writer.record_note(lead, body=f"💬 WhatsApp sent: template “{template}” to +{to}")
        return f"WhatsApp template “{template}” sent to +{to}"

    async def _call_api(
        self, config: Mapping[str, Any], data: Mapping[str, Any], *, mode: Mode
    ) -> str:
        method = str(config.get("method") or "POST").upper()
        url = render(str(config.get("url") or ""), data).strip()
        if method not in HTTP_METHODS or not _safe_url(url):
            raise api_error(422, "unsafe_url", "The address must be https:// and public")
        headers = {
            str(h.get("key")).strip(): render(str(h.get("value") or ""), data)
            for h in (config.get("headers") or [])
            if isinstance(h, dict) and str(h.get("key") or "").strip()
        }
        raw_body = render(str(config.get("body") or ""), data).strip()
        host = urlparse(url).hostname
        if mode == "test":
            return f"Would {method} to {host}"
        json_body: Any = None
        content: str | None = None
        if raw_body:
            try:
                json_body = json.loads(raw_body)
            except ValueError:
                content = raw_body
        response = await self._request(
            method, url, headers=headers, json_body=json_body, content=content
        )
        if response.status_code >= 300:
            raise api_error(
                502,
                "api_refused",
                f"{host} answered {response.status_code}: {_error_text(response)}",
            )
        return f"{method} to {host} → {response.status_code}"

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Any = None,
        content: str | None = None,
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT, transport=self._http, follow_redirects=False
            ) as client:
                return await client.request(
                    method, url, headers=dict(headers), json=json_body, content=content
                )
        except httpx.HTTPError as exc:
            raise api_error(
                502, "unreachable", f"Could not reach {urlparse(url).hostname}"
            ) from exc


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)[:200]
        return str(body.get("message") or body)[:200]
    return str(body)[:200]


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
    "condition": "If…",
    "lead_condition": "If the lead…",
    "update_fields": "Update lead fields",
    "update_stage": "Change stage",
    "add_note": "Add a note",
    "wait": "Wait",
    "ai_call": "AI call",
    "send_whatsapp": "Send WhatsApp",
    "call_api": "Call an API",
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
