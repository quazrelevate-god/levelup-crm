"""Outbound delivery, retry and the DEAD threshold (M10).

The milestone's acceptance criterion is *"kill the worker mid-delivery and show
a retry rather than a lost event"*, so this module is organised around surviving
its own death.

**A claim is committed before the HTTP call.** The row moves to `DELIVERING`
with `claimed_at` set, and that transaction commits. If the process dies during
the request, the row is still `DELIVERING` — visible, attributable, and
reclaimable — rather than sitting in `PENDING` while another worker sends it a
second time, or being lost with an in-memory transaction.

**A stale claim is reclaimed, not abandoned.** Any row in `DELIVERING` older
than `CLAIM_TIMEOUT` is assumed to belong to a dead worker and becomes eligible
again. That is the retry the criterion asks for: no coordination, no leader
election, just a timeout that a crashed worker cannot refresh.

**This makes delivery at-least-once, deliberately.** A worker can die *after* the
consumer accepted and *before* the commit, and the event will be sent again.
That is why `event_id` is stable across retries — the consumer dedupes. The
alternative, at-most-once, loses events, and losing a lead event is worse than
sending it twice.

**Backoff is `2^attempts` minutes capped at 60; DEAD after 8.** That is roughly
four hours of trying, which covers an ordinary deploy or outage, after which a
human decides rather than the queue grinding forever.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.events.envelope import build_envelope, serialise, sign
from app.models import (
    LeadField,
    OutboxEvent,
    OutboxStatus,
    PermissionTemplate,
    WebhookEndpoint,
)
from app.permissions import FieldProjectionService, load_grants
from app.tenancy.session import ScopedSession

__all__ = [
    "CLAIM_TIMEOUT",
    "DEAD_AFTER_ATTEMPTS",
    "MAX_BACKOFF_MINUTES",
    "STALE_ERROR",
    "DeliveryResult",
    "Transport",
    "deliver_once",
    "expire_stale",
    "next_attempt_after",
    "run_dispatch",
]

logger = logging.getLogger(__name__)

#: `2^attempts` minutes, capped. Eight attempts is about four hours.
MAX_BACKOFF_MINUTES = 60
DEAD_AFTER_ATTEMPTS = 8
#: How long a `DELIVERING` row may sit before another worker reclaims it. Longer
#: than any sane HTTP timeout, short enough that a crash is not a long outage.
CLAIM_TIMEOUT = dt.timedelta(minutes=5)
#: A slow consumer must not hold a worker open indefinitely.
REQUEST_TIMEOUT_SECONDS = 10.0


def next_attempt_after(attempts: int, *, now: dt.datetime) -> dt.datetime:
    """Exponential backoff, capped."""
    minutes = min(2**attempts, MAX_BACKOFF_MINUTES)
    return now + dt.timedelta(minutes=minutes)


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    status_code: int | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300


class Transport(Protocol):
    """The HTTP seam.

    A protocol rather than a hard httpx dependency so tests can drive the retry
    and DEAD paths without a server, and without sleeping through real backoff.
    """

    async def post(self, url: str, *, body: bytes, headers: dict[str, str]) -> DeliveryResult: ...


class HttpxTransport:
    """The real one."""

    def __init__(self, timeout: float = REQUEST_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout

    async def post(self, url: str, *, body: bytes, headers: dict[str, str]) -> DeliveryResult:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, content=body, headers=headers)
            return DeliveryResult(status_code=response.status_code, error=None)
        except Exception as exc:
            return DeliveryResult(status_code=None, error=str(exc)[:500])


async def _project(
    session: ScopedSession, *, endpoint: WebhookEndpoint, payload: dict[str, Any]
) -> dict[str, Any]:
    """Strip fields the endpoint's template cannot View.

    A webhook is a read path like any other (rule 3). Without this the bus would
    be a hole straight through the field matrix — the one place a `View` denial
    could be bypassed by subscribing to an event instead of calling an endpoint.
    """
    values = payload.get("values")
    if not isinstance(values, dict):
        return payload

    rows = await session.execute(
        select(LeadField).where(LeadField.workspace_id == session.workspace_id)
    )
    fields = list(rows.scalars().all())
    template = await session.get(PermissionTemplate, endpoint.permission_template_id)
    is_admin = bool(template and (template.capabilities or {}).get("leads", {}).get("admin_access"))
    grants = await load_grants(
        session,
        template_id=endpoint.permission_template_id,
        is_admin=is_admin,
        all_field_keys={field.id: field.key for field in fields},
    )
    projection = FieldProjectionService(grants)
    return {**payload, "values": projection.project_values(values)}


async def deliver_once(
    session: ScopedSession,
    *,
    row: OutboxEvent,
    endpoint: WebhookEndpoint,
    transport: Transport,
    now: dt.datetime,
) -> DeliveryResult:
    """Send one already-claimed row and record what happened.

    Does not commit — the caller owns the transaction boundary, because the
    commit *after* a successful send is what makes a crash between the two a
    duplicate rather than a loss.
    """
    data = await _project(session, endpoint=endpoint, payload=row.payload or {})
    envelope = build_envelope(
        event=row.event,
        event_id=row.event_id,
        workspace_id=row.workspace_id,
        occurred_at=row.occurred_at,
        data=data,
    )
    body = serialise(envelope)
    result = await transport.post(
        endpoint.url,
        body=body,
        headers={
            "Content-Type": "application/json",
            "X-CRM-Event": row.event,
            # Stable across retries. The consumer's dedupe key.
            "X-CRM-Event-Id": str(row.event_id),
            "X-CRM-Signature": sign(endpoint.secret, body),
        },
    )

    row.attempts += 1
    row.last_status_code = result.status_code
    row.claimed_at = None

    if result.ok:
        row.status = OutboxStatus.DELIVERED
        row.delivered_at = now
        row.last_error = None
        return result

    row.last_error = (result.error or f"HTTP {result.status_code}")[:1000]
    if row.attempts >= DEAD_AFTER_ATTEMPTS:
        # Never retried automatically again. An operator redrives it, which is
        # the right call once eight attempts across four hours have failed.
        row.status = OutboxStatus.DEAD
    else:
        row.status = OutboxStatus.FAILED
        row.next_attempt_at = next_attempt_after(row.attempts, now=now)
    return result


async def _claim(raw: AsyncSession, *, now: dt.datetime, limit: int) -> list[OutboxEvent]:
    """Take up to `limit` due rows, committing the claim before any HTTP.

    `SKIP LOCKED` so two workers never fight over the same row, and so one slow
    claim does not stall the other worker's whole batch.
    """
    stale = now - CLAIM_TIMEOUT
    due = (
        select(OutboxEvent.id)
        .where(
            or_(
                OutboxEvent.status.in_((OutboxStatus.PENDING, OutboxStatus.FAILED)),
                # A row a dead worker left behind. This is the retry the
                # acceptance criterion asks for.
                (OutboxEvent.status == OutboxStatus.DELIVERING) & (OutboxEvent.claimed_at < stale),
            ),
            OutboxEvent.next_attempt_at <= now,
        )
        .order_by(OutboxEvent.next_attempt_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    ids = list((await raw.execute(due)).scalars().all())
    if not ids:
        return []

    await raw.execute(
        update(OutboxEvent)
        .where(OutboxEvent.id.in_(ids))
        .values(status=OutboxStatus.DELIVERING, claimed_at=now)
    )
    await raw.commit()

    rows = await raw.execute(select(OutboxEvent).where(OutboxEvent.id.in_(ids)))
    return list(rows.scalars().all())


#: What a too-old event says, so the operator knows why it was never sent.
STALE_ERROR = (
    "Too old to deliver: it waited longer than the outbox allows. Redrive it if it still matters."
)


async def expire_stale(raw: AsyncSession, *, now: dt.datetime, max_age: dt.timedelta) -> int:
    """Mark waiting events older than `max_age` DEAD rather than sending them.

    Only PENDING and FAILED rows: one being delivered right now is left alone.
    """
    result = await raw.execute(
        update(OutboxEvent)
        .where(
            OutboxEvent.status.in_((OutboxStatus.PENDING, OutboxStatus.FAILED)),
            OutboxEvent.occurred_at < now - max_age,
        )
        .values(status=OutboxStatus.DEAD, last_error=STALE_ERROR, claimed_at=None)
    )
    await raw.commit()
    return int(getattr(result, "rowcount", 0) or 0)


async def run_dispatch(
    raw: AsyncSession,
    *,
    transport: Transport,
    now: dt.datetime,
    limit: int = 50,
    max_age: dt.timedelta | None = None,
) -> dict[str, int]:
    """One dispatch pass: claim, send, record.

    Deliberately not workspace-scoped at the top: the outbox is one queue and a
    fair round of it should not depend on which tenant happened to be busy.
    Each row is *handled* through a `ScopedSession` for its own workspace, so
    projection and every read stay tenant-safe.
    """
    expired = await expire_stale(raw, now=now, max_age=max_age) if max_age is not None else 0
    claimed = await _claim(raw, now=now, limit=limit)
    if not claimed:
        return {"claimed": 0, "delivered": 0, "failed": 0, "dead": expired}

    endpoint_rows = await raw.execute(
        select(WebhookEndpoint).where(WebhookEndpoint.id.in_({row.endpoint_id for row in claimed}))
    )
    endpoints = {endpoint.id: endpoint for endpoint in endpoint_rows.scalars().all()}

    totals = {"claimed": len(claimed), "delivered": 0, "failed": 0, "dead": expired}
    for row in claimed:
        endpoint = endpoints.get(row.endpoint_id)
        if endpoint is None:  # pragma: no cover - FK cascade makes this unlikely
            row.status = OutboxStatus.DEAD
            row.last_error = "the endpoint no longer exists"
            row.claimed_at = None
            await raw.commit()
            totals["dead"] += 1
            continue

        scoped = ScopedSession(raw, row.workspace_id)
        try:
            result = await deliver_once(
                scoped, row=row, endpoint=endpoint, transport=transport, now=now
            )
        except Exception:
            # An unexpected failure must not leave the row claimed forever; put
            # it back where the timeout would have, but immediately.
            logger.exception("outbox.deliver_failed", extra={"event_id": str(row.event_id)})
            row.status = OutboxStatus.FAILED
            row.attempts += 1
            row.claimed_at = None
            row.next_attempt_at = next_attempt_after(row.attempts, now=now)
            await raw.commit()
            totals["failed"] += 1
            continue

        # Read the outcome *before* committing. After a commit the ORM expires
        # the instance, so touching `row.status` would trigger a lazy refresh —
        # which raises `MissingGreenlet` unless the caller happened to build
        # their sessionmaker with `expire_on_commit=False`. Every factory in
        # this codebase does, which is exactly why this went unnoticed until a
        # throwaway script used a plain one and crashed on the first retry.
        outcome = row.status

        # Commit per row. A crash mid-batch keeps every delivery already
        # recorded, rather than replaying the whole batch.
        await raw.commit()
        if result.ok:
            totals["delivered"] += 1
        elif outcome is OutboxStatus.DEAD:
            totals["dead"] += 1
        else:
            totals["failed"] += 1

    return totals


async def dispatch_forever(
    session_factory: Any,
    *,
    transport: Transport,
    interval_seconds: float = 5.0,
) -> None:  # pragma: no cover - the long-running loop
    """A worker loop, for a dedicated dispatcher process."""
    while True:
        async with session_factory() as raw:
            await run_dispatch(raw, transport=transport, now=dt.datetime.now(dt.UTC))
        await asyncio.sleep(interval_seconds)
