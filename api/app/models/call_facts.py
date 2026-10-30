"""Call facts: reference details the voice agent is told, chosen by rule (docs/17).

Some things an agent must say are not on the lead at all — a batch's start
date, who mentors it, the fee for that intake. They belong to the business,
change on the business's schedule, and differ by lead: a lead for one batch
must hear that batch's dates. A call fact set is one such group of facts plus
the rule saying which leads it applies to, written in the same filter language
as saved filters and assignment rules. On every call the first active set whose
rule matches the lead, in priority order, supplies its facts to the agent.

Configuration, not product: every label and value here is the workspace's own.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.mixins import TenantModel

__all__ = ["CallFactSet"]


class CallFactSet(TenantModel):
    __tablename__ = "call_fact_sets"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Lowest number is checked first; the first match wins.
    priority: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default=text("0")
    )
    #: The filter DSL. `{}` matches every lead — a default set.
    conditions: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False, server_default="{}")
    #: Ordered `[{"key", "label", "value"}]`. `key` is what the agent's script
    #: can use (`{{batch_start_date}}`), derived from the label.
    facts: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'[]'::jsonb")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=True, server_default=text("true")
    )

    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="call_fact_sets_name_uq"),
        Index("ix_call_fact_sets_order", "workspace_id", "priority", "id"),
    )
