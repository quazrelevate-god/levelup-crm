"""Reading past AI calls back out (docs/13 §6).

The post-call webhook already stores everything this serves: transcript,
duration, summary, extracted data and the vendor's own body all live on
`voice_call_executions`. Nothing here writes, and there is deliberately no new
table — a second store of the same calls is exactly the duplication the
milestone forbids.

Two reads, and one rule they share:

- `list_calls` — a lead's calls, or the workspace's, newest first.
- `get_call` — one call, with the heavy parts (transcript, extracted data,
  raw payload) attached.

**The lead each call belongs to is resolved through `LeadService.project`**,
so the headline values shown next to a call are the caller's View-projected
ones (architecture rule 3) — a field somebody cannot see on the lead page
cannot arrive here either. Visibility is enforced the same way: a call whose
lead the caller cannot see does not exist as far as these reads are concerned,
which is what keeps one member's calls out of another's list.

**The raw payload is sanitised on the way out.** It is a vendor body, stored
verbatim by design; anything credential-shaped in it is redacted here rather
than at write time, so the stored evidence stays intact while the API response
cannot carry a secret.
"""

from __future__ import annotations

import dataclasses
import re
import uuid
from typing import Any

from sqlalchemy import func

from app.errors import not_found
from app.models.field import LeadField
from app.models.lead import Lead
from app.models.voice import VoiceCallExecution
from app.models.workspace import Workspace
from app.services.leads import LeadService, lead_visibility_clause
from app.tenancy.session import ScopedSession

__all__ = [
    "REDACTED",
    "CallWithLead",
    "VoiceCallHistoryService",
    "redact_payload",
]

REDACTED = "<redacted>"

#: A key whose *name* suggests a credential. Same reasoning as the Bolna
#: client's error sanitiser: redact by name, never by guessing which values
#: look secret, because a rule that guesses eventually redacts the call itself.
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|access[_-]?key|secret|token|password|passwd|authorization|auth|credential|"
    r"bearer|signature|private[_-]?key)",
    re.IGNORECASE,
)

#: How deep to walk before giving up. A vendor body is a few levels deep; this
#: exists so a pathological payload cannot spend the request in recursion.
_MAX_DEPTH = 12


def redact_payload(value: Any, *, extra_secrets: tuple[str, ...] = (), _depth: int = 0) -> Any:
    """A copy of `value` with credential-shaped entries replaced.

    Redacts by key name at any depth, and additionally replaces any exact
    occurrence of a configured secret (the deployment's Bolna key) wherever it
    appears as a string — an upstream that echoed it back must not be able to
    put it into an API response.
    """
    if _depth > _MAX_DEPTH:
        return REDACTED

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                out[key] = REDACTED
            else:
                out[key] = redact_payload(item, extra_secrets=extra_secrets, _depth=_depth + 1)
        return out
    if isinstance(value, list):
        return [
            redact_payload(item, extra_secrets=extra_secrets, _depth=_depth + 1) for item in value
        ]
    if isinstance(value, str):
        text = value
        for secret in extra_secrets:
            if secret and secret in text:
                text = text.replace(secret, REDACTED)
        return text
    return value


@dataclasses.dataclass(slots=True)
class CallWithLead:
    """One execution row plus the projected lead it belongs to."""

    call: VoiceCallExecution
    lead: Lead
    #: The caller's View-projected headline values, and their field labels.
    primary_h1: Any = None
    primary_h2: Any = None
    primary_h1_label: str | None = None
    primary_h2_label: str | None = None


class VoiceCallHistoryService:
    """Built per request, like every other service here."""

    def __init__(
        self,
        session: ScopedSession,
        *,
        workspace: Workspace,
        leads: LeadService,
        visible_membership_ids: frozenset[uuid.UUID],
        sees_all: bool,
    ) -> None:
        self._session = session
        self._workspace = workspace
        self._leads = leads
        self._visible = visible_membership_ids
        self._sees_all = sees_all
        self._labels: dict[str, str | None] | None = None

    # --- the lead a call belongs to ----------------------------------------

    async def _primary_labels(self) -> dict[str, str | None]:
        """Labels for the workspace's two headline fields.

        Read from the field definitions rather than assumed: which fields are
        H1 and H2 is the admin's choice, and their labels are the customer's
        own words (there is no product-owned "Name" or "Course").
        """
        if self._labels is not None:
            return self._labels

        wanted = {
            self._workspace.primary_field_1_id,
            self._workspace.primary_field_2_id,
        } - {None}
        labels: dict[str, str | None] = {"h1": None, "h2": None}
        if wanted:
            rows = await self._session.execute(
                self._session.select(LeadField).where(LeadField.id.in_(wanted))
            )
            by_id = {field.id: field for field in rows.scalars().all()}
            first = by_id.get(self._workspace.primary_field_1_id or uuid.uuid4())
            second = by_id.get(self._workspace.primary_field_2_id or uuid.uuid4())
            labels = {
                "h1": first.label if first else None,
                "h2": second.label if second else None,
            }
        self._labels = labels
        return labels

    async def _with_lead(self, call: VoiceCallExecution) -> CallWithLead:
        lead = await self._leads.get_lead(call.lead_id)
        projected = await self._leads.project(lead)
        primary = projected.get("primary") or {}
        labels = await self._primary_labels()
        return CallWithLead(
            call=call,
            lead=lead,
            primary_h1=primary.get("h1"),
            primary_h2=primary.get("h2"),
            primary_h1_label=labels["h1"],
            primary_h2_label=labels["h2"],
        )

    # --- reads --------------------------------------------------------------

    def _visible_calls(self) -> Any:
        """Executions joined to leads this caller may see.

        The join is what enforces isolation: `ScopedSession` already bounds
        both tables to the workspace, and `lead_visibility_clause` bounds the
        leads to the caller's own when their template does not see all.
        """
        statement = self._session.select(VoiceCallExecution).join(
            Lead, Lead.id == VoiceCallExecution.lead_id
        )
        statement = statement.where(Lead.deleted_at.is_(None))
        clause = lead_visibility_clause(sees_all=self._sees_all, visible=self._visible)
        if clause is not None:
            statement = statement.where(clause)
        return statement

    async def list_calls(
        self,
        *,
        lead_id: uuid.UUID | None = None,
        completed_only: bool = False,
        limit: int,
        offset: int,
    ) -> tuple[list[CallWithLead], int]:
        """Calls, newest first. Optionally one lead's, optionally finished ones.

        `completed_only` is what the lead panel's card asks for: a call that
        never completed has no summary to show, and showing a queued attempt
        as though it were a conversation would be a lie.
        """
        statement = self._visible_calls()
        if lead_id is not None:
            statement = statement.where(VoiceCallExecution.lead_id == lead_id)
        if completed_only:
            statement = statement.where(VoiceCallExecution.completed_at.is_not(None))

        counted = statement.with_only_columns(func.count(VoiceCallExecution.id)).order_by(None)
        total = int((await self._session.execute(counted)).scalar_one())

        rows = await self._session.execute(
            statement.order_by(VoiceCallExecution.created_at.desc()).limit(limit).offset(offset)
        )
        calls = list(rows.scalars().all())
        return [await self._with_lead(call) for call in calls], total

    async def get_call(self, call_id: uuid.UUID) -> CallWithLead:
        """One call, or a 404.

        A call in another workspace, or against a lead this caller cannot see,
        is `not_found` rather than forbidden — the same answer
        `LeadService.get_lead` gives, and for the same reason: a 403 would
        confirm the id exists.
        """
        rows = await self._session.execute(
            self._visible_calls().where(VoiceCallExecution.id == call_id).limit(1)
        )
        call: VoiceCallExecution | None = rows.scalar_one_or_none()
        if call is None:
            raise not_found("Call")
        return await self._with_lead(call)

    def sanitised_payload(self, call: VoiceCallExecution, *, secret: str | None) -> dict[str, Any]:
        """The vendor body, safe to send to a browser."""
        extra = (secret,) if secret else ()
        payload = redact_payload(dict(call.raw_payload or {}), extra_secrets=extra)
        return payload if isinstance(payload, dict) else {}
