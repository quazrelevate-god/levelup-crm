"""The `arq` worker process (M8).

The first milestone that needs a *process* rather than a function call. M6's
index builds are invoked directly by the settings service; this extends that
module's home rather than standing up a second worker, as the handoff asks.

Four cron entries (docs/21): the scheduler, the outbox dispatcher, a
heartbeat the operator console reads, and calling-number rent. The scheduler
ticks every minute; the outbox dispatcher runs more often than that, because a
webhook that arrives a minute after the event is a webhook most integrations
would call broken. It walks workspaces and asks each one
whether any of its schedules is due **in that workspace's timezone** — the tick
itself has no timezone, which is the point. A worker that evaluated cron in its
own zone would be correct for exactly the customers who happen to share it.

Run it with:

    uv run arq app.workers.scheduler.WorkerSettings
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.events.dispatcher import HttpxTransport, run_dispatch
from app.models import Workspace
from app.services.email import build_sender
from app.services.number_requests import NumberRequestService
from app.services.scheduling import run_due_schedules
from app.tenancy.session import ScopedSession

__all__ = [
    "HEARTBEAT_KEY",
    "WorkerSettings",
    "dispatch",
    "heartbeat",
    "settle_numbers",
    "tick",
]

#: Where the worker says "I am alive" (docs/21). The operator console reads it.
HEARTBEAT_KEY = "crm:worker:heartbeat"
#: A heartbeat older than this means the worker is not running.
HEARTBEAT_TTL_SECONDS = 300

logger = logging.getLogger(__name__)


async def tick(ctx: dict[str, Any]) -> dict[str, int]:
    """One scheduler pass across every workspace."""
    settings = get_settings()
    session_factory: async_sessionmaker[Any] = ctx["session_factory"]
    sender = ctx["email_sender"]
    now = dt.datetime.now(dt.UTC)

    totals = {"considered": 0, "sent": 0, "failed": 0, "skipped": 0}

    async with session_factory() as raw:
        rows = await raw.execute(select(Workspace))
        workspaces = list(rows.scalars().all())

    for workspace in workspaces:
        # A session per workspace, so one tenant's failure cannot poison the
        # transaction another tenant's schedules are written in.
        async with session_factory() as raw:
            scoped = ScopedSession(raw, workspace.id)
            try:
                result = await run_due_schedules(
                    scoped,
                    workspace=workspace,
                    sender=sender,
                    now=now,
                    catchup_hours=settings.scheduler_max_catchup_hours,
                )
                await raw.commit()
            except Exception:
                await raw.rollback()
                logger.exception(
                    "scheduler.workspace_failed", extra={"workspace": str(workspace.id)}
                )
                continue

        totals["considered"] += result.considered
        totals["sent"] += result.sent
        totals["failed"] += result.failed
        totals["skipped"] += result.skipped

    if totals["sent"] or totals["failed"]:
        logger.info("scheduler.tick", extra=totals)
    return totals


async def dispatch(ctx: dict[str, Any]) -> dict[str, int]:
    """One outbox pass.

    Runs on its own cron rather than inside `tick`, so a slow SMTP server cannot
    delay webhook delivery and a dead consumer cannot delay the morning reports.
    """
    session_factory: async_sessionmaker[Any] = ctx["session_factory"]
    settings = get_settings()
    async with session_factory() as raw:
        return await run_dispatch(
            raw,
            transport=ctx["webhook_transport"],
            now=dt.datetime.now(dt.UTC),
            max_age=dt.timedelta(hours=settings.outbox_max_age_hours),
        )


async def heartbeat(ctx: dict[str, Any]) -> str:
    """Say the worker is alive, so the console can show it (docs/21)."""
    moment = dt.datetime.now(dt.UTC).isoformat()
    redis = ctx.get("redis")
    if redis is not None:
        await redis.set(HEARTBEAT_KEY, moment, ex=HEARTBEAT_TTL_SECONDS)
    return moment


async def settle_numbers(ctx: dict[str, Any]) -> int:
    """Charge calling-number rent that has fallen due, in every workspace.

    `settle_due` also runs whenever a company lists its numbers or places a
    call; this makes renewals happen on time for a company doing neither.
    """
    session_factory: async_sessionmaker[Any] = ctx["session_factory"]
    async with session_factory() as raw:
        workspaces = list((await raw.execute(select(Workspace))).scalars().all())
    settled = 0
    for workspace in workspaces:
        async with session_factory() as raw:
            try:
                await NumberRequestService(
                    ScopedSession(raw, workspace.id), workspace=workspace
                ).settle_due()
                await raw.commit()
                settled += 1
            except Exception:
                await raw.rollback()
                logger.exception("numbers.settle_failed", extra={"workspace": str(workspace.id)})
    return settled


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_size=2, max_overflow=2)
    ctx["engine"] = engine
    ctx["session_factory"] = async_sessionmaker(engine, expire_on_commit=False)
    ctx["email_sender"] = build_sender(settings)
    ctx["webhook_transport"] = HttpxTransport()


async def shutdown(ctx: dict[str, Any]) -> None:
    engine = ctx.get("engine")
    if engine is not None:
        await engine.dispose()


class WorkerSettings:
    """`arq` entry point."""

    # `arq` reads these off the class; they are configuration, not state.
    functions: ClassVar[list[Any]] = []
    cron_jobs: ClassVar[list[Any]] = [
        cron(tick, minute=set(range(60)), run_at_startup=False),
        # Every 10 seconds. The claim query is a partial-index lookup that
        # returns nothing in the common case, so a quiet workspace pays almost
        # nothing for delivery that feels immediate when it is not quiet.
        cron(dispatch, second={0, 10, 20, 30, 40, 50}, run_at_startup=False),
        # Every minute, first thing on start, so the console sees a fresh
        # worker within seconds of a deploy.
        cron(heartbeat, minute=set(range(60)), run_at_startup=True),
        # Hourly, at a quiet minute.
        cron(settle_numbers, minute={17}, run_at_startup=False),
    ]
    on_startup = startup
    on_shutdown = shutdown

    #: An **attribute**, not a method. `arq` reads `WorkerSettings.redis_settings`
    #: and immediately asks it for `.host`; a `@staticmethod` here type-checks,
    #: imports, and passes every unit test, then dies on the first line of the
    #: real worker with `'staticmethod' object has no attribute 'host'`.
    #:
    #: Nothing caught it because the tests call `tick()` and `dispatch()`
    #: directly — the arq entry point was only ever exercised by running the
    #: container. `test_the_arq_entry_point_matches_what_arq_expects` now
    #: covers the contract.
    redis_settings: ClassVar[RedisSettings] = RedisSettings.from_dsn(get_settings().redis_url)
