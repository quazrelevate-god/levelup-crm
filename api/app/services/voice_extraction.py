"""Voice extraction write-back — Bolna's `extracted_data` → lead fields.

This module is the whole feature. It is called from *inside* the terminal
branch of `VoiceCallService.handle_execution` (see `services/voice_calls.py`),
in the same `AUTOMATION` changeset that records the call summary and the call
log — so a completed Bolna call still opens exactly one changeset, and every
field write it produces shares that id (rules 5, 5a).

The interesting behaviour is what it *does not* do, and why:

- **A disabled mapping is silent.** No note, no timeline entry. An operator
  who disables a mapping is explicitly telling the system "do nothing here" —
  logging that as a per-call event would fill timelines with non-events.
- **A missing / disabled / archived target field is a note, not a write.** The
  extracted value is not lost; it is on the timeline, visible to whoever asks.
- **A confidence below threshold is a note, not a write.** This is the whole
  point of the confidence gate (contract §6.2). A probabilistic model quietly
  overwriting a figure a human typed is the failure mode that would cost
  operator trust; a visible note is the price of not paying that cost.
- **An empty extracted value never writes.** Same rule that intake follows
  (`events/intake.py`): a partial payload must not blank a field the human or
  a previous call already filled in.
- **An extraction that matches the current value is a no-op.** `_apply_update`
  already skips no-op writes via its FieldDelta comparison; this module just
  hands it the value and lets that mechanism do its work. The timeline stays
  quiet in this case, which is the right answer: nothing changed.
- **The identity field is never written.** Rewriting a lead's identity through
  an extraction would either violate `leads_identity_uq` or, worse, silently
  reassign the lead — neither is what a voice-call outcome should ever do.
- **The disposition configured as `BOLNA_SUMMARY_DISPOSITION` is refused.** A
  mapping that named the summary disposition would put the whole call recap
  paragraph into a lead field. The refusal runs at both create-time (the
  router) and here at write-time, because a deployment can change the setting
  after a mapping was already saved.

The service reads its own mappings but never writes them; the CRUD lives in
`routers/voice_mappings.py`.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select

from app.models.field import LeadField
from app.models.lead import Lead
from app.models.voice_mapping import VoiceExtractionMapping
from app.models.workspace import Workspace
from app.schemas.voice import VoiceExecutionWebhook
from app.services.actions import ActionWriter
from app.services.leads import LeadService
from app.services.voice_postcall import flatten_extractions
from app.tenancy.session import ScopedSession

__all__ = ["ExtractionOutcome", "VoiceExtractionService", "summary_disposition_conflict"]


@dataclasses.dataclass(slots=True)
class ExtractionOutcome:
    """What one execution's extraction pass ended up doing."""

    #: Mapped disposition names whose extracted value was written to a field.
    written: list[str] = dataclasses.field(default_factory=list)
    #: Disposition names whose value was recorded on the timeline only —
    #: low confidence, invalid, missing field, empty value.
    noted: list[str] = dataclasses.field(default_factory=list)
    #: Disposition names for which no enabled mapping exists.
    unmapped: list[str] = dataclasses.field(default_factory=list)
    #: Disposition names whose payload entry was malformed and defensively
    #: skipped — not a note, because we cannot say what the value was.
    skipped: list[str] = dataclasses.field(default_factory=list)


def summary_disposition_conflict(
    disposition_name: str, *, summary_disposition: str | None
) -> bool:
    """True when `disposition_name` is the configured summary disposition.

    Case-insensitive because Bolna's dashboards do not enforce a single casing
    across the disposition name and the environment variable, and a mismatch
    of "Call Recap" vs "call recap" would defeat the guard for exactly the
    reason it exists.
    """
    if not summary_disposition:
        return False
    return disposition_name.strip().casefold() == summary_disposition.strip().casefold()


class VoiceExtractionService:
    """Fold `extracted_data` back onto the lead, one mapping at a time.

    Built per request, like every other service. The `LeadService` it composes
    is the caller's — API-key-scoped in production, session-scoped in tests —
    so the same `FieldWriteFilter` that governs every other lead write governs
    this one. A key that cannot Edit a mapped field is refused *by name*,
    which is what makes the machine path safe.
    """

    def __init__(
        self,
        session: ScopedSession,
        *,
        workspace: Workspace,
        leads: LeadService,
        actor_id: Any = None,
        summary_disposition: str | None = None,
    ) -> None:
        self._session = session
        self._workspace = workspace
        self._leads = leads
        self._actor_id = actor_id
        self._summary_disposition = summary_disposition

    async def _load_mappings(self) -> dict[str, VoiceExtractionMapping]:
        """Enabled mappings for this workspace, keyed by disposition name.

        Case-preserved: Bolna emits the exact string the disposition was named
        with (contract §5), so the lookup uses the same string.
        """
        rows = await self._session.execute(
            select(VoiceExtractionMapping).where(VoiceExtractionMapping.is_enabled.is_(True))
        )
        found: dict[str, VoiceExtractionMapping] = {}
        for mapping in rows.scalars().all():
            found[mapping.disposition_name] = mapping
        return found

    async def _load_field(self, key: str) -> LeadField | None:
        rows = await self._session.execute(
            select(LeadField).where(LeadField.key == key).limit(1)
        )
        result: LeadField | None = rows.scalar_one_or_none()
        return result

    @staticmethod
    def _extract(entry: Any) -> tuple[Any, Decimal | None, bool]:
        """Return `(value, confidence, invalid)` from one `extracted_data` entry.

        Handles the shapes the codebase already sees:

        - `{"value": ..., "confidence": 0.94}` — the documented shape
        - `{"value": ..., "confidence": 0.94, "validation": {"is_valid": true}}`
        - `{"value": ..., "validation": {"is_valid": false}}` → `invalid=True`
        - A bare scalar (string, number, bool) — treated as value with no
          confidence; the caller decides how to handle "no confidence."
        - Anything else → `(None, None, False)` and the caller skips.

        Never raises. A malformed entry becomes "nothing to write", not a 500.
        """
        if isinstance(entry, dict):
            value = entry.get("value")
            raw_confidence = entry.get("confidence")
            confidence: Decimal | None = None
            if raw_confidence is not None:
                try:
                    confidence = Decimal(str(raw_confidence))
                except (InvalidOperation, TypeError, ValueError):
                    confidence = None
            validation = entry.get("validation")
            invalid = False
            if isinstance(validation, dict) and validation.get("is_valid") is False:
                invalid = True
            return value, confidence, invalid
        if isinstance(entry, (str, int, float, bool)):
            return entry, None, False
        return None, None, False

    @staticmethod
    def _is_empty(value: Any) -> bool:
        """Empty extracted values are the ones we refuse to overwrite with."""
        if value is None:
            return True
        return isinstance(value, str) and value.strip() == ""

    async def _note(self, writer: ActionWriter, lead: Lead, *, body: str) -> None:
        """A timeline note in the *already-open* changeset.

        Never opens a new one — the caller owns the changeset, and every note
        this service writes must share the same id as the summary and the call
        log so an undo folds them together (rule 5a).
        """
        writer.record_note(lead, body=body)

    async def apply(
        self,
        payload: VoiceExecutionWebhook,
        lead: Lead,
        writer: ActionWriter,
    ) -> ExtractionOutcome:
        """Apply every applicable mapping to `payload.extracted_data`.

        Assumes an `AUTOMATION` changeset is already open on `writer` — the
        webhook handler opens it once for the whole delivery.

        The identity key is computed once and consulted per mapping: refusing
        to write to it is a hard rule, not a per-mapping toggle.
        """
        outcome = ExtractionOutcome()
        extracted = payload.extracted_data or {}
        if not isinstance(extracted, dict) or not extracted:
            return outcome

        mappings = await self._load_mappings()
        identity_key = await self._leads.identity_key()

        # Flatten first. Bolna groups its extractions — `General` holds
        # `Call Summary` — so iterating the top level would match mappings
        # against the *group* name and never against the extraction itself.
        for item in flatten_extractions(extracted):
            disposition = item.path
            entry = item.raw

            # A mapping may be written against the extraction's own name
            # (`Call Summary`) or its full path (`General / Call Summary`).
            # Both are unambiguous; neither invents a mapping that an operator
            # did not create.
            mapping = mappings.get(item.name) or mappings.get(item.path)

            # An extraction with no enabled mapping is not an error — an
            # operator need not map everything Bolna can produce. It is
            # reported here, and the Call Details page shows it regardless:
            # unmapped means "not written to a field", never "discarded".
            if mapping is None:
                outcome.unmapped.append(disposition)
                continue

            # Defence in depth for the summary-disposition guard. The router
            # refuses to create a mapping whose name matches the configured
            # summary disposition, but a deployment can change the setting
            # after mappings already exist. Refusing here as well keeps the
            # mapping table's history intact while still not writing.
            if summary_disposition_conflict(
                item.name, summary_disposition=self._summary_disposition
            ) or summary_disposition_conflict(
                item.path, summary_disposition=self._summary_disposition
            ):
                await self._note(
                    writer,
                    lead,
                    body=(
                        f"Voice extraction '{disposition}' skipped: this disposition is "
                        "configured as the call summary and must not be mapped to a "
                        "lead field."
                    ),
                )
                outcome.noted.append(disposition)
                continue

            # Never rewrite the identity — it would either fail the unique
            # index or, worse, silently reassign the lead.
            if mapping.target_field_key == identity_key:
                await self._note(
                    writer,
                    lead,
                    body=(
                        f"Voice extraction '{disposition}' skipped: the target "
                        f"field ({identity_key!r}) is the lead's identity field and "
                        "cannot be rewritten by an extraction."
                    ),
                )
                outcome.noted.append(disposition)
                continue

            value, confidence, invalid = self._extract(entry)
            # A leaf that spells its value some other way — Bolna's own
            # `{"subjective": …}` — still has one. The normaliser already
            # found it; `_extract` only knows the `value` key, so defer to it
            # rather than treating a present value as an empty one.
            if value is None and isinstance(item.value, (str, int, float, bool)):
                value = item.value

            if invalid:
                await self._note(
                    writer,
                    lead,
                    body=(
                        f"Voice extraction '{disposition}' skipped: Bolna reported "
                        "the extracted value failed its own validation."
                    ),
                )
                outcome.noted.append(disposition)
                continue

            if self._is_empty(value):
                # No note, no write. An empty extraction is not a signal — it
                # is Bolna reporting the agent did not learn anything about
                # this field. A note here would fill timelines with silence.
                outcome.skipped.append(disposition)
                continue

            if confidence is None:
                await self._note(
                    writer,
                    lead,
                    body=(
                        f"Voice extraction '{disposition}' skipped: no confidence "
                        f"reported (threshold {float(mapping.min_confidence):.2f})."
                    ),
                )
                outcome.noted.append(disposition)
                continue

            if confidence < mapping.min_confidence:
                await self._note(
                    writer,
                    lead,
                    body=(
                        f"Voice extraction '{disposition}' below threshold: "
                        f"confidence {float(confidence):.2f} < "
                        f"{float(mapping.min_confidence):.2f}. Value not written."
                    ),
                )
                outcome.noted.append(disposition)
                continue

            # Target field must exist, not be hidden, and grant Edit to the
            # caller. The last check is the FieldWriteFilter inside
            # _apply_update; the first two are here so a hidden or renamed-
            # away field produces a clear timeline note rather than a 422 that
            # sinks the whole webhook delivery.
            field = await self._load_field(mapping.target_field_key)
            if field is None or field.is_hidden:
                await self._note(
                    writer,
                    lead,
                    body=(
                        f"Voice extraction '{disposition}' skipped: target field "
                        f"{mapping.target_field_key!r} is not available in this workspace."
                    ),
                )
                outcome.noted.append(disposition)
                continue

            # Existing value comparison. `_apply_update`'s FieldDelta pass will
            # skip the write anyway if `old == new`, but doing the check here
            # too means we can produce a clearer timeline note when the value
            # is unchanged — the delta pass is silent in that case.
            existing = (lead.values or {}).get(field.key)
            if existing is not None and existing != "" and existing == value:
                # Same value: do nothing, don't note. This is the "if same, do
                # nothing" branch of the requested flow.
                continue

            # Route the write through LeadService, which:
            #   1. runs the FieldWriteFilter (rule 4),
            #   2. runs the field-type validator (invalid types → 422),
            #   3. records a FIELD_CHANGE action with old/new on the timeline,
            #   4. skips no-op writes via its FieldDelta comparison.
            # A write that trips (1) or (2) raises 422 — a rogue mapping should
            # not silently corrupt a call. That 422 bubbles up to
            # handle_execution, which is inside the delivery's transaction, so
            # the changeset is rolled back cleanly. In practice the write
            # filter is not tripped: extraction runs against the same
            # permission template the caller established.
            await self._leads._apply_update(
                lead,
                writer,
                values={field.key: value},
            )
            # The change note itself — the audit line the requirements list
            # under (10). `FIELD_CHANGE` already carries old and new; this
            # note is the human-readable "why", and carries the confidence.
            await self._note(
                writer,
                lead,
                body=(
                    f"Voice extraction updated {field.label} from Bolna extraction "
                    f"{disposition!r}. Confidence: {float(confidence):.2f}."
                ),
            )
            outcome.written.append(disposition)

        return outcome
