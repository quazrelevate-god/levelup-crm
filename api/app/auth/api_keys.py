"""API-key authentication for the intake path (M10).

A key is a machine credential with the blast radius of a password: it can create
leads in a customer's workspace and, through its permission template, read
whatever that template grants. So it is generated with `secrets`, stored as an
Argon2 hash, and shown exactly once.

**The prefix is the lookup key.** Hashing is deliberately slow, so verifying a
presented key against every row in the table would be a denial-of-service
primitive — an attacker with one bad key could pin a CPU per request. The
plaintext prefix narrows it to (almost always) one candidate before any hashing
happens, and it is short enough to be useless on its own.

**A wrong key still pays for one hash.** Returning early on "no such prefix"
would leak, by timing, which prefixes exist. The dummy verify keeps the two
paths comparable, exactly as the login path does.
"""

from __future__ import annotations

import datetime as dt
import secrets
import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.passwords import PasswordHasherService
from app.dependencies import get_session
from app.errors import api_error
from app.models import ApiKey, PermissionTemplate, Workspace
from app.tenancy.session import ScopedSession

__all__ = [
    "KEY_BYTES",
    "PREFIX_LENGTH",
    "ApiKeyScope",
    "generate_key",
    "require_api_key",
    "resolve_api_key",
]

#: 32 bytes of urlsafe randomness. Long enough that guessing is not a strategy.
KEY_BYTES = 32
PREFIX_LENGTH = 12
_PREFIX_TAG = "crmk_"


def generate_key() -> tuple[str, str]:
    """A new key and its prefix. The plaintext is returned once and never kept."""
    secret = f"{_PREFIX_TAG}{secrets.token_urlsafe(KEY_BYTES)}"
    return secret, secret[:PREFIX_LENGTH]


@dataclass(slots=True)
class ApiKeyScope:
    """What a key-authenticated handler gets instead of a `WorkspaceScope`.

    Deliberately not a `WorkspaceScope`: a key has no membership, no manager, no
    availability and no visibility hierarchy, and inventing a fake membership to
    satisfy a type would put a non-person into the org chart. What it does have
    is a workspace and a permission template, which is all the intake path
    needs.
    """

    workspace: Workspace
    api_key: ApiKey
    template: PermissionTemplate
    session: ScopedSession

    @property
    def workspace_id(self) -> uuid.UUID:
        return self.workspace.id

    @property
    def is_admin(self) -> bool:
        return bool((self.template.capabilities or {}).get("leads", {}).get("admin_access"))


async def resolve_api_key(
    request: Request,
    session: AsyncSession,
    presented: str | None,
) -> ApiKeyScope:
    """Verify a presented key and resolve it to a workspace and template.

    Extracted from `require_api_key` so a key can be verified whichever way it
    arrived. The header is the right channel and stays the default; the one
    caller that cannot use it is the Bolna completion webhook, because Bolna's
    agent configuration accepts a bare `webhook_url` and nothing else — no
    custom headers, no signing secret (`.claude/skills/setup-webhook/SKILL.md`).

    Every failure is the same 401 with the same message. Distinguishing "no such
    key" from "revoked" would tell a prober which of their guesses was once
    real.
    """
    hasher = getattr(request.app.state, "password_hasher", None)
    if hasher is None:  # pragma: no cover - wired in create_app
        hasher = PasswordHasherService(request.app.state.settings)

    def refuse() -> None:
        raise api_error(401, "invalid_api_key", "That API key is not valid")

    if not presented or not presented.startswith(_PREFIX_TAG):
        hasher.dummy_verify()
        refuse()

    assert presented is not None
    x_api_key = presented
    rows = await session.execute(
        select(ApiKey).where(
            ApiKey.prefix == x_api_key[:PREFIX_LENGTH], ApiKey.revoked_at.is_(None)
        )
    )
    candidates = list(rows.scalars().all())
    if not candidates:
        # Pay the same cost as a real verification so timing says nothing.
        hasher.dummy_verify()
        refuse()

    matched: ApiKey | None = None
    for candidate in candidates:
        if hasher.verify(candidate.hashed_key, x_api_key):
            matched = candidate
            break
    if matched is None:
        refuse()
    assert matched is not None

    workspace = await session.get(Workspace, matched.workspace_id)
    template = await session.get(PermissionTemplate, matched.permission_template_id)
    if workspace is None or template is None:  # pragma: no cover - FKs guarantee
        refuse()
    assert workspace is not None and template is not None

    # Best-effort: this is a write on every read, so it is not worth failing a
    # lead over. The commit belongs to whatever the handler does next.
    matched.last_used_at = dt.datetime.now(dt.UTC)

    return ApiKeyScope(
        workspace=workspace,
        api_key=matched,
        template=template,
        session=ScopedSession(session, workspace.id),
    )


async def require_api_key(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> ApiKeyScope:
    """Resolve `X-API-Key` into a workspace and a permission template.

    Unchanged behaviour: this is still the only way the intake API and the
    header-authenticated voice endpoint accept a key. It is now a thin wrapper
    so that exactly one implementation verifies keys.
    """
    return await resolve_api_key(request, session, x_api_key)
