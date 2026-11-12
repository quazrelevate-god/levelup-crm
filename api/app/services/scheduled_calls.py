"""AI calls to place later (docs/22).

A lead says "call me tomorrow at 5:30"; a workflow says "call them"; a person
picks a time. Each becomes a `voice_scheduled_calls` row, and the worker places
it when due (`app/services/scheduled_call_runner.py`).

**Reading "tomorrow 5:30 pm".** The lead's words are turned into an exact
moment in the company's own timezone, relative to when the call ended, always
in the future. A small translator first turns loose and Hinglish phrasing
("evening 6", "kal shaam 6 baje") into words the date reader knows. Anything it
still cannot pin down becomes a **task for a person** — a guessed call at the
wrong time is worse than a human reading the note.

**Calling hours.** A time outside the permitted window moves to the next time
calls are allowed (and the timeline says so), because the worker could not
place it then anyway.

**One waiting call per lead.** A newer schedule cancels the older one: a lead
who said "Monday" and then "Tuesday" meant Tuesday.

**Bulk AI calls** (docs/23). Selected leads are queued to be called now, in the
order chosen; the worker keeps up to `voice_max_parallel_calls` going and starts
the next as each ends. A lead already waiting for a call keeps that one, and the
never-AI-call rule is checked up front so the person sees who was left out.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import dateparser
from sqlalchemy import func, select

from app.errors import api_error, not_found
from app.models.enums import ChangesetSource
from app.models.lead import Lead
from app.models.voice import VoiceScheduledCall
from app.models.workspace import Workspace
from app.services.actions import ActionWriter
from app.services.system_filters import lead_matches, parse_rule
from app.services.voice_hours import CallingHours
from app.services.voice_settings import read_never_call
from app.services.work import TaskService
from app.tenancy.session import ScopedSession

__all__ = [
    "SOURCES",
    "BulkQueued",
    "ScheduledCallService",
    "clamp_to_hours",
    "parse_callback",
]

SOURCES = frozenset({"CALLBACK", "WORKFLOW", "MANUAL", "BULK"})
#: A callback closer than this is not a callback; it is the call that just ended.
_MIN_LEAD = dt.timedelta(minutes=2)
#: Nobody means "call me in a year"; treat it as unreadable.
_MAX_AHEAD = dt.timedelta(days=60)

#: Loose words → words the date reader understands. Order matters: longer
#: phrases first. Hinglish because that is how leads in India actually speak.
_PHRASES: tuple[tuple[str, str], ...] = (
    (r"\bday after tomorrow\b", "in 2 days"),
    (r"\bparso\b", "in 2 days"),
    (r"\bkal\b", "tomorrow"),
    (r"\baaj\b", "today"),
    (r"\bsubah\b", "morning"),
    (r"\bdopahar\b", "afternoon"),
    (r"\bshaam\b", "evening"),
    (r"\braat\b", "night"),
    (r"\bbaje\b", ""),
    (r"\bo'?clock\b", ""),
    (r"\b(around|by|at about|approximately|approx|maybe)\b", ""),
)
#: A part of the day with no clock time → a sensible hour.
_DAYPARTS = {"morning": "10 am", "afternoon": "2 pm", "evening": "6 pm", "night": "8 pm"}
#: "evening 6" or "6 evening" → "6 pm": the number is the hour, the daypart
#: says am or pm.
_DAYPART_HOUR = re.compile(r"\b(morning|afternoon|evening|night)\s+(\d{1,2})(?::(\d{2}))?\b")
_HOUR_DAYPART = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s+(morning|afternoon|evening|night)\b")
#: Words people wrap a time in that only confuse the date reader.
_FILLER = re.compile(
    r"\b(please|pls|plz|call|me|back|can|could|you|i am|i'm|i|will|would|be|free|available|then|"
    r"only|ok|okay|sir|madam|mam|maybe|around|by|approximately|approx|about|at|on)\b"
)
#: Anything that names a day; without one, a time means today (or tomorrow).
_HAS_DAY = re.compile(
    r"\b(today|tomorrow|tonight|in \d|next|week|month|day|days|hour|hours|minute|"
    r"minutes|mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|"
    r"sunday|jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|"
    r"april|june|july|august|september|october|november|december|\d{1,2}(st|nd|rd|th)|"
    r"\d{1,2}/\d{1,2})\b"
)


def _normalise(text: str) -> str:
    out = " " + text.lower().strip() + " "
    for pattern, replacement in _PHRASES:
        out = re.sub(pattern, replacement, out)

    def daypart_hour(match: re.Match[str]) -> str:
        part, hour, minute = match.group(1), int(match.group(2)), match.group(3) or "00"
        suffix = "am" if part == "morning" else "pm"
        return f"{hour}:{minute} {suffix}"

    def hour_daypart(match: re.Match[str]) -> str:
        hour, minute, part = int(match.group(1)), match.group(2) or "00", match.group(3)
        suffix = "am" if part == "morning" else "pm"
        return f"{hour}:{minute} {suffix}"

    out = _DAYPART_HOUR.sub(daypart_hour, out)
    out = _HOUR_DAYPART.sub(hour_daypart, out)
    has_clock = bool(re.search(r"\d{1,2}(:\d{2})?\s*(am|pm)|\d{1,2}:\d{2}", out))
    for part, clock in _DAYPARTS.items():
        if part in out:
            out = out.replace(part, "" if has_clock else clock)
    # "after 2 hours" must keep its "after"; any other filler goes.
    out = re.sub(r"\bafter\s+(\d)", r"in \1", out)
    out = _FILLER.sub(" ", out)
    # "5:30 pm" and "6 pm" alone are read as the 1st of the month; a bare
    # time means today.
    out = re.sub(r"\s+", " ", out).strip()
    if out and not _HAS_DAY.search(out):
        out = f"today {out}"
    return out


def clamp_to_hours(
    moment: dt.datetime, *, timezone: str, hours: CallingHours | None
) -> dt.datetime:
    """The same moment, or the next time calls are allowed if it is outside them."""
    if hours is None:
        return moment
    start, end = hours
    zone = ZoneInfo(timezone)
    local = moment.astimezone(zone)
    if start <= local.time() < end:
        return moment
    day = local.date() if local.time() < start else local.date() + dt.timedelta(days=1)
    return dt.datetime.combine(day, start, tzinfo=zone).astimezone(dt.UTC)


def parse_callback(
    text: str, *, now: dt.datetime, timezone: str, hours: CallingHours | None
) -> dt.datetime | None:
    """The moment a lead asked to be called, in UTC; None if it cannot be read."""
    cleaned = _normalise(text)
    if not cleaned:
        return None
    zone = ZoneInfo(timezone)
    base = now.astimezone(zone).replace(tzinfo=None)
    parsed = dateparser.parse(
        cleaned,
        settings={
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": base,
            "TIMEZONE": timezone,
            "RETURN_AS_TIMEZONE_AWARE": True,
        },
    )
    if parsed is None:
        return None
    moment = parsed.astimezone(dt.UTC)
    if cleaned.startswith("today ") and moment < now + _MIN_LEAD:
        # "10 am" said at 3 pm means tomorrow at 10.
        moment += dt.timedelta(days=1)
    if moment < now + _MIN_LEAD or moment > now + _MAX_AHEAD:
        return None
    return clamp_to_hours(moment, timezone=timezone, hours=hours)


def _local(moment: dt.datetime, timezone: str) -> str:
    return moment.astimezone(ZoneInfo(timezone)).strftime("%a %d %b, %I:%M %p")


@dataclass
class BulkQueued:
    """What a bulk AI call did with each selected lead."""

    queued: int = 0
    #: Already waiting for an AI call (a callback, a workflow's): left as it was.
    already_waiting: int = 0
    #: Covered by the never-AI-call rule.
    never_call: int = 0
    #: When the first call can start: now, or when calling hours open.
    starts_at: dt.datetime | None = None


class ScheduledCallService:
    def __init__(
        self,
        session: ScopedSession,
        *,
        workspace: Workspace,
        actor_id: uuid.UUID | None,
        hours: CallingHours | None,
    ) -> None:
        self._session = session
        self._workspace = workspace
        self._actor_id = actor_id
        self._hours = hours

    async def list_calls(
        self,
        *,
        lead_id: uuid.UUID | None,
        statuses: Sequence[str] | None,
        limit: int,
        offset: int,
    ) -> tuple[list[tuple[VoiceScheduledCall, Lead]], int]:
        base = self._session.select(VoiceScheduledCall)
        if lead_id is not None:
            base = base.where(VoiceScheduledCall.lead_id == lead_id)
        if statuses:
            base = base.where(VoiceScheduledCall.status.in_(list(statuses)))
        total = (
            await self._session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        rows = list(
            (
                await self._session.execute(
                    base.order_by(VoiceScheduledCall.due_at).limit(limit).offset(offset)
                )
            ).scalars()
        )
        leads: dict[uuid.UUID, Lead] = {}
        if rows:
            found = await self._session.execute(
                self._session.select(Lead).where(Lead.id.in_({r.lead_id for r in rows}))
            )
            leads = {lead.id: lead for lead in found.scalars().all()}
        return [(r, leads[r.lead_id]) for r in rows if r.lead_id in leads], int(total)

    async def get(self, scheduled_id: uuid.UUID) -> VoiceScheduledCall:
        row = await self._session.get(VoiceScheduledCall, scheduled_id)
        if row is None:
            raise not_found("Scheduled call")
        return row

    async def pending_for(self, lead_id: uuid.UUID) -> VoiceScheduledCall | None:
        found: VoiceScheduledCall | None = (
            await self._session.execute(
                self._session.select(VoiceScheduledCall).where(
                    VoiceScheduledCall.lead_id == lead_id,
                    VoiceScheduledCall.status == "PENDING",
                )
            )
        ).scalar_one_or_none()
        return found

    async def schedule(
        self,
        lead: Lead,
        *,
        due_at: dt.datetime,
        source: str,
        reason: str | None,
        purpose: str | None = None,
        workflow_run_id: uuid.UUID | None = None,
        note: bool = True,
    ) -> VoiceScheduledCall:
        if source not in SOURCES:  # pragma: no cover - programming error
            raise ValueError(source)
        if due_at.tzinfo is None:
            raise api_error(422, "naive_time", "A scheduled time needs a timezone")
        moment = clamp_to_hours(due_at, timezone=self._workspace.timezone, hours=self._hours)
        previous = await self.pending_for(lead.id)
        if previous is not None:
            previous.status = "CANCELLED"
            previous.last_error = "Replaced by a newer schedule"
            await self._session.flush()
        row = VoiceScheduledCall(
            lead_id=lead.id,
            due_at=moment,
            status="PENDING",
            source=source,
            purpose=purpose,
            reason=(reason or "")[:500] or None,
            workflow_run_id=workflow_run_id,
            created_by_id=self._actor_id,
        )
        self._session.add(row)
        await self._session.flush()
        if note:
            moved = " (moved into calling hours)" if moment != due_at else ""
            why = f" — “{reason}”" if reason and source == "CALLBACK" else ""
            await self._note(
                lead,
                f"📅 AI call scheduled for {_local(moment, self._workspace.timezone)}{moved}{why}",
            )
        return row

    async def queue_bulk(self, leads: Sequence[Lead], *, now: dt.datetime) -> BulkQueued:
        """Queue these leads to be called as soon as possible, in this order.

        All due at the same moment, a microsecond apart, so the worker takes
        them in the order the person chose. One changeset for the whole batch.
        """
        result = BulkQueued()
        if not leads:
            return result
        waiting = set(
            (
                await self._session.execute(
                    select(VoiceScheduledCall.lead_id).where(
                        VoiceScheduledCall.workspace_id == self._session.workspace_id,
                        VoiceScheduledCall.lead_id.in_([lead.id for lead in leads]),
                        VoiceScheduledCall.status.in_(("PENDING", "PLACING")),
                    )
                )
            ).scalars()
        )
        rule = parse_rule(read_never_call(self._workspace.settings))
        moment = clamp_to_hours(now, timezone=self._workspace.timezone, hours=self._hours)
        result.starts_at = moment
        queued: list[Lead] = []
        for lead in leads:
            if lead.id in waiting:
                result.already_waiting += 1
            elif rule is not None and await lead_matches(
                self._session, self._workspace, rule, lead.id
            ):
                result.never_call += 1
            else:
                queued.append(lead)
        if not queued:
            return result

        writer = ActionWriter(self._session, actor_id=self._actor_id)
        await writer.open_changeset(
            source=ChangesetSource.AUTOMATION,
            summary=f"Bulk AI call: {len(queued)} leads",
            lead_count=len(queued),
        )
        when = "now" if moment == now else f"from {_local(moment, self._workspace.timezone)}"
        for index, lead in enumerate(queued):
            self._session.add(
                VoiceScheduledCall(
                    lead_id=lead.id,
                    due_at=moment + dt.timedelta(microseconds=index),
                    status="PENDING",
                    source="BULK",
                    created_by_id=self._actor_id,
                )
            )
            writer.record_note(lead, body=f"📞 Added to a bulk AI call — calling {when}")
        await self._session.flush()
        result.queued = len(queued)
        return result

    async def cancel_bulk(self, *, lead_ids: Sequence[uuid.UUID] | None, why: str) -> int:
        """Stop every bulk call still waiting (among these leads, if given)."""
        statement = self._session.select(VoiceScheduledCall).where(
            VoiceScheduledCall.source == "BULK", VoiceScheduledCall.status == "PENDING"
        )
        if lead_ids is not None:
            statement = statement.where(VoiceScheduledCall.lead_id.in_(list(lead_ids)))
        rows = list((await self._session.execute(statement)).scalars())
        if not rows:
            return 0
        found = await self._session.execute(
            self._session.select(Lead).where(Lead.id.in_({row.lead_id for row in rows}))
        )
        leads = {lead.id: lead for lead in found.scalars()}
        writer = ActionWriter(self._session, actor_id=self._actor_id)
        await writer.open_changeset(
            source=ChangesetSource.AUTOMATION,
            summary=f"Bulk AI call stopped: {len(rows)} leads",
            lead_count=len(rows),
        )
        for row in rows:
            row.status, row.last_error = "CANCELLED", why
            if (lead := leads.get(row.lead_id)) is not None:
                writer.record_note(lead, body=f"📞 Bulk AI call stopped — {why}")
        await self._session.flush()
        return len(rows)

    async def cancel(self, scheduled_id: uuid.UUID, *, why: str) -> VoiceScheduledCall:
        row = await self.get(scheduled_id)
        if row.status != "PENDING":
            raise api_error(
                409, "not_pending", "Only a call that is still waiting can be cancelled"
            )
        row.status = "CANCELLED"
        row.last_error = why
        lead = await self._session.get(Lead, row.lead_id)
        if lead is not None:
            await self._note(lead, f"📅 Scheduled AI call cancelled — {why}")
        await self._session.flush()
        return row

    async def from_callback_request(
        self, lead: Lead, *, said: str, call_ended_at: dt.datetime
    ) -> VoiceScheduledCall | None:
        """Schedule what the lead asked for; a task for a person if unreadable."""
        when = parse_callback(
            said, now=call_ended_at, timezone=self._workspace.timezone, hours=self._hours
        )
        if when is not None:
            return await self.schedule(lead, due_at=when, source="CALLBACK", reason=said)
        await self._unreadable(lead, said, call_ended_at)
        return None

    async def _unreadable(self, lead: Lead, said: str, now: dt.datetime) -> None:
        tasks = TaskService(
            self._session,
            workspace=self._workspace,
            actor_id=self._actor_id,
            visible_membership_ids=frozenset(),
            sees_all=True,
        )
        await tasks.create_task(
            title=f"Call back: lead asked for “{said[:120]}”",
            due_at=clamp_to_hours(
                now + dt.timedelta(hours=1), timezone=self._workspace.timezone, hours=self._hours
            ),
            lead_id=lead.id,
            notes="The AI could not turn this into an exact time, so a person should call.",
            assignee_id=lead.assignee_id,
        )

    async def _note(self, lead: Lead, body: str) -> None:
        writer = ActionWriter(self._session, actor_id=self._actor_id)
        await writer.open_changeset(
            source=ChangesetSource.AUTOMATION,
            summary=f"Scheduled AI call for {lead.identity_value}"[:200],
        )
        writer.record_note(lead, body=body)
        await self._session.flush()
