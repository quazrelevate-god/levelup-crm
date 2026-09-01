"""Translate a Tally form response into the CRM's intake shape.

This module does one job: turn `data.fields` — a list of `{label, type,
value}` answers — into the `{"values": {field_key: value}}` map that
`IntakeService.ingest_lead` already understands. It creates nothing, writes
nothing, and knows nothing about leads; the existing intake path does all of
that, unchanged.

**Nothing here knows any customer's vocabulary.** "Video Editing Academy" and
"Course Intrested" are LevelUp Learning's words, and CLAUDE.md is explicit
that they must never appear in product code. So every mapping is resolved at
runtime from the workspace's own `lead_fields` and `field_options` rows. A
different customer with different questions and different courses gets the
same code path and none of this file changes.

The resolution that matters is the choice one, and it has three hops:

    Tally value   "opt_a1b2"                        (Tally's own option id)
      └─ payload's own options[] ──▶ "Video Editing Academy"   (option text)
           └─ workspace FieldOption.label ──▶ video_editing_academy   (code)

Skipping the last hop is not a cosmetic bug. Dropdown values are *stored* as
the option code (`app/fields/registry.py::_norm_dropdown`), and validation
rejects anything not in the field's `option_codes` — so an unresolved id would
422 the entire submission and lose the lead.

**Nothing is ever lost.** An answer that matches no field, or a choice that
resolves to no CRM option, is preserved under a suffixed key and reported as
a warning rather than dropped or rejected. That is the intake rule the whole
path is built on: a rejected payload at 2am is a lost lead.
"""

from __future__ import annotations

import dataclasses
import re
import unicodedata
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.enums import LeadFieldType
from app.models.field import LeadField
from app.models.workspace import Workspace
from app.schemas.tally import TallyField, TallyWebhook
from app.tenancy.session import ScopedSession

__all__ = ["UNRESOLVED_SUFFIX", "TallyTranslator", "TranslationResult", "normalise_label"]

#: Appended to a field key when a choice answer could not be resolved to one of
#: that field's options. The raw text is stored under the suffixed key, which
#: `ValueValidator` treats as an unknown key — accepted, stored as-is, and
#: reported in `warnings`. The lead survives; the typed field stays empty
#: rather than holding something the schema never agreed to.
UNRESOLVED_SUFFIX = "__tally_unresolved"

#: Distinguishes "not looked up yet" from "looked up, and there is none".
_UNSET = object()

#: Shortest field label that may be matched by affix rather than exactly.
#: Below this, a label is too generic to carry a question on its own.
MIN_AFFIX = 3

#: Tally's question type -> the CRM field type it can safely fall back to.
#:
#: Deliberately tiny and type-only. This is the last resort when no label
#: matches, and it exists so a form asking "Phone Number" against a field
#: labelled "Phone" still fills the identity — the failure that made a real
#: submission 422 with `identity_required`. Mapping *types* rather than words
#: is what keeps any customer's vocabulary out of this file.
_TALLY_TYPE_TO_FIELD_TYPE: dict[str, LeadFieldType] = {
    "INPUT_PHONE_NUMBER": LeadFieldType.PHONE,
    "PHONE_NUMBER": LeadFieldType.PHONE,
    "INPUT_EMAIL": LeadFieldType.EMAIL,
    "EMAIL": LeadFieldType.EMAIL,
}

#: Choice questions, where `value` is a list of option ids rather than text.
_LIST_VALUED_TYPES = frozenset(
    {"MULTIPLE_CHOICE", "CHECKBOXES", "DROPDOWN", "MULTI_SELECT", "RANKING"}
)


def normalise_label(text: str) -> str:
    """Fold a label to something two spellings of the same thing agree on.

    `"E-mail ID"`, `"e mail id"` and `"E_Mail_ID"` all become `emailid`. Kept
    deliberately aggressive because the two sides of this match are written by
    different people at different times — one in Tally, one in CRM settings —
    and punctuation drift between them is the common case, not the exception.

    Not the same as `slugify_key`: that one *generates* a stable key and keeps
    separators, this one *compares* two human strings and throws them away.
    """
    ascii_only = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", ascii_only.lower())


def _affix_match(question: str, field_label: str) -> bool:
    """True when one normalised label starts or ends with the other.

    Both directions, because a form may be either more or less verbose than
    the field it means. `MIN_AFFIX` keeps a one- or two-letter field label
    ("ID") from matching most questions on the form.
    """
    if len(field_label) < MIN_AFFIX or len(question) < MIN_AFFIX:
        return False
    if question == field_label:
        return True
    return (
        question.startswith(field_label)
        or question.endswith(field_label)
        or field_label.startswith(question)
        or field_label.endswith(question)
    )


@dataclasses.dataclass(slots=True)
class TranslationResult:
    """The intake payload, plus everything the translation had to give up on."""

    values: dict[str, Any] = dataclasses.field(default_factory=dict)
    warnings: list[str] = dataclasses.field(default_factory=list)
    #: Answers that landed on a real CRM field. Distinct from `len(values)`,
    #: which also counts values parked under an unresolved key.
    mapped: int = 0


class TallyTranslator:
    """Built per request. Reads the workspace's schema, writes nothing."""

    def __init__(self, session: ScopedSession) -> None:
        self._session = session
        self._fields: list[LeadField] | None = None
        # `_UNSET` rather than None: a workspace with no identity field
        # designated is a real answer worth caching, not a cache miss.
        self._identity_id: Any = _UNSET

    async def _load_fields(self) -> list[LeadField]:
        """The workspace's lead fields and their options.

        Scoped by the session's loader criteria, so this cannot see another
        workspace's schema however it is called.
        """
        if self._fields is None:
            rows = await self._session.execute(
                select(LeadField)
                .options(selectinload(LeadField.options))
                .order_by(LeadField.sort_order)
            )
            self._fields = list(rows.scalars().all())
        return self._fields

    async def _field_for(self, label: str, answer: TallyField | None = None) -> LeadField | None:
        """Match a Tally question to a CRM field.

        Four tiers, strongest first, first hit wins. Nothing weaker can ever
        override something exact, which is what keeps the loose tiers from
        turning a near-miss into a wrong answer.

        1. **Exact label** — "Preferred Contact Time" is the field's label.
        2. **Exact key** — the admin named the question after the JSONB key.
        3. **Unique containment** — one normalised label contains the other
           ("Full Name" ⊃ "Name", "Course Intrested" ⊃ "Course"), and *exactly
           one* field matches. Ambiguity is not resolved by guessing: if two
           fields both match, none is returned and the answer is warned about.
        4. **Identity by type** — a phone- or email-typed answer falls back to
           the workspace's field of that type, preferring the designated
           identity field.

        Tier 4 exists because of a real production failure. A form asking
        "Phone Number" against a field labelled "Phone" matched nothing, so the
        identity never mapped, and `create_lead` refused the lead outright with
        `identity_required` — an ordinary field degrading to a warning is
        survivable, the identity degrading is not. It is keyed on *type*, never
        on wording, so no customer's vocabulary enters this decision.
        """
        wanted = normalise_label(label)
        if not wanted:
            return None
        fields = [f for f in await self._load_fields() if not f.is_hidden]
        # Hidden fields are dropped up front: the write filter would refuse them
        # anyway, and a clear warning beats a 422.

        for field in fields:
            if normalise_label(field.label) == wanted:
                return field

        for field in fields:
            if normalise_label(field.key) == wanted:
                return field

        # Tier 3. Affix containment, and only when exactly one field matches.
        #
        # Prefix-or-suffix rather than "appears anywhere": a raw substring test
        # matches `phone` inside `somephonenote` and would file a free-text
        # note against the phone field, which then fails validation and loses
        # the lead — the very failure this tier exists to prevent. Qualifiers
        # in real forms sit at the edges ("Full Name", "Phone Number",
        # "E-mail ID", "Course Intrested"), so the edges are where to look.
        contained = [
            field for field in fields if _affix_match(wanted, normalise_label(field.label))
        ]
        if len(contained) == 1:
            return contained[0]

        # Tier 4. The identity rescue.
        if answer is not None:
            wanted_type = _TALLY_TYPE_TO_FIELD_TYPE.get((answer.type or "").upper())
            if wanted_type is not None:
                typed = [f for f in fields if f.field_type is wanted_type]
                if typed:
                    identity_id = await self._identity_field_id()
                    for field in typed:
                        if identity_id is not None and field.id == identity_id:
                            return field
                    if len(typed) == 1:
                        return typed[0]
        return None

    async def _identity_field_id(self) -> uuid.UUID | None:
        """The workspace's designated identity field, cached for the request.

        Loaded from the `workspaces` row rather than read off the session:
        `ScopedSession` carries a `workspace_id`, not the workspace object, so
        an attribute read would have quietly returned `None` for ever and made
        the identity rescue below a no-op that still looked implemented.
        """
        if self._identity_id is _UNSET:
            # An explicit select, not `ScopedSession.get`: that helper is typed
            # for tenant-scoped models, and `workspaces` is the tenant itself.
            rows = await self._session.execute(
                select(Workspace).where(Workspace.id == self._session.workspace_id).limit(1)
            )
            workspace: Workspace | None = rows.scalar_one_or_none()
            self._identity_id = workspace.identity_field_id if workspace else None
        found: uuid.UUID | None = self._identity_id
        return found

    @staticmethod
    def _option_texts(answer: TallyField) -> list[str]:
        """Resolve a choice answer's option ids to their human text.

        The lookup table travels inside the answer itself, which is the one
        genuinely convenient thing about Tally's shape — no second request, no
        cached form definition that could be stale.

        A value that is already text (some Tally question types send the text
        directly) passes through unchanged, so this is safe to call on both.
        """
        by_id = {opt.id: opt.text for opt in answer.options if opt.id}
        raw = answer.value
        items = raw if isinstance(raw, list) else [raw]
        texts: list[str] = []
        for item in items:
            if item is None:
                continue
            key = str(item)
            resolved = by_id.get(key)
            texts.append(str(resolved if resolved is not None else key))
        return [t for t in texts if t.strip()]

    @staticmethod
    def _code_for(field: LeadField, text: str) -> str | None:
        """Find the workspace option whose label (or code) is this text.

        Archived options are excluded: an admin who retired a course should not
        have a form still filing leads under it.
        """
        wanted = normalise_label(text)
        live = [opt for opt in field.options if not opt.is_archived]
        for option in live:
            if normalise_label(option.label) == wanted:
                return option.code
        for option in live:
            if normalise_label(option.code) == wanted:
                return option.code
        return None

    def _translate_choice(
        self, field: LeadField, answer: TallyField, result: TranslationResult
    ) -> None:
        """A choice answer, resolved to option codes or preserved as text."""
        texts = self._option_texts(answer)
        if not texts:
            return

        codes: list[str] = []
        unresolved: list[str] = []
        for text in texts:
            code = self._code_for(field, text)
            if code is None:
                unresolved.append(text)
            else:
                codes.append(code)

        if unresolved:
            # The fallback that keeps the lead. The raw text is parked under a
            # suffixed key — an unknown key as far as the validator is
            # concerned, so it is stored and reported rather than rejected.
            parked = f"{field.key}{UNRESOLVED_SUFFIX}"
            existing = result.values.get(parked)
            prior = [existing] if isinstance(existing, str) else list(existing or [])
            merged = [*prior, *unresolved]
            result.values[parked] = ", ".join(str(x) for x in merged if x)
            result.warnings.append(
                f"{field.label!r}: no option matching "
                f"{', '.join(repr(u) for u in unresolved)} — kept as text under "
                f"{parked!r}, the field itself was left unchanged"
            )

        if not codes:
            return

        if field.field_type is LeadFieldType.TAGS:
            result.values[field.key] = codes
        else:
            # A scalar field cannot hold two answers. Take the first and say so
            # rather than silently discarding the rest.
            result.values[field.key] = codes[0]
            if len(codes) > 1:
                result.warnings.append(
                    f"{field.label!r} accepts one value; kept the first of {len(codes)} selected"
                )
        result.mapped += 1

    def _translate_scalar(
        self, field: LeadField, answer: TallyField, result: TranslationResult
    ) -> None:
        """Text, email, phone, number, checkbox — whatever Tally sent, as-is.

        No coercion here beyond dropping blanks: `ValueValidator` owns type
        rules, and a second opinion in this module would be a second place for
        them to drift.
        """
        value = answer.value
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value if v not in (None, ""))
        if value is None or (isinstance(value, str) and not value.strip()):
            # An unanswered optional question must never blank a value a human
            # or an earlier submission already put there.
            return
        result.values[field.key] = value
        result.mapped += 1

    async def translate(self, payload: TallyWebhook) -> TranslationResult:
        """Turn one form response into `{"values": {...}}` plus warnings."""
        result = TranslationResult()

        # `fields` is typed, so a non-list body is already a ValidationError at
        # the router's `model_validate_json` — a guard here would be dead code.
        answers = payload.data.fields if payload.data else []

        for answer in answers:
            label = (answer.label or "").strip()
            if not label:
                continue

            field = await self._field_for(label, answer)
            if field is None:
                # Unmatched question. Stored under a slugified key, which the
                # validator accepts as unknown and reports — the lead lands
                # either way, and renaming the question in Tally later upgrades
                # it to a real field with no code change.
                texts = self._option_texts(answer)
                raw: Any = ", ".join(texts) if answer.options else answer.value
                if raw is None or (isinstance(raw, str) and not raw.strip()):
                    continue
                parked = normalise_label(label) or "tally_answer"
                result.values[parked] = raw
                result.warnings.append(
                    f"No CRM field matches the question {label!r} — stored under {parked!r}"
                )
                continue

            if answer.options or (answer.type or "").upper() in _LIST_VALUED_TYPES:
                self._translate_choice(field, answer, result)
            else:
                self._translate_scalar(field, answer, result)

        return result
