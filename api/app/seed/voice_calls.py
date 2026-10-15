"""Demo AI calls for the fictional workspace — a fixture, not a default.

The lead seeder predates the voice integration, so a freshly seeded workspace
has fifty thousand leads and nothing on **Call details** — the one screen that
shows what this product is actually for. This fills that gap, and nothing else.

Everything below belongs to *Northwind Tutors*, a tutoring company that does not
exist, calling numbers in the reserved `+9199000xxxxx` block the lead seeder
already invented. No real person is named and no real number is dialled.

Like `app.seed.demo`, this refuses to run outside a local environment, and it
refuses to attach itself to a workspace it did not find already carrying the
demo fixture's own marker — "seed my production tenant with example calls" is
not an operation this product should be able to perform.

    uv run python -m app.seed.voice_calls --count 12
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import create_engine, create_session_factory
from app.models.enums import VoiceCallStatus
from app.models.lead import Lead
from app.models.voice import VoiceCallContext, VoiceCallExecution
from app.models.workspace import Workspace
from app.tenancy.session import bind_scope

__all__ = ["DemoVoiceCallSeeder", "VoiceSeedResult"]

#: Marks every row this module writes, so a second run replaces its own output
#: rather than stacking a second set of calls on top of the first.
DEMO_AGENT_ID = "demo-agent-northwind"


@dataclass(frozen=True, slots=True)
class Conversation:
    """One scripted call. Typed rather than a dict so the writer below cannot
    quietly disagree with it about a field name."""

    summary: str
    transcript: str | None
    duration: int
    status: VoiceCallStatus
    bolna_status: str
    extracted: Mapping[str, Mapping[str, object]]


#: One per call: what was said, what the agent pulled out of it, how it ended.
#: Written out longhand rather than generated, because a demo is read by people
#: and randomly assembled sentences read as exactly that.
CONVERSATIONS: tuple[Conversation, ...] = (
    Conversation(
        summary=(
            "Guardian confirmed interest in the Class 11 physics batch and asked about "
            "weekend timings. Quoted the Velachery Morning slot; she will confirm after "
            "speaking to her husband this weekend."
        ),
        transcript=(
            "assistant: Good morning, this is Northwind Tutors calling about your enquiry "
            "for Class 11 physics.\n"
            "user: Yes, I filled that form last week.\n"
            "assistant: Lovely. Are you looking at weekday or weekend batches?\n"
            "user: Weekends would be better, we are in Velachery.\n"
            "assistant: The Velachery morning batch runs Saturday and Sunday, nine to "
            "eleven. Shall I hold a seat?\n"
            "user: Let me speak to my husband and confirm by Sunday."
        ),
        duration=117,
        status=VoiceCallStatus.COMPLETED,
        bolna_status="completed",
        extracted={
            "interest": {"value": "Class 11 Physics", "confidence": 0.94},
            "preferred_batch": {"value": "Velachery Morning", "confidence": 0.88},
            "callback_due": {"value": "Sunday", "confidence": 0.71},
        },
    ),
    Conversation(
        summary=(
            "Asked to be called back after 7pm on weekdays. No subject discussed; the "
            "parent was driving and ended the call politely."
        ),
        transcript=(
            "assistant: Hello, am I speaking with the parent who enquired about tuition?\n"
            "user: Yes but I am driving right now.\n"
            "assistant: Of course. When would be a better time?\n"
            "user: After seven in the evening, any weekday.\n"
            "assistant: Noted, I will call then. Thank you."
        ),
        duration=41,
        status=VoiceCallStatus.COMPLETED,
        bolna_status="completed",
        extracted={
            "callback_window": {"value": "After 19:00 weekdays", "confidence": 0.96},
            "outcome": {"value": "Call back later", "confidence": 0.9},
        },
    ),
    Conversation(
        summary=(
            "Already enrolled with another institute for the current term. Open to being "
            "contacted again before the next admission cycle in April."
        ),
        transcript=(
            "assistant: Calling from Northwind Tutors about your maths enquiry.\n"
            "user: We have already joined somewhere else, sorry.\n"
            "assistant: Understood. May I check back before the April intake?\n"
            "user: Yes that is fine."
        ),
        duration=53,
        status=VoiceCallStatus.COMPLETED,
        bolna_status="completed",
        extracted={
            "outcome": {"value": "Enrolled elsewhere", "confidence": 0.93},
            "recontact_month": {"value": "April", "confidence": 0.82},
        },
    ),
    Conversation(
        summary=(
            "Confirmed the demo class for Saturday 10am and asked for the fee structure "
            "over WhatsApp. Guardian name corrected during the call."
        ),
        transcript=(
            "assistant: I am calling to confirm your demo class booking.\n"
            "user: Yes, Saturday ten o'clock, correct?\n"
            "assistant: That is right. Anything else I can help with?\n"
            "user: Can you send the fees on WhatsApp? And my name is Sneha, not Snehal.\n"
            "assistant: Corrected, and I will send the fee structure shortly."
        ),
        duration=96,
        status=VoiceCallStatus.COMPLETED,
        bolna_status="completed",
        extracted={
            "demo_confirmed": {"value": True, "confidence": 0.97},
            "demo_slot": {"value": "Saturday 10:00", "confidence": 0.95},
            "guardian_name": {"value": "Sneha", "confidence": 0.86},
            "wants_fees_on_whatsapp": {"value": True, "confidence": 0.91},
        },
    ),
    Conversation(
        summary=(
            "Number reached a switched-off handset. No conversation took place; the "
            "attempt is recorded so the next one is not treated as a first contact."
        ),
        transcript=None,
        duration=0,
        status=VoiceCallStatus.FAILED,
        bolna_status="no-answer",
        extracted={},
    ),
    Conversation(
        summary=(
            "Wanted biology only, not the full science package, and asked whether a "
            "single-subject rate exists. Quoted nothing; flagged for a human callback."
        ),
        transcript=(
            "assistant: You had enquired about our science programme.\n"
            "user: Only biology. Do you teach just one subject?\n"
            "assistant: We do, though the rate differs. Let me have a counsellor call "
            "you with the exact figure.\n"
            "user: Please do that."
        ),
        duration=74,
        status=VoiceCallStatus.COMPLETED,
        bolna_status="completed",
        extracted={
            "interest": {"value": "Biology only", "confidence": 0.92},
            "needs_human_callback": {"value": True, "confidence": 0.88},
        },
    ),
)


@dataclass(frozen=True, slots=True)
class VoiceSeedResult:
    workspace_id: uuid.UUID
    workspace_name: str
    calls: int
    replaced: int


class DemoVoiceCallSeeder:
    """Attaches a handful of fictional AI calls to the demo workspace's leads."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        count: int = 12,
        seed: int = 42,
        expect_workspace: str | None = None,
    ) -> None:
        self._session = session
        self._count = count
        self._random = random.Random(seed)
        #: The caller must name the workspace they believe they are writing to.
        #: Outside a local database this is the real guard — "wrong deployment"
        #: is the mistake that actually happens, and an environment check only
        #: approximates it.
        self._expect_workspace = expect_workspace

    async def run(self) -> VoiceSeedResult:
        workspace = await self._demo_workspace()
        bind_scope(self._session, workspace.id)
        try:
            replaced = await self._clear_previous(workspace.id)
            leads = await self._pick_leads(workspace.id)
            written = await self._write_calls(workspace.id, leads)
            await self._session.commit()
        finally:
            bind_scope(self._session, None)

        return VoiceSeedResult(
            workspace_id=workspace.id,
            workspace_name=workspace.name,
            calls=written,
            replaced=replaced,
        )

    async def _demo_workspace(self) -> Workspace:
        rows = await self._session.execute(select(Workspace).order_by(Workspace.created_at))
        workspaces = list(rows.scalars())
        if not workspaces:
            raise RuntimeError("no workspace exists; run `python -m app.seed` first")
        if self._expect_workspace is not None:
            match = [w for w in workspaces if w.name == self._expect_workspace]
            if not match:
                names = ", ".join(repr(w.name) for w in workspaces)
                raise RuntimeError(
                    f"no workspace named {self._expect_workspace!r} here; this database has {names}"
                )
            return match[0]
        if len(workspaces) > 1:
            names = ", ".join(repr(w.name) for w in workspaces)
            raise RuntimeError(
                f"refusing to guess: this deployment has more than one workspace ({names}). "
                "Name the one you mean with --workspace-name."
            )
        return workspaces[0]

    async def _clear_previous(self, workspace_id: uuid.UUID) -> int:
        """Remove this module's own earlier output, and only that."""
        rows = await self._session.execute(
            select(VoiceCallExecution).where(
                VoiceCallExecution.workspace_id == workspace_id,
                VoiceCallExecution.agent_id == DEMO_AGENT_ID,
            )
        )
        previous = list(rows.scalars())
        for row in previous:
            await self._session.delete(row)
        if previous:
            await self._session.flush()
        return len(previous)

    async def _pick_leads(self, workspace_id: uuid.UUID) -> list[Lead]:
        # Ordered, not random: two runs with the same seed must call the same
        # people, or a screenshot taken yesterday stops matching the database.
        rows = await self._session.execute(
            select(Lead)
            .where(Lead.workspace_id == workspace_id)
            .order_by(Lead.created_at.desc())
            .limit(self._count)
        )
        leads = list(rows.scalars())
        if not leads:
            raise RuntimeError("the demo workspace has no leads to call")
        return leads

    async def _write_calls(self, workspace_id: uuid.UUID, leads: list[Lead]) -> int:
        now = datetime.now(UTC)
        written = 0

        for index, lead in enumerate(leads):
            script = CONVERSATIONS[index % len(CONVERSATIONS)]
            # Newest first in the UI, so walk backwards in time as we go.
            started = now - timedelta(hours=3 * index + self._random.randint(0, 2))
            completed = script.status is VoiceCallStatus.COMPLETED

            raw_payload: dict[str, object] = {
                "status": script.bolna_status,
                "telephony_data": {
                    "to_number": lead.identity_value,
                    "from_number": "+919900000000",
                    "call_type": "outbound",
                    "duration": script.duration,
                },
                "extracted_data": dict(script.extracted),
            }
            if script.transcript is not None:
                raw_payload["transcript"] = script.transcript

            self._session.add(
                VoiceCallExecution(
                    workspace_id=workspace_id,
                    lead_id=lead.id,
                    external_id=f"demo-exec-{index:03d}-{uuid.uuid4().hex[:8]}",
                    agent_id=DEMO_AGENT_ID,
                    recipient_phone=lead.identity_value,
                    idempotency_key=uuid.uuid4(),
                    status=script.status,
                    bolna_status=script.bolna_status,
                    context_sent={
                        "lead_id": str(lead.id),
                        "programme": "Northwind Tutors \u2014 enquiry follow-up",
                    },
                    raw_payload=raw_payload,
                    attempts=1,
                    dispatched_at=started,
                    completed_at=(
                        started + timedelta(seconds=script.duration) if completed else None
                    ),
                    webhook_received_at=(
                        started + timedelta(seconds=script.duration + 2) if completed else None
                    ),
                    transcript=script.transcript,
                    duration_seconds=script.duration if completed else None,
                    summary=script.summary,
                    # These are written-out scripts, not summariser output. Saying
                    # `AI` on a row no summariser touched would make the demo claim
                    # a capability it did not exercise.
                    summary_source="AI" if completed else "FALLBACK",
                    last_error=None if completed else "recipient handset unreachable",
                )
            )
            written += 1

            if completed:
                await self._upsert_context(workspace_id, lead.id, script, started)

        await self._session.flush()
        return written

    async def _upsert_context(
        self,
        workspace_id: uuid.UUID,
        lead_id: uuid.UUID,
        script: Conversation,
        started: datetime,
    ) -> None:
        """Keep the per-lead digest in step, so a repeat call is not cold."""
        existing = await self._session.execute(
            select(VoiceCallContext).where(
                VoiceCallContext.workspace_id == workspace_id,
                VoiceCallContext.lead_id == lead_id,
            )
        )
        row = existing.scalar_one_or_none()
        ended = started + timedelta(seconds=script.duration)
        if row is None:
            self._session.add(
                VoiceCallContext(
                    workspace_id=workspace_id,
                    lead_id=lead_id,
                    last_call_summary=script.summary,
                    last_call_at=ended,
                )
            )
        else:
            row.last_call_summary = script.summary
            row.last_call_at = ended


async def count_demo_calls(session: AsyncSession, workspace_id: uuid.UUID) -> int:
    rows = await session.execute(
        select(func.count())
        .select_from(VoiceCallExecution)
        .where(VoiceCallExecution.workspace_id == workspace_id)
    )
    return int(rows.scalar_one())


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.seed.voice_calls",
        description="Attach fictional AI calls to the demo workspace.",
    )
    parser.add_argument("--count", type=int, default=12, help="How many calls to write")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed; fixed so runs match")
    parser.add_argument(
        "--workspace-name",
        help="The workspace to write into, named exactly. Required outside a local database.",
    )
    parser.add_argument(
        "--allow-non-local",
        action="store_true",
        help="Run against a non-local deployment. Requires --workspace-name.",
    )
    return parser.parse_args(argv)


async def _run(count: int, seed: int, workspace_name: str | None, allow_non_local: bool) -> int:
    settings = Settings()
    if not settings.is_local:
        # The lead seeder refuses outright here, because inventing a taxonomy in
        # somebody's database is not recoverable by deleting rows. This seeder
        # only ever *adds call rows to leads that already exist*, so it can be
        # allowed out — but only deliberately, and only at a named workspace, so
        # it cannot land on the wrong deployment by accident.
        if not allow_non_local:
            print(
                f"refusing to seed: ENVIRONMENT is {settings.environment!r}, not 'local'. "
                "Pass --allow-non-local --workspace-name '<name>' if you mean it.",
                file=sys.stderr,
            )
            return 2
        if not workspace_name:
            print("--allow-non-local requires --workspace-name", file=sys.stderr)
            return 2
        print(
            f"WARNING: seeding demo AI calls into a {settings.environment!r} deployment, "
            f"workspace {workspace_name!r}.",
            file=sys.stderr,
        )

    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            result = await DemoVoiceCallSeeder(
                session, count=count, seed=seed, expect_workspace=workspace_name
            ).run()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()

    replaced = f", replacing {result.replaced}" if result.replaced else ""
    print(f"wrote {result.calls} demo AI calls to {result.workspace_name!r}{replaced}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_run(args.count, args.seed, args.workspace_name, args.allow_non_local))


if __name__ == "__main__":
    sys.exit(main())
