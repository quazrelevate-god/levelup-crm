"""Tally's form-response webhook shape.

Permissive on purpose, exactly like `VoiceExecutionWebhook`: Tally sends a
dozen fields that vary by form and adds more over time, and rejecting an
unknown key would turn a vendor release into a lost lead. Only the parts the
translator actually reads are named; everything else is kept and ignored.

Two things about Tally's shape drive the whole translator:

**Answers arrive as a list, not a map.** `data.fields` is an array of
`{label, type, value}` objects — there is no key the CRM would recognise, so
the label is the only thing to match on.

**Choice answers are option IDs, not text.** A `MULTIPLE_CHOICE` answer's
`value` is `["opt_a1b2"]`, and the human-readable text lives in that same
field's `options` array. Anything that skipped the lookup would store an
opaque Tally id in a CRM field.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["TallyField", "TallyOption", "TallyWebhook", "TallyWebhookData"]


class TallyOption(BaseModel):
    """One selectable choice, as Tally describes it inside an answer."""

    model_config = ConfigDict(extra="allow")

    id: str | None = None
    text: str | None = None


class TallyField(BaseModel):
    """One answer on a submitted form."""

    model_config = ConfigDict(extra="allow")

    #: Tally's own question id (`question_abc123`). Not useful for matching —
    #: it is generated, not chosen — but kept so a warning can name the
    #: question that could not be mapped.
    key: str | None = None
    #: The question text as the admin wrote it. The only thing worth matching
    #: a CRM field on.
    label: str | None = None
    #: `INPUT_TEXT`, `INPUT_EMAIL`, `MULTIPLE_CHOICE`, `CHECKBOX`, …
    type: str | None = None
    #: A scalar for text/email/number/checkbox answers; a list of option ids
    #: for choice answers.
    value: Any = None
    #: Present on choice questions. The id → text lookup table for `value`.
    options: list[TallyOption] = Field(default_factory=list)


class TallyWebhookData(BaseModel):
    model_config = ConfigDict(extra="allow")

    responseId: str | None = None  # noqa: N815 — Tally's spelling, not ours.
    submissionId: str | None = None  # noqa: N815
    formId: str | None = None  # noqa: N815
    formName: str | None = None  # noqa: N815
    fields: list[TallyField] = Field(default_factory=list)


class TallyWebhook(BaseModel):
    """The envelope Tally posts."""

    model_config = ConfigDict(extra="allow")

    eventId: str | None = None  # noqa: N815
    #: `FORM_RESPONSE` is the only type this endpoint acts on. Anything else is
    #: acknowledged and ignored — a 200, because a non-2xx would make Tally
    #: retry an event the CRM will never want.
    eventType: str | None = None  # noqa: N815
    data: TallyWebhookData = Field(default_factory=TallyWebhookData)


class TallyIntakeResponse(BaseModel):
    """What the CRM answers Tally with.

    Always a 2xx for a delivery that was understood, including one that mapped
    nothing. The body says what happened; the status code says "stop retrying".
    """

    model_config = ConfigDict(frozen=True)

    #: `CREATED`, `UPDATED`, `SKIPPED`, `REJECTED`, or `IGNORED` for a
    #: non-form-response event.
    outcome: str
    lead_id: str | None = None
    #: Everything the submission got away with: an unmatched question, a choice
    #: that resolved to no CRM option, a field stored under an unknown key.
    warnings: list[str] = Field(default_factory=list)
    #: How many answers were mapped onto real CRM fields.
    mapped: int = 0
