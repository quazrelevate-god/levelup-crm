"""What the platform operator did, across every company (docs/19).

Not tenant data: the operator acts *on* workspaces from outside them, so this
is a global table like `users`. Every console action that changes a company —
creating it, seats, features, money, suspending, people, support access —
writes one row, so "who gave this company ₹5,000 and why" always has an answer.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.mixins import Base, TimestampMixin

__all__ = ["OperatorAction"]


class OperatorAction(Base, TimestampMixin):
    __tablename__ = "operator_actions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: Kept as text too: the account may later be removed, the record may not.
    actor_email: Mapped[str] = mapped_column(String(320), nullable=False)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    #: `company.created`, `wallet.topup`, `member.added`, `support.started`, …
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    #: One readable line: "Seats 3 → 10".
    summary: Mapped[str] = mapped_column(Text(), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        Index("ix_operator_actions_recent", "created_at"),
        Index("ix_operator_actions_workspace", "workspace_id", "created_at"),
    )
