"""The action-writing service (M5).

PROMPTS.md M5: "Write the action-writing service and its tests FIRST, before any
endpoint." This is why — every lead mutation in the product funnels through
here, and the guarantees it makes are the ones M7's undo and M6's history
filters are built on:

1. **Every mutation opens a changeset** (architecture rule 5a). Single edit,
   bulk edit, import, distribution, intake — all of them. Every action produced
   carries the `changeset_id`.
2. **Every mutation writes its actions in the same transaction as the change**
   (rule 5). The timeline is the audit trail; a timeline that can diverge from
   the data is not one.
3. **`STAGE_CHANGE` and `ASSIGNMENT_CHANGE` carry old and new ids** (rule 5b),
   because M6's transition filters query them through expression indexes.
4. **`score_applied` is snapshotted**, so editing a custom action type's score
   later does not rewrite history.

Nothing here commits. The caller owns the transaction boundary, which is what
lets a lead update and its four actions land atomically.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from sqlalchemy import func, select

from app.errors import api_error
from app.models.enums import ChangesetSource, SystemActionKind
from app.models.field import CustomActionType
from app.models.integration import OutboxEvent, WebhookEndpoint
from app.models.lead import Action, Changeset, Lead
from app.tenancy.session import ScopedSession

__all__ = ["ActionWriter", "FieldDelta"]


class FieldDelta:
    """One field's before and after, as a `FIELD_CHANGE` payload.

    A tiny value type rather than a tuple, because the payload shape is part of
    the contract M7's undo reads: `{field_key, label, old, new}`.
    """

    __slots__ = ("field_key", "label", "new", "old")

    def __init__(self, field_key: str, label: str, old: Any, new: Any) -> None:
        self.field_key = field_key
        self.label = label
        self.old = old
        self.new = new

    def as_payload(self) -> dict[str, Any]:
        return {
            "field_key": self.field_key,
            "label": self.label,
            "old": self.old,
            "new": self.new,
        }


class ActionWriter:
    """Opens changesets and appends actions.

    One instance per request. `open_changeset` is called once per mutation
    batch; every `record_*` after it stamps that changeset onto the action.
    """

    def __init__(self, session: ScopedSession, *, actor_id: uuid.UUID | None) -> None:
        self._session = session
        self._actor_id = actor_id
        self._changeset: Changeset | None = None
        #: Endpoints subscribed in this workspace, loaded once in
        #: `open_changeset`. Held so `_append` can queue outbound events
        #: synchronously — see `_publish`.
        self._endpoints: list[WebhookEndpoint] = []

    # --- changesets --------------------------------------------------------

    async def open_changeset(
        self, *, source: ChangesetSource, summary: str, lead_count: int = 1
    ) -> Changeset:
        """Start a batch. Every action written afterwards belongs to it.

        Deliberately explicit rather than implicit-on-first-write: the summary
        ("Set Stage on 312 leads") is knowable at the start and not at the end,
        and the edit report is unreadable without it.
        """
        changeset = Changeset(
            source=source,
            actor_id=self._actor_id,
            summary=summary,
            lead_count=lead_count,
        )
        self._session.add(changeset)
        await self._session.flush()
        self._changeset = changeset

        # Load subscriptions now, while we are still in an async context. This
        # is what lets `_append` queue events without being async itself, and
        # therefore what makes publishing unforgettable rather than something
        # every call site has to remember (rule 8, and the same reasoning as
        # rule 5a's changesets).
        rows = await self._session.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.workspace_id == self._session.workspace_id,
                WebhookEndpoint.is_active.is_(True),
            )
        )
        self._endpoints = list(rows.scalars().all())
        return changeset

    @property
    def changeset(self) -> Changeset:
        if self._changeset is None:  # pragma: no cover — a programming error
            raise RuntimeError(
                "No changeset is open. Every mutation opens one before writing "
                "actions (architecture rule 5a) — otherwise it cannot be undone."
            )
        return self._changeset

    # --- writing actions ---------------------------------------------------

    def _append(
        self,
        lead: Lead,
        *,
        kind: SystemActionKind,
        payload: Mapping[str, Any] | None = None,
        body: str | None = None,
        action_type_id: uuid.UUID | None = None,
        score_applied: int = 0,
        performed_at: dt.datetime | None = None,
    ) -> Action:
        action = Action(
            lead_id=lead.id,
            changeset_id=self.changeset.id,
            kind=kind,
            action_type_id=action_type_id,
            actor_id=self._actor_id,
            payload=dict(payload or {}),
            body=body,
            score_applied=score_applied,
            performed_at=performed_at or dt.datetime.now(dt.UTC),
        )
        self._session.add(action)

        # Keep the denormalised rollups on the lead in step, in the same
        # transaction. A `last_action_at` that lags its timeline would make the
        # "no contact in 14 days" filter wrong.
        if score_applied:
            lead.score = (lead.score or 0) + score_applied
        if action.performed_at and (
            lead.last_action_at is None or action.performed_at > lead.last_action_at
        ):
            lead.last_action_at = action.performed_at

        self._publish(lead, action)
        return action

    # --- outbound events ---------------------------------------------------

    #: Which timeline event becomes which outbound event (§Outbound). Anything
    #: not named here is an ordinary timeline entry and rides `action.created`.
    _EVENT_FOR_KIND: ClassVar[dict[SystemActionKind, str]] = {
        SystemActionKind.LEAD_CREATED: "lead.created",
        SystemActionKind.FIELD_CHANGE: "lead.field_changed",
        SystemActionKind.STAGE_CHANGE: "lead.stage_changed",
        SystemActionKind.ASSIGNMENT_CHANGE: "lead.assigned",
        SystemActionKind.RATING_CHANGE: "lead.updated",
        SystemActionKind.TASK_CREATED: "task.created",
        SystemActionKind.TASK_COMPLETED: "task.completed",
    }

    def _publish(self, lead: Lead, action: Action) -> None:
        """Queue this action's outbound event, in the same transaction.

        Written here rather than at each call site for the same reason actions
        themselves are: there is one place a mutation passes through, so there
        is one place the event can be missed, and it is this one.

        Costs nothing in a workspace with no integrations — `_endpoints` is
        empty and this returns immediately.
        """
        if not self._endpoints:
            return

        event = self._EVENT_FOR_KIND.get(action.kind, "action.created")
        data = {
            "lead_id": str(lead.id),
            "identity_value": lead.identity_value,
            "stage_id": str(lead.stage_id) if lead.stage_id else None,
            "assignee_id": str(lead.assignee_id) if lead.assignee_id else None,
            "action_kind": action.kind.value,
            "actor_id": str(self._actor_id) if self._actor_id else None,
            "changeset_id": str(self.changeset.id),
            # Unprojected. The dispatcher projects per endpoint at delivery, so
            # a row queued before a permission was revoked still respects it.
            "values": dict(lead.values or {}),
            "payload": dict(action.payload or {}),
        }

        for endpoint in self._endpoints:
            # An empty subscription list means everything — the useful default
            # for somebody wiring up their first integration.
            if endpoint.events and event not in endpoint.events:
                continue
            self._session.add(
                OutboxEvent(
                    event=event,
                    event_id=uuid.uuid4(),
                    endpoint_id=endpoint.id,
                    payload=data,
                    occurred_at=action.performed_at or dt.datetime.now(dt.UTC),
                    next_attempt_at=action.performed_at or dt.datetime.now(dt.UTC),
                )
            )

    def record_created(self, lead: Lead) -> Action:
        return self._append(lead, kind=SystemActionKind.LEAD_CREATED)

    def record_field_changes(self, lead: Lead, deltas: Sequence[FieldDelta]) -> list[Action]:
        """One `FIELD_CHANGE` per changed field, all sharing one changeset.

        Per-field rather than one action listing everything, because the
        timeline is read per field ("when did the budget change?") and undo
        reverses per field.
        """
        return [
            self._append(lead, kind=SystemActionKind.FIELD_CHANGE, payload=delta.as_payload())
            for delta in deltas
        ]

    def record_stage_change(
        self,
        lead: Lead,
        *,
        old_stage_id: uuid.UUID | None,
        new_stage_id: uuid.UUID | None,
        lost_reason_id: uuid.UUID | None = None,
    ) -> Action:
        """Old and new ids in the payload (rule 5b).

        M6's "status went from HOT to Lost last week" filter reads exactly these
        two keys through `actions_status_change_idx`. Storing only the new value
        would make that filter impossible without a self-join over the timeline.
        """
        return self._append(
            lead,
            kind=SystemActionKind.STAGE_CHANGE,
            payload={
                "old_stage_id": str(old_stage_id) if old_stage_id else None,
                "new_stage_id": str(new_stage_id) if new_stage_id else None,
                "lost_reason_id": str(lost_reason_id) if lost_reason_id else None,
            },
        )

    def record_assignment_change(
        self,
        lead: Lead,
        *,
        old_assignee_id: uuid.UUID | None,
        new_assignee_id: uuid.UUID | None,
    ) -> Action:
        """Old and new ids again — "moved off Priya this month" needs both."""
        return self._append(
            lead,
            kind=SystemActionKind.ASSIGNMENT_CHANGE,
            payload={
                "old_assignee_id": str(old_assignee_id) if old_assignee_id else None,
                "new_assignee_id": str(new_assignee_id) if new_assignee_id else None,
            },
        )

    def record_rating_change(
        self, lead: Lead, *, old_rating: int | None, new_rating: int | None
    ) -> Action:
        return self._append(
            lead,
            kind=SystemActionKind.RATING_CHANGE,
            payload={"old": old_rating, "new": new_rating},
        )

    def record_note(self, lead: Lead, *, body: str) -> Action:
        return self._append(lead, kind=SystemActionKind.NOTE, body=body)

    def record_task(
        self,
        lead: Lead,
        *,
        completed: bool,
        task_id: uuid.UUID,
        title: str,
        due_at: dt.datetime | None = None,
    ) -> Action:
        """A task appearing on, or leaving, a lead's timeline.

        The timeline is the audit trail (rule 5), and "someone promised to call
        this lead back on Thursday" belongs in it as much as the call itself.
        `task_id` travels in the payload so the timeline entry can link to the
        task rather than merely describing it.
        """
        return self._append(
            lead,
            kind=(SystemActionKind.TASK_COMPLETED if completed else SystemActionKind.TASK_CREATED),
            payload={
                "task_id": str(task_id),
                "title": title,
                "due_at": due_at.isoformat() if due_at else None,
            },
            body=title,
        )

    def record_call(
        self,
        lead: Lead,
        *,
        direction: str,
        disposition_id: uuid.UUID,
        duration_seconds: int,
        notes: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> Action:
        """A logged call.

        There is no telephony in v1 (CLAUDE.md): this records what a human says
        happened. Nothing here implies a provider, and there is deliberately no
        provider interface to "fill in later".

        `extra` is additive payload for a call logged by an integration rather
        than a person — the voice webhook uses it to mark `source: AI_CALL` and
        carry the execution id. It cannot overwrite the four core keys.
        """
        payload: dict[str, Any] = dict(extra or {})
        payload.update(
            {
                "direction": direction,
                "disposition_id": str(disposition_id),
                "duration_seconds": duration_seconds,
                "notes": notes,
            }
        )
        return self._append(
            lead,
            kind=SystemActionKind.CALL_LOGGED,
            payload=payload,
            body=notes,
        )

    def record_message(
        self, lead: Lead, *, kind: SystemActionKind, body: str, template_id: uuid.UUID | None
    ) -> Action:
        """A WhatsApp/SMS/email send.

        WhatsApp is a client-side `wa.me` deep link followed by this action, so
        nothing in the payload may imply delivery — only that the operator was
        handed a composed message.
        """
        return self._append(
            lead,
            kind=kind,
            body=body,
            payload={
                "template_id": str(template_id) if template_id else None,
                # Explicit, so no reader mistakes this for a delivery receipt.
                "delivery_confirmed": False,
            },
        )

    def record_imported(
        self,
        lead: Lead,
        *,
        kind: SystemActionKind,
        performed_at: dt.datetime,
        body: str | None = None,
        payload: Mapping[str, Any] | None = None,
        action_type_id: uuid.UUID | None = None,
        score_applied: int = 0,
    ) -> Action:
        """A historical event, migrated from another system (M7).

        Predated on purpose. Every other writer here stamps `performed_at` with
        now, because it is recording something that just happened; an imported
        timeline is recording things that happened years ago, and one whose
        events all landed at import o'clock is not a timeline.

        Deliberately restricted to kinds that describe *contact* — the caller
        chooses from `NOTE`, calls and messages. A sheet must not be able to
        fabricate a `FIELD_CHANGE` or `STAGE_CHANGE`, whose payloads carry old
        and new values that M7's undo would later try to replay against a
        history that never happened.
        """
        return self._append(
            lead,
            kind=kind,
            payload={**(payload or {}), "imported": True},
            body=body,
            action_type_id=action_type_id,
            score_applied=score_applied,
            performed_at=performed_at,
        )

    async def record_custom(
        self,
        lead: Lead,
        *,
        action_type: CustomActionType,
        values: Mapping[str, Any],
        performed_at: dt.datetime | None = None,
    ) -> Action:
        """A custom action, with its score snapshotted.

        Predated timestamps are refused unless the type allows them (§4.2) —
        otherwise a rep could quietly backdate activity into a closed reporting
        period.
        """
        now = dt.datetime.now(dt.UTC)
        if performed_at is not None and performed_at < now - dt.timedelta(minutes=1):
            if not action_type.allow_predated:
                raise api_error(
                    422,
                    "predated_not_allowed",
                    f"{action_type.name} cannot be logged with a past timestamp",
                )
            if performed_at > now:
                raise api_error(422, "future_action", "An action cannot be logged in the future")

        return self._append(
            lead,
            kind=SystemActionKind.CUSTOM,
            action_type_id=action_type.id,
            payload=dict(values),
            # Snapshot: editing the type's score later must not rewrite this.
            score_applied=action_type.score,
            performed_at=performed_at,
        )

    # --- reads -------------------------------------------------------------

    async def recompute_score(self, lead: Lead) -> int:
        """Re-derive a lead's score from its timeline.

        The rollup on `leads.score` is maintained incrementally; this is the
        authority it is checked against, and what a repair job would use.
        """
        result = await self._session.execute(
            select(func.coalesce(func.sum(Action.score_applied), 0)).where(
                Action.lead_id == lead.id,
                Action.workspace_id == self._session.workspace_id,
            )
        )
        total = int(result.scalar_one())
        lead.score = total
        return total
