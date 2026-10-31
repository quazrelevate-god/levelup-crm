"""The outbound event bus (M10).

The milestone's acceptance criterion is *"kill the worker mid-delivery and show
a retry rather than a lost event"*, so the first test simulates exactly that: a
row left in `DELIVERING` by a process that never came back, and a later pass
that picks it up.

The rest cover the properties a consumer depends on — a stable `event_id` across
retries, a signature over the exact bytes sent, backoff, the DEAD threshold —
and the one a *customer* depends on: that a webhook payload is projected through
the endpoint's template, so the bus is not a hole in the field matrix.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import WorkspaceFixture, build_workspace

from app.auth.passwords import PasswordHasherService
from app.events.dispatcher import (
    CLAIM_TIMEOUT,
    DEAD_AFTER_ATTEMPTS,
    STALE_ERROR,
    DeliveryResult,
    next_attempt_after,
    run_dispatch,
)
from app.events.envelope import EVENT_NAMES, build_envelope, serialise, sign, verify
from app.events.outbox import publish
from app.models import Lead, OutboxEvent, OutboxStatus, WebhookEndpoint
from app.tenancy.session import ScopedSession

pytestmark = pytest.mark.integration


@dataclass
class RecordingTransport:
    """A transport that answers however the test needs, and remembers."""

    status_code: int | None = 200
    error: str | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    async def post(self, url: str, *, body: bytes, headers: dict[str, str]) -> DeliveryResult:
        self.calls.append({"url": url, "body": body, "headers": dict(headers)})
        return DeliveryResult(status_code=self.status_code, error=self.error)


@pytest.fixture
def hasher(wired_app: FastAPI) -> PasswordHasherService:
    hasher = wired_app.state.password_hasher
    assert isinstance(hasher, PasswordHasherService)
    return hasher


async def _setup(
    db_session: AsyncSession,
    hasher: PasswordHasherService,
    *,
    name: str,
    events: list[str] | None = None,
) -> tuple[WorkspaceFixture, WebhookEndpoint]:
    fixture = await build_workspace(
        db_session, hasher, name=name, owner_email=f"{name.lower()}@example.com"
    )
    endpoint = WebhookEndpoint(
        workspace_id=fixture.workspace.id,
        name="Consumer",
        url="https://consumer.example.com/hook",
        secret="whsec_test_secret",
        events=events or [],
        permission_template_id=fixture.templates["Root"].id,
    )
    db_session.add(endpoint)
    await db_session.commit()
    return fixture, endpoint


async def _queue(
    db_session: AsyncSession, fixture: WorkspaceFixture, endpoint: WebhookEndpoint, **overrides
) -> OutboxEvent:
    row = OutboxEvent(
        workspace_id=fixture.workspace.id,
        event="lead.created",
        event_id=uuid.uuid4(),
        endpoint_id=endpoint.id,
        payload={"lead_id": str(uuid.uuid4()), "values": {"name": "Someone"}},
        **overrides,
    )
    db_session.add(row)
    await db_session.commit()
    return row


# --- the acceptance criterion ------------------------------------------------


async def test_a_row_left_claimed_by_a_dead_worker_is_retried_not_lost(
    db_session: AsyncSession, hasher: PasswordHasherService
) -> None:
    """Kill the worker mid-delivery; the event is delivered, not lost.

    A crashed worker leaves its row in `DELIVERING` with a `claimed_at` it can
    no longer refresh. That is the whole recovery mechanism: no coordination, no
    leader election, just a timeout only a live worker can beat.

    This is what makes delivery at-least-once rather than at-most-once, which is
    the right trade — a lead event delivered twice is a consumer's dedupe
    problem, and one delivered never is a lost customer.
    """
    fixture, endpoint = await _setup(db_session, hasher, name="Crashy")
    now = dt.datetime.now(dt.UTC)

    abandoned = await _queue(
        db_session,
        fixture,
        endpoint,
        status=OutboxStatus.DELIVERING,
        claimed_at=now - CLAIM_TIMEOUT - dt.timedelta(minutes=1),
        next_attempt_at=now - dt.timedelta(minutes=1),
    )

    transport = RecordingTransport()
    result = await run_dispatch(db_session, transport=transport, now=now)

    assert result["claimed"] == 1
    assert result["delivered"] == 1
    await db_session.refresh(abandoned)
    assert abandoned.status is OutboxStatus.DELIVERED
    assert len(transport.calls) == 1


async def test_a_freshly_claimed_row_is_left_alone(
    db_session: AsyncSession, hasher: PasswordHasherService
) -> None:
    """The other half: a *live* worker's row must not be stolen.

    Without the timeout check this would double-send every in-flight event.
    """
    fixture, endpoint = await _setup(db_session, hasher, name="Busy")
    now = dt.datetime.now(dt.UTC)
    await _queue(
        db_session,
        fixture,
        endpoint,
        status=OutboxStatus.DELIVERING,
        claimed_at=now - dt.timedelta(seconds=5),
        next_attempt_at=now - dt.timedelta(minutes=1),
    )

    transport = RecordingTransport()
    result = await run_dispatch(db_session, transport=transport, now=now)

    assert result["claimed"] == 0
    assert transport.calls == []


# --- retry, backoff, DEAD ----------------------------------------------------


async def test_a_failure_backs_off_and_stays_pending(
    db_session: AsyncSession, hasher: PasswordHasherService
) -> None:
    fixture, endpoint = await _setup(db_session, hasher, name="Flaky")
    now = dt.datetime.now(dt.UTC)
    row = await _queue(db_session, fixture, endpoint, next_attempt_at=now)

    transport = RecordingTransport(status_code=500)
    await run_dispatch(db_session, transport=transport, now=now)

    await db_session.refresh(row)
    assert row.status is OutboxStatus.FAILED
    assert row.attempts == 1
    assert row.last_status_code == 500
    assert row.next_attempt_at > now, "a failure must not be retried immediately"


async def test_backoff_is_exponential_and_capped() -> None:
    now = dt.datetime(2026, 8, 21, 12, 0, tzinfo=dt.UTC)
    assert (next_attempt_after(1, now=now) - now).total_seconds() == 120
    assert (next_attempt_after(3, now=now) - now).total_seconds() == 480
    # Capped, so eight attempts is about four hours rather than four days.
    assert (next_attempt_after(10, now=now) - now).total_seconds() == 3600


async def test_a_row_goes_dead_after_the_attempt_budget(
    db_session: AsyncSession, hasher: PasswordHasherService
) -> None:
    """And is never retried automatically again — an operator decides."""
    fixture, endpoint = await _setup(db_session, hasher, name="Doomed")
    now = dt.datetime.now(dt.UTC)
    row = await _queue(
        db_session,
        fixture,
        endpoint,
        attempts=DEAD_AFTER_ATTEMPTS - 1,
        status=OutboxStatus.FAILED,
        next_attempt_at=now,
    )

    transport = RecordingTransport(status_code=None, error="connection refused")
    await run_dispatch(db_session, transport=transport, now=now)

    await db_session.refresh(row)
    assert row.status is OutboxStatus.DEAD
    assert row.attempts == DEAD_AFTER_ATTEMPTS
    assert "connection refused" in (row.last_error or "")

    # A later pass must not pick it up.
    transport.calls.clear()
    later = await run_dispatch(db_session, transport=transport, now=now + dt.timedelta(hours=2))
    assert later["claimed"] == 0


async def test_the_event_id_is_stable_across_retries(
    db_session: AsyncSession, hasher: PasswordHasherService
) -> None:
    """The consumer's dedupe key.

    Regenerating it per attempt would turn one retried delivery into eight
    distinct events at the far end — exactly the duplication an at-least-once
    bus asks consumers to absorb, made unabsorbable.
    """
    fixture, endpoint = await _setup(db_session, hasher, name="Stable")
    now = dt.datetime.now(dt.UTC)
    row = await _queue(db_session, fixture, endpoint, next_attempt_at=now)
    original = row.event_id

    transport = RecordingTransport(status_code=503)
    await run_dispatch(db_session, transport=transport, now=now)
    await db_session.refresh(row)

    row.next_attempt_at = now
    await db_session.commit()
    await run_dispatch(db_session, transport=transport, now=now)
    await db_session.refresh(row)

    assert row.event_id == original
    assert len(transport.calls) == 2
    sent = {call["headers"]["X-CRM-Event-Id"] for call in transport.calls}  # type: ignore[index]
    assert sent == {str(original)}


# --- the signature -----------------------------------------------------------


async def test_the_signature_covers_the_exact_bytes_sent(
    db_session: AsyncSession, hasher: PasswordHasherService
) -> None:
    """Over the bytes, not over the dict.

    A consumer re-serialising the JSON and hashing that would fail on key order,
    and the mismatch would look like an attack rather than a bug.
    """
    fixture, endpoint = await _setup(db_session, hasher, name="Signed")
    now = dt.datetime.now(dt.UTC)
    await _queue(db_session, fixture, endpoint, next_attempt_at=now)

    transport = RecordingTransport()
    await run_dispatch(db_session, transport=transport, now=now)

    call = transport.calls[0]
    assert verify(endpoint.secret, call["body"], call["headers"]["X-CRM-Signature"])  # type: ignore[index,arg-type]
    assert call["headers"]["X-CRM-Event"] == "lead.created"  # type: ignore[index]


def test_serialisation_is_stable() -> None:
    """Same envelope, same bytes — twice, in different key order."""
    now = dt.datetime(2026, 8, 21, 12, 0, tzinfo=dt.UTC)
    event_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    first = build_envelope(
        event="lead.created",
        event_id=event_id,
        workspace_id=workspace_id,
        occurred_at=now,
        data={"b": 2, "a": 1},
    )
    second = build_envelope(
        event="lead.created",
        event_id=event_id,
        workspace_id=workspace_id,
        occurred_at=now,
        data={"a": 1, "b": 2},
    )
    assert serialise(first) == serialise(second)
    assert sign("s", serialise(first)) == sign("s", serialise(second))


# --- the field matrix --------------------------------------------------------


async def test_a_webhook_payload_is_projected_through_its_own_template(
    db_session: AsyncSession, hasher: PasswordHasherService
) -> None:
    """A webhook is a read path like any other (rule 3).

    Without this the bus would be the one place a `View` denial could be
    bypassed — by subscribing to an event instead of calling an endpoint.

    A field added *after* provisioning is the honest case: no template holds a
    grant on it until an admin gives one, so it is invisible to Caller while the
    built-in fields Caller does hold View on come through. Asserting both halves
    matters — "everything was stripped" would also pass a projection that simply
    dropped the payload.
    """
    from app.models import LeadField, LeadFieldType

    fixture, endpoint = await _setup(db_session, hasher, name="Projected")
    endpoint.permission_template_id = fixture.templates["Caller"].id
    ungranted = LeadField(
        workspace_id=fixture.workspace.id,
        key="salary",
        label="Salary",
        field_type=LeadFieldType.NUMBER,
        sort_order=99,
    )
    db_session.add(ungranted)
    await db_session.commit()

    now = dt.datetime.now(dt.UTC)
    row = await _queue(db_session, fixture, endpoint, next_attempt_at=now)
    row.payload = {
        "lead_id": str(uuid.uuid4()),
        "values": {"name": "Someone", "salary": "1000000"},
    }
    await db_session.commit()

    transport = RecordingTransport()
    await run_dispatch(db_session, transport=transport, now=now)

    import json

    body = json.loads(transport.calls[0]["body"])  # type: ignore[arg-type]
    values = body["data"]["values"]
    assert "salary" not in values, "an ungranted field reached the consumer"
    assert values.get("name") == "Someone", "a granted field was stripped too"


# --- publishing --------------------------------------------------------------


async def test_publish_fans_out_to_subscribed_endpoints_only(
    db_session: AsyncSession, hasher: PasswordHasherService
) -> None:
    fixture, wide = await _setup(db_session, hasher, name="FanOut")
    narrow = WebhookEndpoint(
        workspace_id=fixture.workspace.id,
        name="Only stage changes",
        url="https://consumer.example.com/stages",
        secret="whsec_two",
        events=["lead.stage_changed"],
        permission_template_id=fixture.templates["Root"].id,
    )
    db_session.add(narrow)
    await db_session.commit()

    scoped = ScopedSession(db_session, fixture.workspace.id)
    queued = await publish(scoped, event="lead.created", data={"lead_id": "x"})
    await db_session.commit()

    # The wide endpoint subscribes to everything (empty list); the narrow one
    # asked for one event and must not receive this.
    assert len(queued) == 1
    assert queued[0].endpoint_id == wide.id


async def test_publishing_costs_nothing_with_no_endpoints(
    db_session: AsyncSession, hasher: PasswordHasherService
) -> None:
    """A workspace should not pay for a bus it does not use."""
    fixture = await build_workspace(
        db_session, hasher, name="No Bus", owner_email="nobus@example.com"
    )
    scoped = ScopedSession(db_session, fixture.workspace.id)
    assert await publish(scoped, event="lead.created", data={}) == []


async def test_publish_refuses_an_event_the_product_does_not_emit(
    db_session: AsyncSession, hasher: PasswordHasherService
) -> None:
    fixture = await build_workspace(
        db_session, hasher, name="Unknown Event", owner_email="unknown@example.com"
    )
    scoped = ScopedSession(db_session, fixture.workspace.id)
    with pytest.raises(ValueError, match="not a known event"):
        await publish(scoped, event="lead.exploded", data={})


async def test_every_lead_mutation_queues_an_event(
    db_session: AsyncSession, hasher: PasswordHasherService
) -> None:
    """Publishing lives in `ActionWriter`, so no write path can forget it.

    Every mutation already writes an action (rule 5); the event rides the same
    chokepoint, which is why this holds for bulk edits, imports and undo too
    without any of them knowing the bus exists.
    """
    from app.permissions import FieldGrants, FieldProjectionService, FieldWriteFilter
    from app.services.leads import LeadService

    fixture, endpoint = await _setup(db_session, hasher, name="Chokepoint")
    scoped = ScopedSession(db_session, fixture.workspace.id)

    every = frozenset({"phone", "name", "email"})
    grants = FieldGrants(view=every, edit=every, import_=every, export=every, is_admin=True)
    service = LeadService(
        scoped,
        workspace=fixture.workspace,
        projection=FieldProjectionService(grants),
        write_filter=FieldWriteFilter(grants),
        actor_id=fixture.owner.membership.id,
        visible_membership_ids=frozenset({fixture.owner.membership.id}),
        sees_all=True,
    )
    lead, _ = await service.create_lead(values={"phone": "+19995551212", "name": "Queued"})
    await db_session.commit()

    rows = await db_session.execute(
        scoped.select(OutboxEvent).where(OutboxEvent.endpoint_id == endpoint.id)
    )
    events = [row.event for row in rows.scalars().all()]
    assert "lead.created" in events
    assert isinstance(lead, Lead)


def test_the_event_catalogue_matches_the_contract() -> None:
    """Product concepts, so a constant. Stages are rows; these are not."""
    assert {
        "lead.created",
        "lead.updated",
        "lead.stage_changed",
        "lead.assigned",
        "lead.field_changed",
        "action.created",
        "task.created",
        "task.completed",
    } == EVENT_NAMES


async def test_dispatch_survives_a_session_that_expires_on_commit(
    postgres_async_dsn: str, db_session: AsyncSession, hasher: PasswordHasherService
) -> None:
    """`run_dispatch` must not depend on the caller's sessionmaker settings.

    Every factory in this codebase sets `expire_on_commit=False`, which hid a
    real defect: the loop read `row.status` *after* committing, so the ORM
    expired the instance and the attribute access tried to lazy-load outside a
    greenlet. A throwaway script using a plain `async_sessionmaker` crashed on
    the first retry — and so would any second worker somebody writes.

    So this test deliberately builds the session the way somebody *not* reading
    `db.py` would.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    fixture, endpoint = await _setup(db_session, hasher, name="Expiring")
    now = dt.datetime.now(dt.UTC)
    await _queue(db_session, fixture, endpoint, next_attempt_at=now)

    engine = create_async_engine(postgres_async_dsn)
    try:
        plain = async_sessionmaker(engine)  # expire_on_commit defaults to True
        async with plain() as raw:
            result = await run_dispatch(raw, transport=RecordingTransport(status_code=500), now=now)
        assert result["failed"] == 1
    finally:
        await engine.dispose()


async def test_an_event_older_than_the_limit_is_retired_not_sent(
    db_session: AsyncSession, hasher: PasswordHasherService
) -> None:
    """After an outage with no worker, days-old webhooks are not flooded out.

    The young event still goes; the old one is DEAD with a reason the operator
    can read, and can be redriven by hand if it still matters (docs/21).
    """
    fixture, endpoint = await _setup(db_session, hasher, name="Backlog")
    now = dt.datetime.now(dt.UTC)
    old = await _queue(
        db_session,
        fixture,
        endpoint,
        occurred_at=now - dt.timedelta(days=5),
        next_attempt_at=now - dt.timedelta(days=5),
    )
    young = await _queue(db_session, fixture, endpoint, next_attempt_at=now)

    transport = RecordingTransport()
    result = await run_dispatch(
        db_session, transport=transport, now=now, max_age=dt.timedelta(hours=48)
    )

    await db_session.refresh(old)
    await db_session.refresh(young)
    assert (old.status, old.last_error) == (OutboxStatus.DEAD, STALE_ERROR)
    assert young.status is OutboxStatus.DELIVERED
    assert (result["delivered"], result["dead"]) == (1, 1)
    assert len(transport.calls) == 1
