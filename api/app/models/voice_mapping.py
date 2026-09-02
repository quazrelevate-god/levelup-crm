"""The `voice_extraction_mappings` model.

One row per (workspace, Bolna disposition name), naming which of the
workspace's own lead fields the extracted value should be folded into and at
what confidence threshold.

Kept in its own module rather than added to `app/models/voice.py` because it
belongs to a different lifecycle: `voice.py`'s tables are per-call artefacts
that grow with each Bolna call, this is admin-managed configuration that only
changes when an operator opens a settings page. Splitting the file keeps the
two concerns visibly separate.

The `target_field_key` is a `LeadField.key`, not a foreign key. `key` is
immutable by construction (docs/01-data-model.md §3.1) and fields are hidden,
never deleted (§1.1), so a mapping cannot be silently invalidated by a rename
or a delete. The router validates the key resolves to a live field at
create/update time; the write-back service re-checks at runtime as defence in
depth so an archived field cannot be written through.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Index, Numeric, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.mixins import TenantModel

__all__ = ["VoiceExtractionMapping"]


class VoiceExtractionMapping(TenantModel):
    """One workspace's mapping from a Bolna disposition to a lead field."""

    __tablename__ = "voice_extraction_mappings"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "disposition_name",
            name="voice_extraction_mappings_disposition_uq",
        ),
        CheckConstraint(
            "min_confidence >= 0 AND min_confidence <= 1",
            name="voice_extraction_mappings_confidence_ck",
        ),
        Index(
            "ix_voice_extraction_mappings_enabled",
            "workspace_id",
            postgresql_where=text("is_enabled"),
        ),
    )

    #: The Bolna disposition name, verbatim. Customer's vocabulary — never
    #: normalised, never lowercased, because Bolna's `extracted_data` uses the
    #: exact string the disposition was named with (contract §5).
    disposition_name: Mapped[str] = mapped_column(String(120), nullable=False)

    #: The workspace's own `LeadField.key`. Validated to exist at create/update
    #: time by the router; re-checked at runtime by the write-back service so an
    #: archived field cannot leak through.
    target_field_key: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Below this, an extracted value is not written to the field — a timeline
    #: note is recorded instead so the low-confidence event is visible without
    #: costing operator trust in the field's value. 0.70 mirrors contract §6.1.
    min_confidence: Mapped[Decimal] = mapped_column(
        Numeric(precision=3, scale=2),
        nullable=False,
        default=Decimal("0.70"),
        server_default=text("0.70"),
    )

    #: Disabled mappings are skipped without any timeline record — a disabled
    #: mapping is an operator's explicit "do nothing", not a failure to report.
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
