"""The CRM-side voice-context service (pre-Bolna milestone).

`docs/06-voice-integration-contract.md` and
`docs/09-context-continuity-and-bolna-integration.md`. This module knows
nothing about Bolna and calls no external service — it stores and serves one
thing: a durable, per-lead digest of the most recent voice conversation, plus
the lead's current CRM state, assembled the same way every other read in this
product is assembled: through `FieldProjectionService`, never around it.

Two operations:

- `get_context` — read everything a voice agent needs for its next call to a
  lead, resolved either by CRM lead id or by phone number.
- `update_last_call_summary` — record what happened on a call, idempotently.

Both reuse `LeadService` for identity resolution, phone normalisation and
field projection rather than re-implementing any of it, and both write
through `ActionWriter` so a call summary lands on the timeline exactly like
every other lead mutation (architecture rules 5 and 5a) — undoable, auditable,
and never a bypass of the one place lead writes are supposed to go through.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from typing import Any

from sqlalchemy.orm import selectinload

from app.errors import api_error, not_found
from app.models.enums import ChangesetSource
from app.models.lead import Action, Lead
from app.models.pipeline import Stage
from app.models.voice import VoiceCallContext
from app.models.workspace import Membership, Workspace
from app.services.actions import ActionWriter
from app.services.leads import LeadService, lead_visibility_clause
from app.tenancy.session import ScopedSession

__all__ = ["VoiceContext", "VoiceContextService"]

#: How many recent timeline entries ride along in a context payload. Not a
#: full timeline dump — `GET /leads/{id}/actions` already exists for that —
#: just enough for a voice prompt to reference "we last spoke about X".
RECENT_INTERACTIONS_LIMIT = 10


@dataclasses.dataclass(slots=True)
class VoiceContext:
    """The assembled payload a voice agent needs to continue a conversation."""

    lead_id: uuid.UUID
    identity_value: str
    name: str | None
    phone: str | None
    email: str | None
    stage_id: uuid.UUID | None
    stage_name: str | None
    assignee_id: uuid.UUID | None
    assignee_name: str | None
    values: dict[str, Any]
    last_call_summary: str | None
    last_call_at: dt.datetime | None
    call_count: int
    recent_interactions: list[dict[str, Any]]
    created_at: dt.datetime
    last_action_at: dt.datetime | None


class VoiceContextService:
    """Built per request, exactly like `LeadService`, with the same chokepoints
    already bound (it is handed a `LeadService` rather than rebuilding one)."""

    def __init__(
        self,
        session: ScopedSession,
        *,
        workspace: Workspace,
        leads: LeadService,
        visible_membership_ids: frozenset[uuid.UUID],
        sees_all: bool,
        actor_id: uuid.UUID | None,
    ) -> None:
        self._session = session
        self._workspace = workspace
        self._leads = leads
        self._visible = visible_membership_ids
        self._sees_all = sees_all
        self._actor_id = actor_id

    # --- lookup --------------------------------------------------------

    async def find_lead_by_phone(self, raw_phone: str) -> Lead:
        """Resolve a lead by phone number, normalised exactly as the write
        path does — so a lookup and a create can never disagree about
        identity, and so calling the same number twice never creates a
        second lead (architecture rule 7's identity uniqueness already
        enforces this at the schema level; this just has to query it the
        same way).

        Raises a 422 when `raw_phone` cannot be parsed as a phone number at
        all — distinct from `not_found`, which is for a well-formed number
        that simply matches no lead, a soft-deleted lead, or one outside this
        caller's visibility (all indistinguishable, matching
        `LeadService.get_lead`'s own behaviour for the same reasons).
        """
        normalised = await self._leads.normalise_identity(raw_phone)
        if normalised is None:
            raise api_error(422, "invalid_phone", "Not a valid phone number for this workspace")

        statement = (
            self._session.select(Lead)
            .where(Lead.identity_value == normalised, Lead.deleted_at.is_(None))
            .limit(1)
        )
        clause = lead_visibility_clause(sees_all=self._sees_all, visible=self._visible)
        if clause is not None:
            statement = statement.where(clause)

        rows = await self._session.execute(statement)
        lead: Lead | None = rows.scalar_one_or_none()
        if lead is None:
            raise not_found("Lead")
        return lead

    async def _voice_row(self, lead_id: uuid.UUID) -> VoiceCallContext | None:
        rows = await self._session.execute(
            self._session.select(VoiceCallContext)
            .where(VoiceCallContext.lead_id == lead_id)
            .limit(1)
        )
        result: VoiceCallContext | None = rows.scalar_one_or_none()
        return result

    # --- reads -----------------------------------------------------------

    async def get_context(self, lead: Lead) -> VoiceContext:
        """Assemble the full voice-agent context for one lead.

        Every field pulled from `lead.values` goes through
        `LeadService.project`, so a field the caller's template does not
        grant View on is absent here exactly as it is everywhere else in the
        product — never a separate, looser read path for "internal" callers.
        """
        projected = await self._leads.project(lead)
        voice_row = await self._voice_row(lead.id)
        identity_key = await self._leads.identity_key()

        stage_name: str | None = None
        if lead.stage_id is not None:
            stage = await self._session.get(Stage, lead.stage_id)
            stage_name = stage.label if stage else None

        assignee_name: str | None = None
        if lead.assignee_id is not None:
            rows = await self._session.execute(
                self._session.select(Membership)
                .where(Membership.id == lead.assignee_id)
                .options(selectinload(Membership.user))
                .limit(1)
            )
            membership = rows.scalar_one_or_none()
            if membership is not None and membership.user is not None:
                assignee_name = membership.user.full_name

        values = projected["values"]
        recent = await self._recent_interactions(lead.id)

        return VoiceContext(
            lead_id=lead.id,
            identity_value=lead.identity_value,
            # `name` and `email` are two of the four fields every workspace is
            # provisioned with (docs/01-data-model.md §7) — structural
            # built-ins, not a customer's own taxonomy, so referencing their
            # keys directly is the same kind of reference `identity_field_id`
            # and `primary_field_1_id` already make. Absent (never renamed
            # away, but conceivably archived) degrades to None, not an error.
            name=values.get("name"),
            # The workspace's own identity field — Phone by default, but
            # whichever field the admin designated. Contract §4 calls this
            # "the lead identity value, E.164".
            phone=values.get(identity_key) if identity_key else None,
            email=values.get("email"),
            stage_id=lead.stage_id,
            stage_name=stage_name,
            assignee_id=lead.assignee_id,
            assignee_name=assignee_name,
            values=values,
            last_call_summary=voice_row.last_call_summary if voice_row else None,
            last_call_at=voice_row.last_call_at if voice_row else None,
            call_count=voice_row.call_count if voice_row else 0,
            recent_interactions=recent,
            created_at=lead.created_at,
            last_action_at=lead.last_action_at,
        )

    async def _recent_interactions(self, lead_id: uuid.UUID) -> list[dict[str, Any]]:
        rows = await self._session.execute(
            self._session.select(Action)
            .where(Action.lead_id == lead_id)
            .order_by(Action.performed_at.desc())
            .limit(RECENT_INTERACTIONS_LIMIT)
        )
        actions: list[Action] = list(rows.scalars().all())
        return [
            {"kind": action.kind.value, "body": action.body, "performed_at": action.performed_at}
            for action in actions
        ]

    # --- writes ------------------------------------------------------------

    async def update_last_call_summary(
        self,
        lead: Lead,
        *,
        summary: str,
        external_id: str | None = None,
    ) -> tuple[VoiceCallContext, bool]:
        """Record the digest of the latest call. Idempotent on `external_id`.

        One row per lead, upserted — never a second `voice_call_contexts` row
        for the same lead (enforced at the schema level too, by
        `voice_call_contexts_lead_uq`). A request carrying the same
        `external_id` as the last recorded write is treated as a retried
        delivery of the same call: the row is left untouched and no second
        timeline action is written. Returns `(row, written)`; `written` is
        `False` on that no-op path.

        Interim design note: this writes a `NOTE` action, not a `CUSTOM` one
        tied to an admin-configured "Voice Call" action type. The full
        contract (docs/06 §6.4) calls for the latter, but that requires a
        workspace admin to have configured the action type first — out of
        scope for this CRM-only milestone, which must work against a bare
        workspace. Swapping this for the `CUSTOM` shape is one of the changes
        the real Bolna wiring will make.
        """
        existing = await self._voice_row(lead.id)
        if (
            existing is not None
            and external_id is not None
            and existing.last_call_external_id == external_id
        ):
            return existing, False

        now = dt.datetime.now(dt.UTC)
        if existing is None:
            row = VoiceCallContext(lead_id=lead.id, call_count=0)
            self._session.add(row)
        else:
            row = existing

        row.last_call_summary = summary
        row.last_call_at = now
        row.last_call_external_id = external_id
        row.call_count = (row.call_count or 0) + 1

        writer = ActionWriter(self._session, actor_id=self._actor_id)
        await writer.open_changeset(
            source=ChangesetSource.AUTOMATION,
            summary=f"Voice call summary recorded for {lead.identity_value}",
        )
        writer.record_note(lead, body=summary)

        await self._session.flush()
        return row, True
