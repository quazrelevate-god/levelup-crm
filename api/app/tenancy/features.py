"""Per-workspace feature flags (M3).

`docs/03-configuration-model.md` §5 and CLAUDE.md's known traps:

> **Feature flags must gate endpoints, not just navigation.** A disabled
> feature returns 403, it doesn't merely hide a menu item.

Hiding a menu item is a cosmetic change that any client can undo with a URL. If
`campaign` is off for a workspace, `POST /campaigns` must refuse — otherwise the
flag is decoration and the workspace is paying for a boundary that does not
exist.

Used as a dependency factory:

    @router.get("/campaigns", dependencies=[Depends(require_feature("campaign"))])

The flag set is declared here because it is a *product* concept — the list of
modules this product can switch off. What each workspace turns on is a row.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends

from app.errors import forbidden
from app.tenancy.scoping import WorkspaceScope, require_workspace

__all__ = ["FEATURE_FLAGS", "feature_enabled", "require_feature"]

#: The modules a workspace can switch off (§5, "Features"). Observed live.
#: A flag absent from a workspace's `features` map is **off** — a workspace
#: does not silently gain a module because the product shipped one.
FEATURE_FLAGS: frozenset[str] = frozenset(
    {
        "automations",
        "location_check_in",
        "campaign",
        "custom_actions",
        "sales_group",
        "lead_recapture",
        "new_leads_view",
        "system_fields",
    }
)

#: Flags a newly provisioned workspace has on. Kept deliberately small: a
#: customer opts into a module, rather than opting out of six.
DEFAULT_ENABLED: frozenset[str] = frozenset({"automations", "campaign", "custom_actions"})


def feature_enabled(scope: WorkspaceScope, flag: str) -> bool:
    """Whether one flag is on for this workspace.

    Unknown flags are always off. A typo in a `require_feature("campain")` call
    therefore closes the endpoint rather than opening it — the safe direction to
    fail.
    """
    if flag not in FEATURE_FLAGS:
        return False
    features = scope.workspace.features or {}
    return bool(features.get(flag, False))


def require_feature(
    flag: str,
) -> Callable[[WorkspaceScope], Coroutine[Any, Any, WorkspaceScope]]:
    """Dependency that refuses with `403 feature_disabled` when the flag is off.

    Returns the scope so a handler can depend on this *instead of*
    `require_workspace` rather than in addition to it.
    """
    if flag not in FEATURE_FLAGS:  # pragma: no cover — a programming error
        raise ValueError(
            f"Unknown feature flag {flag!r}. Add it to FEATURE_FLAGS if the product "
            f"genuinely gained a switchable module."
        )

    async def dependency(
        scope: Annotated[WorkspaceScope, Depends(require_workspace)],
    ) -> WorkspaceScope:
        if not feature_enabled(scope, flag):
            raise forbidden(
                "feature_disabled",
                f"The {flag.replace('_', ' ')} feature is not enabled for this workspace",
                feature=flag,
            )
        return scope

    return dependency
