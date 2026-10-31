"""Member lifecycle: invite, licence, availability, deactivate, reactivate.

Every method takes a `ScopedSession`, so none of them can address a membership
in another workspace — including the reassignment target, which is the one that
would hurt most if it could.

Two rules are enforced here rather than in the router, because both have to hold
whichever path reaches them:

- **A licence is a seat.** Assigning one past `seat_limit` is a 409, not a
  silent overage.
- **Deactivation never orphans a pipeline.** A member holding open leads cannot
  be deactivated without a reassignment target.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.errors import conflict, not_found, unprocessable
from app.models import (
    AvailabilityLog,
    AvailabilityStatus,
    Membership,
    PermissionTemplate,
    User,
    Workspace,
)
from app.services.lead_ownership import LeadOwnership
from app.tenancy.session import ScopedSession

__all__ = ["HierarchyNode", "MemberService"]


@dataclass(frozen=True, slots=True)
class HierarchyNode:
    """One membership and everyone reporting into it."""

    membership: Membership
    reports: list[HierarchyNode]


class MemberService:
    """Member administration within one workspace."""

    def __init__(self, session: ScopedSession, *, lead_ownership: LeadOwnership) -> None:
        self._session = session
        self._leads = lead_ownership

    # --- reads -------------------------------------------------------------

    async def get(self, membership_id: uuid.UUID) -> Membership:
        """Load a membership or raise 404.

        Returns `None` for another workspace's id exactly as it does for a
        nonexistent one, so this raises the same 404 either way.

        `user` and `template` are eager-loaded because every caller renders a
        `MemberDetail`, and a lazy load after the request's commit fails
        outside the async greenlet.
        """
        result = await self._session.execute(
            self._session.select(Membership)
            .where(Membership.id == membership_id)
            .options(selectinload(Membership.user), selectinload(Membership.template))
            .limit(1)
        )
        membership: Membership | None = result.scalar_one_or_none()
        if membership is None:
            raise not_found("Member")
        return membership

    async def list_members(
        self,
        *,
        limit: int,
        offset: int,
        include_inactive: bool = True,
        visible_membership_ids: frozenset[uuid.UUID] | None = None,
    ) -> tuple[Sequence[Membership], int]:
        """List memberships, optionally narrowed to the caller's visibility set.

        `visible_membership_ids` comes from `WorkspaceScope` — the hierarchy
        rule resolved once at the scoping layer. Passing `None` means "no
        hierarchy narrowing", which is what an admin gets.
        """
        statement = self._session.select(Membership).options(
            selectinload(Membership.user),
            selectinload(Membership.template),
        )
        if not include_inactive:
            statement = statement.where(Membership.is_active.is_(True))
        if visible_membership_ids is not None:
            statement = statement.where(Membership.id.in_(visible_membership_ids))

        count_statement = select(func.count()).select_from(statement.subquery())
        total = (await self._session.execute(count_statement)).scalar_one()

        rows = await self._session.execute(
            statement.order_by(Membership.created_at).limit(limit).offset(offset)
        )
        return rows.scalars().all(), int(total)

    async def hierarchy(self) -> list[HierarchyNode]:
        """The manager tree for this workspace.

        Built in memory from a single scoped query — a workspace's member count
        is in the tens, not the millions.
        """
        rows = await self._session.execute(
            self._session.select(Membership)
            .options(selectinload(Membership.user), selectinload(Membership.template))
            .order_by(Membership.created_at)
        )
        memberships = list(rows.scalars().all())

        nodes = {m.id: HierarchyNode(membership=m, reports=[]) for m in memberships}
        roots: list[HierarchyNode] = []
        for membership in memberships:
            node = nodes[membership.id]
            parent = nodes.get(membership.manager_id) if membership.manager_id else None
            if parent is None:
                roots.append(node)
            else:
                parent.reports.append(node)
        return roots

    # --- create / update ---------------------------------------------------

    async def invite(
        self,
        *,
        user: User,
        template_id: uuid.UUID,
        manager_id: uuid.UUID | None = None,
        grant_license: bool = False,
    ) -> Membership:
        template = await self._session.get(PermissionTemplate, template_id)
        if template is None:
            raise not_found("Permission template")

        existing = await self._session.execute(
            self._session.select(Membership).where(Membership.user_id == user.id).limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            raise conflict("member_exists", "This user is already a member of the workspace")

        if manager_id is not None:
            await self.get(manager_id)

        if grant_license:
            await self._assert_seat_available()

        membership = Membership(
            user_id=user.id,
            template_id=template_id,
            manager_id=manager_id,
            has_license=grant_license,
        )
        self._session.add(membership)
        await self._session.flush()
        return membership

    async def set_manager(
        self,
        membership_id: uuid.UUID,
        manager_id: uuid.UUID | None,
    ) -> Membership:
        """Reassign reporting line, refusing cycles.

        A cycle would make the visibility CTE in the scoping layer loop, and a
        member would end up "seeing" their own manager's reports.
        """
        membership = await self.get(membership_id)

        if manager_id is not None:
            if manager_id == membership_id:
                raise unprocessable("invalid_manager", "A member cannot manage themselves")
            await self.get(manager_id)
            if await self._would_create_cycle(membership_id, manager_id):
                raise unprocessable(
                    "manager_cycle",
                    "That manager already reports into this member",
                )

        membership.manager_id = manager_id
        await self._session.flush()
        return membership

    async def set_template(self, membership_id: uuid.UUID, template_id: uuid.UUID) -> Membership:
        membership = await self.get(membership_id)
        template = await self._session.get(PermissionTemplate, template_id)
        if template is None:
            raise not_found("Permission template")
        membership.template_id = template_id
        await self._session.flush()
        return membership

    # --- licensing ---------------------------------------------------------

    async def assign_license(self, membership_id: uuid.UUID) -> Membership:
        membership = await self.get(membership_id)
        if membership.has_license:
            return membership
        await self._assert_seat_available()
        membership.has_license = True
        await self._session.flush()
        return membership

    async def revoke_license(self, membership_id: uuid.UUID) -> Membership:
        membership = await self.get(membership_id)
        membership.has_license = False
        await self._session.flush()
        return membership

    async def seat_usage(self) -> tuple[int, int]:
        """`(used, limit)` for this workspace."""
        used = await self._session.execute(
            select(func.count()).select_from(
                self._session.select(Membership)
                .where(Membership.has_license.is_(True), Membership.is_support.is_(False))
                .subquery()
            )
        )
        workspace = await self._workspace()
        return int(used.scalar_one()), workspace.seat_limit

    async def _assert_seat_available(self) -> None:
        used, limit = await self.seat_usage()
        if used >= limit:
            raise conflict(
                "seat_limit_reached",
                f"All {limit} licensed seats are in use",
                seat_limit=limit,
                seats_used=used,
            )

    # --- availability ------------------------------------------------------

    async def set_availability(
        self,
        membership_id: uuid.UUID,
        *,
        status: AvailabilityStatus,
        note: str | None,
        changed_by_id: uuid.UUID | None,
    ) -> Membership:
        """Change availability and record why.

        The log row is written even when the status is unchanged — a note
        explaining an unchanged status is still information an admin wanted
        recorded.
        """
        membership = await self.get(membership_id)
        membership.availability = status
        self._session.add(
            AvailabilityLog(
                membership_id=membership_id,
                status=status,
                note=note,
                changed_by_id=changed_by_id,
            )
        )
        await self._session.flush()
        return membership

    async def availability_log(
        self,
        membership_id: uuid.UUID,
        *,
        limit: int,
        offset: int,
    ) -> tuple[Sequence[AvailabilityLog], int]:
        # Resolve the membership first so an id from another workspace 404s
        # here rather than returning an empty log, which would confirm nothing
        # but read as "this member has no history".
        await self.get(membership_id)

        statement = self._session.select(AvailabilityLog).where(
            AvailabilityLog.membership_id == membership_id
        )
        total = await self._session.execute(select(func.count()).select_from(statement.subquery()))
        rows = await self._session.execute(
            statement.order_by(AvailabilityLog.changed_at.desc()).limit(limit).offset(offset)
        )
        return rows.scalars().all(), int(total.scalar_one())

    # --- deactivate / reactivate -------------------------------------------

    async def open_lead_count(self, membership_id: uuid.UUID) -> int:
        return await self._leads.count_open_leads(self._session, membership_id)

    async def deactivate(
        self,
        membership_id: uuid.UUID,
        *,
        reassign_to_membership_id: uuid.UUID | None,
        changed_by_id: uuid.UUID | None,
    ) -> tuple[Membership, int]:
        """Deactivate a member, moving their open pipeline first.

        Returns the membership and how many leads were transferred. Refuses with
        `409 reassignment_required` when the member holds open leads and no
        target was named — never orphan a pipeline.
        """
        membership = await self.get(membership_id)
        open_leads = await self.open_lead_count(membership_id)

        transferred = 0
        if open_leads > 0:
            if reassign_to_membership_id is None:
                # Reachable for the first time now that open leads are counted
                # for real, so it is worth reading like a sentence.
                held = "1 open lead" if open_leads == 1 else f"{open_leads} open leads"
                raise conflict(
                    "reassignment_required",
                    f"This member holds {held}. "
                    f"Provide reassign_to_membership_id to transfer them.",
                    open_lead_count=open_leads,
                )
            target = await self.get(reassign_to_membership_id)
            if target.id == membership.id:
                raise unprocessable(
                    "invalid_reassignment_target",
                    "Cannot reassign a member's leads to themselves",
                )
            if not target.is_active:
                raise unprocessable(
                    "invalid_reassignment_target",
                    "Cannot reassign leads to a deactivated member",
                )
            transferred = await self._leads.reassign_open_leads(
                self._session,
                from_membership_id=membership.id,
                to_membership_id=target.id,
                actor_id=changed_by_id,
            )

        # Reports would otherwise dangle under a deactivated manager and drop
        # out of their skip-level's visibility set.
        await self._reparent_reports(membership)

        membership.is_active = False
        membership.has_license = False
        membership.availability = AvailabilityStatus.INACTIVE
        self._session.add(
            AvailabilityLog(
                membership_id=membership.id,
                status=AvailabilityStatus.INACTIVE,
                note="Membership deactivated",
                changed_by_id=changed_by_id,
            )
        )
        await self._session.flush()
        return membership, transferred

    async def reactivate(
        self,
        membership_id: uuid.UUID,
        *,
        changed_by_id: uuid.UUID | None,
    ) -> Membership:
        """Reactivate and re-licence, if a seat is free."""
        membership = await self.get(membership_id)
        if membership.is_active:
            return membership

        await self._assert_seat_available()

        membership.is_active = True
        membership.has_license = True
        membership.availability = AvailabilityStatus.WORKING
        self._session.add(
            AvailabilityLog(
                membership_id=membership.id,
                status=AvailabilityStatus.WORKING,
                note="Membership reactivated",
                changed_by_id=changed_by_id,
            )
        )
        await self._session.flush()
        return membership

    # --- internals ---------------------------------------------------------

    async def _workspace(self) -> Workspace:
        result = await self._session.execute(
            select(Workspace).where(Workspace.id == self._session.workspace_id).limit(1)
        )
        workspace: Workspace = result.scalar_one()
        return workspace

    async def _reparent_reports(self, membership: Membership) -> None:
        """Move a departing manager's reports up to their own manager."""
        rows = await self._session.execute(
            self._session.select(Membership).where(Membership.manager_id == membership.id)
        )
        for report in rows.scalars().all():
            report.manager_id = membership.manager_id

    async def _would_create_cycle(
        self,
        membership_id: uuid.UUID,
        proposed_manager_id: uuid.UUID,
    ) -> bool:
        """True when the proposed manager already reports into this member."""
        cursor: uuid.UUID | None = proposed_manager_id
        seen: set[uuid.UUID] = set()
        while cursor is not None and cursor not in seen:
            if cursor == membership_id:
                return True
            seen.add(cursor)
            row = await self._session.execute(
                self._session.select(Membership).where(Membership.id == cursor).limit(1)
            )
            current = row.scalar_one_or_none()
            cursor = current.manager_id if current else None
        return False
