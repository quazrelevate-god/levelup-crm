/**
 * Which columns the product itself owns.
 *
 * Split out of `LeadTable` so that file exports only components: a module that
 * mixes constants and components breaks React Fast Refresh, which silently
 * turns every edit into a full reload.
 *
 * Everything not listed here is a `lead_fields.key` — a column some workspace
 * admin invented — and is resolved against the field list at render time.
 */

export interface BuiltinColumn {
  readonly id: string
  readonly label: string
}

export const BUILTIN_COLUMNS: readonly BuiltinColumn[] = [
  { id: 'identity_value', label: 'Identifier' },
  { id: 'stage_id', label: 'Stage' },
  { id: 'assignee_id', label: 'Assignee' },
  { id: 'score', label: 'Score' },
  { id: 'rating', label: 'Rating' },
  { id: 'last_action_at', label: 'Last activity' },
  { id: 'created_at', label: 'Created' },
]

export const BUILTIN_COLUMN_IDS: ReadonlySet<string> = new Set(
  BUILTIN_COLUMNS.map((column) => column.id),
)

/** What a member sees before they have chosen anything. */
export const DEFAULT_COLUMNS: readonly string[] = [
  'identity_value',
  'stage_id',
  'rating',
  'assignee_id',
  'last_action_at',
]
