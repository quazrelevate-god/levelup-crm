"""Where connected apps post (docs/18): `POST /api/v1/hooks/{provider}/{token}`.

The token in the path finds the workspace; the app's own signature proves the
sender. Both are needed — the token alone would let anyone who saw the URL
mark a lead paid.

**Answers are chosen for the sender's retry logic.** Razorpay retries anything
that is not a 2xx and eventually disables the webhook, so a delivery the CRM
understood but could not use — no matching lead, a non-payment event, a
duplicate — is a 200 with the outcome in the body and in the event log. Only an
unknown URL (404) and a bad signature (401) are refused.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_session
from app.errors import api_error
from app.models.automation import AppConnection
from app.models.workspace import Workspace
from app.services.apps import RazorpayReceiver
from app.tenancy.session import ScopedSession

router = APIRouter(tags=["hooks"])

#: Comfortably above any Razorpay event; refuses a body meant to exhaust memory.
MAX_BODY_BYTES = 256_000


class HookResult(BaseModel):
    outcome: str


@router.post(
    "/hooks/razorpay/{token}",
    response_model=HookResult,
    summary="Razorpay webhook (signed; the URL comes from the Razorpay app screen)",
)
async def razorpay_hook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    token: Annotated[str, Path(min_length=20, max_length=64)],
) -> HookResult:
    def gone() -> Exception:
        return api_error(404, "not_found", "Not found")

    connection = (
        await session.execute(
            select(AppConnection).where(
                AppConnection.hook_token == token, AppConnection.provider == "razorpay"
            )
        )
    ).scalar_one_or_none()
    if connection is None or not connection.is_active:
        raise gone()
    workspace = await session.get(Workspace, connection.workspace_id)
    if workspace is None or not workspace.is_active:
        raise gone()
    if not (workspace.features or {}).get("automations"):
        raise gone()

    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise api_error(413, "too_large", "That body is too large")

    receipt = await RazorpayReceiver(
        ScopedSession(session, workspace.id), workspace=workspace, connection=connection
    ).receive(body, request.headers.get("x-razorpay-signature"))
    outcome = receipt.event.outcome
    # The rejection is committed too, so a wrong secret shows on the app screen.
    await session.commit()
    if not receipt.accepted:
        raise api_error(401, "invalid_signature", "The signature did not verify")
    return HookResult(outcome=outcome)
