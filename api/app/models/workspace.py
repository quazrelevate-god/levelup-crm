"""Workspaces, permission templates, memberships, availability history.

`Workspace` *is* the tenant, so it carries no `workspace_id`. Everything else
here inherits `TenantModel` and therefore does — including `AvailabilityLog`,
which could have reached its workspace through `membership_id` but would then
sit outside the blanket scoping criteria in `app.tenancy.session`. Architecture
rule 1 is "every table that holds customer data has `workspace_id`", with no
exception for tables that could technically join their way there.

Permission templates are workspace-authored: there is no "Admin" role shared
across tenants, only an Admin template each workspace owns and can rewrite.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.enums import AvailabilityStatus
from app.models.mixins import Base, TenantModel, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User

availability_status_enum = Enum(
    AvailabilityStatus,
    name="availability_status",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
)


class Workspace(Base, TimestampMixin):
    """One customer tenant.

    The preference columns are the localisation seam: phone normalisation reads
    `default_country_code`, timestamps render in `timezone`, and MONEY fields
    render in `currency`. A US customer must work without a code change, so
    nothing downstream may assume these values.
    """

    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(CITEXT(), nullable=False, unique=True)

    default_country_code: Mapped[str] = mapped_column(
        String(5), nullable=False, default="91", server_default="91"
    )
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Asia/Kolkata", server_default="Asia/Kolkata"
    )
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="INR", server_default="INR"
    )
    connected_call_min_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    session_timeout_minutes: Mapped[int | None] = mapped_column(Integer)

    leaderboard_metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=lambda: {"stage": True, "rating": False},
        server_default=text('\'{"stage": true, "rating": false}\'::jsonb'),
    )
    features: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    # Declared now so the schema is stable; populated in M2 when `lead_fields`
    # exists. No FK yet — the target table does not exist, and adding the
    # constraint in M2 is a cleaner migration than dropping a dangling one.
    identity_field_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    primary_field_1_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    primary_field_2_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    seat_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default=text("3")
    )
    #: The operator's numbers this workspace's AI calls go out from, in
    #: rotation order (docs/16). Empty: Bolna's default number.
    voice_calling_numbers: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    memberships: Mapped[list[Membership]] = relationship(back_populates="workspace")
    permission_templates: Mapped[list[PermissionTemplate]] = relationship(
        back_populates="workspace",
    )

    __table_args__ = (
        CheckConstraint("seat_limit >= 0", name="seat_limit_non_negative"),
        CheckConstraint("length(currency) = 3", name="currency_iso_length"),
    )


class PermissionTemplate(TenantModel):
    """A named permission set assigned to memberships.

    M1 owns the shell. The per-field View/Edit/Import/Export matrix
    (`template_field_grants`) lands in M4 once `lead_fields` exists, and
    `capabilities` gets its validating Pydantic model there — a JSONB blob
    nobody validates becomes a JSONB blob nobody understands.

    `is_system` marks the five templates created at provisioning; `is_readonly`
    marks Root, whose editor is view-only.
    """

    __tablename__ = "permission_templates"

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    is_readonly: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    capabilities: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )

    workspace: Mapped[Workspace] = relationship(back_populates="permission_templates")
    memberships: Mapped[list[Membership]] = relationship(back_populates="template")

    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_permission_templates_ws_name"),
    )


class Membership(TenantModel):
    """A user's presence in one workspace.

    Permissions attach here, not to the user. `manager_id` is a self-FK for the
    reporting hierarchy; the visibility rule it drives — a manager sees their
    reports' leads — is resolved once in `app.tenancy.scoping`, never per
    endpoint.

    `has_license` gates login. `availability` gates assignment.
    """

    __tablename__ = "memberships"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("permission_templates.id"),
        nullable=False,
    )
    manager_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("memberships.id", ondelete="SET NULL"),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    has_license: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: The platform operator, let in to help (docs/19). Holds no seat the
    #: client paid for, and is labelled as support wherever members are listed.
    is_support: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    availability: Mapped[AvailabilityStatus] = mapped_column(
        availability_status_enum,
        nullable=False,
        default=AvailabilityStatus.WORKING,
        server_default=AvailabilityStatus.WORKING.value,
    )

    workspace: Mapped[Workspace] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="memberships")
    template: Mapped[PermissionTemplate] = relationship(back_populates="memberships")
    manager: Mapped[Membership | None] = relationship(
        remote_side=lambda: [Membership.id],
        foreign_keys=lambda: [Membership.manager_id],
        back_populates="reports",
    )
    reports: Mapped[list[Membership]] = relationship(
        foreign_keys=lambda: [Membership.manager_id],
        back_populates="manager",
    )

    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_memberships_ws_user"),
        Index("ix_memberships_ws_active", "workspace_id", "is_active"),
        Index("ix_memberships_ws_manager", "workspace_id", "manager_id"),
    )


class AvailabilityLog(TenantModel):
    """One row per availability change, so M8 can prove why a member was
    skipped by an assignment rule on a given day."""

    __tablename__ = "availability_log"

    membership_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("memberships.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[AvailabilityStatus] = mapped_column(availability_status_enum, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    changed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("memberships.id", ondelete="SET NULL"),
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    membership: Mapped[Membership] = relationship(foreign_keys=[membership_id])

    __table_args__ = (Index("ix_availability_log_membership_time", "membership_id", "changed_at"),)
