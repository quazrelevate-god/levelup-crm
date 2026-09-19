"""Lead persistence (M5).

Every read here goes through `FieldProjectionService` and every write through
`FieldWriteFilter` — no exceptions, no shortcut for "internal" calls, because an
internal caller is exactly how the first leak happens.

The mutation shape is the same everywhere:

    open a changeset -> apply the change -> append the actions -> commit once

All in one transaction, so the timeline can never diverge from the data it
describes.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import func, literal_column, or_, select
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement

from app.errors import api_error, conflict, not_found
from app.fields.search import SEARCH_CONFIG, search_text_for
from app.fields.values import FieldValidationError, ValueValidator
from app.filters.compiler import FilterCompiler
from app.filters.dsl import FilterNode, validate_shape
from app.models.enums import ChangesetSource, IndexedFieldStatus, StageKind
from app.models.field import FieldOption, IndexedField, LeadField
from app.models.lead import Action, Changeset, Lead
from app.models.pipeline import LostReason, Stage
from app.models.workspace import Membership, Workspace
from app.permissions.projection import FieldProjectionService, FieldWriteFilter
from app.services.actions import ActionWriter, FieldDelta
from app.services.assignment import AssignmentEngine
from app.tenancy.session import ScopedSession

__all__ = ["MAX_BULK_LEADS", "LeadService", "QuickFilters"]

#: docs/02-api-contract.md — "Max 500". A larger batch is a job, not a
#: request: it would hold a transaction open long enough to matter and
#: produce an undo preview nobody can read.
MAX_BULK_LEADS = 500


@dataclasses.dataclass(frozen=True, slots=True)
class QuickFilters:
    """The one-click filters that live beside the DSL rather than inside it.

    `docs/02-api-contract.md` gives `GET /leads` "quick filters" as a separate
    concern from the filter document, and the split is the right one: these are
    columns on `leads`, while a DSL field rule is by definition about a
    workspace-defined field in `values`. Folding stage into the DSL would mean
    inventing a pseudo-field key that no `lead_fields` row backs.
    """

    stage_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None
    unassigned: bool = False
    rating: int | None = None
    #: Structural pipeline kinds — "show me everything still open" without
    #: naming stages the workspace may rename tomorrow.
    stage_kinds: tuple[str, ...] = ()


def lead_visibility_clause(
    *, sees_all: bool, visible: frozenset[uuid.UUID]
) -> ColumnElement[bool] | None:
    """The manager-sees-their-reports rule, as a predicate. `None` means no filter.

    Resolved in the scoping layer (M1) into `visible_membership_ids`; this is
    the one place it becomes SQL. **An unassigned lead is visible to everyone**
    who can see the workspace — it belongs to nobody yet, and hiding it would
    mean new leads vanished until somebody assigned them.

    A module-level function rather than a method because M9's reports need the
    identical rule, and the first version of them reimplemented it and quietly
    dropped the unassigned half — so a caller's dashboard would have counted
    fewer leads than their own list showed, with nothing to indicate why.
    """
    if sees_all:
        return None
    return Lead.assignee_id.in_(visible) | Lead.assignee_id.is_(None)


class LeadService:
    """Create, read and update leads.

    Constructed per request with the caller's projection and write filter
    already bound, so no method can accidentally run unfiltered.
    """

    def __init__(
        self,
        session: ScopedSession,
        *,
        workspace: Workspace,
        projection: FieldProjectionService,
        write_filter: FieldWriteFilter,
        actor_id: uuid.UUID | None,
        visible_membership_ids: frozenset[uuid.UUID],
        sees_all: bool,
    ) -> None:
        self._session = session
        self._workspace = workspace
        self._projection = projection
        self._write_filter = write_filter
        self._actor_id = actor_id
        self._visible = visible_membership_ids
        self._sees_all = sees_all
        self._validator: ValueValidator | None = None
        self._fields: list[LeadField] = []
        self._assignment: AssignmentEngine | None = None
        self._last_validation: Any = None

    # --- setup -------------------------------------------------------------

    async def _load_schema(self) -> ValueValidator:
        """Build the validator once per request from the workspace's schema."""
        if self._validator is None:
            rows = await self._session.execute(
                self._session.select(LeadField).order_by(LeadField.sort_order)
            )
            self._fields = list(rows.scalars().all())

            option_rows = await self._session.execute(self._session.select(FieldOption))
            grouped: dict[uuid.UUID, list[FieldOption]] = {}
            for option in option_rows.scalars().all():
                grouped.setdefault(option.field_id, []).append(option)

            self._validator = ValueValidator(
                self._fields,
                default_country_code=self._workspace.default_country_code,
                currency=self._workspace.currency,
                timezone=self._workspace.timezone,
                options_by_field=grouped,
            )
        return self._validator

    async def identity_key(self) -> str:
        """The workspace's identity field key. Public for M10's intake path.

        Intake has to resolve identity exactly the way the UI does — otherwise a
        lead posted by a form and the same lead typed by a person would not be
        the same lead.
        """
        return await self._identity_key()

    async def normalise_identity(self, raw: str) -> str | None:
        """Run a raw identity through the same validator the write path uses.

        The stored `identity_value` is normalised (phone digits, the workspace's
        country code), so matching against the raw string would miss every
        existing lead and turn an update into a duplicate.
        """
        return await self.normalise_field_value(await self._identity_key(), raw)

    async def normalise_field_value(self, key: str, raw: str) -> str | None:
        """Normalise `raw` exactly as the write path would store it in `key`.

        `normalise_identity` is this, for the identity field. The voice webhook
        needs it for the *phone* field, which is not the identity in a
        workspace that identifies leads by name.
        """
        validator = await self._load_schema()
        try:
            validated = validator.validate({key: raw}, is_create=False)
        except FieldValidationError:
            return None
        value = validated.values.get(key)
        return str(value) if value not in (None, "") else None

    @property
    def last_validation(self) -> Any:
        """The last `ValidatedValues` this service produced, or None.

        Exists so intake can report unknown keys and quarantined templates. Both
        are *accepted* rather than refused, so without this they would be
        invisible — a field quietly not arriving, with no trace.
        """
        return self._last_validation

    async def _identity_key(self) -> str:
        """The key of the field this workspace identifies leads by.

        Falls back to the first PHONE-typed field only if the setting is unset,
        which provisioning makes impossible — but a workspace created before
        M2 landed would otherwise be unusable.
        """
        await self._load_schema()
        identity_id = self._workspace.identity_field_id
        for field in self._fields:
            if field.id == identity_id:
                return field.key
        raise api_error(
            409,
            "no_identity_field",
            "This workspace has no lead identity field configured",
        )

    # --- reads -------------------------------------------------------------

    def _visibility_clause(self) -> Any:
        """This caller's lead-visibility predicate. See `lead_visibility_clause`."""
        return lead_visibility_clause(sees_all=self._sees_all, visible=self._visible)

    async def get_lead(self, lead_id: uuid.UUID) -> Lead:
        statement = (
            self._session.select(Lead)
            .where(Lead.id == lead_id, Lead.deleted_at.is_(None))
            .options(selectinload(Lead.actions))
            .limit(1)
        )
        clause = self._visibility_clause()
        if clause is not None:
            statement = statement.where(clause)

        rows = await self._session.execute(statement)
        lead: Lead | None = rows.scalar_one_or_none()
        if lead is None:
            # Absent, deleted, another workspace's, or outside this caller's
            # visibility — all indistinguishable, by design.
            raise not_found("Lead")
        return lead

    async def list_leads(
        self, *, limit: int, offset: int, search: str | None = None
    ) -> tuple[Sequence[Lead], int]:
        """Server-paginated (architecture rule 9). Never returns actions (rule 6)."""
        statement = self._session.select(Lead).where(Lead.deleted_at.is_(None))
        clause = self._visibility_clause()
        if clause is not None:
            statement = statement.where(clause)
        if search:
            statement = statement.where(Lead.identity_value.ilike(f"%{search}%"))

        total_result = await self._session.execute(
            select(func.count()).select_from(statement.subquery())
        )
        rows = await self._session.execute(
            statement.order_by(Lead.created_at.desc()).limit(limit).offset(offset)
        )
        return rows.scalars().all(), int(total_result.scalar_one())

    async def project(self, lead: Lead, *, columns: Sequence[str] | None = None) -> dict[str, Any]:
        """The API representation, View-projected.

        The single place a lead becomes JSON. Every endpoint returns this, so
        there is no path by which a non-View field reaches a response.

        `columns` narrows the hydrated values to what the caller asked for — a
        50-field workspace should not ship 50 values per row to draw six
        columns. It is applied *after* projection and can only ever subtract:
        naming a field the caller cannot view does not reveal it.
        """
        validator = await self._load_schema()
        visible = self._projection.project_values(lead.values or {})
        if columns is not None:
            wanted = set(columns)
            visible = {k: v for k, v in visible.items() if k in wanted}

        by_id = {f.id: f for f in self._fields}
        h1 = by_id.get(self._workspace.primary_field_1_id or uuid.uuid4())
        h2 = by_id.get(self._workspace.primary_field_2_id or uuid.uuid4())

        return {
            "id": str(lead.id),
            "identity_value": lead.identity_value,
            "primary": {
                "h1": visible.get(h1.key) if h1 else None,
                "h2": visible.get(h2.key) if h2 else None,
            },
            "stage_id": str(lead.stage_id) if lead.stage_id else None,
            "lost_reason_id": str(lead.lost_reason_id) if lead.lost_reason_id else None,
            "assignee_id": str(lead.assignee_id) if lead.assignee_id else None,
            "rating": lead.rating,
            "score": lead.score,
            "values": visible,
            # Option labels for the visible subset only — decorating a field the
            # caller cannot see would leak it through the label.
            "labels": validator.project_labels(visible),
            "last_action_at": (lead.last_action_at.isoformat() if lead.last_action_at else None),
            "created_at": lead.created_at.isoformat(),
        }

    # --- writes ------------------------------------------------------------

    def assignment_engine(self) -> AssignmentEngine:
        """The rule engine, built once per service so an import reuses it."""
        if self._assignment is None:
            self._assignment = AssignmentEngine(self._session, workspace=self._workspace)
        return self._assignment

    async def create_lead(
        self,
        *,
        values: Mapping[str, Any],
        stage_id: uuid.UUID | None = None,
        assignee_id: uuid.UUID | None = None,
        rating: int | None = None,
        apply_assignment_rules: bool = True,
    ) -> tuple[Lead, ActionWriter]:
        """Create a lead, its changeset and its `LEAD_CREATED` action together.

        Assignment rules run here rather than in the router, because *every*
        create path lands in this method — UI, import, and M10's intake API.
        The spec asks for one implementation called from three places; this
        is the one place. An explicit `assignee_id` wins over the rules: a
        person naming a rep is a decision, not a default to be overridden.
        """
        validator = await self._load_schema()
        identity_key = await self._identity_key()

        self._write_filter.check(values, known_keys=validator.field_keys)

        try:
            validated = validator.validate(dict(values), is_create=True)
            self._last_validation = validated
        except FieldValidationError as exc:
            raise api_error(
                422, "invalid_values", "One or more values are invalid", fields=exc.errors
            ) from exc

        identity = validated.values.get(identity_key)
        if identity in (None, ""):
            raise api_error(
                422,
                "identity_required",
                "A lead needs a value for the workspace's identity field",
                field=identity_key,
            )

        await self._assert_identity_free(str(identity))

        stage = await self._resolve_initial_stage(stage_id)
        if assignee_id is not None:
            await self._assert_member(assignee_id)

        lead = Lead(
            identity_value=str(identity),
            values=validated.values,
            stage_id=stage.id if stage else None,
            assignee_id=assignee_id,
            rating=rating,
            created_by_id=self._actor_id,
        )
        self._session.add(lead)
        self._refresh_search_vector(lead)
        await self._session.flush()

        # After the flush: the engine evaluates rules as SQL against this lead's
        # row, so the row has to exist in the transaction first.
        routed_to: uuid.UUID | None = None
        if assignee_id is None and apply_assignment_rules:
            outcome = await self.assignment_engine().decide(lead)
            if outcome.membership_id is not None:
                lead.assignee_id = outcome.membership_id
                routed_to = outcome.membership_id
                await self._session.flush()

        writer = ActionWriter(self._session, actor_id=self._actor_id)
        await writer.open_changeset(
            source=ChangesetSource.SINGLE_EDIT,
            summary=f"Created lead {identity}",
            lead_count=1,
        )
        writer.record_created(lead)
        new_assignee = assignee_id if assignee_id is not None else routed_to
        if new_assignee is not None:
            writer.record_assignment_change(
                lead, old_assignee_id=None, new_assignee_id=new_assignee
            )
        await self._session.flush()
        return lead, writer

    async def update_lead(
        self,
        lead_id: uuid.UUID,
        *,
        values: Mapping[str, Any] | None = None,
        stage_id: uuid.UUID | None = None,
        lost_reason_id: uuid.UUID | None = None,
        assignee_id: uuid.UUID | None = None,
        rating: int | None = None,
        unset: frozenset[str] = frozenset(),
    ) -> tuple[Lead, ActionWriter]:
        """Apply a change and append every action it implies, atomically.

        `unset` names keys the caller explicitly cleared, so "set to null" is
        distinguishable from "not mentioned" — a PATCH must not wipe fields it
        never talked about.
        """
        lead = await self.get_lead(lead_id)
        await self._load_schema()

        writer = ActionWriter(self._session, actor_id=self._actor_id)
        await writer.open_changeset(
            source=ChangesetSource.SINGLE_EDIT,
            summary=f"Updated lead {lead.identity_value}",
            lead_count=1,
        )
        await self._apply_update(
            lead,
            writer,
            values=values,
            stage_id=stage_id,
            lost_reason_id=lost_reason_id,
            assignee_id=assignee_id,
            rating=rating,
            unset=unset,
        )
        await self._session.flush()
        return lead, writer

    async def _apply_update(
        self,
        lead: Lead,
        writer: ActionWriter,
        *,
        values: Mapping[str, Any] | None = None,
        stage_id: uuid.UUID | None = None,
        lost_reason_id: uuid.UUID | None = None,
        assignee_id: uuid.UUID | None = None,
        rating: int | None = None,
        unset: frozenset[str] = frozenset(),
    ) -> None:
        """One lead's mutation, appended to an already-open changeset.

        Shared by the single PATCH and by bulk edit so the two cannot drift.
        That matters more than the deduplication: if bulk had its own copy, a
        permission check or an action added to one would silently not exist in
        the other, and the bulk path is the one that touches 500 leads at once.

        Deliberately does not flush or open a changeset — the caller owns both,
        which is what lets 500 leads share one batch.
        """
        validator = await self._load_schema()

        deltas: list[FieldDelta] = []
        if values or unset:
            payload = dict(values or {})
            for key in unset:
                payload[key] = None

            self._write_filter.check(payload, known_keys=validator.field_keys)
            try:
                validated = validator.validate(payload, is_create=False, existing=lead.values or {})
            except FieldValidationError as exc:
                raise api_error(
                    422, "invalid_values", "One or more values are invalid", fields=exc.errors
                ) from exc

            by_key = {f.key: f for f in self._fields}
            merged = dict(lead.values or {})
            for key, new_value in validated.values.items():
                old_value = merged.get(key)
                if old_value == new_value:
                    continue
                merged[key] = new_value
                deltas.append(
                    FieldDelta(
                        field_key=key,
                        label=by_key[key].label if key in by_key else key,
                        old=old_value,
                        new=new_value,
                    )
                )
            lead.values = merged

            identity_key = await self._identity_key()
            if identity_key in validated.values:
                new_identity = validated.values[identity_key]
                if new_identity and str(new_identity) != lead.identity_value:
                    await self._assert_identity_free(str(new_identity))
                    lead.identity_value = str(new_identity)

        if deltas:
            writer.record_field_changes(lead, deltas)

        if stage_id is not None and stage_id != lead.stage_id:
            stage = await self._session.get(Stage, stage_id)
            if stage is None or stage.is_archived:
                raise not_found("Stage")

            resolved_reason = await self._resolve_lost_reason(stage, lost_reason_id)
            old_stage_id = lead.stage_id
            lead.stage_id = stage.id
            lead.lost_reason_id = resolved_reason
            writer.record_stage_change(
                lead,
                old_stage_id=old_stage_id,
                new_stage_id=stage.id,
                lost_reason_id=resolved_reason,
            )

        if assignee_id is not None and assignee_id != lead.assignee_id:
            await self._assert_member(assignee_id)
            old_assignee = lead.assignee_id
            lead.assignee_id = assignee_id
            writer.record_assignment_change(
                lead, old_assignee_id=old_assignee, new_assignee_id=assignee_id
            )

        if rating is not None and rating != lead.rating:
            old_rating = lead.rating
            lead.rating = rating
            writer.record_rating_change(lead, old_rating=old_rating, new_rating=rating)

        # After every mutation, not inside the `values` branch: the identity
        # value feeds the vector too, and it can change on a path that touched
        # no searchable field.
        self._refresh_search_vector(lead)

    async def bulk_update(
        self,
        *,
        lead_ids: Sequence[uuid.UUID],
        values: Mapping[str, Any] | None = None,
        stage_id: uuid.UUID | None = None,
        lost_reason_id: uuid.UUID | None = None,
        assignee_id: uuid.UUID | None = None,
        rating: int | None = None,
        unset: frozenset[str] = frozenset(),
    ) -> tuple[Changeset, list[Lead]]:
        """Apply one change to many leads as a single undoable batch.

        Three properties this has to hold, all of them load-bearing for M7's
        undo:

        1. **One changeset.** Every action the run produces carries its id, so
           the whole batch reverses as a unit. A changeset per lead would make
           "undo that mistake" 500 separate decisions.
        2. **Permission-checked per field, once.** `_apply_update` runs the
           write filter for every lead, and the filter refuses rather than
           dropping — so a forbidden field fails the batch on the first lead
           instead of writing 200 leads and then stopping.
        3. **Capped.** 500 per the contract. A request that would touch more is
           refused with the cap named, not silently truncated to the first 500 —
           truncation would report success over work it did not do.
        """
        if not lead_ids:
            raise api_error(422, "no_leads_selected", "Select at least one lead to edit")
        if len(lead_ids) > MAX_BULK_LEADS:
            raise api_error(
                422,
                "bulk_limit_exceeded",
                f"A bulk edit may cover at most {MAX_BULK_LEADS} leads at once",
                limit=MAX_BULK_LEADS,
                requested=len(lead_ids),
            )

        await self._load_schema()

        statement = self._session.select(Lead).where(
            Lead.id.in_(list(lead_ids)), Lead.deleted_at.is_(None)
        )
        if (visibility := self._visibility_clause()) is not None:
            statement = statement.where(visibility)
        rows = await self._session.execute(statement)
        leads = list(rows.scalars().all())

        # Absent means deleted, another workspace's, or outside this caller's
        # visibility. Refused rather than quietly edited-around: an operator who
        # selected 300 leads and sees "297 updated" has learnt something about
        # the three; one who sees 300 has been misled.
        if len(leads) != len(set(lead_ids)):
            found = {lead.id for lead in leads}
            raise api_error(
                404,
                "leads_not_found",
                "Some of the selected leads are no longer available to edit",
                missing=[str(i) for i in dict.fromkeys(lead_ids) if i not in found],
            )

        writer = ActionWriter(self._session, actor_id=self._actor_id)
        await writer.open_changeset(
            source=ChangesetSource.BULK_EDIT,
            summary=self._bulk_summary(values, stage_id, assignee_id, rating, unset, len(leads)),
            lead_count=len(leads),
        )
        for lead in leads:
            await self._apply_update(
                lead,
                writer,
                values=values,
                stage_id=stage_id,
                lost_reason_id=lost_reason_id,
                assignee_id=assignee_id,
                rating=rating,
                unset=unset,
            )
        await self._session.flush()
        return writer.changeset, leads

    def _bulk_summary(
        self,
        values: Mapping[str, Any] | None,
        stage_id: uuid.UUID | None,
        assignee_id: uuid.UUID | None,
        rating: int | None,
        unset: frozenset[str],
        count: int,
    ) -> str:
        """What the edit report will show for this run.

        Written at open time rather than derived afterwards, because the intent
        ("Set Stage on 312 leads") is knowable now and unreconstructable later.
        """
        by_key = {f.key: f.label for f in self._fields}
        changed = [by_key.get(key, key) for key in (values or {})]
        changed += [by_key.get(key, key) for key in sorted(unset)]
        if stage_id is not None:
            changed.append("Stage")
        if assignee_id is not None:
            changed.append("Assignee")
        if rating is not None:
            changed.append("Rating")

        what = ", ".join(changed) if changed else "nothing"
        return f"Set {what} on {count} lead{'s' if count != 1 else ''}"

    def _refresh_search_vector(self, lead: Lead) -> None:
        """Recompute `search_vector` from the lead's searchable values.

        Assigned as a SQL expression rather than a computed string so the
        `to_tsvector` runs in Postgres with the same configuration the query
        side uses — building it in Python would mean shipping a parsed vector
        and trusting the two implementations to agree.

        Called on every write path. A lead whose vector lagged its values would
        be invisible to search, which is worse than being unindexed: the user
        sees an empty result and concludes the lead is not there.
        """
        document = search_text_for(
            lead.values or {}, self._fields, identity_value=lead.identity_value
        )
        lead.search_vector = func.to_tsvector(SEARCH_CONFIG, document)

    # --- searching ---------------------------------------------------------

    def sortable_columns(self) -> dict[str, Any]:
        """Built-in columns a caller may sort by, by their API name.

        Deliberately a closed set of real columns. Everything else has to be a
        declared indexed field, because sorting 50,000 rows on an unindexed
        JSONB expression is the one query shape that cannot be made fast after
        the fact.
        """
        return {
            "created_at": Lead.created_at,
            "updated_at": Lead.updated_at,
            "last_action_at": Lead.last_action_at,
            "score": Lead.score,
            "rating": Lead.rating,
            "identity_value": Lead.identity_value,
            "stage_id": Lead.stage_id,
            "assignee_id": Lead.assignee_id,
        }

    async def _sort_expression(self, sort: str) -> Any:
        """Resolve a sort key to a column, or refuse with the fix.

        `-key` sorts descending, matching the API contract's `sort` parameter.
        """
        await self._load_schema()
        descending = sort.startswith("-")
        key = sort.lstrip("-")

        if (column := self.sortable_columns().get(key)) is not None:
            return column.desc().nullslast() if descending else column.asc().nullslast()

        field = next((f for f in self._fields if f.key == key), None)
        if field is None or not self._projection.filterable(key):
            raise api_error(
                400, "unknown_sort_field", f"There is no field named {key!r} to sort by"
            )

        rows = await self._session.execute(
            self._session.select(IndexedField).where(IndexedField.field_id == field.id).limit(1)
        )
        declared: IndexedField | None = rows.scalar_one_or_none()
        if declared is None:
            raise api_error(
                400,
                "field_not_indexed",
                f"{field.label} must be declared an indexed field before the list can "
                f"be sorted by it. Settings -> Fields -> {field.label} -> Index.",
                field_key=key,
            )
        if declared.status != IndexedFieldStatus.READY:
            # Sorting on a PENDING declaration would work and be slow, which is
            # exactly what the restriction exists to prevent. Distinguished from
            # "not indexed" so the admin waits rather than re-declaring.
            raise api_error(
                400,
                "index_not_ready",
                f"The index on {field.label} is {declared.status.lower()}. "
                f"Sorting by it will be available once the index finishes building.",
                field_key=key,
                status=declared.status,
            )

        # The literal spelling the expression index was built on — see
        # `app.filters.compiler._json_path`.
        expression: ColumnElement[Any] = literal_column(f"leads.values ->> '{field.key}'")
        return expression.desc().nullslast() if descending else expression.asc().nullslast()

    async def compile_filter(self, node: FilterNode | None) -> Any:
        """Turn a filter document into a WHERE clause, or None for no filter.

        The permission gate lives inside `FilterCompiler`, per field, so this
        is a thin seam — but it is the seam every caller uses, which is what
        keeps the gate from being skipped by a new endpoint.
        """
        if node is None:
            return None
        # The compiler resolves keys against the workspace's field definitions,
        # and those are loaded lazily. Without this the field map is empty and
        # every rule looks like a filter on a field nobody has.
        await self._load_schema()
        try:
            validate_shape(node)
        except ValueError as exc:
            raise api_error(422, "invalid_filter", str(exc)) from exc

        compiler = FilterCompiler(
            fields=self._fields,
            projection=self._projection,
            timezone=self._workspace.timezone,
        )
        return compiler.compile(node)

    async def count_by_stage(
        self, clause: Any = None, quick: QuickFilters | None = None
    ) -> tuple[int, dict[str, int]]:
        """Total and per-stage counts for a filter, in one query.

        Grouped rather than a count per stage: a workspace with a dozen stages
        would otherwise pay a dozen full filter evaluations to draw one summary,
        and a history predicate is not cheap enough to run twelve times.

        Leads with no stage are counted in the total under a `null` key, so the
        parts always sum to the whole.
        """
        await self._load_schema()
        statement = self._session.select(Lead).where(Lead.deleted_at.is_(None))
        if (visibility := self._visibility_clause()) is not None:
            statement = statement.where(visibility)
        if clause is not None:
            statement = statement.where(clause)
        for quick_clause in self._quick_filter_clauses(quick or QuickFilters()):
            statement = statement.where(quick_clause)

        subquery = statement.subquery()
        rows = await self._session.execute(
            select(subquery.c.stage_id, func.count())
            .select_from(subquery)
            .group_by(subquery.c.stage_id)
        )
        by_stage: dict[str, int] = {}
        total = 0
        for stage_id, count in rows.all():
            by_stage[str(stage_id) if stage_id else "null"] = int(count)
            total += int(count)
        return total, by_stage

    def _quick_filter_clauses(self, quick: QuickFilters) -> list[ColumnElement[bool]]:
        """The current-state filters that are columns rather than fields.

        Stage, assignee and rating live on `leads`, not in `values`, so the DSL
        cannot reach them: §6.1 defines a field rule as referencing
        `lead_fields.key`. They are the most-used filters in any CRM, which is
        why the API contract gives `GET /leads` quick filters alongside the DSL
        rather than folding them into it.
        """
        clauses: list[ColumnElement[bool]] = []
        if quick.stage_id is not None:
            clauses.append(Lead.stage_id == quick.stage_id)
        if quick.assignee_id is not None:
            clauses.append(Lead.assignee_id == quick.assignee_id)
        if quick.unassigned:
            # A separate flag rather than `assignee_id=null`: a query string
            # cannot tell "no value given" from "explicitly nobody", and those
            # mean opposite things here.
            clauses.append(Lead.assignee_id.is_(None))
        if quick.rating is not None:
            clauses.append(Lead.rating == quick.rating)
        if quick.stage_kinds:
            closed = {StageKind.WON, StageKind.LOST}
            wanted = {StageKind(kind) for kind in quick.stage_kinds}
            stage_ids = select(Stage.id).where(Stage.kind.in_(wanted))
            if closed & wanted:
                clauses.append(Lead.stage_id.in_(stage_ids))
            else:
                # A stageless lead is open, so "open" must include it — the
                # same NULL trap the ownership check has to sidestep.
                clauses.append(or_(Lead.stage_id.is_(None), Lead.stage_id.in_(stage_ids)))
        return clauses

    async def search_leads(
        self,
        *,
        limit: int,
        offset: int,
        clause: Any = None,
        search: str | None = None,
        sort: str = "-created_at",
        quick: QuickFilters | None = None,
    ) -> tuple[Sequence[Lead], int]:
        """The list endpoint's query: filter, search, sort, paginate.

        Never returns actions (architecture rule 6) and never returns a lead
        outside the caller's visibility.
        """
        statement = self._session.select(Lead).where(Lead.deleted_at.is_(None))
        if (visibility := self._visibility_clause()) is not None:
            statement = statement.where(visibility)
        if clause is not None:
            statement = statement.where(clause)
        for quick_clause in self._quick_filter_clauses(quick or QuickFilters()):
            statement = statement.where(quick_clause)
        if search:
            await self._load_schema()
            compiler = FilterCompiler(
                fields=self._fields,
                projection=self._projection,
                timezone=self._workspace.timezone,
            )
            statement = statement.where(compiler.search_clause(search))

        total_result = await self._session.execute(
            select(func.count()).select_from(statement.subquery())
        )
        order = await self._sort_expression(sort)
        # `id` breaks ties: two leads created in the same millisecond would
        # otherwise be ordered arbitrarily, and a row could appear on both page
        # one and page two.
        rows = await self._session.execute(
            statement.order_by(order, Lead.id).limit(limit).offset(offset)
        )
        return rows.scalars().all(), int(total_result.scalar_one())

    async def soft_delete(self, lead_id: uuid.UUID) -> Lead:
        """Soft delete only (architecture rule 13). Leads never hard-delete."""
        lead = await self.get_lead(lead_id)
        lead.deleted_at = dt.datetime.now(dt.UTC)
        await self._session.flush()
        return lead

    async def list_actions(
        self, lead_id: uuid.UUID, *, limit: int, offset: int
    ) -> Sequence[Action]:
        """The timeline. Reached only through a lead the caller can already see."""
        await self.get_lead(lead_id)
        rows = await self._session.execute(
            self._session.select(Action)
            .where(Action.lead_id == lead_id)
            .order_by(Action.performed_at.desc())
            .limit(limit)
            .offset(offset)
        )
        actions: Sequence[Action] = rows.scalars().all()
        return actions

    # --- internals ---------------------------------------------------------

    async def _assert_identity_free(self, identity: str) -> None:
        rows = await self._session.execute(
            self._session.select(Lead)
            .where(Lead.identity_value == identity, Lead.deleted_at.is_(None))
            .limit(1)
        )
        if rows.scalar_one_or_none() is not None:
            raise conflict(
                "duplicate_identity",
                f"A lead with identity {identity!r} already exists in this workspace",
                identity=identity,
            )

    async def _assert_member(self, membership_id: uuid.UUID) -> None:
        member = await self._session.get(Membership, membership_id)
        if member is None:
            raise not_found("Member")

    async def _resolve_initial_stage(self, stage_id: uuid.UUID | None) -> Stage | None:
        if stage_id is not None:
            stage = await self._session.get(Stage, stage_id)
            if stage is None or stage.is_archived:
                raise not_found("Stage")
            return stage
        rows = await self._session.execute(
            self._session.select(Stage)
            .where(Stage.kind == StageKind.INITIAL, Stage.is_archived.is_(False))
            .limit(1)
        )
        initial: Stage | None = rows.scalar_one_or_none()
        return initial

    async def _resolve_lost_reason(
        self, stage: Stage, lost_reason_id: uuid.UUID | None
    ) -> uuid.UUID | None:
        """Entering LOST requires a reason; leaving clears it.

        PROMPTS.md M5 states both halves. Clearing on the way out matters:
        a lead reopened from Lost that kept its reason would show up in "why we
        lose deals" forever.
        """
        if stage.kind is not StageKind.LOST:
            return None

        if lost_reason_id is not None:
            reason = await self._session.get(LostReason, lost_reason_id)
            if reason is None or reason.is_archived:
                raise not_found("Lost reason")
            return reason.id

        rows = await self._session.execute(
            self._session.select(LostReason)
            .where(LostReason.is_default.is_(True), LostReason.is_archived.is_(False))
            .limit(1)
        )
        default: LostReason | None = rows.scalar_one_or_none()
        if default is None:
            raise api_error(
                422,
                "lost_reason_required",
                "Moving a lead to the lost stage requires a lost reason",
            )
        return default.id
