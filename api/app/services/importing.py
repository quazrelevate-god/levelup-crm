"""Importing a spreadsheet (M7).

`04-feature-coverage.md` found four distinct flows hiding behind "upload an
Excel", and treating them as one is what produced a mapping screen that could
express none of them properly. They are separate `ImportJobKind`s here:

| Kind | What it is |
|---|---|
| `LEAD_IMPORT` | Create or update, keyed on the workspace's identity field |
| `LEAD_UPDATE` | Update only. A row whose identity matches nothing *fails*
  rather than quietly creating a lead — the entire difference, and the reason
  a separate kind exists |
| `ACTION_IMPORT` | Historical timeline migration. Every customer switching CRMs needs it |
| `EXPORT` | The other direction, sharing the job record |

**Distribution and owner assignment are options on a run, not flows of their
own.** "Excel Advance Distribution" and "Owner Specific Assignment" are both
answers to "who gets these leads", so they are one setting with several
strategies rather than two more import types.

Three rules the whole module is built around:

1. **Mapping is limited to fields the caller has `Import` on *and* that carry
   `show_in_import`.** Both, not either. `FieldWriteFilter.check_import` is the
   gate and it refuses rather than dropping.
2. **The dry run and the commit read the same file through the same code.** A
   preview that came from a different path than the write is a preview of
   something else.
3. **A whole run is one changeset**, so a bad import is one undo.

**Where a lead was first received** (`created_at_column`). A lead migrated from
another CRM was not received today: its enquiry date is in the old system's
export. Left alone, every imported lead would be dated the moment of the
import, and the voice agent — told "Enquiry received: <created_at>" — would
say someone who applied in August had just enquired. The run can therefore
name a column that sets `created_at` on the leads it *creates*. Existing leads
are never re-dated: an update describes the same person, whose history the
CRM already holds. The date format is chosen, never guessed (`03/04/2026` is
3 April in India and 4 March in the US), and a date without a timezone is read
in the workspace's own (architecture rule 10).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from collections.abc import Mapping, Sequence
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import selectinload

from app.errors import conflict, not_found, unprocessable
from app.fields.search import SEARCH_CONFIG, search_text_for
from app.fields.values import FieldValidationError, ValueValidator
from app.models.enums import (
    AvailabilityStatus,
    ChangesetSource,
    ImportJobKind,
    ImportJobStatus,
    StageKind,
    SystemActionKind,
)
from app.models.field import CustomActionType, FieldOption, LeadField
from app.models.lead import Lead
from app.models.pipeline import CallDisposition, Stage
from app.models.work import ImportJob
from app.models.workspace import Membership, Workspace
from app.permissions.projection import FieldWriteFilter
from app.services.actions import ActionWriter, FieldDelta
from app.services.sheets import Sheet, read_sheet
from app.tenancy.session import ScopedSession

__all__ = [
    "CREATED_AT_FORMATS",
    "DistributionStrategy",
    "ImportService",
    "RowOutcome",
    "parse_created_at",
]

#: Rows reported back individually. Beyond this the operator needs the summary
#: counts, not five thousand messages — and a response carrying all of them
#: would be unreadable and enormous.
MAX_REPORTED_ROWS = 200

#: `created_at_format` values, and the patterns each accepts after ISO 8601.
#: ISO is always accepted: it cannot be misread.
CREATED_AT_FORMATS: dict[str, tuple[str, ...]] = {
    "iso": (),
    "day_first": (
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y",
    ),
    "month_first": (
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y",
        "%m-%d-%Y %H:%M:%S",
        "%m-%d-%Y",
    ),
}


def parse_created_at(raw: str, fmt: str, timezone: str) -> dt.datetime:
    """A received-on timestamp from a sheet, as an aware datetime.

    Raises `ValueError` with an operator-readable reason. A value without a
    timezone is the workspace's local time.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("no date given")
    parsed: dt.datetime | None = None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for pattern in CREATED_AT_FORMATS.get(fmt, ()):
            try:
                parsed = dt.datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
    if parsed is None:
        shape = {"day_first": "DD/MM/YYYY HH:MM:SS", "month_first": "MM/DD/YYYY HH:MM:SS"}
        raise ValueError(f"{text!r} is not a date in {shape.get(fmt, 'YYYY-MM-DD HH:MM:SS')} form")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
    if parsed > dt.datetime.now(dt.UTC):
        raise ValueError(f"{text!r} is in the future")
    return parsed


class DistributionStrategy:
    """How an imported batch is shared out.

    `04-feature-coverage.md` lists these as separate features; they are one
    decision with several answers, which is why they live together.
    """

    #: Leave `assignee_id` unset. M8's assignment rules will pick them up.
    NONE = "NONE"
    #: Even spread across the named members, in order.
    ROUND_ROBIN = "ROUND_ROBIN"
    #: Round-robin, but each member's share is proportional to their weight.
    WEIGHTED = "WEIGHTED"
    #: Round-robin over only those whose availability is WORKING.
    AVAILABILITY = "AVAILABILITY"
    #: Read the owner from a column in the sheet — "Owner Specific Assignment".
    COLUMN = "COLUMN"

    ALL = (NONE, ROUND_ROBIN, WEIGHTED, AVAILABILITY, COLUMN)


@dataclasses.dataclass(frozen=True, slots=True)
class RowOutcome:
    """What happened, or would happen, to one row."""

    #: 1-based and counting the header, so it matches what the operator sees in
    #: Excel's row gutter. Off-by-one here wastes a support call per import.
    row_number: int
    status: str
    identity: str | None = None
    message: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "row_number": self.row_number,
            "status": self.status,
            "identity": self.identity,
            "message": self.message,
        }


class ImportService:
    """Upload, map, preview, commit."""

    def __init__(
        self,
        session: ScopedSession,
        *,
        workspace: Workspace,
        write_filter: FieldWriteFilter,
        actor_id: uuid.UUID | None,
        storage: Any = None,
        bucket: str | None = None,
    ) -> None:
        self._session = session
        self._workspace = workspace
        self._write_filter = write_filter
        self._actor_id = actor_id
        self._storage = storage
        self._bucket = bucket
        self._fields: list[LeadField] = []
        self._validator: ValueValidator | None = None

    # --- schema -------------------------------------------------------------

    async def _schema(self) -> ValueValidator:
        if self._validator is None:
            rows = await self._session.execute(
                self._session.select(LeadField).order_by(LeadField.sort_order)
            )
            self._fields = list(rows.scalars().all())

            options: dict[uuid.UUID, list[FieldOption]] = {}
            option_rows = await self._session.execute(self._session.select(FieldOption))
            for option in option_rows.scalars().all():
                options.setdefault(option.field_id, []).append(option)

            self._validator = ValueValidator(
                self._fields,
                default_country_code=self._workspace.default_country_code,
                currency=self._workspace.currency,
                timezone=self._workspace.timezone,
                options_by_field=options,
            )
        return self._validator

    async def importable_fields(self) -> list[LeadField]:
        """What the mapping UI may offer.

        Both conditions, not either: `show_in_import` is the admin's decision
        that a field belongs in a sheet at all, and the Import grant is this
        caller's permission to write it. A field failing either is simply not
        offered, so the UI and the API agree on what can be mapped.
        """
        await self._schema()
        return [
            field
            for field in self._fields
            if field.show_in_import and self._write_filter.can_import(field.key)
        ]

    async def _identity_key(self) -> str:
        await self._schema()
        identity = next(
            (f for f in self._fields if f.id == self._workspace.identity_field_id), None
        )
        if identity is None:  # pragma: no cover - provisioning always sets one
            raise conflict("no_identity_field", "This workspace has no identity field set")
        return identity.key

    # --- job lifecycle ------------------------------------------------------

    async def get_job(self, job_id: uuid.UUID) -> ImportJob:
        job = await self._session.get(ImportJob, job_id)
        if job is None:
            raise not_found("Import job")
        return job

    async def create_job(
        self, *, kind: ImportJobKind, filename: str, content: bytes
    ) -> tuple[ImportJob, Sheet]:
        """Parse the header row and keep the file for the steps that follow.

        The sheet is re-read at preview and at commit rather than cached in the
        job row: a 20,000-row file is megabytes of JSONB nobody would ever query,
        and re-reading guarantees the preview and the write saw the same bytes.
        """
        sheet = read_sheet(content, filename=filename)

        job = ImportJob(
            kind=kind,
            status=ImportJobStatus.UPLOADED,
            filename=filename,
            source_columns=list(sheet.columns),
            row_count=sheet.row_count,
            created_by_id=self._actor_id,
        )
        self._session.add(job)
        await self._session.flush()

        job.storage_key = f"imports/{self._workspace.id}/{job.id}/{filename}"
        if self._storage is not None and self._bucket:
            self._storage.put_object(Bucket=self._bucket, Key=job.storage_key, Body=content)
        await self._session.flush()
        return job, sheet

    async def _load_sheet(self, job: ImportJob) -> Sheet:
        if self._storage is None or not self._bucket or not job.storage_key:
            raise conflict(
                "upload_unavailable",
                "The uploaded file is no longer available. Upload it again.",
            )
        try:
            response = self._storage.get_object(Bucket=self._bucket, Key=job.storage_key)
            content = response["Body"].read()
        except Exception as exc:
            raise conflict(
                "upload_unavailable",
                "The uploaded file could not be read back. Upload it again.",
            ) from exc
        return read_sheet(content, filename=job.filename)

    async def set_mapping(
        self,
        job_id: uuid.UUID,
        *,
        mapping: Mapping[str, str],
        options: Mapping[str, Any] | None = None,
    ) -> ImportJob:
        """Store the operator's column choices, after checking they may make them."""
        job = await self.get_job(job_id)
        if job.status in {ImportJobStatus.RUNNING, ImportJobStatus.COMPLETED}:
            raise conflict("import_already_run", "That import has already been committed")

        unknown = sorted(set(mapping) - set(job.source_columns))
        if unknown:
            raise unprocessable(
                "unknown_column",
                "The mapping names columns that are not in the file",
                columns=unknown,
            )

        await self._schema()
        if job.kind in {ImportJobKind.LEAD_IMPORT, ImportJobKind.LEAD_UPDATE}:
            self._check_lead_mapping(job, mapping)
            self._check_created_at_options(job, options or {})

        job.mapping = dict(mapping)
        job.options = dict(options or {})
        job.status = ImportJobStatus.MAPPED
        await self._session.flush()
        return job

    def _check_lead_mapping(self, job: ImportJob, mapping: Mapping[str, str]) -> None:
        known = {field.key for field in self._fields}
        targets = [target for target in mapping.values() if target in known]

        # The Import grant, which is independent of Edit. Refuses by name.
        self._write_filter.check_import(targets, known_keys=frozenset(known))

        hidden = sorted(
            {
                target
                for target in targets
                if not next(f.show_in_import for f in self._fields if f.key == target)
            }
        )
        if hidden:
            raise unprocessable(
                "field_not_importable",
                "Some mapped fields are not available for import in this workspace",
                fields=hidden,
            )

    @staticmethod
    def _check_created_at_options(job: ImportJob, options: Mapping[str, Any]) -> None:
        column = str(options.get("created_at_column") or "")
        fmt = str(options.get("created_at_format") or "iso")
        if fmt not in CREATED_AT_FORMATS:
            raise unprocessable(
                "invalid_created_at_format",
                f"created_at_format must be one of: {', '.join(CREATED_AT_FORMATS)}",
            )
        if column and column not in job.source_columns:
            raise unprocessable(
                "unknown_column",
                "The created-on column is not in the file",
                columns=[column],
            )

    # --- the run ------------------------------------------------------------

    async def preview(self, job_id: uuid.UUID) -> ImportJob:
        """A dry run: what a commit would create and update, and what would fail.

        `04-feature-coverage.md` calls this the answer to "Pitfalls of Excel
        Upload", and it is the single most useful thing in the whole flow —
        an operator who can see "180 create, 4 update, 6 errors" before
        committing does not have to undo afterwards.
        """
        job = await self.get_job(job_id)
        if not job.mapping:
            raise conflict("mapping_required", "Choose how the columns map before previewing")

        sheet = await self._load_sheet(job)
        outcomes = await self._evaluate(job, sheet, commit=False)

        job.result = self._summarise(outcomes)
        job.status = ImportJobStatus.PREVIEWED
        await self._session.flush()
        return job

    async def commit(self, job_id: uuid.UUID) -> ImportJob:
        """Apply the run as one changeset."""
        job = await self.get_job(job_id)
        if job.status is ImportJobStatus.COMPLETED:
            raise conflict("import_already_run", "That import has already been committed")
        if not job.mapping:
            raise conflict("mapping_required", "Choose how the columns map before committing")

        sheet = await self._load_sheet(job)
        job.status = ImportJobStatus.RUNNING
        await self._session.flush()

        try:
            outcomes = await self._evaluate(job, sheet, commit=True)
        except Exception as exc:
            job.status = ImportJobStatus.FAILED
            job.error = str(exc)[:500]
            raise

        job.result = self._summarise(outcomes)
        job.status = ImportJobStatus.COMPLETED
        await self._session.flush()
        return job

    def _summarise(self, outcomes: Sequence[RowOutcome]) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for outcome in outcomes:
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
        failed = [o for o in outcomes if o.status == "error"]
        return {
            "counts": counts,
            "total": len(outcomes),
            # Errors first and in full up to the cap: they are the rows the
            # operator has to act on, and a truncated list says so explicitly
            # rather than pretending it is complete.
            "errors": [o.as_payload() for o in failed[:MAX_REPORTED_ROWS]],
            "errors_truncated": len(failed) > MAX_REPORTED_ROWS,
        }

    async def _evaluate(self, job: ImportJob, sheet: Sheet, *, commit: bool) -> list[RowOutcome]:
        """The one code path both the dry run and the write go through."""
        if job.kind is ImportJobKind.ACTION_IMPORT:
            return await self._evaluate_actions(job, sheet, commit=commit)
        return await self._evaluate_leads(job, sheet, commit=commit)

    # --- leads --------------------------------------------------------------

    async def _distribution_targets(self, job: ImportJob) -> list[uuid.UUID]:
        """Who a round-robin or weighted run will hand leads to."""
        strategy = str(job.options.get("strategy", DistributionStrategy.NONE))
        if strategy in {DistributionStrategy.NONE, DistributionStrategy.COLUMN}:
            return []

        wanted = [uuid.UUID(str(i)) for i in job.options.get("membership_ids", [])]
        rows = await self._session.execute(self._session.select(Membership))
        members = {m.id: m for m in rows.scalars().all() if m.is_active}

        chosen = [mid for mid in wanted if mid in members] or list(members)
        if strategy == DistributionStrategy.AVAILABILITY:
            # M1 already models availability with a full log; the engine simply
            # skips anyone not WORKING rather than queueing for them.
            chosen = [
                mid for mid in chosen if members[mid].availability is AvailabilityStatus.WORKING
            ]
        if strategy == DistributionStrategy.WEIGHTED:
            weights = {
                uuid.UUID(str(k)): max(1, int(v))
                for k, v in (job.options.get("weights") or {}).items()
            }
            expanded: list[uuid.UUID] = []
            for mid in chosen:
                expanded.extend([mid] * weights.get(mid, 1))
            chosen = expanded

        if not chosen:
            raise unprocessable(
                "no_distribution_targets",
                "No active members are available to receive these leads",
            )
        return chosen

    async def _evaluate_leads(
        self, job: ImportJob, sheet: Sheet, *, commit: bool
    ) -> list[RowOutcome]:
        validator = await self._schema()
        identity_key = await self._identity_key()
        known = frozenset(field.key for field in self._fields)
        by_key = {field.key: field for field in self._fields}

        targets = await self._distribution_targets(job)
        owner_column = str(job.options.get("owner_column") or "")
        created_column = str(job.options.get("created_at_column") or "")
        created_format = str(job.options.get("created_at_format") or "iso")
        strategy = str(job.options.get("strategy", DistributionStrategy.NONE))
        members_by_email = (
            await self._members_by_email() if strategy == DistributionStrategy.COLUMN else {}
        )

        writer: ActionWriter | None = None
        if commit:
            writer = ActionWriter(self._session, actor_id=self._actor_id)
            await writer.open_changeset(
                source=ChangesetSource.IMPORT,
                summary=f"Imported {sheet.row_count} rows from {job.filename}",
                lead_count=sheet.row_count,
            )
            job.changeset_id = writer.changeset.id

        existing = await self._existing_by_identity()
        outcomes: list[RowOutcome] = []
        cursor = 0
        # Identifiers this file has already created, by row. A second row with
        # the same one would create a second lead with the same identifier,
        # which the unique index refuses at commit — after the preview said all
        # was well. It is a row error instead, naming the row it repeats.
        created_here: dict[str, int] = {}

        for index, row in enumerate(sheet.rows):
            row_number = index + 2  # header is row 1
            raw: dict[str, Any] = {}
            for column, target in job.mapping.items():
                if target in known:
                    raw[target] = row.get(column, "")

            identity = str(raw.get(identity_key) or "").strip()
            if not identity:
                outcomes.append(
                    RowOutcome(
                        row_number, "error", None, f"No value for {by_key[identity_key].label}"
                    )
                )
                continue

            try:
                validated = validator.validate(dict(raw), is_create=True)
            except FieldValidationError as exc:
                first = next(iter(exc.errors.items()), ("", "invalid"))
                outcomes.append(
                    RowOutcome(row_number, "error", identity, f"{first[0]}: {first[1]}")
                )
                continue

            normalised = str(validated.values.get(identity_key) or identity)
            match = existing.get(normalised)

            if match is None and job.kind is ImportJobKind.LEAD_UPDATE:
                # The whole reason LEAD_UPDATE is a separate kind: in an update
                # run, an identity that matches nothing is a mistake in the
                # sheet, not an instruction to create somebody.
                outcomes.append(
                    RowOutcome(
                        row_number, "error", normalised, "No existing lead with that identifier"
                    )
                )
                continue

            assignee = self._resolve_owner(
                strategy, row, owner_column, members_by_email, targets, cursor
            )
            if strategy in {
                DistributionStrategy.ROUND_ROBIN,
                DistributionStrategy.WEIGHTED,
                DistributionStrategy.AVAILABILITY,
            }:
                cursor += 1

            created_at: dt.datetime | None = None
            if match is None and created_column:
                try:
                    created_at = parse_created_at(
                        row.get(created_column, ""), created_format, self._workspace.timezone
                    )
                except ValueError as exc:
                    outcomes.append(
                        RowOutcome(row_number, "error", normalised, f"{created_column}: {exc}")
                    )
                    continue

            if match is None and normalised in created_here:
                outcomes.append(
                    RowOutcome(
                        row_number,
                        "error",
                        normalised,
                        f"Same {by_key[identity_key].label} as row {created_here[normalised]}"
                        " in this file",
                    )
                )
                continue

            if match is None:
                created_here[normalised] = row_number
                outcomes.append(RowOutcome(row_number, "create", normalised))
                if commit and writer is not None:
                    await self._create_lead(
                        writer, validated.values, normalised, assignee, created_at=created_at
                    )
            else:
                outcomes.append(RowOutcome(row_number, "update", normalised))
                if commit and writer is not None:
                    self._update_lead(writer, match, validated.values, assignee, by_key)

        if commit:
            await self._session.flush()
        return outcomes

    def _resolve_owner(
        self,
        strategy: str,
        row: Mapping[str, str],
        owner_column: str,
        members_by_email: Mapping[str, uuid.UUID],
        targets: Sequence[uuid.UUID],
        cursor: int,
    ) -> uuid.UUID | None:
        """Who this row's lead belongs to.

        "Owner Specific Assignment" and the three distribution strategies all
        answer the same question, so they resolve in one place.
        """
        if strategy == DistributionStrategy.COLUMN:
            email = (row.get(owner_column) or "").strip().lower()
            return members_by_email.get(email)
        if not targets:
            return None
        return targets[cursor % len(targets)]

    async def _members_by_email(self) -> dict[str, uuid.UUID]:
        """Owner column -> membership, by the user's email.

        `user` is eager-loaded: it is a lazy relationship, and touching it per
        row would emit IO from a sync context (MissingGreenlet) as well as one
        query per member.
        """
        rows = await self._session.execute(
            self._session.select(Membership).options(selectinload(Membership.user))
        )
        result: dict[str, uuid.UUID] = {}
        for membership in rows.scalars().all():
            if membership.user is not None:
                result[membership.user.email.lower()] = membership.id
        return result

    async def _existing_by_identity(self) -> dict[str, Lead]:
        rows = await self._session.execute(
            self._session.select(Lead).where(Lead.deleted_at.is_(None))
        )
        return {lead.identity_value: lead for lead in rows.scalars().all()}

    async def _create_lead(
        self,
        writer: ActionWriter,
        values: Mapping[str, Any],
        identity: str,
        assignee_id: uuid.UUID | None,
        *,
        created_at: dt.datetime | None = None,
    ) -> Lead:
        stage = await self._initial_stage()
        lead = Lead(
            identity_value=identity,
            values=dict(values),
            stage_id=stage.id if stage else None,
            assignee_id=assignee_id,
            created_by_id=self._actor_id,
        )
        if created_at is not None:
            # When the enquiry first reached the business, from the old
            # system's export — not the moment of this import.
            lead.created_at = created_at
        lead.search_vector = self._vector(lead)
        self._session.add(lead)
        await self._session.flush()

        writer.record_created(lead)
        if assignee_id is not None:
            writer.record_assignment_change(lead, old_assignee_id=None, new_assignee_id=assignee_id)
        return lead

    def _update_lead(
        self,
        writer: ActionWriter,
        lead: Lead,
        values: Mapping[str, Any],
        assignee_id: uuid.UUID | None,
        by_key: Mapping[str, LeadField],
    ) -> None:
        merged = dict(lead.values or {})
        deltas: list[FieldDelta] = []
        for key, value in values.items():
            if merged.get(key) == value:
                continue
            deltas.append(
                FieldDelta(
                    field_key=key,
                    label=by_key[key].label if key in by_key else key,
                    old=merged.get(key),
                    new=value,
                )
            )
            merged[key] = value

        lead.values = merged
        if deltas:
            writer.record_field_changes(lead, deltas)
        if assignee_id is not None and assignee_id != lead.assignee_id:
            old = lead.assignee_id
            lead.assignee_id = assignee_id
            writer.record_assignment_change(lead, old_assignee_id=old, new_assignee_id=assignee_id)
        lead.search_vector = self._vector(lead)

    def _vector(self, lead: Lead) -> Any:
        document = search_text_for(
            lead.values or {}, self._fields, identity_value=lead.identity_value
        )
        return func.to_tsvector(SEARCH_CONFIG, document)

    async def _initial_stage(self) -> Stage | None:
        rows = await self._session.execute(
            self._session.select(Stage).where(Stage.kind == StageKind.INITIAL).limit(1)
        )
        stage: Stage | None = rows.scalar_one_or_none()
        return stage

    # --- historical actions -------------------------------------------------

    async def _evaluate_actions(
        self, job: ImportJob, sheet: Sheet, *, commit: bool
    ) -> list[RowOutcome]:
        """Historical timeline migration.

        The flow the audit called out as missing and every customer switching
        CRMs needs. Rows carry a lead identifier, a kind, a timestamp and a
        note; the timestamps are *historical*, so these actions are written
        predated on purpose — a migrated timeline whose events all happened at
        import o'clock is not a timeline.
        """
        identity_key = await self._identity_key()
        existing = await self._existing_by_identity()
        dispositions = await self._dispositions()
        custom_types = await self._custom_action_types()

        validator = await self._schema()
        mapping = job.mapping
        column_for = {target: column for column, target in mapping.items()}
        identity_column = column_for.get("identity") or column_for.get(identity_key)
        if not identity_column:
            raise unprocessable(
                "mapping_required",
                "Map a column to the lead identifier so each action can find its lead",
            )

        writer: ActionWriter | None = None
        if commit:
            writer = ActionWriter(self._session, actor_id=self._actor_id)
            await writer.open_changeset(
                source=ChangesetSource.IMPORT,
                summary=f"Imported {sheet.row_count} historical actions from {job.filename}",
                lead_count=sheet.row_count,
            )
            job.changeset_id = writer.changeset.id

        outcomes: list[RowOutcome] = []
        for index, row in enumerate(sheet.rows):
            row_number = index + 2
            identity = self._normalise_identity(
                validator, identity_key, (row.get(identity_column) or "").strip()
            )
            lead = existing.get(identity) if identity else None
            if lead is None:
                outcomes.append(
                    RowOutcome(row_number, "error", identity, "No lead with that identifier")
                )
                continue

            kind_raw = (row.get(column_for.get("kind", "")) or "").strip().upper()
            kind = self._action_kind(kind_raw)
            if kind is None:
                outcomes.append(
                    RowOutcome(row_number, "error", identity, f"Unknown action kind {kind_raw!r}")
                )
                continue

            performed_at = self._parse_timestamp(row.get(column_for.get("performed_at", "")))
            if performed_at is None:
                outcomes.append(
                    RowOutcome(row_number, "error", identity, "Could not read the timestamp")
                )
                continue

            outcomes.append(RowOutcome(row_number, "create", identity))
            if not commit or writer is None:
                continue

            body = (row.get(column_for.get("body", "")) or "").strip() or None
            if kind is SystemActionKind.CUSTOM:
                type_name = (row.get(column_for.get("action_type", "")) or "").strip().lower()
                action_type = custom_types.get(type_name)
                if action_type is None:
                    outcomes[-1] = RowOutcome(
                        row_number, "error", identity, f"Unknown action type {type_name!r}"
                    )
                    continue
                writer.record_imported(
                    lead,
                    kind=kind,
                    performed_at=performed_at,
                    body=body,
                    action_type_id=action_type.id,
                    score_applied=action_type.score,
                )
            elif kind is SystemActionKind.CALL_LOGGED:
                direction = (row.get(column_for.get("direction", "")) or "OUTGOING").strip().upper()
                default = next(iter(dispositions.values()), None)
                writer.record_imported(
                    lead,
                    kind=kind,
                    performed_at=performed_at,
                    body=body,
                    payload={
                        "direction": direction,
                        "disposition_id": str(default.id) if default else None,
                        "duration_seconds": 0,
                        "notes": body,
                        "imported": True,
                    },
                )
            else:
                writer.record_imported(lead, kind=kind, performed_at=performed_at, body=body)

        if commit:
            await self._session.flush()
        return outcomes

    @staticmethod
    def _normalise_identity(validator: ValueValidator, key: str, raw: str) -> str:
        """Put a sheet's identifier into the form `identity_value` holds.

        A spreadsheet carries what a human typed — `9876543210` — while the
        column stores the normalised `+919876543210`. Matching one against the
        other finds nothing, and the operator gets "no lead with that
        identifier" for every row of a file that is entirely correct.

        Normalised through the same validator the lead write path uses, so the
        two can never disagree about what a phone number is.
        """
        if not raw:
            return ""
        try:
            validated = validator.validate({key: raw}, is_create=True)
        except FieldValidationError:
            return raw
        return str(validated.values.get(key) or raw)

    @staticmethod
    def _action_kind(raw: str) -> SystemActionKind | None:
        """Map a spreadsheet's word for an event onto a kind we own.

        Only the kinds a *history* can meaningfully contain. A migrated sheet
        must not be able to fabricate a FIELD_CHANGE or a STAGE_CHANGE, because
        those carry old/new values that undo would later try to replay.
        """
        allowed = {
            "NOTE": SystemActionKind.NOTE,
            "CALL": SystemActionKind.CALL_LOGGED,
            "CALL_LOGGED": SystemActionKind.CALL_LOGGED,
            "WHATSAPP": SystemActionKind.WHATSAPP_SENT,
            "WHATSAPP_SENT": SystemActionKind.WHATSAPP_SENT,
            "EMAIL": SystemActionKind.EMAIL_SENT,
            "EMAIL_SENT": SystemActionKind.EMAIL_SENT,
            "SMS": SystemActionKind.SMS_SENT,
            "SMS_SENT": SystemActionKind.SMS_SENT,
            "CUSTOM": SystemActionKind.CUSTOM,
        }
        return allowed.get(raw)

    @staticmethod
    def _parse_timestamp(raw: str | None) -> dt.datetime | None:
        if not raw:
            return None
        text = raw.strip()
        for parse in (
            dt.datetime.fromisoformat,
            lambda t: dt.datetime.combine(dt.date.fromisoformat(t), dt.time.min),
        ):
            try:
                parsed = parse(text)
            except ValueError:
                continue
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
        return None

    async def _dispositions(self) -> dict[str, CallDisposition]:
        rows = await self._session.execute(self._session.select(CallDisposition))
        return {d.label.lower(): d for d in rows.scalars().all()}

    async def _custom_action_types(self) -> dict[str, CustomActionType]:
        rows = await self._session.execute(self._session.select(CustomActionType))
        return {t.name.lower(): t for t in rows.scalars().all()}
