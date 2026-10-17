"""Which follow-up call a lead is due, and whether it may be placed at all.

The voice agent holds three follow-up openings and branches on
`crm_call_purpose`. Something has to decide which one, and the honest place for
that decision is here rather than in the browser: a button that let an operator
pick the opening would eventually pick the wrong one, and the agent would greet
somebody with "you were going to come back to me" who never said any such
thing.

**The rule is deliberately crude, and that is the point.** It counts how many
follow-up calls this lead has already been sent, and nothing else:

    none yet            -> FOLLOWUP          "I've been meaning to get back to you"
    one or more         -> FOLLOWUP_MISSED   "you were going to come back to me"

It would be possible to read the last call's summary and look for a promise to
call back. It would also be guesswork over a model's prose, and being wrong
there is not a small error — it is the agent telling somebody they broke a
promise they never made. Counting attempts cannot do that: if this is the
second follow-up, the caller has demonstrably not come back, whatever was said.

`FOLLOWUP_BOOKING` is never returned. It is the opening for someone who has
paid but not booked their slot, and the CRM cannot see payments yet. An agent
that opened with "your application fee came through" when it had not would be
worse than no call at all, so the only safe number of ways to reach that
opening, today, is zero.

**Why `context_sent` rather than a column.** The purpose of a call is already
recorded: `voice_call_executions.context_sent` is the exact `user_data` that
went to Bolna, stored for audit, and `crm_call_purpose` is one of its keys. A
column would duplicate that and could drift from it. Counts per lead are in
single figures, so the JSONB read costs nothing worth a migration.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from app.models.enums import VoiceCallPurpose
from app.models.voice import VoiceCallExecution

if TYPE_CHECKING:
    from app.tenancy.session import ScopedSession

__all__ = ["PURPOSE_KEY", "FollowupReadiness", "VoiceFollowupService"]

#: The `user_data` key the agent's graph reads on its expression edge. Changing
#: this string silently disables every follow-up: the greeting node simply never
#: hands over, and calls complete as ordinary first calls with nobody the wiser.
PURPOSE_KEY = "crm_call_purpose"


class FollowupReadiness:
    """Whether a follow-up may be placed for a lead, and which one.

    A plain object rather than a tuple because the two answers are read in
    different places — the endpoint refuses on `reason`, the trigger passes
    `purpose` to Bolna — and a bare tuple at two call sites is how they end up
    swapped.
    """

    __slots__ = ("purpose", "reason")

    def __init__(self, purpose: VoiceCallPurpose | None, reason: str | None) -> None:
        self.purpose = purpose
        self.reason = reason

    @property
    def allowed(self) -> bool:
        return self.reason is None


class VoiceFollowupService:
    """Reads a lead's call history to decide its next follow-up."""

    def __init__(self, session: ScopedSession) -> None:
        self._session = session

    async def completed_calls(self, lead_id: uuid.UUID) -> int:
        """How many AI calls to this lead have actually finished.

        `completed_at` rather than `status`: it is the column the webhook sets
        on the first terminal delivery, and the same gate the rest of the voice
        code treats as "this call is done".
        """
        rows = await self._session.execute(
            select(func.count())
            .select_from(VoiceCallExecution)
            .where(
                VoiceCallExecution.lead_id == lead_id,
                VoiceCallExecution.completed_at.is_not(None),
            )
        )
        return int(rows.scalar_one())

    async def followups_sent(self, lead_id: uuid.UUID) -> int:
        """How many calls to this lead already carried a follow-up purpose.

        Every attempt counts, finished or not. A follow-up that rang out is
        still a follow-up the caller did not answer, and counting only the
        completed ones would open the same conversation twice.
        """
        rows = await self._session.execute(
            select(func.count())
            .select_from(VoiceCallExecution)
            .where(
                VoiceCallExecution.lead_id == lead_id,
                VoiceCallExecution.context_sent[PURPOSE_KEY].astext.is_not(None),
            )
        )
        return int(rows.scalar_one())

    async def readiness(self, lead_id: uuid.UUID) -> FollowupReadiness:
        """The follow-up this lead is due, or why it may not be called.

        The one refusal is a lead with no finished call. The follow-up node
        opens by referring to a conversation that has already happened — "last
        time you were asking about…" — and its own rules fall back to "we spoke
        a few days back" when the summary is missing. Both are lies if nobody
        ever spoke. So the first call has to be a first call, and this refuses
        rather than letting the agent invent a shared history.
        """
        if await self.completed_calls(lead_id) == 0:
            return FollowupReadiness(
                None,
                "This lead has no finished AI call yet, so there is no previous "
                "conversation for a follow-up to refer back to.",
            )
        sent = await self.followups_sent(lead_id)
        return FollowupReadiness(
            VoiceCallPurpose.FOLLOWUP if sent == 0 else VoiceCallPurpose.FOLLOWUP_MISSED,
            None,
        )
