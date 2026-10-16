"""Pipeline and taxonomy administration (M3).

Stages, lost reasons, call dispositions and custom action types. Everything a
workspace calls its own.

Two rules are enforced by the database rather than here, and this service's job
is to turn their constraint violations into the documented errors:

- stage cardinality  -> `409 stage_cardinality`
- one default disposition -> handled by clearing the previous default first

Everything else — the 25-reason cap, the system/custom tier, the sequential
action code — is enforced here, because each needs a message rather than a
constraint violation.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.errors import api_error, conflict, not_found
from app.models.enums import ActionDirection, ActionFieldType, StageKind
from app.models.field import ActionField, ActionFieldOption, CustomActionType
from app.models.pipeline import (
    MAX_LOST_REASONS,
    MAX_STAGE_LABEL,
    CallDisposition,
    LostReason,
    Stage,
)
from app.models.workspace import Workspace
from app.services.voice_settings import (
    OUTCOME_ANSWERED,
    OUTCOME_UNANSWERED,
    clear_outcome_stages,
    read_outcome_stages,
    write_outcome_stages,
)
from app.tenancy.session import ScopedSession

__all__ = ["CustomActionService", "DispositionService", "PipelineService"]

#: The kinds a workspace may have only one live instance of.
SINGLETON_KINDS = (StageKind.INITIAL, StageKind.WON, StageKind.LOST)

#: docs/03-configuration-model.md §4.1 — "workspace-scoped sequential integers
#: starting at 1001".
FIRST_ACTION_CODE = 1001


class PipelineService:
    """Stages and lost reasons."""

    def __init__(self, session: ScopedSession) -> None:
        self._session = session

    # --- stages ------------------------------------------------------------

    async def list_stages(self, *, include_archived: bool = False) -> Sequence[Stage]:
        statement = self._session.select(Stage).order_by(Stage.sort_order, Stage.label)
        if not include_archived:
            statement = statement.where(Stage.is_archived.is_(False))
        rows = await self._session.execute(statement)
        stages: Sequence[Stage] = rows.scalars().all()
        return stages

    async def get_stage(self, stage_id: uuid.UUID) -> Stage:
        stage = await self._session.get(Stage, stage_id)
        if stage is None:
            raise not_found("Stage")
        return stage

    async def create_stage(self, *, label: str, color: str | None = None) -> Stage:
        """Create an ACTIVE stage.

        `kind` is not a parameter. The three singleton kinds arrive with
        provisioning and are renamed, never created — offering `kind` here
        would invite a second WON stage and turn the partial unique index into
        a 500 rather than a validation message.
        """
        label = self._validate_label(label)
        stage = Stage(
            kind=StageKind.ACTIVE,
            label=label,
            color=color or "#6b7280",
            sort_order=await self._next_stage_order(),
        )
        self._session.add(stage)
        await self._flush_translating_cardinality()
        return stage

    async def update_stage(
        self, stage_id: uuid.UUID, *, label: str | None = None, color: str | None = None
    ) -> Stage:
        """Rename or recolour. Every kind may be renamed, including the
        singletons — that is how a workspace turns "Won" into "Enrolled"."""
        stage = await self.get_stage(stage_id)
        if label is not None:
            stage.label = self._validate_label(label)
        if color is not None:
            stage.color = color
        await self._session.flush()
        return stage

    async def archive_stage(self, stage_id: uuid.UUID) -> Stage:
        """Archive an ACTIVE stage.

        The singletons cannot be archived: a pipeline with no won state is not
        a pipeline. §2.1 lists their operations as rename and recolour only.
        """
        stage = await self.get_stage(stage_id)
        if stage.kind is not StageKind.ACTIVE:
            raise conflict(
                "stage_cardinality",
                f"The {stage.kind.value.lower()} stage cannot be removed. "
                f"A pipeline needs exactly one of it.",
                kind=stage.kind.value,
            )
        stage.is_archived = True
        await self._session.flush()
        return stage

    # --- which stages a finished AI call implies -----------------------------

    async def voice_outcome_stages(self, workspace: Workspace) -> tuple[Stage | None, Stage | None]:
        """The configured pair, resolved to stage rows, or `(None, None)`.

        A configured id that has since been deleted or archived reads as absent
        rather than raising: the mapping describes intent, and the automation
        already declines to move a lead when the stage is gone.
        """
        answered_id, unanswered_id = read_outcome_stages(workspace.settings)

        async def one(stage_id: uuid.UUID | None) -> Stage | None:
            if stage_id is None:
                return None
            stage = await self._session.get(Stage, stage_id)
            return stage if stage is not None and not stage.is_archived else None

        return await one(answered_id), await one(unanswered_id)

    async def set_voice_outcome_stages(
        self, workspace: Workspace, *, answered: uuid.UUID, unanswered: uuid.UUID
    ) -> tuple[Stage, Stage]:
        """Validate both stages, then store the pair.

        Every rejection happens before anything is written, so the mapping is
        either wholly valid or wholly unchanged:

        - the two ids must differ — one stage for both outcomes cannot be acted
          on, and the automation refuses it anyway;
        - each must exist *in this workspace*. `ScopedSession.get` returns
          `None` for another tenant's id exactly as for one that does not
          exist, so a cross-workspace id is a 404 and reveals nothing;
        - each must be a live `ACTIVE` stage. The initial, won and lost
          singletons are the pipeline's own ends; moving a lead into them is a
          decision about the deal, not a record of a phone call.

        Only this one namespace in `workspace.settings` is replaced; every
        other key is preserved.
        """
        if answered == unanswered:
            raise api_error(
                422,
                "stage_mapping_ambiguous",
                "The answered and unanswered outcomes need two different stages.",
                stage_id=str(answered),
            )

        resolved: dict[str, Stage] = {}
        for outcome, stage_id in ((OUTCOME_ANSWERED, answered), (OUTCOME_UNANSWERED, unanswered)):
            stage = await self.get_stage(stage_id)  # 404 for absent or another workspace's
            if stage.is_archived:
                raise api_error(
                    422,
                    "stage_archived",
                    f"The {outcome} stage is archived. Choose a live stage.",
                    outcome=outcome,
                    stage_id=str(stage_id),
                )
            if stage.kind is not StageKind.ACTIVE:
                raise api_error(
                    422,
                    "stage_kind_not_active",
                    f"The {outcome} stage must be an active stage, not the "
                    f"{stage.kind.value.lower()} one.",
                    outcome=outcome,
                    stage_id=str(stage_id),
                    kind=stage.kind.value,
                )
            resolved[outcome] = stage

        workspace.settings = write_outcome_stages(
            workspace.settings, answered=answered, unanswered=unanswered
        )
        await self._session.flush()
        return resolved[OUTCOME_ANSWERED], resolved[OUTCOME_UNANSWERED]

    async def clear_voice_outcome_stages(self, workspace: Workspace) -> None:
        """Stop the behaviour, leaving every other setting intact."""
        workspace.settings = clear_outcome_stages(workspace.settings)
        await self._session.flush()

    async def reorder_stages(self, *, ordered_ids: Sequence[uuid.UUID]) -> Sequence[Stage]:
        """Reorder the ACTIVE band. The singletons bracket it and do not move."""
        stages = {s.id: s for s in await self.list_stages()}
        for stage_id in ordered_ids:
            stage = stages.get(stage_id)
            if stage is None:
                raise api_error(422, "unknown_stage", "One or more stages are not in this pipeline")
            if stage.kind is not StageKind.ACTIVE:
                raise api_error(
                    422,
                    "stage_not_reorderable",
                    f"The {stage.kind.value.lower()} stage has a fixed position",
                )
        for position, stage_id in enumerate(ordered_ids):
            stages[stage_id].sort_order = position
        await self._session.flush()
        return await self.list_stages()

    # --- lost reasons ------------------------------------------------------

    async def list_lost_reasons(self, *, include_archived: bool = False) -> Sequence[LostReason]:
        statement = self._session.select(LostReason).order_by(LostReason.sort_order)
        if not include_archived:
            statement = statement.where(LostReason.is_archived.is_(False))
        rows = await self._session.execute(statement)
        reasons: Sequence[LostReason] = rows.scalars().all()
        return reasons

    async def create_lost_reason(self, *, label: str) -> LostReason:
        label = label.strip()
        if not 1 <= len(label) <= 80:
            raise api_error(422, "invalid_label", "A lost reason must be 1-80 characters")

        live = await self.list_lost_reasons()
        if len(live) >= MAX_LOST_REASONS:
            # A clear message, not a constraint violation (§2.1).
            raise conflict(
                "lost_reason_limit",
                f"A workspace may have at most {MAX_LOST_REASONS} lost reasons. "
                f"Archive one before adding another.",
                limit=MAX_LOST_REASONS,
                used=len(live),
            )

        reason = LostReason(label=label, sort_order=len(live))
        self._session.add(reason)
        await self._session.flush()
        return reason

    async def update_lost_reason(
        self, reason_id: uuid.UUID, *, label: str | None = None, sort_order: int | None = None
    ) -> LostReason:
        reason = await self._session.get(LostReason, reason_id)
        if reason is None:
            raise not_found("Lost reason")
        if label is not None:
            label = label.strip()
            if not 1 <= len(label) <= 80:
                raise api_error(422, "invalid_label", "A lost reason must be 1-80 characters")
            reason.label = label
        if sort_order is not None:
            reason.sort_order = sort_order
        await self._session.flush()
        return reason

    async def archive_lost_reason(self, reason_id: uuid.UUID) -> LostReason:
        reason = await self._session.get(LostReason, reason_id)
        if reason is None:
            raise not_found("Lost reason")
        reason.is_archived = True
        await self._session.flush()
        return reason

    # --- internals ---------------------------------------------------------

    @staticmethod
    def _validate_label(label: str) -> str:
        label = label.strip()
        if not 1 <= len(label) <= MAX_STAGE_LABEL:
            raise api_error(
                422, "invalid_label", f"A stage name must be 1-{MAX_STAGE_LABEL} characters"
            )
        return label

    async def _next_stage_order(self) -> int:
        result = await self._session.execute(
            select(func.coalesce(func.max(Stage.sort_order), -1) + 1).where(
                Stage.workspace_id == self._session.workspace_id
            )
        )
        return int(result.scalar_one())

    async def _flush_translating_cardinality(self) -> None:
        """Turn `stages_singleton_uq` into the documented 409.

        The index is the real enforcement — it is the only check two concurrent
        requests cannot both pass.
        """
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if "stages_singleton_uq" in str(exc.orig):
                raise conflict(
                    "stage_cardinality",
                    "A workspace has exactly one initial, one won and one lost stage.",
                ) from exc
            raise


class DispositionService:
    """Call dispositions (§3)."""

    def __init__(self, session: ScopedSession) -> None:
        self._session = session

    async def list_dispositions(
        self, *, include_archived: bool = False
    ) -> Sequence[CallDisposition]:
        statement = self._session.select(CallDisposition).order_by(CallDisposition.sort_order)
        if not include_archived:
            statement = statement.where(CallDisposition.is_archived.is_(False))
        rows = await self._session.execute(statement)
        dispositions: Sequence[CallDisposition] = rows.scalars().all()
        return dispositions

    async def get_disposition(self, disposition_id: uuid.UUID) -> CallDisposition:
        disposition = await self._session.get(CallDisposition, disposition_id)
        if disposition is None:
            raise not_found("Call disposition")
        return disposition

    async def create_disposition(self, *, label: str) -> CallDisposition:
        label = label.strip()
        if not 1 <= len(label) <= 80:
            raise api_error(422, "invalid_label", "A disposition must be 1-80 characters")
        live = await self.list_dispositions()
        disposition = CallDisposition(label=label, is_system=False, sort_order=len(live))
        self._session.add(disposition)
        await self._session.flush()
        return disposition

    async def update_disposition(
        self, disposition_id: uuid.UUID, *, label: str | None = None, sort_order: int | None = None
    ) -> CallDisposition:
        """Rename a custom disposition. System entries refuse.

        Observed verbatim in the source system: "can't edit system generated".
        """
        disposition = await self.get_disposition(disposition_id)
        if disposition.is_system and label is not None:
            raise api_error(
                403,
                "system_disposition",
                f"{disposition.label} is a system status and cannot be renamed. "
                f"It can be archived instead.",
            )
        if label is not None:
            label = label.strip()
            if not 1 <= len(label) <= 80:
                raise api_error(422, "invalid_label", "A disposition must be 1-80 characters")
            disposition.label = label
        if sort_order is not None:
            disposition.sort_order = sort_order
        await self._session.flush()
        return disposition

    async def set_default(self, disposition_id: uuid.UUID) -> CallDisposition:
        """Make this the default, clearing the previous one first.

        Order matters: the partial unique index permits exactly one live
        default, so the clear has to land before the set within the same
        transaction.
        """
        disposition = await self.get_disposition(disposition_id)
        if disposition.is_archived:
            raise conflict("disposition_archived", "An archived status cannot be the default")

        for existing in await self.list_dispositions():
            if existing.is_default and existing.id != disposition.id:
                existing.is_default = False
        await self._session.flush()

        disposition.is_default = True
        await self._session.flush()
        return disposition

    async def archive_disposition(self, disposition_id: uuid.UUID) -> CallDisposition:
        """Archive — allowed on system entries too (§3).

        The default cannot be archived without another taking its place: a
        workspace with no default has no value to preselect on the call form.
        """
        disposition = await self.get_disposition(disposition_id)
        if disposition.is_default:
            raise conflict(
                "default_disposition",
                "Set another status as the default before archiving this one.",
            )
        disposition.is_archived = True
        await self._session.flush()
        return disposition


class CustomActionService:
    """Custom action types and their nested fields (§4).

    The nested field builder reuses M2's action-field registry and the same
    `ActionField` / `ActionFieldOption` tables — this service supplies the
    action-type rules, not a second field engine.
    """

    def __init__(self, session: ScopedSession) -> None:
        self._session = session

    async def list_types(
        self, *, include_archived: bool = False, search: str | None = None
    ) -> Sequence[CustomActionType]:
        statement = (
            self._session.select(CustomActionType)
            .options(selectinload(CustomActionType.fields).selectinload(ActionField.options))
            .order_by(CustomActionType.code)
        )
        if not include_archived:
            statement = statement.where(CustomActionType.is_archived.is_(False))
        if search:
            # Search by name or code — the list screen offers both.
            statement = statement.where(CustomActionType.name.ilike(f"%{search}%"))
        rows = await self._session.execute(statement)
        types: Sequence[CustomActionType] = rows.scalars().unique().all()
        return types

    async def get_type(self, type_id: uuid.UUID) -> CustomActionType:
        rows = await self._session.execute(
            self._session.select(CustomActionType)
            .where(CustomActionType.id == type_id)
            .options(selectinload(CustomActionType.fields).selectinload(ActionField.options))
            .limit(1)
        )
        action_type: CustomActionType | None = rows.scalar_one_or_none()
        if action_type is None:
            raise not_found("Custom action")
        return action_type

    async def create_type(
        self,
        *,
        name: str,
        icon: str | None = None,
        score: int = 0,
        direction: ActionDirection = ActionDirection.INFORMATION,
        description: str | None = None,
        allow_predated: bool = False,
    ) -> CustomActionType:
        """Create a custom action type with the next workspace code.

        Every action starts with one field — `Notes` (Text, required) — exactly
        as the source system does (§4.2).
        """
        name = name.strip()
        if not 1 <= len(name) <= 80:
            raise api_error(422, "invalid_label", "An action name must be 1-80 characters")
        if not -1000 <= score <= 1000:
            raise api_error(422, "score_out_of_range", "Score must be between -1000 and 1000")

        action_type = CustomActionType(
            code=await self._next_code(),
            name=name,
            icon=icon,
            score=score,
            direction=direction,
            description=description,
            allow_predated=allow_predated,
        )
        self._session.add(action_type)
        await self._session.flush()

        self._session.add(
            ActionField(
                action_type_id=action_type.id,
                key="notes",
                label="Notes",
                field_type=ActionFieldType.TEXT,
                is_required=True,
                sort_order=0,
            )
        )
        await self._session.flush()
        return action_type

    async def update_type(
        self,
        type_id: uuid.UUID,
        *,
        name: str | None = None,
        icon: str | None = None,
        score: int | None = None,
        direction: ActionDirection | None = None,
        description: str | None = None,
        allow_predated: bool | None = None,
    ) -> CustomActionType:
        """Update the definition. `code` is absent — it is the stable handle
        reports and filters refer to."""
        action_type = await self.get_type(type_id)
        if name is not None:
            name = name.strip()
            if not 1 <= len(name) <= 80:
                raise api_error(422, "invalid_label", "An action name must be 1-80 characters")
            action_type.name = name
        if icon is not None:
            action_type.icon = icon or None
        if score is not None:
            if not -1000 <= score <= 1000:
                raise api_error(422, "score_out_of_range", "Score must be between -1000 and 1000")
            # Changing the score does not rewrite history: M5 snapshots
            # `score_applied` on each action at write time.
            action_type.score = score
        if direction is not None:
            action_type.direction = direction
        if description is not None:
            action_type.description = description
        if allow_predated is not None:
            action_type.allow_predated = allow_predated
        await self._session.flush()
        return action_type

    async def archive_type(self, type_id: uuid.UUID) -> CustomActionType:
        action_type = await self.get_type(type_id)
        action_type.is_archived = True
        await self._session.flush()
        return action_type

    # --- nested action fields ----------------------------------------------

    async def add_field(
        self,
        type_id: uuid.UUID,
        *,
        label: str,
        field_type: ActionFieldType,
        description: str | None = None,
        is_required: bool = False,
        options: Sequence[tuple[str, str | None]] = (),
    ) -> ActionField:
        """Add a field to an action's form, reusing the M2 builder.

        Same slug derivation, same validation contract, different registry —
        `docs/03-configuration-model.md` §4.3: "Build one field-definition
        system with two registries, not two systems."
        """
        from app.fields.registry import ACTION_FIELD_TYPES
        from app.fields.values import slugify_key

        action_type = await self.get_type(type_id)
        label = label.strip()
        if not 1 <= len(label) <= 40:
            raise api_error(422, "invalid_label", "A field name must be 1-40 characters")

        taken = {f.key for f in action_type.fields}
        field = ActionField(
            action_type_id=type_id,
            key=slugify_key(label, taken=taken),
            label=label,
            field_type=field_type,
            description=description,
            is_required=is_required,
            sort_order=len(action_type.fields),
        )
        self._session.add(field)
        await self._session.flush()

        if options and not ACTION_FIELD_TYPES[field_type].uses_options:
            raise api_error(
                422,
                "type_has_no_options",
                f"A {field_type.value} field does not have options",
            )
        for order, (option_label, color) in enumerate(options):
            self._session.add(
                ActionFieldOption(
                    action_field_id=field.id,
                    code=slugify_key(option_label),
                    label=option_label,
                    color=color,
                    sort_order=order,
                )
            )
        await self._session.flush()
        return field

    async def update_field(
        self,
        type_id: uuid.UUID,
        field_id: uuid.UUID,
        *,
        label: str | None = None,
        description: str | None = None,
        is_required: bool | None = None,
        is_hidden: bool | None = None,
        sort_order: int | None = None,
    ) -> ActionField:
        await self.get_type(type_id)
        field = await self._session.get(ActionField, field_id)
        if field is None or field.action_type_id != type_id:
            raise not_found("Action field")
        if label is not None:
            field.label = label.strip()
        if description is not None:
            field.description = description
        if is_required is not None:
            field.is_required = is_required
        if is_hidden is not None:
            field.is_hidden = is_hidden
        if sort_order is not None:
            field.sort_order = sort_order
        await self._session.flush()
        return field

    async def hide_field(self, type_id: uuid.UUID, field_id: uuid.UUID) -> ActionField:
        """Hide rather than delete — the same reasoning as lead fields: values
        are already stored under this key in existing actions."""
        return await self.update_field(type_id, field_id, is_hidden=True)

    async def _next_code(self) -> int:
        """Workspace-sequential, from 1001 (§4.1)."""
        result = await self._session.execute(
            select(func.coalesce(func.max(CustomActionType.code), FIRST_ACTION_CODE - 1) + 1).where(
                CustomActionType.workspace_id == self._session.workspace_id
            )
        )
        return int(result.scalar_one())
