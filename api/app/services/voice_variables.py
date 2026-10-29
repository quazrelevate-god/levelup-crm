"""The variables a voice agent's prompt can use, and what each one holds (docs/15).

Bolna substitutes `{name}`-style placeholders in an agent's prompt from the
`user_data` the CRM sends with every call. Two kinds reach it:

- **Workspace fields** — every lead field the caller may view, under its key:
  `{name}`, `{program}`, `{age}`. Sent on every call, empty when the lead has
  no value, so the prompt never sees a raw `{age}` it might try to fill in.
- **Reserved `crm_*` variables** — what the CRM knows about the relationship:
  the last call's summary, how many calls there have been, why this call is
  being made. Defined here once, so the Settings page and the agent's script
  can never disagree about their names.

This module only describes. What is actually sent is decided by
`VoiceCallService.build_user_data`, and `test_voice_variables.py` checks the
two agree.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable

from app.models.field import LeadField
from app.services.voice_followup import OPENING_KEY, PURPOSE_KEY

__all__ = ["RESERVED_VARIABLES", "VoiceVariable", "variable_catalogue"]


@dataclasses.dataclass(frozen=True, slots=True)
class VoiceVariable:
    #: Exactly what goes in the prompt, without braces: `program`.
    name: str
    #: `field` for a workspace field, `crm` for a reserved variable.
    kind: str
    #: The field's label, or a short title for a reserved variable.
    label: str
    description: str
    #: Always sent, or only in some calls (e.g. follow-ups).
    always_sent: bool
    #: The lead field's type, for `field` variables.
    field_type: str | None = None


RESERVED_VARIABLES: tuple[VoiceVariable, ...] = (
    VoiceVariable(
        OPENING_KEY,
        "crm",
        "Opening line",
        'What the call opens with. Always "Hello?": nobody is named until the caller has '
        "confirmed who they are.",
        True,
    ),
    VoiceVariable(
        "crm_call_count",
        "crm",
        "Calls before this one",
        "How many AI calls this lead has had before. 0 on a first call.",
        True,
    ),
    VoiceVariable(
        "crm_is_repeat_caller",
        "crm",
        "Spoken to before",
        '"yes" if there has been an earlier call, otherwise "no".',
        True,
    ),
    VoiceVariable(
        "crm_call_direction",
        "crm",
        "Who called whom",
        '"outbound" when the CRM placed the call, "inbound" when the lead rang one of your '
        "numbers and the caller lookup found them.",
        True,
    ),
    VoiceVariable(
        "crm_last_call_summary",
        "crm",
        "Last call's summary",
        "What happened on the previous call. Absent on a first call.",
        False,
    ),
    VoiceVariable(
        "crm_last_call_at",
        "crm",
        "When the last call was",
        "Date and time of the previous call. Absent on a first call.",
        False,
    ),
    VoiceVariable(
        PURPOSE_KEY,
        "crm",
        "Why we are calling",
        '"followup" or "followup_missed" on a follow-up call. Absent on a first call.',
        False,
    ),
    VoiceVariable(
        "crm_stage",
        "crm",
        "Current stage",
        "The lead's stage, in your own stage names (e.g. Fee Link Sent).",
        False,
    ),
    VoiceVariable(
        "crm_owner",
        "crm",
        "Assigned to",
        "The team member the lead is assigned to, when there is one.",
        False,
    ),
    VoiceVariable(
        "crm_recent_notes",
        "crm",
        "Recent notes",
        'The latest few notes from the lead\'s timeline, joined by " | ".',
        False,
    ),
    VoiceVariable(
        "crm_lead_context",
        "crm",
        "What the record says",
        "A readable summary of the lead's record, including which facts are not recorded.",
        False,
    ),
    VoiceVariable(
        "crm_program_name",
        "crm",
        "Programme (from the knowledge base)",
        "The programme's published title, when the deployment has a knowledge base and the "
        "lead's programme matches one exactly.",
        False,
    ),
    VoiceVariable(
        "crm_program_context",
        "crm",
        "Programme facts (from the knowledge base)",
        "A short summary of that programme from the knowledge base. Same condition as above.",
        False,
    ),
    VoiceVariable(
        "crm_lead_id",
        "crm",
        "CRM lead id",
        "The lead's id in this CRM. For matching results, not for speaking.",
        True,
    ),
    VoiceVariable(
        "crm_workspace_id",
        "crm",
        "CRM workspace id",
        "Which workspace placed the call. For matching results, not for speaking.",
        True,
    ),
    VoiceVariable(
        "crm_idempotency",
        "crm",
        "Call reference",
        "A unique id for this call attempt. For matching results, not for speaking.",
        True,
    ),
)


def variable_catalogue(
    fields: Iterable[LeadField], visible_keys: Iterable[str]
) -> list[VoiceVariable]:
    """The workspace's own fields the caller may see, then the reserved ones."""
    visible = set(visible_keys)
    own = [
        VoiceVariable(
            name=field.key,
            kind="field",
            label=field.label,
            description=field.description or "",
            always_sent=True,
            field_type=field.field_type.value,
        )
        for field in fields
        if field.key in visible
    ]
    return [*own, *RESERVED_VARIABLES]
