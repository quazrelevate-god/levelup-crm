"""Auth request and response schemas."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field

__all__ = [
    "ChangePasswordRequest",
    "LoginRequest",
    "LogoutRequest",
    "MeResponse",
    "MembershipSummary",
    "PasswordResetConfirm",
    "PasswordResetRequest",
    "RefreshRequest",
    "TokenResponse",
    "UserSummary",
    "WorkspaceSummary",
]

# Long enough to resist offline cracking of the argon2 hash, short enough that a
# password manager's generated passphrase fits.
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 200


class LoginRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    email: EmailStr
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    refresh_token: str = Field(min_length=1)


class LogoutRequest(RefreshRequest):
    """Logout revokes a specific token's family, so it needs the token."""


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)


class UserSummary(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str
    is_active: bool


class WorkspaceSummary(BaseModel):
    """Enough for the workspace picker; the full record comes from GET /workspaces/{id}."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    timezone: str
    currency: str
    default_country_code: str


class MembershipSummary(BaseModel):
    """A membership as the client needs it to pick a workspace and render nav."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID
    workspace: WorkspaceSummary
    template_id: uuid.UUID
    template_name: str
    is_active: bool
    has_license: bool
    availability: str
    manager_id: uuid.UUID | None
    #: The operator, let in to help (docs/19).
    is_support: bool = False


class TokenResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int
    user: UserSummary
    memberships: list[MembershipSummary]
    #: Sends the operator straight to the console after signing in (docs/19).
    is_platform_operator: bool = False


class MeResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    user: UserSummary
    memberships: list[MembershipSummary]
    #: The platform operator (docs/16): may create workspaces and change seats.
    is_platform_operator: bool = False


class ResolvedPermissions(BaseModel):
    """What `GET /me/permissions?workspace_id=` returns.

    M1 returns capabilities and the resolved visibility set. `field_grants`
    lands in M4 — the key is present and empty now so the frontend can be
    written against its final shape.
    """

    model_config = ConfigDict(frozen=True)

    workspace_id: uuid.UUID
    membership_id: uuid.UUID
    template_id: uuid.UUID
    template_name: str
    capabilities: dict[str, dict[str, bool]]
    visible_membership_ids: list[uuid.UUID]
    sees_all_members: bool
    field_grants: dict[str, list[str]] = Field(default_factory=dict)


class PasswordResetRequest(BaseModel):
    """Ask for a set-password link."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr


class PasswordResetConfirm(BaseModel):
    """Redeem a link and choose a password."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)
