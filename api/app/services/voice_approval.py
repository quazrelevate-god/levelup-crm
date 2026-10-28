"""Approval for AI calling: a person's yes before a batch may dial a lead (docs/15).

A batch call reaches many people nobody has looked at one by one. The switch
on the lead screen is that look: open the lead, check what the agent will
know, and approve it. Batches dial approved leads only. A single "AI Call"
pressed on the lead screen does not need it — pressing it *is* the approval.

Each flip is a mutation, so it opens a changeset and writes one timeline note
(architecture rules 5 and 5a). Setting the switch to where it already is
writes nothing: a second click is not a second decision.
"""

from __future__ import annotations

import datetime as dt
import uuid

from app.models.enums import ChangesetSource
from app.models.lead import Lead
from app.services.actions import ActionWriter
from app.tenancy.session import ScopedSession

__all__ = ["APPROVED_NOTE", "REVOKED_NOTE", "VoiceApprovalService"]

APPROVED_NOTE = "Approved for AI calling."
REVOKED_NOTE = "Removed from AI calling."


class VoiceApprovalService:
    def __init__(self, session: ScopedSession, *, actor_id: uuid.UUID | None) -> None:
        self._session = session
        self._actor_id = actor_id

    async def set_approved(self, lead: Lead, *, approved: bool) -> bool:
        """Flip the switch. Returns whether anything changed."""
        if approved == (lead.voice_approved_at is not None):
            return False

        writer = ActionWriter(self._session, actor_id=self._actor_id)
        await writer.open_changeset(
            source=ChangesetSource.SINGLE_EDIT,
            summary=f"{'Approved' if approved else 'Removed'} {lead.identity_value} for AI calling",
        )
        if approved:
            lead.voice_approved_at = dt.datetime.now(dt.UTC)
            lead.voice_approved_by_id = self._actor_id
        else:
            lead.voice_approved_at = None
            lead.voice_approved_by_id = None
        writer.record_note(lead, body=APPROVED_NOTE if approved else REVOKED_NOTE)
        await self._session.flush()
        return True
