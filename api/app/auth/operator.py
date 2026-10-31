"""The platform operator: whoever runs this deployment and sells it (docs/16).

A client's own Root or Admin runs their workspace. They do not decide how many
seats they have bought, which paid modules are switched on, or whether a new
workspace exists — those are the operator's, because they are what is sold.

The operator is named by email in `PLATFORM_OPERATOR_EMAILS`, a deployment
setting. Nothing inside the app can grant it: there is no role, flag or
endpoint that turns a member into the operator, so no client admin can
promote themselves.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.auth.deps import CurrentUser, get_current_user
from app.config import Settings
from app.errors import forbidden

__all__ = ["is_operator_email", "is_platform_operator", "require_platform_operator"]


def is_operator_email(settings: Settings, email: str) -> bool:
    allowed = {address.strip().lower() for address in settings.platform_operator_emails}
    return email.strip().lower() in allowed


def is_platform_operator(settings: Settings, user: CurrentUser) -> bool:
    return is_operator_email(settings, user.email)


async def require_platform_operator(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    if not is_platform_operator(request.app.state.settings, current_user):
        raise forbidden(
            "operator_only",
            "Only the platform operator can do this. Contact your account manager.",
        )
    return current_user
