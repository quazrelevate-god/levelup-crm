"""The operator console: running every client company from one screen (docs/19).

Everything here is the platform operator's — the person who sells this CRM.
They create companies, decide seats and paid modules, put money in wallets,
add people at any role, suspend a company that has not paid, and step inside a
company to help. A client's own Root or Admin can do none of that.

**One company at a time, scoped.** The operator stands outside every tenant, so
reads across companies are explicit `WHERE workspace_id = …` aggregates, and any
work *inside* a company goes through a `ScopedSession` for that company alone.
The tenant scope is sticky on the underlying session (`bind_scope`), so it is
cleared again before the next company is touched.

**Every change is written down.** Each action leaves an `operator_actions` row
saying what changed, from what to what — above all for money.

**No password ever passes through here.** A new person gets a "set your
password" link; the operator can copy it to send on WhatsApp, and it is also
emailed. Nobody, the operator included, ever knows a client's password.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.passwords import PasswordHasherService
from app.config import Settings
from app.errors import api_error, conflict, not_found
from app.models import (
    Membership,
    OperatorAction,
    PermissionTemplate,
    User,
    VoiceCallExecution,
    VoiceNumberRequest,
    Wallet,
    WalletTransaction,
    Workspace,
)
from app.models.enums import AvailabilityStatus, WalletTransactionKind
from app.schemas.common import PageParams
from app.services.credentials import CredentialService, IssuedResetToken, TokenPurpose
from app.services.lead_ownership import LeadOwnership
from app.services.members import MemberService
from app.services.number_requests import NumberRequestService
from app.services.provisioning import ROOT_TEMPLATE_NAME, WorkspaceProvisioner
from app.services.wallet import WalletService
from app.tenancy.features import FEATURE_FLAGS
from app.tenancy.session import ScopedSession, bind_scope

__all__ = [
    "FEATURE_INFO",
    "CompanyRow",
    "Invitation",
    "OperatorConsole",
]

#: What each switchable module is, in the words the console shows. The set of
#: modules is `FEATURE_FLAGS`; this only names them.
FEATURE_INFO: dict[str, tuple[str, str]] = {
    "automations": ("Workflows & apps", "Razorpay and other apps, and workflows that act on them."),
    "campaign": ("Campaigns", "Grouped outreach to a set of leads."),
    "custom_actions": ("Custom actions", "Their own activity types on the lead timeline."),
    "sales_group": ("Sales groups", "Teams with weights, for routing and reports."),
    "lead_recapture": ("Lead recapture", "Re-open a lead when it comes in again."),
    "new_leads_view": ("New leads view", "A separate inbox for fresh leads."),
    "location_check_in": ("Location check-in", "Field staff record where they are."),
    "system_fields": ("System fields", "Show the CRM's built-in tracking fields."),
}


@dataclasses.dataclass(slots=True)
class CompanyRow:
    workspace: Workspace
    owner_name: str | None
    owner_email: str | None
    seats_used: int
    members: int
    balance: Decimal | None
    rate_per_minute: Decimal | None
    minutes_this_month: int
    low_balance: bool
    support_active: bool


@dataclasses.dataclass(slots=True)
class Invitation:
    """A person added, and how they first sign in."""

    membership: Membership
    #: A one-time "set your password" link; `None` when the person already had
    #: an account and signs in with their own password.
    link: str | None
    expires_at: dt.datetime | None


def _counts(rows: Sequence[Any]) -> dict[uuid.UUID, int]:
    """`{workspace_id: n}` from `(workspace_id, n)` rows."""
    return {row[0]: int(row[1] or 0) for row in rows}


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}"


class OperatorConsole:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        actor_id: uuid.UUID,
        actor_email: str,
        hasher: PasswordHasherService,
        lead_ownership: LeadOwnership,
    ) -> None:
        self._raw = session
        self._settings = settings
        self._actor_id = actor_id
        self._actor_email = actor_email
        self._hasher = hasher
        self._lead_ownership = lead_ownership

    # --- plumbing --------------------------------------------------------------

    def _scoped(self, workspace_id: uuid.UUID) -> ScopedSession:
        return ScopedSession(self._raw, workspace_id)

    def _unscope(self) -> None:
        bind_scope(self._raw, None)

    @property
    def low_balance_at(self) -> Decimal:
        return self._settings.operator_low_balance

    def link_for(self, issued: IssuedResetToken) -> str:
        return f"{self._settings.app_base_url.rstrip('/')}/set-password?token={issued.token}"

    def record(
        self,
        action: str,
        summary: str,
        *,
        workspace_id: uuid.UUID | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self._raw.add(
            OperatorAction(
                actor_user_id=self._actor_id,
                actor_email=self._actor_email,
                workspace_id=workspace_id,
                action=action,
                summary=summary[:2000],
                details=dict(details or {}),
            )
        )

    async def workspace(self, workspace_id: uuid.UUID) -> Workspace:
        self._unscope()
        row = await self._raw.get(Workspace, workspace_id)
        if row is None:
            raise not_found("Company")
        return row

    # --- the ledger ------------------------------------------------------------

    async def companies(self) -> list[CompanyRow]:
        self._unscope()
        workspaces = list(
            (await self._raw.execute(select(Workspace).order_by(Workspace.created_at))).scalars()
        )
        seats: dict[uuid.UUID, int] = _counts(
            (
                await self._raw.execute(
                    select(Membership.workspace_id, func.count())
                    .where(
                        Membership.has_license.is_(True),
                        Membership.is_support.is_(False),
                        Membership.is_active.is_(True),
                    )
                    .group_by(Membership.workspace_id)
                )
            ).all()
        )
        members: dict[uuid.UUID, int] = _counts(
            (
                await self._raw.execute(
                    select(Membership.workspace_id, func.count())
                    .where(Membership.is_support.is_(False))
                    .group_by(Membership.workspace_id)
                )
            ).all()
        )
        support = {
            row[0]
            for row in (
                await self._raw.execute(
                    select(Membership.workspace_id).where(
                        Membership.user_id == self._actor_id,
                        Membership.is_support.is_(True),
                        Membership.is_active.is_(True),
                    )
                )
            ).all()
        }
        owners: dict[uuid.UUID, tuple[str, str]] = {}
        owner_rows = await self._raw.execute(
            select(Membership.workspace_id, User.full_name, User.email)
            .join(User, User.id == Membership.user_id)
            .join(PermissionTemplate, PermissionTemplate.id == Membership.template_id)
            .where(
                PermissionTemplate.name == ROOT_TEMPLATE_NAME,
                PermissionTemplate.is_readonly.is_(True),
                Membership.is_support.is_(False),
            )
            .order_by(Membership.created_at)
        )
        for workspace_id, name, email in owner_rows.all():
            owners.setdefault(workspace_id, (name, email))
        wallets = {
            w.workspace_id: w for w in (await self._raw.execute(select(Wallet))).scalars().all()
        }
        month_start = dt.datetime.now(dt.UTC).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        minutes: dict[uuid.UUID, int] = _counts(
            (
                await self._raw.execute(
                    select(
                        WalletTransaction.workspace_id,
                        func.coalesce(func.sum(WalletTransaction.billed_minutes), 0),
                    )
                    .where(
                        WalletTransaction.kind == WalletTransactionKind.CALL_CHARGE,
                        WalletTransaction.created_at >= month_start,
                    )
                    .group_by(WalletTransaction.workspace_id)
                )
            ).all()
        )
        out: list[CompanyRow] = []
        for workspace in workspaces:
            wallet = wallets.get(workspace.id)
            owner = owners.get(workspace.id)
            out.append(
                CompanyRow(
                    workspace=workspace,
                    owner_name=owner[0] if owner else None,
                    owner_email=owner[1] if owner else None,
                    seats_used=int(seats.get(workspace.id, 0)),
                    members=int(members.get(workspace.id, 0)),
                    balance=wallet.balance if wallet else None,
                    rate_per_minute=wallet.rate_per_minute if wallet else None,
                    minutes_this_month=int(minutes.get(workspace.id, 0)),
                    low_balance=bool(wallet and wallet.balance < self.low_balance_at),
                    support_active=workspace.id in support,
                )
            )
        return out

    async def company(self, workspace_id: uuid.UUID) -> CompanyRow:
        await self.workspace(workspace_id)
        for row in await self.companies():
            if row.workspace.id == workspace_id:
                return row
        raise not_found("Company")  # pragma: no cover - just fetched

    async def calls_today(self) -> int:
        self._unscope()
        since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=24)
        count = await self._raw.execute(
            select(func.count())
            .select_from(VoiceCallExecution)
            .where(VoiceCallExecution.created_at >= since)
        )
        return int(count.scalar_one())

    # --- creating a company -------------------------------------------------------

    async def _user_for(self, *, email: str, full_name: str) -> tuple[User, bool]:
        """The account for an address, created if new. `True` when created."""
        self._unscope()
        address = email.strip().casefold()
        found = (
            await self._raw.execute(select(User).where(User.email == address).limit(1))
        ).scalar_one_or_none()
        if found is not None:
            return found, False
        user = User(
            email=address,
            full_name=full_name.strip(),
            # Unguessable and never told to anyone: the person sets their own.
            password_hash=self._hasher.hash(uuid.uuid4().hex + uuid.uuid4().hex),
        )
        self._raw.add(user)
        await self._raw.flush()
        return user, True

    async def _link(self, user: User, created: bool) -> tuple[str | None, dt.datetime | None]:
        if not created:
            return None, None
        issued = await CredentialService(self._raw).issue(user=user, purpose=TokenPurpose.INVITE)
        return self.link_for(issued), issued.expires_at

    async def create_company(
        self,
        *,
        name: str,
        default_country_code: str,
        timezone: str,
        currency: str,
        seat_limit: int,
        features: Mapping[str, bool],
        opening_balance: Decimal,
        rate_per_minute: Decimal,
        owner_name: str,
        owner_email: str,
    ) -> tuple[Workspace, Invitation]:
        unknown = set(features) - FEATURE_FLAGS
        if unknown:
            raise api_error(422, "unknown_feature", f"No feature called {sorted(unknown)[0]!r}")
        if opening_balance < 0 or rate_per_minute < 0:
            raise api_error(422, "invalid_amount", "Money cannot be negative")

        owner, created = await self._user_for(email=owner_email, full_name=owner_name)
        workspace, membership = await WorkspaceProvisioner(self._raw).provision(
            name=name.strip(),
            owner=owner,
            default_country_code=default_country_code,
            timezone=timezone,
            currency=currency,
            seat_limit=seat_limit,
        )
        workspace.features = {**(workspace.features or {}), **dict(features)}

        scoped = self._scoped(workspace.id)
        wallets = WalletService(scoped, workspace=workspace)
        await wallets.create(rate_per_minute=rate_per_minute)
        if opening_balance > 0:
            await wallets.record(
                WalletTransactionKind.TOPUP,
                opening_balance,
                note="Opening balance",
                recorded_by=self._actor_email,
            )
        self._unscope()

        link, expires = await self._link(owner, created)
        self.record(
            "company.created",
            f"Created {workspace.name}: {seat_limit} seats, owner {owner.email}, "
            f"opening balance {workspace.currency} {_money(opening_balance)}",
            workspace_id=workspace.id,
            details={
                "seat_limit": seat_limit,
                "owner_email": owner.email,
                "opening_balance": _money(opening_balance),
                "rate_per_minute": _money(rate_per_minute),
                "features": dict(features),
            },
        )
        await self._raw.flush()
        return workspace, Invitation(membership=membership, link=link, expires_at=expires)

    # --- editing a company ------------------------------------------------------------

    async def update_company(
        self,
        workspace_id: uuid.UUID,
        *,
        name: str | None = None,
        seat_limit: int | None = None,
        features: Mapping[str, bool] | None = None,
        is_active: bool | None = None,
        timezone: str | None = None,
        default_country_code: str | None = None,
    ) -> Workspace:
        workspace = await self.workspace(workspace_id)
        changes: list[str] = []
        details: dict[str, Any] = {}

        if name is not None and name.strip() and name.strip() != workspace.name:
            changes.append(f"Name “{workspace.name}” → “{name.strip()}”")
            workspace.name = name.strip()
        if seat_limit is not None and seat_limit != workspace.seat_limit:
            used = (await self.company(workspace_id)).seats_used
            if seat_limit < used:
                raise conflict(
                    "seats_in_use",
                    f"{used} people already hold a seat. Remove or deactivate some first, "
                    f"or keep at least {used} seats.",
                )
            changes.append(f"Seats {workspace.seat_limit} → {seat_limit}")
            details["seat_limit"] = [workspace.seat_limit, seat_limit]
            workspace.seat_limit = seat_limit
        if features is not None:
            unknown = set(features) - FEATURE_FLAGS
            if unknown:
                raise api_error(422, "unknown_feature", f"No feature called {sorted(unknown)[0]!r}")
            current = dict(workspace.features or {})
            for key, on in features.items():
                if bool(current.get(key, False)) != bool(on):
                    label = FEATURE_INFO.get(key, (key, ""))[0]
                    changes.append(f"{label} {'on' if on else 'off'}")
                    current[key] = bool(on)
            details["features"] = dict(features)
            workspace.features = current
        if is_active is not None and is_active != workspace.is_active:
            changes.append("Reactivated" if is_active else "Suspended")
            workspace.is_active = is_active
        if timezone is not None and timezone != workspace.timezone:
            changes.append(f"Timezone {workspace.timezone} → {timezone}")
            workspace.timezone = timezone
        if default_country_code is not None and default_country_code != (
            workspace.default_country_code
        ):
            before_code = workspace.default_country_code
            changes.append(f"Country code +{before_code} → +{default_country_code}")
            workspace.default_country_code = default_country_code

        if changes:
            action = (
                "company.suspended"
                if is_active is False and "Suspended" in changes
                else "company.updated"
            )
            self.record(action, "; ".join(changes), workspace_id=workspace.id, details=details)
        await self._raw.flush()
        return workspace

    # --- people ---------------------------------------------------------------------------

    async def people(self, workspace_id: uuid.UUID) -> list[Membership]:
        await self.workspace(workspace_id)
        rows = await self._raw.execute(
            select(Membership)
            .where(Membership.workspace_id == workspace_id)
            .options(selectinload(Membership.user), selectinload(Membership.template))
            .order_by(Membership.created_at)
        )
        return list(rows.scalars().all())

    async def roles(self, workspace_id: uuid.UUID) -> list[PermissionTemplate]:
        await self.workspace(workspace_id)
        rows = await self._raw.execute(
            select(PermissionTemplate)
            .where(PermissionTemplate.workspace_id == workspace_id)
            .order_by(PermissionTemplate.created_at)
        )
        return list(rows.scalars().all())

    def _members(self, workspace_id: uuid.UUID) -> MemberService:
        return MemberService(self._scoped(workspace_id), lead_ownership=self._lead_ownership)

    async def add_person(
        self,
        workspace_id: uuid.UUID,
        *,
        full_name: str,
        email: str,
        template_id: uuid.UUID,
        manager_id: uuid.UUID | None,
    ) -> Invitation:
        workspace = await self.workspace(workspace_id)
        user, created = await self._user_for(email=email, full_name=full_name)
        service = self._members(workspace_id)
        membership = await service.invite(
            user=user, template_id=template_id, manager_id=manager_id, grant_license=True
        )
        role = await self._raw.get(PermissionTemplate, template_id)
        self._unscope()
        link, expires = await self._link(user, created)
        self.record(
            "person.added",
            f"Added {user.full_name} <{user.email}> to {workspace.name} as "
            f"{role.name if role else 'a member'}",
            workspace_id=workspace_id,
            details={"email": user.email, "role": role.name if role else None},
        )
        await self._raw.flush()
        return Invitation(membership=membership, link=link, expires_at=expires)

    async def update_person(
        self,
        workspace_id: uuid.UUID,
        membership_id: uuid.UUID,
        *,
        template_id: uuid.UUID | None = None,
        manager_id: uuid.UUID | None = None,
        clear_manager: bool = False,
        is_active: bool | None = None,
        reassign_to: uuid.UUID | None = None,
    ) -> Membership:
        workspace = await self.workspace(workspace_id)
        service = self._members(workspace_id)
        membership = await service.get(membership_id)
        await self._raw.refresh(membership, ["user", "template"])
        who = f"{membership.user.full_name} <{membership.user.email}>"
        changes: list[str] = []
        if template_id is not None and template_id != membership.template_id:
            before = membership.template.name
            membership = await service.set_template(membership_id, template_id)
            after = await self._raw.get(PermissionTemplate, template_id)
            changes.append(f"role {before} → {after.name if after else '?'}")
        if clear_manager or (manager_id is not None and manager_id != membership.manager_id):
            membership = await service.set_manager(
                membership_id, None if clear_manager else manager_id
            )
            changes.append("reporting line changed")
        if is_active is not None and is_active != membership.is_active:
            if is_active:
                membership = await service.reactivate(membership_id, changed_by_id=None)
                changes.append("reactivated")
            else:
                membership, moved = await service.deactivate(
                    membership_id, reassign_to_membership_id=reassign_to, changed_by_id=None
                )
                changes.append(
                    "deactivated" + (f", {moved} open leads handed over" if moved else "")
                )
        self._unscope()
        if changes:
            self.record(
                "person.updated",
                f"{who} in {workspace.name}: " + ", ".join(changes),
                workspace_id=workspace_id,
            )
        await self._raw.flush()
        return membership

    async def sign_in_link(self, workspace_id: uuid.UUID, membership_id: uuid.UUID) -> Invitation:
        """A fresh "set your password" link, for someone locked out or never started."""
        workspace = await self.workspace(workspace_id)
        membership = await self._members(workspace_id).get(membership_id)
        self._unscope()
        user = await self._raw.get(User, membership.user_id)
        if user is None:  # pragma: no cover - FK
            raise not_found("Person")
        issued = await CredentialService(self._raw).issue(user=user, purpose=TokenPurpose.RESET)
        self.record(
            "person.sign_in_link",
            f"New sign-in link for {user.email} in {workspace.name}",
            workspace_id=workspace_id,
        )
        await self._raw.flush()
        return Invitation(
            membership=membership, link=self.link_for(issued), expires_at=issued.expires_at
        )

    # --- money -------------------------------------------------------------------------------

    async def wallet(self, workspace_id: uuid.UUID) -> tuple[WalletService, Workspace]:
        workspace = await self.workspace(workspace_id)
        service = WalletService(self._scoped(workspace_id), workspace=workspace)
        return service, workspace

    async def money(
        self,
        workspace_id: uuid.UUID,
        *,
        kind: WalletTransactionKind,
        amount: Decimal,
        note: str | None,
    ) -> WalletTransaction:
        service, workspace = await self.wallet(workspace_id)
        if await service.get() is None:
            await service.create(rate_per_minute=Decimal("0"))
        row = await service.record(kind, amount, note=note, recorded_by=self._actor_email)
        self._unscope()
        words = {
            WalletTransactionKind.TOPUP: "Added",
            WalletTransactionKind.DISCOUNT: "Gave free credit of",
            WalletTransactionKind.ADJUSTMENT: "Corrected by",
        }
        self.record(
            f"wallet.{kind.value.lower()}",
            f"{words.get(kind, kind.value)} {workspace.currency} {_money(row.amount)} for "
            f"{workspace.name}; balance now {_money(row.balance_after)}"
            + (f" — {row.note}" if row.note else ""),
            workspace_id=workspace_id,
            details={"amount": _money(row.amount), "balance_after": _money(row.balance_after)},
        )
        await self._raw.flush()
        return row

    async def set_rate(self, workspace_id: uuid.UUID, rate: Decimal) -> Wallet:
        service, workspace = await self.wallet(workspace_id)
        existing = await service.get()
        if existing is None:
            wallet = await service.create(rate_per_minute=rate)
            before = None
        else:
            before = existing.rate_per_minute
            wallet = await service.set_rate(rate)
        self._unscope()
        self.record(
            "wallet.rate",
            f"Price per AI minute for {workspace.name}: "
            f"{_money(before) if before is not None else 'none'} → "
            f"{_money(wallet.rate_per_minute)}",
            workspace_id=workspace_id,
        )
        await self._raw.flush()
        return wallet

    async def statement(self, workspace_id: uuid.UUID, page: PageParams) -> Any:
        service, _ = await self.wallet(workspace_id)
        if await service.get() is None:
            self._unscope()
            return None
        result = await service.statement(page)
        self._unscope()
        return result

    # --- support access ------------------------------------------------------------------

    async def start_support(self, workspace_id: uuid.UUID) -> Membership:
        """Let the operator into a company as Root, without taking a seat."""
        workspace = await self.workspace(workspace_id)
        if not workspace.is_active:
            raise conflict("company_suspended", "Reactivate the company before going inside it")
        existing = (
            await self._raw.execute(
                select(Membership).where(
                    Membership.workspace_id == workspace_id,
                    Membership.user_id == self._actor_id,
                )
            )
        ).scalar_one_or_none()
        root = (
            await self._raw.execute(
                select(PermissionTemplate).where(
                    PermissionTemplate.workspace_id == workspace_id,
                    PermissionTemplate.name == ROOT_TEMPLATE_NAME,
                    PermissionTemplate.is_readonly.is_(True),
                )
            )
        ).scalar_one()
        if existing is not None and not existing.is_support:
            # Already a real member there (their own company): nothing to grant.
            return existing
        if existing is None:
            existing = Membership(
                workspace_id=workspace_id,
                user_id=self._actor_id,
                template_id=root.id,
                is_support=True,
                has_license=True,
                is_active=True,
                # Never in anyone's queue: no lead is routed to support access.
                availability=AvailabilityStatus.INACTIVE,
            )
            self._raw.add(existing)
        else:
            existing.is_active = True
            existing.has_license = True
            existing.template_id = root.id
            existing.availability = AvailabilityStatus.INACTIVE
        self.record(
            "support.started", f"Went inside {workspace.name} to help", workspace_id=workspace_id
        )
        await self._raw.flush()
        return existing

    async def end_support(self, workspace_id: uuid.UUID) -> None:
        workspace = await self.workspace(workspace_id)
        row = (
            await self._raw.execute(
                select(Membership).where(
                    Membership.workspace_id == workspace_id,
                    Membership.user_id == self._actor_id,
                    Membership.is_support.is_(True),
                )
            )
        ).scalar_one_or_none()
        if row is None or not row.is_active:
            return
        row.is_active = False
        row.has_license = False
        self.record("support.ended", f"Left {workspace.name}", workspace_id=workspace_id)
        await self._raw.flush()

    # --- number requests ---------------------------------------------------------------

    async def pending_numbers(self) -> list[tuple[VoiceNumberRequest, Workspace]]:
        self._unscope()
        rows = await self._raw.execute(
            select(VoiceNumberRequest, Workspace)
            .join(Workspace, Workspace.id == VoiceNumberRequest.workspace_id)
            .where(VoiceNumberRequest.status == "PENDING")
            .order_by(VoiceNumberRequest.created_at)
        )
        return [(request, workspace) for request, workspace in rows.all()]

    async def settle_number(
        self, workspace_id: uuid.UUID, request_id: uuid.UUID, *, phone_number: str | None
    ) -> VoiceNumberRequest:
        workspace = await self.workspace(workspace_id)
        service = NumberRequestService(self._scoped(workspace_id), workspace=workspace)
        if phone_number:
            row = await service.fulfil(request_id, phone_number)
            summary = f"Gave {workspace.name} the number {row.phone_number}"
            action = "number.fulfilled"
        else:
            row = await service.cancel(request_id)
            summary = f"Cancelled a number request for {workspace.name}"
            action = "number.cancelled"
        self._unscope()
        self.record(action, summary, workspace_id=workspace_id)
        await self._raw.flush()
        return row

    # --- history and operators -------------------------------------------------------------

    async def history(
        self, *, workspace_id: uuid.UUID | None, limit: int, offset: int
    ) -> tuple[Sequence[OperatorAction], int]:
        self._unscope()
        base = select(OperatorAction)
        if workspace_id is not None:
            base = base.where(OperatorAction.workspace_id == workspace_id)
        total = (
            await self._raw.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        rows = await self._raw.execute(
            base.order_by(OperatorAction.created_at.desc()).limit(limit).offset(offset)
        )
        return list(rows.scalars().all()), int(total)

    async def operator_accounts(self) -> list[tuple[str, User | None]]:
        self._unscope()
        listed = self._settings.platform_operator_emails
        emails = [e.strip().casefold() for e in listed if e.strip()]
        found = {
            u.email.casefold(): u
            for u in (await self._raw.execute(select(User).where(User.email.in_(emails)))).scalars()
        }
        return [(email, found.get(email)) for email in emails]

    async def operator_link(self, email: str) -> tuple[str, dt.datetime]:
        """A "set your password" link for an operator address, creating the
        account if it does not exist. Only addresses already on the deployment's
        operator list qualify: the console cannot make anyone an operator."""
        address = email.strip().casefold()
        if address not in {e.strip().casefold() for e in self._settings.platform_operator_emails}:
            raise api_error(403, "not_an_operator", "That address is not on the operator list")
        user, _ = await self._user_for(email=address, full_name="Operator")
        issued = await CredentialService(self._raw).issue(user=user, purpose=TokenPurpose.RESET)
        self.record("operator.sign_in_link", f"Sign-in link for operator {address}")
        await self._raw.flush()
        return self.link_for(issued), issued.expires_at
