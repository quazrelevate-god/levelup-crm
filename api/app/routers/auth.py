"""Auth endpoints.

Thin: credential checks, the licence and activity gates, and token lifecycle all
live in `app.services.auth`. What this file owns is the HTTP shape — status
codes, the rate-limit header, and the fact that `/auth/*` and `/me/*` sit
outside the workspace prefix.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import (
    CurrentUser,
    get_auth_rate_limiter,
    get_current_user,
    get_password_hasher,
    get_token_service,
)
from app.auth.operator import is_operator_email, is_platform_operator
from app.auth.passwords import PasswordHasherService
from app.auth.rate_limit import AuthRateLimiter, RateLimitExceededError
from app.auth.tokens import TokenService
from app.dependencies import get_session
from app.errors import api_error, not_found
from app.models import Membership, User
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    LogoutRequest,
    MembershipSummary,
    MeResponse,
    PasswordResetConfirm,
    PasswordResetRequest,
    RefreshRequest,
    ResolvedPermissions,
    TokenResponse,
    UserSummary,
)
from app.services.auth import AuthService
from app.services.credentials import CredentialService, IssuedResetToken, TokenPurpose
from app.services.email import Outgoing, build_sender
from app.tenancy.scoping import resolve_visible_membership_ids

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


def _auth_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    tokens: Annotated[TokenService, Depends(get_token_service)],
    hasher: Annotated[PasswordHasherService, Depends(get_password_hasher)],
) -> AuthService:
    return AuthService(session, tokens=tokens, hasher=hasher)


def _membership_summaries(memberships: list[Membership]) -> list[MembershipSummary]:
    return [
        MembershipSummary(
            id=membership.id,
            workspace=membership.workspace,
            template_id=membership.template_id,
            template_name=membership.template.name,
            is_active=membership.is_active,
            has_license=membership.has_license,
            availability=membership.availability.value,
            manager_id=membership.manager_id,
            is_support=membership.is_support,
        )
        for membership in memberships
    ]


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post(
    "/auth/login",
    response_model=TokenResponse,
    summary="Exchange credentials for a token pair",
)
async def login(
    payload: LoginRequest,
    request: Request,
    service: Annotated[AuthService, Depends(_auth_service)],
    limiter: Annotated[AuthRateLimiter, Depends(get_auth_rate_limiter)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TokenResponse:
    email = str(payload.email)
    client_ip = _client_ip(request)

    try:
        await limiter.check_login(email=email, client_ip=client_ip)
    except RateLimitExceededError as exc:
        raise api_error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate_limited",
            "Too many login attempts. Try again shortly.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc

    user = await service.authenticate(email=email, password=payload.password)
    memberships = await service.load_memberships(user)
    # Only reached with valid credentials, so `no_license` and `member_inactive`
    # cannot be used to probe which addresses are real.
    service.assert_can_access_any_workspace(memberships)

    pair = await service.issue_tokens(user)
    await session.commit()
    await limiter.reset_login(email=email, client_ip=client_ip)

    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        token_type=pair.token_type,
        expires_in=pair.expires_in_seconds,
        user=UserSummary.model_validate(user),
        memberships=_membership_summaries(memberships),
        is_platform_operator=is_operator_email(request.app.state.settings, user.email),
    )


@router.post(
    "/auth/refresh",
    response_model=TokenResponse,
    summary="Rotate a refresh token",
)
async def refresh(
    payload: RefreshRequest,
    request: Request,
    service: Annotated[AuthService, Depends(_auth_service)],
    limiter: Annotated[AuthRateLimiter, Depends(get_auth_rate_limiter)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TokenResponse:
    # Checked before the token is looked up, so a client that is over budget
    # cannot use the endpoint to probe which token values exist.
    try:
        await limiter.check_refresh(
            refresh_token=payload.refresh_token,
            client_ip=_client_ip(request),
        )
    except RateLimitExceededError as exc:
        raise api_error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate_limited",
            "Too many refresh attempts. Try again shortly.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc

    user, pair = await service.rotate(payload.refresh_token)
    memberships = await service.load_memberships(user)
    await session.commit()

    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        token_type=pair.token_type,
        expires_in=pair.expires_in_seconds,
        user=UserSummary.model_validate(user),
        memberships=_membership_summaries(memberships),
        is_platform_operator=is_operator_email(request.app.state.settings, user.email),
    )


@router.post(
    "/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the presented refresh token's family",
)
async def logout(
    payload: LogoutRequest,
    service: Annotated[AuthService, Depends(_auth_service)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    # Deliberately does not report whether the token was already invalid:
    # logging out is idempotent and should not be a token-validity oracle.
    await service.logout(payload.refresh_token)
    await session.commit()


@router.post(
    "/auth/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change the caller's password and end every other session",
)
async def change_password(
    payload: ChangePasswordRequest,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    service: Annotated[AuthService, Depends(_auth_service)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    await service.change_password(
        current_user,
        current=payload.current_password,
        replacement=payload.new_password,
    )
    await session.commit()


@router.get("/me", response_model=MeResponse, summary="The caller and their memberships")
async def me(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    service: Annotated[AuthService, Depends(_auth_service)],
) -> MeResponse:
    memberships = await service.load_memberships(current_user)
    return MeResponse(
        user=UserSummary.model_validate(current_user),
        memberships=_membership_summaries(memberships),
        is_platform_operator=is_platform_operator(request.app.state.settings, current_user),
    )


@router.get(
    "/me/permissions",
    response_model=ResolvedPermissions,
    summary="Resolved capabilities for one workspace",
)
async def my_permissions(
    workspace_id: Annotated[uuid.UUID, Query(description="Workspace to resolve against")],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    service: Annotated[AuthService, Depends(_auth_service)],
) -> ResolvedPermissions:
    """What the frontend builds its UI from.

    Not a member of that workspace? 404, same as any other cross-workspace
    read — answering "you have no permissions there" would confirm it exists.
    """
    memberships = await service.load_memberships(current_user)
    membership = next((m for m in memberships if m.workspace_id == workspace_id), None)
    if membership is None or not membership.is_active:
        raise not_found("Workspace")

    visible = await resolve_visible_membership_ids(
        session,
        workspace_id=workspace_id,
        membership_id=membership.id,
    )
    capabilities = {
        group: {name: bool(value) for name, value in flags.items()}
        for group, flags in membership.template.capabilities.items()
        if isinstance(flags, dict)
    }
    sees_all = any(flags.get("admin_access") for flags in capabilities.values())

    return ResolvedPermissions(
        workspace_id=workspace_id,
        membership_id=membership.id,
        template_id=membership.template_id,
        template_name=membership.template.name,
        capabilities=capabilities,
        visible_membership_ids=sorted(visible),
        sees_all_members=sees_all,
    )


async def send_credential_email(
    request: Request,
    *,
    user: User,
    issued: IssuedResetToken,
) -> None:
    """Mail the link. Best effort — never fails the caller.

    With no SMTP host configured the sender records instead of delivering, so
    local runs and tests can assert on it without anything reaching a real
    address. See `services/email.py`.
    """
    settings = request.app.state.settings
    sender = getattr(request.app.state, "email_sender", None) or build_sender(settings)

    invited = issued.purpose == TokenPurpose.INVITE
    link = f"{settings.app_base_url.rstrip('/')}/set-password?token={issued.token}"
    hours = int((issued.expires_at - dt.datetime.now(dt.UTC)).total_seconds() // 3600)

    try:
        await sender.send(
            Outgoing(
                to=(user.email,),
                subject=(
                    "You have been invited to a workspace" if invited else "Set a new password"
                ),
                body=(
                    f"Hello {user.full_name},\n\n"
                    + (
                        "Somebody has invited you to a workspace. "
                        if invited
                        else "Somebody asked to reset the password for this address. "
                    )
                    + f"Use this link to choose a password:\n\n{link}\n\n"
                    f"It works once, and expires in about {max(hours, 1)} hours.\n\n"
                    "If you were not expecting this, you can ignore it — "
                    "nothing changes until the link is used.\n"
                ),
            )
        )
    except Exception:
        logger.exception("credential_email.failed", extra={"user": str(user.id)})


@router.post(
    "/auth/password-reset/request",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ask for a set-password link",
)
async def request_password_reset(
    payload: PasswordResetRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """Always answers the same, whether or not the address exists.

    Anything else turns this into a membership oracle: a prober works through a
    list of addresses and learns which ones have accounts. For a B2B product
    "does this company use this tool" is itself commercially interesting, so the
    answer is identical either way and the mail — or its absence — is the only
    difference.
    """
    service = CredentialService(session)
    user = await service.find_user(str(payload.email))

    if user is not None and user.is_active:
        issued = await service.issue(user=user, purpose=TokenPurpose.RESET)
        await session.commit()
        await send_credential_email(request, user=user, issued=issued)

    return {
        "status": "accepted",
        "message": "If that address has an account, a link is on its way.",
    }


@router.post(
    "/auth/password-reset/confirm",
    status_code=status.HTTP_200_OK,
    summary="Redeem a link and set a password",
)
async def confirm_password_reset(
    payload: PasswordResetConfirm,
    hasher: Annotated[PasswordHasherService, Depends(get_password_hasher)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """Spend the token, set the password, and end every existing session.

    Somebody resetting a password is very often somebody who believes their
    account is compromised; leaving their other sessions live would make the
    reset theatre.
    """
    service = CredentialService(session)
    await service.redeem(token=payload.token, password_hash=hasher.hash(payload.new_password))
    await session.commit()
    return {"status": "ok", "message": "Password set. Sign in with it."}
