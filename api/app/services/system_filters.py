"""Lead rules the workspace configured, checked with the workspace's own authority.

A workflow's "if the lead is in stage X" and the never-AI-call rule are the
workspace's configuration, not a read on behalf of whoever happened to cause
them to run — a payment webhook has no person at all. So they are compiled with
every field visible, exactly as assignment rules and call facts are.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select

from app.errors import api_error
from app.filters.compiler import FilterCompiler
from app.filters.dsl import FilterNode, validate_shape
from app.models.field import LeadField
from app.models.lead import Lead
from app.models.workspace import Workspace
from app.permissions import FieldGrants, FieldProjectionService, FieldWriteFilter
from app.services.leads import LeadService
from app.tenancy.session import ScopedSession

__all__ = ["lead_matches", "parse_rule", "system_grants", "system_lead_service"]

_FILTER_ADAPTER: TypeAdapter[FilterNode] = TypeAdapter(FilterNode)


def parse_rule(conditions: Any, *, code: str = "invalid_rule") -> FilterNode | None:
    """The filter DSL from stored JSON; `None` for an empty rule (every lead)."""
    if not conditions:
        return None
    try:
        node = _FILTER_ADAPTER.validate_python(conditions)
        validate_shape(node)
    except (ValidationError, ValueError) as exc:
        raise api_error(422, code, f"The rule is not a valid filter: {exc}") from exc
    return node


async def _fields(session: ScopedSession) -> list[LeadField]:
    rows = await session.execute(session.select(LeadField).order_by(LeadField.sort_order))
    return list(rows.scalars().all())


def system_grants(fields: list[LeadField]) -> FieldGrants:
    every = frozenset(field.key for field in fields)
    return FieldGrants(view=every, edit=every, import_=every, export=every, is_admin=True)


async def lead_matches(
    session: ScopedSession, workspace: Workspace, node: FilterNode, lead_id: uuid.UUID
) -> bool:
    """Whether one lead meets a rule, as one query over its row."""
    fields = await _fields(session)
    compiler = FilterCompiler(
        fields=fields,
        projection=FieldProjectionService(system_grants(fields)),
        timezone=workspace.timezone,
    )
    row = (
        await session.execute(
            select(compiler.compile(node))
            .select_from(Lead)
            .where(Lead.id == lead_id, Lead.workspace_id == session.workspace_id)
        )
    ).one_or_none()
    return bool(row is not None and row[0])


async def system_lead_service(session: ScopedSession, workspace: Workspace) -> LeadService:
    """A `LeadService` acting for the workspace itself: no actor, every field.

    Writes still go through the validator, the changeset and the timeline
    (rules 4, 5, 5a); only the person is absent, so nothing is attributed to
    someone who did not do it.
    """
    grants = system_grants(await _fields(session))
    return LeadService(
        session,
        workspace=workspace,
        projection=FieldProjectionService(grants),
        write_filter=FieldWriteFilter(grants),
        actor_id=None,
        visible_membership_ids=frozenset(),
        sees_all=True,
    )
