"""Connected apps: outside systems that tell the CRM something happened (docs/18).

Today one app, Razorpay. Connecting it gives the workspace a webhook URL to
paste into Razorpay's dashboard; Razorpay's own signing secret, pasted back
here, is how every delivery is proven to be Razorpay's.

What a delivery does, in order:

1. **Verify** the `X-Razorpay-Signature` HMAC over the raw body. A delivery
   that fails is logged as REJECTED (so a wrong secret is visible on the
   settings screen) and refused with 401.
2. **Normalise** the payment into the fields workflows read — amount in rupees,
   phone, email, name, payment id, notes.
3. **Match** the lead: an explicit lead id in the payment notes, else exactly
   one lead with that phone, else exactly one with that email. Never a guess.
4. **Claim** the payment id. Razorpay sends several events for one payment and
   retries on any hiccup; a unique index lets exactly one of them through.
5. Optionally **create** the lead, then note the payment on the lead's timeline
   and **run** every switched-on workflow for that trigger.

Steps 4 and 5 are one transaction, so a payment either did everything or nothing.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import hmac
import json
import secrets
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.errors import api_error, not_found
from app.models.automation import AppConnection, AppEvent
from app.models.enums import ChangesetSource, LeadFieldType
from app.models.field import LeadField
from app.models.lead import Lead
from app.models.workspace import Workspace
from app.services.actions import ActionWriter
from app.services.leads import LeadService
from app.services.system_filters import system_lead_service
from app.services.workflows import WorkflowEngine
from app.tenancy.session import ScopedSession

__all__ = [
    "APPS",
    "AppService",
    "AppSpec",
    "RazorpayReceiver",
    "normalise_razorpay",
    "razorpay_signature",
]


@dataclasses.dataclass(frozen=True, slots=True)
class AppSpec:
    key: str
    label: str
    description: str
    #: Options a workspace may set, with their defaults.
    settings: Mapping[str, Any]


APPS: dict[str, AppSpec] = {
    "razorpay": AppSpec(
        key="razorpay",
        label="Razorpay",
        description=(
            "Know the moment a lead pays. Payments arrive here, are matched to the lead, "
            "and start your workflows."
        ),
        settings={"create_lead_if_missing": False},
    )
}

#: Razorpay event → the CRM trigger it starts. A payment link, an order and a
#: bare payment all end in a captured payment; they share a trigger and are
#: deduplicated on the payment id, so one payment runs its workflows once.
RAZORPAY_EVENTS: dict[str, str] = {
    "payment.captured": "razorpay.payment_captured",
    "payment_link.paid": "razorpay.payment_captured",
    "order.paid": "razorpay.payment_captured",
    "payment.failed": "razorpay.payment_failed",
}

#: Note keys that may carry the CRM's own lead id, if whoever made the payment
#: link put it there. The surest match there is.
_LEAD_ID_NOTES = ("crm_lead_id", "lead_id")
_MAX_PAYLOAD_BYTES = 32_000


def razorpay_signature(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _entity(payload: Mapping[str, Any], name: str) -> dict[str, Any]:
    block = payload.get("payload")
    if not isinstance(block, Mapping):
        return {}
    wrapper = block.get(name)
    entity = wrapper.get("entity") if isinstance(wrapper, Mapping) else None
    return dict(entity) if isinstance(entity, Mapping) else {}


def _rupees(paise: Any) -> str | None:
    """`40000` → `"400"`; `40050` → `"400.50"`. Razorpay counts in paise."""
    try:
        amount = Decimal(int(paise)) / 100
    except (TypeError, ValueError):
        return None
    return str(amount.quantize(Decimal(1))) if amount == amount.to_integral() else f"{amount:.2f}"


def _first(*values: Any) -> str | None:
    for value in values:
        if value not in (None, ""):
            return str(value).strip()
    return None


def normalise_razorpay(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The fields a workflow can check or copy, from any payment event."""
    payment = _entity(payload, "payment")
    link = _entity(payload, "payment_link")
    order = _entity(payload, "order")
    raw_customer = link.get("customer")
    customer: Mapping[str, Any] = raw_customer if isinstance(raw_customer, Mapping) else {}
    notes: dict[str, Any] = {}
    for source in (order, link, payment):  # payment's own notes win
        raw = source.get("notes")
        if isinstance(raw, Mapping):
            notes.update({str(k): v for k, v in raw.items()})
    error = payment.get("error_description") or payment.get("error_reason")
    data: dict[str, Any] = {
        "amount": _rupees(payment.get("amount") or link.get("amount_paid") or order.get("amount")),
        "currency": _first(payment.get("currency"), link.get("currency"), order.get("currency")),
        "phone": _first(payment.get("contact"), customer.get("contact"), notes.get("phone")),
        "email": _first(payment.get("email"), customer.get("email"), notes.get("email")),
        "name": _first(customer.get("name"), notes.get("name"), notes.get("customer_name")),
        "payment_id": _first(payment.get("id")),
        "order_id": _first(payment.get("order_id"), order.get("id")),
        "payment_link_id": _first(link.get("id")),
        "method": _first(payment.get("method")),
        "description": _first(payment.get("description"), link.get("description")),
        "status": _first(payment.get("status"), link.get("status"), order.get("status")),
        "error_reason": _first(error),
        "notes": notes,
    }
    return {k: v for k, v in data.items() if v not in (None, "", {})}


def _bounded(payload: Mapping[str, Any]) -> dict[str, Any]:
    text = json.dumps(payload, default=str)
    if len(text) <= _MAX_PAYLOAD_BYTES:
        return dict(payload)
    return {"truncated": True, "event": payload.get("event")}


class AppService:
    """Connecting, configuring and inspecting a workspace's apps."""

    def __init__(self, session: ScopedSession, *, workspace: Workspace) -> None:
        self._session = session
        self._workspace = workspace

    async def list_connections(self) -> dict[str, AppConnection]:
        rows = await self._session.execute(self._session.select(AppConnection))
        return {row.provider: row for row in rows.scalars().all()}

    async def get(self, provider: str) -> AppConnection:
        if provider not in APPS:
            raise not_found("App")
        connection = (await self.list_connections()).get(provider)
        if connection is None:
            raise not_found("App connection")
        return connection

    async def connect(self, provider: str, *, actor_id: uuid.UUID | None) -> AppConnection:
        if provider not in APPS:
            raise not_found("App")
        existing = (await self.list_connections()).get(provider)
        if existing is not None:
            existing.is_active = True
            await self._session.flush()
            return existing
        row = AppConnection(
            provider=provider,
            hook_token=secrets.token_urlsafe(32),
            settings=dict(APPS[provider].settings),
            created_by_id=actor_id,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def update(
        self,
        provider: str,
        *,
        is_active: bool | None = None,
        secret: str | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> AppConnection:
        row = await self.get(provider)
        if is_active is not None:
            row.is_active = is_active
        if secret is not None:
            row.secret = secret.strip() or None
        if settings is not None:
            allowed = APPS[provider].settings
            unknown = set(settings) - set(allowed)
            if unknown:
                raise api_error(422, "unknown_setting", f"Unknown setting {sorted(unknown)[0]!r}")
            row.settings = {**allowed, **(row.settings or {}), **settings}
        await self._session.flush()
        return row

    async def rotate_url(self, provider: str) -> AppConnection:
        """A new webhook URL; the old one stops working at once."""
        row = await self.get(provider)
        row.hook_token = secrets.token_urlsafe(32)
        await self._session.flush()
        return row

    async def events(
        self, provider: str, *, limit: int, offset: int
    ) -> tuple[Sequence[tuple[AppEvent, Lead | None]], int]:
        connection = await self.get(provider)
        base = self._session.select(AppEvent).where(AppEvent.connection_id == connection.id)
        total = (
            await self._session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        rows = await self._session.execute(
            base.order_by(AppEvent.created_at.desc()).limit(limit).offset(offset)
        )
        found = list(rows.scalars().all())
        lead_ids = {e.lead_id for e in found if e.lead_id}
        leads: dict[uuid.UUID, Lead] = {}
        if lead_ids:
            lead_rows = await self._session.execute(
                self._session.select(Lead).where(Lead.id.in_(lead_ids))
            )
            leads = {lead.id: lead for lead in lead_rows.scalars().all()}
        return [(e, leads.get(e.lead_id) if e.lead_id else None) for e in found], int(total)


@dataclasses.dataclass(slots=True)
class Receipt:
    event: AppEvent
    accepted: bool


class RazorpayReceiver:
    """One Razorpay delivery, from raw bytes to workflows run."""

    def __init__(
        self, session: ScopedSession, *, workspace: Workspace, connection: AppConnection
    ) -> None:
        self._session = session
        self._workspace = workspace
        self._connection = connection
        # Kept apart from the row: a rolled-back savepoint expires the row,
        # and an async session cannot lazy-load its id afterwards.
        self._connection_id = connection.id
        self._leads: LeadService | None = None

    async def _lead_service(self) -> LeadService:
        if self._leads is None:
            self._leads = await system_lead_service(self._session, self._workspace)
        return self._leads

    async def receive(self, body: bytes, signature: str | None) -> Receipt:
        connection = self._connection
        connection.last_event_at = dt.datetime.now(dt.UTC)

        if not connection.secret:
            return Receipt(
                self._log("unknown", "REJECTED", error="The webhook secret is not set in the CRM"),
                accepted=False,
            )
        expected = razorpay_signature(body, connection.secret)
        if not signature or not hmac.compare_digest(expected, signature.strip()):
            return Receipt(
                self._log(
                    "unknown",
                    "REJECTED",
                    error="The signature did not match. The secret in Razorpay and in the "
                    "CRM must be the same.",
                ),
                accepted=False,
            )

        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return Receipt(self._log("unknown", "IGNORED", error="Not a JSON object"), True)

        event_type = str(payload.get("event") or "unknown")[:80]
        trigger = RAZORPAY_EVENTS.get(event_type)
        if trigger is None:
            return Receipt(
                self._log(event_type, "IGNORED", payload=payload, error="Not a payment event"),
                True,
            )

        data = normalise_razorpay(payload)
        dedupe = data.get("payment_id") or data.get("payment_link_id") or data.get("order_id")
        lead = await self._match(data)
        create = lead is None and bool((connection.settings or {}).get("create_lead_if_missing"))
        outcome = "MATCHED" if lead else ("CREATED_LEAD" if create else "NO_LEAD")

        claimed = await self._claim(
            event_type,
            outcome,
            payload=payload,
            trigger=trigger,
            data=data,
            dedupe_key=str(dedupe)[:160] if dedupe else None,
            lead=lead,
        )
        if claimed is None:
            # Another delivery for this payment got here first.
            event = self._log(
                event_type,
                "DUPLICATE",
                payload=payload,
                trigger=trigger,
                data=data,
                dedupe_key=str(dedupe)[:160] if dedupe else None,
                lead=lead,
            )
            await self._session.flush()
            return Receipt(event, True)
        event = claimed

        if create:
            lead = await self._create_lead(data)
            if lead is None:
                event.outcome = "NO_LEAD"
                event.error = (
                    "No lead has this phone or email, and a new one could not be created "
                    "from this payment"
                )
            else:
                event.lead_id = lead.id
        if lead is None:
            if event.error is None:
                event.error = "No lead has this phone or email"
            await self._session.flush()
            return Receipt(event, True)

        await self._note(lead, trigger, data)
        runs = await WorkflowEngine(self._session, workspace=self._workspace).run_trigger(
            trigger, lead, data, app_event_id=event.id
        )
        event.workflows_run = len(runs)
        await self._session.flush()
        return Receipt(event, True)

    async def _claim(
        self,
        event_type: str,
        outcome: str,
        *,
        payload: Mapping[str, Any],
        trigger: str,
        data: Mapping[str, Any],
        dedupe_key: str | None,
        lead: Lead | None,
    ) -> AppEvent | None:
        """Insert the event unless this payment already has one; `None` if so.

        `ON CONFLICT DO NOTHING` against the partial unique index: a second
        delivery racing the first waits for it, then inserts nothing — no
        exception, so nothing else in the transaction is disturbed.
        """
        statement = (
            pg_insert(AppEvent)
            .values(
                id=uuid.uuid4(),
                workspace_id=self._session.workspace_id,
                connection_id=self._connection_id,
                event_type=event_type,
                trigger=trigger,
                dedupe_key=dedupe_key,
                data=dict(data),
                payload=_bounded(payload),
                outcome=outcome,
                lead_id=lead.id if lead else None,
                workflows_run=0,
            )
            .on_conflict_do_nothing(
                index_elements=["workspace_id", "connection_id", "trigger", "dedupe_key"],
                index_where=text(
                    "dedupe_key IS NOT NULL AND outcome IN ('MATCHED', 'CREATED_LEAD', 'NO_LEAD')"
                ),
            )
            .returning(AppEvent.id)
        )
        event_id = (await self._session.execute(statement)).scalar_one_or_none()
        if event_id is None:
            return None
        return await self._session.get(AppEvent, event_id)

    def _log(
        self,
        event_type: str,
        outcome: str,
        *,
        payload: Mapping[str, Any] | None = None,
        trigger: str | None = None,
        data: Mapping[str, Any] | None = None,
        dedupe_key: str | None = None,
        lead: Lead | None = None,
        error: str | None = None,
    ) -> AppEvent:
        event = AppEvent(
            id=uuid.uuid4(),
            connection_id=self._connection_id,
            event_type=event_type,
            trigger=trigger,
            dedupe_key=dedupe_key,
            data=dict(data or {}),
            # A rejected body is not trusted, so it is not kept.
            payload=_bounded(payload) if payload is not None else {},
            outcome=outcome,
            lead_id=lead.id if lead else None,
            error=error,
        )
        self._session.add(event)
        return event

    async def _fields(self, kind: LeadFieldType) -> list[LeadField]:
        rows = await self._session.execute(
            self._session.select(LeadField)
            .where(LeadField.field_type == kind)
            .order_by(LeadField.sort_order)
        )
        fields = list(rows.scalars().all())
        identity = self._workspace.identity_field_id
        return sorted(fields, key=lambda f: 0 if f.id == identity else 1)

    async def _one(self, key: str, value: str, *, lower: bool = False) -> Lead | bool | None:
        """The one live lead whose `key` is `value`; `False` when several are."""
        column = Lead.values[key].astext
        clause = func.lower(column) == value.lower() if lower else column == value
        rows = await self._session.execute(
            self._session.select(Lead).where(clause, Lead.deleted_at.is_(None)).limit(2)
        )
        found = list(rows.scalars().all())
        if len(found) > 1:
            return False
        return found[0] if found else None

    async def _match(self, data: Mapping[str, Any]) -> Lead | None:
        notes = data.get("notes") if isinstance(data.get("notes"), Mapping) else {}
        for key in _LEAD_ID_NOTES:
            raw = notes.get(key) if isinstance(notes, Mapping) else None
            try:
                lead_id = uuid.UUID(str(raw)) if raw else None
            except ValueError:
                lead_id = None
            if lead_id is not None:
                lead = await self._session.get(Lead, lead_id)
                if lead is not None and lead.deleted_at is None:
                    return lead

        leads = await self._lead_service()
        phone = data.get("phone")
        if phone:
            for field in await self._fields(LeadFieldType.PHONE):
                normalised = await leads.normalise_field_value(field.key, str(phone))
                if normalised is None:
                    continue
                found = await self._one(field.key, normalised)
                if found is False:
                    return None  # two leads share it: refuse to guess
                if isinstance(found, Lead):
                    return found
        email = data.get("email")
        if email:
            for field in await self._fields(LeadFieldType.EMAIL):
                found = await self._one(field.key, str(email).strip(), lower=True)
                if found is False:
                    return None
                if isinstance(found, Lead):
                    return found
        return None

    async def _create_lead(self, data: Mapping[str, Any]) -> Lead | None:
        """Only from a phone, and only when the phone is the identity field."""
        leads = await self._lead_service()
        identity_key = await leads.identity_key()
        phones = await self._fields(LeadFieldType.PHONE)
        if not data.get("phone") or not phones or phones[0].key != identity_key:
            return None
        values: dict[str, Any] = {identity_key: str(data["phone"])}
        for field in await self._fields(LeadFieldType.EMAIL):
            if data.get("email"):
                values[field.key] = str(data["email"])
            break
        name_field = await self._session.execute(
            self._session.select(LeadField).where(LeadField.key == "name")
        )
        if data.get("name") and name_field.scalars().first() is not None:
            values["name"] = str(data["name"])[:200]
        lead, _ = await leads.create_lead(values=values)
        await self._session.flush()
        return lead

    async def _note(self, lead: Lead, trigger: str, data: Mapping[str, Any]) -> None:
        """The payment on the lead's timeline, whatever the workflows do."""
        writer = ActionWriter(self._session, actor_id=None)
        await writer.open_changeset(
            source=ChangesetSource.AUTOMATION,
            summary=f"Razorpay payment for {lead.identity_value}"[:200],
        )
        amount = f"{data.get('currency', '')} {data.get('amount', '?')}".strip()
        if trigger == "razorpay.payment_failed":
            reason = data.get("error_reason")
            body = f"💳 Razorpay: payment of {amount} failed" + (f" — {reason}" if reason else "")
        else:
            body = f"💳 Razorpay: payment received — {amount}"
        reference = ", ".join(str(data[k]) for k in ("payment_id", "method") if data.get(k))
        writer.record_note(lead, body=body + (f" ({reference})" if reference else ""))
        await self._session.flush()
