"""The one workspace setting the voice automation reads.

`workspace.settings` is an open JSONB bag. Rather than make all of it writable
— which would let any admin put anything anywhere and give the product no way
to validate it — this module owns one namespace inside it, and both the writer
(`settings/voice-outcome-stages`) and the reader (`VoiceCallService`) go
through here. Nothing else in `settings` is touched by either.

No I/O: these are dictionary operations on an already-loaded workspace row, so
they are unit-testable without a database and the caller keeps control of the
transaction.
"""

from __future__ import annotations

import uuid
from typing import Any

__all__ = [
    "OUTCOME_ANSWERED",
    "OUTCOME_UNANSWERED",
    "VOICE_OUTCOME_STAGES",
    "clear_outcome_stages",
    "read_outcome_stages",
    "write_outcome_stages",
]

#: Where a workspace says which stage a finished AI call should move a lead to.
#: `workspace.settings[VOICE_OUTCOME_STAGES] = {"answered": <id>, "unanswered": <id>}`
#:
#: Stage *ids*, configured per workspace, because stage labels are the
#: customer's vocabulary and not the product's. One workspace's "Contacted" is
#: another's "Spoke to" or "Qualified", and a workspace with no equivalent of
#: either stage simply does not get the behaviour — which is what an absent
#: key means. Nothing here is hardcoded and no stage is ever created.
VOICE_OUTCOME_STAGES = "voice_outcome_stages"
OUTCOME_ANSWERED = "answered"
OUTCOME_UNANSWERED = "unanswered"


def read_outcome_stages(settings: Any) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """`(answered, unanswered)` from a workspace's settings, or `(None, None)`.

    Tolerant on purpose. A malformed mapping disables the behaviour rather than
    raising: this is read on the webhook path, and a settings typo must never
    cost a call result. The endpoint is where bad input is refused.
    """
    raw = (settings or {}).get(VOICE_OUTCOME_STAGES) if isinstance(settings, dict) else None
    if not isinstance(raw, dict):
        return None, None

    def one(key: str) -> uuid.UUID | None:
        value = raw.get(key)
        if not value:
            return None
        try:
            return uuid.UUID(str(value))
        except ValueError:
            return None

    return one(OUTCOME_ANSWERED), one(OUTCOME_UNANSWERED)


def write_outcome_stages(
    settings: Any, *, answered: uuid.UUID, unanswered: uuid.UUID
) -> dict[str, Any]:
    """The settings dict with this namespace replaced and everything else kept.

    Returns a new dict rather than mutating in place, so SQLAlchemy sees the
    JSONB column change — an in-place edit of a mutable column value is not
    reliably detected.
    """
    existing = dict(settings) if isinstance(settings, dict) else {}
    existing[VOICE_OUTCOME_STAGES] = {
        OUTCOME_ANSWERED: str(answered),
        OUTCOME_UNANSWERED: str(unanswered),
    }
    return existing


def clear_outcome_stages(settings: Any) -> dict[str, Any]:
    """The settings dict with this namespace removed and everything else kept."""
    existing = dict(settings) if isinstance(settings, dict) else {}
    existing.pop(VOICE_OUTCOME_STAGES, None)
    return existing


#: Leads the AI must never ring, as a filter rule the workspace writes —
#: "fee status is Paid", "stage is Enrolled". A safety net under every call the
#: CRM places, whoever or whatever asked for it. `{}` or absent: no exclusions.
VOICE_NEVER_CALL = "voice_never_call"


def read_never_call(settings: Any) -> dict[str, Any]:
    raw = (settings or {}).get(VOICE_NEVER_CALL) if isinstance(settings, dict) else None
    return dict(raw) if isinstance(raw, dict) else {}


def write_never_call(settings: Any, conditions: dict[str, Any]) -> dict[str, Any]:
    """The settings dict with this namespace replaced and everything else kept."""
    existing = dict(settings) if isinstance(settings, dict) else {}
    if conditions:
        existing[VOICE_NEVER_CALL] = conditions
    else:
        existing.pop(VOICE_NEVER_CALL, None)
    return existing
