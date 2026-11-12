"""The worker's half of scheduled calls and paused workflows (docs/22).

`place_due_calls` dials what is due, keeping **at most
`voice_max_parallel_calls` going at once** (10 by default) — the rest wait for
the next pass, so a bulk AI call of 300 leads runs ten at a time instead of
piling 300 calls onto Bolna at once (docs/23). The limit is across every
company together, because they all share one Bolna account and its
concurrency limit. Every call goes through
`VoiceCallService.trigger`, the exact path of the AI Call button: calling hours,
the wallet and the never-AI-call rule all apply, and their refusals are handled:

| Refusal | What happens |
|---|---|
| outside calling hours | waits for the next window |
| never-AI-call rule, no phone | SKIPPED, with the reason on the lead's timeline |
| wallet empty, Bolna unreachable | tried again later; FAILED after 5 tries |

`resume_waiting_runs` carries on every workflow whose Wait has run out, and
sends the WhatsApp messages and API calls workflows queued for the worker.

Both take a session factory and build each company's services themselves, so
one company's failure never stops another's.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.integrations.bolna import BolnaClient, HttpxBolnaClient, bolna_settings_from
from app.models import Lead, VoiceCallExecution, VoiceScheduledCall, Workspace
from app.models.automation import WorkflowRun
from app.models.enums import ChangesetSource, VoiceCallPurpose, VoiceCallStatus
from app.services.actions import ActionWriter
from app.services.program_context import ProgramContextService
from app.services.scheduled_calls import clamp_to_hours
from app.services.system_filters import system_lead_service
from app.services.voice_calls import VoiceCallService
from app.services.voice_context import VoiceContextService
from app.services.voice_hours import parse_calling_hours
from app.services.workflows import WorkflowEngine
from app.tenancy.session import ScopedSession

__all__ = ["MAX_ATTEMPTS", "calls_in_progress", "place_due_calls", "resume_waiting_runs"]

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
#: A call this young and not finished is still going (or about to).
_IN_PROGRESS = dt.timedelta(minutes=20)
_RETRY_AFTER = dt.timedelta(minutes=30)


def _detail(exc: HTTPException) -> tuple[str, str]:
    detail: Any = exc.detail
    if isinstance(detail, dict):
        return str(detail.get("code") or ""), str(detail.get("message") or "")
    return "", str(detail)


async def calls_in_progress(session: ScopedSession, now: dt.datetime) -> int:
    """How many of this company's AI calls are still going."""
    count = await session.execute(
        select(func.count())
        .select_from(VoiceCallExecution)
        .where(
            VoiceCallExecution.workspace_id == session.workspace_id,
            VoiceCallExecution.status.in_((VoiceCallStatus.QUEUED, VoiceCallStatus.DISPATCHED)),
            VoiceCallExecution.completed_at.is_(None),
            VoiceCallExecution.created_at >= now - _IN_PROGRESS,
        )
    )
    return int(count.scalar_one())


async def _reload[T](raw: AsyncSession, model: type[T], row_id: uuid.UUID) -> T:
    """A fresh copy of a row after a commit or rollback expired it."""
    found = await raw.get(model, row_id, populate_existing=True)
    assert found is not None
    return found


async def _note(session: ScopedSession, lead: Lead, body: str) -> None:
    writer = ActionWriter(session, actor_id=None)
    await writer.open_changeset(
        source=ChangesetSource.AUTOMATION,
        summary=f"Scheduled AI call for {lead.identity_value}"[:200],
    )
    writer.record_note(lead, body=body)
    await session.flush()


async def _place_one(
    raw: AsyncSession,
    workspace: Workspace,
    row: VoiceScheduledCall,
    *,
    settings: Settings,
    client: BolnaClient | None,
    now: dt.datetime,
) -> str:
    session = ScopedSession(raw, workspace.id)
    hours = parse_calling_hours(settings.voice_calling_hours)
    lead = await session.get(Lead, row.lead_id)
    if lead is None or lead.deleted_at is not None:
        row.status, row.last_error = "SKIPPED", "The lead no longer exists"
        return row.status

    leads = await system_lead_service(session, workspace)
    context = VoiceContextService(
        session,
        workspace=workspace,
        leads=leads,
        visible_membership_ids=frozenset(),
        sees_all=True,
        actor_id=None,
    )
    service = VoiceCallService(
        session,
        workspace=workspace,
        leads=leads,
        context=context,
        client=client,
        config=bolna_settings_from(settings),
        actor_id=None,
        program_context=ProgramContextService(settings.kb_base_url),
        calling_hours=hours,
    )
    row.status = "PLACING"
    row.attempts += 1
    # Kept as plain values: a rollback or commit expires the rows, and an async
    # session cannot lazy-load them back.
    row_id, lead_id, attempts = row.id, lead.id, row.attempts
    await raw.commit()

    purpose = VoiceCallPurpose(row.purpose) if row.purpose else None
    try:
        outcome = await service.trigger(lead, purpose=purpose)
    except HTTPException as exc:
        await raw.rollback()
        row = await _reload(raw, VoiceScheduledCall, row_id)
        lead = await _reload(raw, Lead, lead_id)
        code, message = _detail(exc)
        if code == "outside_calling_hours":
            row.status = "PENDING"
            row.due_at = clamp_to_hours(
                now + dt.timedelta(minutes=1), timezone=workspace.timezone, hours=hours
            )
            row.last_error = message
        elif code in {"lead_excluded_from_ai_calls", "lead_has_no_phone"}:
            row.status, row.last_error = "SKIPPED", message
            await _note(session, lead, f"📅 Scheduled AI call not placed — {message}")
        elif row.attempts >= MAX_ATTEMPTS:
            row.status, row.last_error = "FAILED", message
            await _note(
                session, lead, f"📅 Scheduled AI call failed after {row.attempts} tries — {message}"
            )
        else:
            row.status, row.last_error = "PENDING", message
            row.due_at = now + _RETRY_AFTER
        await raw.commit()
        return row.status

    call_id = outcome.execution.id
    error = outcome.error
    row = await _reload(raw, VoiceScheduledCall, row_id)
    row.call_id = call_id
    row.placed_at = now
    if error:
        row.status = "PENDING" if attempts < MAX_ATTEMPTS else "FAILED"
        row.last_error = error
        row.due_at = now + _RETRY_AFTER
    else:
        row.status, row.last_error = "PLACED", None
    await raw.commit()
    return row.status


async def _account_calls_in_progress(raw: AsyncSession, now: dt.datetime) -> int:
    """AI calls still going across every company: they share one Bolna account."""
    count = await raw.execute(
        select(func.count())
        .select_from(VoiceCallExecution)
        .where(
            VoiceCallExecution.status.in_((VoiceCallStatus.QUEUED, VoiceCallStatus.DISPATCHED)),
            VoiceCallExecution.completed_at.is_(None),
            VoiceCallExecution.created_at >= now - _IN_PROGRESS,
        )
    )
    return int(count.scalar_one())


async def place_due_calls(
    factory: async_sessionmaker[Any],
    *,
    settings: Settings,
    client: BolnaClient | None = None,
    clock: Callable[[], dt.datetime] | None = None,
) -> dict[str, int]:
    """Place what is due, up to the free call slots on the Bolna account.

    A refusal that is about the company rather than the lead — outside calling
    hours, an empty wallet, Bolna unreachable — stops that company's pass: the
    next lead would be refused for the same reason.
    """
    now = (clock or (lambda: dt.datetime.now(dt.UTC)))()
    config = bolna_settings_from(settings)
    caller = client or (HttpxBolnaClient(config) if config is not None else None)
    totals = {"placed": 0, "waiting": 0, "skipped": 0, "failed": 0}

    async with factory() as raw:
        due = list(
            (
                await raw.execute(
                    select(VoiceScheduledCall.workspace_id, VoiceScheduledCall.id)
                    .where(VoiceScheduledCall.status == "PENDING", VoiceScheduledCall.due_at <= now)
                    .order_by(VoiceScheduledCall.due_at)
                )
            ).all()
        )
        free = settings.voice_max_parallel_calls - await _account_calls_in_progress(raw, now)
    by_workspace: dict[uuid.UUID, list[uuid.UUID]] = {}
    for workspace_id, scheduled_id in due:
        by_workspace.setdefault(workspace_id, []).append(scheduled_id)

    for workspace_id, scheduled_ids in by_workspace.items():
        async with factory() as raw:
            company = await raw.get(Workspace, workspace_id)
            if company is None or not company.is_active:
                continue
        for scheduled_id in scheduled_ids:
            if free <= 0:
                totals["waiting"] += 1
                break
            async with factory() as raw:
                try:
                    # Loaded in the session that places the call: the services
                    # below write through it (wallet, number rotation).
                    workspace = await raw.get(Workspace, workspace_id)
                    assert workspace is not None
                    row = (
                        await raw.execute(
                            select(VoiceScheduledCall)
                            .where(VoiceScheduledCall.id == scheduled_id)
                            .with_for_update(skip_locked=True)
                        )
                    ).scalar_one_or_none()
                    if row is None or row.status != "PENDING":
                        continue
                    status = await _place_one(
                        raw, workspace, row, settings=settings, client=caller, now=now
                    )
                except Exception:
                    await raw.rollback()
                    logger.exception(
                        "scheduled_call.failed", extra={"workspace": str(workspace_id)}
                    )
                    totals["failed"] += 1
                    break
            key = {"PLACED": "placed", "SKIPPED": "skipped", "FAILED": "failed"}.get(
                status, "waiting"
            )
            totals[key] += 1
            if status == "PLACED":
                free -= 1
            elif status != "SKIPPED":
                break
    return totals


async def resume_waiting_runs(
    factory: async_sessionmaker[Any],
    *,
    http: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], dt.datetime] | None = None,
    limit: int = 50,
) -> dict[str, int]:
    """Carry on every paused workflow whose moment has come."""
    now = (clock or (lambda: dt.datetime.now(dt.UTC)))()
    totals = {"resumed": 0, "failed": 0}
    async with factory() as raw:
        due = list(
            (
                await raw.execute(
                    select(WorkflowRun.workspace_id, WorkflowRun.id)
                    .where(WorkflowRun.status == "WAITING", WorkflowRun.resume_at <= now)
                    .order_by(WorkflowRun.resume_at)
                    .limit(limit)
                )
            ).all()
        )
    for workspace_id, run_id in due:
        async with factory() as raw:
            try:
                workspace = await raw.get(Workspace, workspace_id)
                if workspace is None:
                    continue
                run = (
                    await raw.execute(
                        select(WorkflowRun)
                        .where(WorkflowRun.id == run_id, WorkflowRun.status == "WAITING")
                        .with_for_update(skip_locked=True)
                    )
                ).scalar_one_or_none()
                if run is None:
                    continue
                engine = WorkflowEngine(
                    ScopedSession(raw, workspace_id),
                    workspace=workspace,
                    http=http,
                    clock=lambda: now,
                )
                await engine.resume(run)
                await raw.commit()
                totals["resumed"] += 1
            except Exception:
                await raw.rollback()
                logger.exception("workflow.resume_failed", extra={"run": str(run_id)})
                totals["failed"] += 1
    return totals
