/**
 * API response types.
 *
 * Hand-written for M1. The plan is to generate these from the API's OpenAPI
 * schema once the surface stabilises — until then, `test:contract` in the E2E
 * suite is what keeps them honest against the live schema.
 *
 * Field names stay `snake_case` to match the wire format exactly. Converting
 * at the boundary reads nicer but means every mismatch becomes a silent
 * `undefined` instead of a type error.
 */

export type AvailabilityStatus = 'WORKING' | 'ON_LEAVE' | 'INACTIVE'

export interface UserSummary {
  readonly id: string
  readonly email: string
  readonly full_name: string
  readonly is_active: boolean
}

export interface WorkspaceSummary {
  readonly id: string
  readonly name: string
  readonly slug: string
  readonly timezone: string
  readonly currency: string
  readonly default_country_code: string
}

export interface MembershipSummary {
  readonly id: string
  readonly workspace: WorkspaceSummary
  readonly template_id: string
  readonly template_name: string
  readonly is_active: boolean
  readonly has_license: boolean
  readonly availability: AvailabilityStatus
  readonly manager_id: string | null
}

export interface TokenResponse {
  readonly access_token: string
  readonly refresh_token: string
  readonly token_type: string
  readonly expires_in: number
  readonly user: UserSummary
  readonly memberships: readonly MembershipSummary[]
}

export interface MeResponse {
  readonly user: UserSummary
  readonly memberships: readonly MembershipSummary[]
}

export interface ResolvedPermissions {
  readonly workspace_id: string
  readonly membership_id: string
  readonly template_id: string
  readonly template_name: string
  readonly capabilities: Record<string, Record<string, boolean>>
  readonly visible_membership_ids: readonly string[]
  readonly sees_all_members: boolean
  /** Populated from M4. Present and empty until then. */
  readonly field_grants: Record<string, readonly string[]>
}

export interface WorkspaceDetail {
  readonly id: string
  readonly name: string
  readonly slug: string
  readonly default_country_code: string
  readonly timezone: string
  readonly currency: string
  readonly connected_call_min_seconds: number
  readonly session_timeout_minutes: number | null
  readonly leaderboard_metrics: Record<string, unknown>
  readonly features: Record<string, unknown>
  readonly seat_limit: number
  readonly seats_used: number
  readonly is_active: boolean
  readonly identity_field_id: string | null
  readonly primary_field_1_id: string | null
  readonly primary_field_2_id: string | null
}

export interface MemberDetail {
  readonly id: string
  readonly workspace_id: string
  readonly user: UserSummary
  readonly template_id: string
  readonly template_name: string
  readonly manager_id: string | null
  readonly is_active: boolean
  readonly has_license: boolean
  readonly availability: AvailabilityStatus
  readonly created_at: string
}

export interface Page<T> {
  readonly items: readonly T[]
  readonly total: number
  readonly limit: number
  readonly offset: number
}

export interface SeatUsage {
  readonly seats_used: number
  readonly seat_limit: number
}

export interface AvailabilityLogEntry {
  readonly id: string
  readonly membership_id: string
  readonly status: AvailabilityStatus
  readonly note: string | null
  readonly changed_by_id: string | null
  readonly changed_at: string
}

export interface HierarchyNode {
  readonly member: MemberDetail
  readonly reports: readonly HierarchyNode[]
}

export interface DeactivateResponse {
  readonly member: MemberDetail
  readonly leads_reassigned: number
}

export interface BulkUploadRow {
  readonly row_number: number
  readonly email: string | null
  readonly full_name: string | null
  readonly template_name: string | null
  readonly manager_email: string | null
  readonly status: 'created' | 'skipped' | 'error'
  readonly message: string | null
}

export interface BulkUploadReport {
  readonly dry_run: boolean
  readonly total_rows: number
  readonly created: number
  readonly skipped: number
  readonly errored: number
  readonly rows: readonly BulkUploadRow[]
}

// --- M2: the field definition engine -----------------------------------------

/**
 * One entry of the lead or action field-type registry.
 *
 * Fetched from `/settings/field-types`; never hardcoded here. The frontend
 * knows how to draw a `widget`, which is a product concept, and knows nothing
 * about which *types* exist — that list is the backend's to declare, so adding
 * one is a backend change plus a renderer, never an edit to a TypeScript union.
 */
export interface FieldTypeSpec {
  readonly key: string
  readonly label: string
  readonly description: string
  readonly storage: 'scalar' | 'list' | 'object'
  readonly uses_options: boolean
  readonly operators: readonly string[]
  readonly renderer: FieldRendererContract
  readonly config_schema: Record<string, unknown>
}

/** What the backend tells the frontend about drawing a type. */
export interface FieldRendererContract {
  readonly widget: string
  readonly multiline?: boolean
  readonly multiple?: boolean
  readonly inputMode?: string
  readonly optionsAreTree?: boolean
  readonly valueKey?: string
  readonly parentKey?: string
  readonly startKey?: string
  readonly frequencyKey?: string
  readonly intervalKey?: string
  readonly derivedKey?: string
  readonly frequencies?: readonly string[]
  readonly textKeys?: readonly string[]
  readonly latKey?: string
  readonly lngKey?: string
  readonly amountKey?: string
  readonly currencyKey?: string
}

export interface FieldOption {
  readonly id: string
  readonly code: string
  readonly label: string
  readonly color: string | null
  readonly sort_order: number
  readonly is_archived: boolean
  readonly parent_option_id: string | null
}

export interface LeadField {
  readonly id: string
  readonly key: string
  readonly label: string
  readonly field_type: string
  readonly description: string | null
  readonly is_builtin: boolean
  readonly is_hidden: boolean
  readonly is_required: boolean
  readonly sort_order: number
  readonly field_group: string | null
  readonly show_in_import: boolean
  readonly show_in_quick_add: boolean
  readonly lock_after_create: boolean
  readonly can_use_variable: boolean
  readonly config: Record<string, unknown>
  readonly options: readonly FieldOption[]
  readonly created_at: string
  readonly updated_at: string
}

export interface IndexedField {
  readonly id: string
  readonly field_id: string
  readonly index_name: string
  readonly status: string
  readonly last_error: string | null
  readonly created_at: string
}

// --- M3: pipeline and taxonomy ------------------------------------------------

export type StageKind = 'INITIAL' | 'ACTIVE' | 'WON' | 'LOST'

export interface Stage {
  readonly id: string
  readonly kind: StageKind
  readonly label: string
  readonly color: string
  readonly sort_order: number
  readonly is_archived: boolean
}

/** Grouped, not flat — the settings screen is a three-column pipeline. */
export interface StagePipeline {
  readonly initial: Stage | null
  readonly active: readonly Stage[]
  readonly won: Stage | null
  readonly lost: Stage | null
  readonly archived: readonly Stage[]
}

export interface LostReason {
  readonly id: string
  readonly label: string
  readonly sort_order: number
  readonly is_default: boolean
  readonly is_archived: boolean
}

export interface CallDisposition {
  readonly id: string
  readonly label: string
  readonly is_default: boolean
  readonly is_system: boolean
  readonly is_archived: boolean
  readonly sort_order: number
}

export interface ActionFieldDef {
  readonly id: string
  readonly action_type_id: string
  readonly key: string
  readonly label: string
  readonly field_type: string
  readonly description: string | null
  readonly is_required: boolean
  readonly is_hidden: boolean
  readonly sort_order: number
  readonly config: Record<string, unknown>
  readonly options: readonly {
    readonly id: string
    readonly code: string
    readonly label: string
    readonly color: string | null
  }[]
}

export interface CustomActionType {
  readonly id: string
  readonly code: number
  readonly name: string
  readonly icon: string | null
  readonly score: number
  readonly direction: 'INBOUND' | 'OUTBOUND' | 'INFORMATION'
  readonly description: string | null
  readonly allow_predated: boolean
  readonly is_archived: boolean
  readonly fields: readonly ActionFieldDef[]
  readonly created_at: string
}

export interface WorkspacePreferences {
  readonly default_country_code: string
  readonly timezone: string
  readonly currency: string
  readonly connected_call_min_seconds: number
  readonly session_timeout_minutes: number | null
  readonly leaderboard_metrics: Record<string, unknown>
  readonly features: Record<string, boolean>
}

// --- M4: permissions ----------------------------------------------------------

export interface CapabilityGroupSchema {
  readonly key: string
  /** True when this codebase proposed the contents rather than observing them. */
  readonly proposed: boolean
  readonly capabilities: readonly string[]
}

export interface CapabilitySchema {
  readonly access: readonly CapabilityGroupSchema[]
  readonly view: readonly CapabilityGroupSchema[]
}

/**
 * The capability blob.
 *
 * Deliberately `unknown` at the leaf: the ten Access groups are flat
 * `{capability: boolean}` maps, but `view` nests one level deeper into three
 * sub-groups. A single flat type would describe one shape and lie about the
 * other, so the accessors in the permissions screen narrow it instead.
 */
export type TemplateCapabilities = Record<string, unknown>

/** The list shape of a permission template. Shared by Team and Integrations. */
export interface PermissionTemplateSummary {
  readonly id: string
  readonly name: string
  readonly is_system: boolean
  readonly is_readonly: boolean
}

export interface PermissionTemplateDetail {
  readonly id: string
  readonly name: string
  readonly is_system: boolean
  readonly is_readonly: boolean
  readonly capabilities: TemplateCapabilities
}

export interface FieldGrantRow {
  readonly field_id: string
  readonly key: string
  readonly label: string
  readonly field_type: string
  readonly is_hidden: boolean
  readonly view: boolean
  readonly edit: boolean
  readonly import: boolean
  readonly export: boolean
}

export interface FieldGrantColumn {
  readonly count: number
  readonly total: number
  readonly rollup: 'Full' | 'Partial' | 'None'
}

export interface FieldMatrix {
  readonly fields: readonly FieldGrantRow[]
  readonly columns: Record<string, FieldGrantColumn>
}

export interface LeadViewGroup {
  readonly label: string
  readonly collapsed: boolean
  readonly field_ids: readonly string[]
}

// --- M5: leads, actions, templates --------------------------------------------

/** Option metadata for a stored code, so an archived option still renders. */
export interface OptionLabel {
  readonly code: string
  readonly label: string
  readonly color: string | null
  readonly archived: boolean
}

export interface Lead {
  readonly id: string
  readonly identity_value: string
  readonly primary: { readonly h1: unknown; readonly h2: unknown }
  readonly stage_id: string | null
  readonly lost_reason_id: string | null
  readonly assignee_id: string | null
  readonly rating: number | null
  readonly score: number
  /** View-granted fields only. A field absent here is one the caller cannot see. */
  readonly values: Record<string, unknown>
  readonly labels: Record<string, OptionLabel | readonly OptionLabel[]>
  readonly last_action_at: string | null
  readonly created_at: string
}

export type ActionKind =
  | 'LEAD_CREATED'
  | 'FIELD_CHANGE'
  | 'STAGE_CHANGE'
  | 'ASSIGNMENT_CHANGE'
  | 'RATING_CHANGE'
  | 'NOTE'
  | 'CALL_LOGGED'
  | 'WHATSAPP_SENT'
  | 'EMAIL_SENT'
  | 'SMS_SENT'
  | 'TASK_CREATED'
  | 'TASK_COMPLETED'
  | 'CUSTOM'

export interface LeadAction {
  readonly id: string
  readonly lead_id: string
  readonly changeset_id: string | null
  readonly kind: ActionKind
  readonly action_type_id: string | null
  readonly actor_id: string | null
  readonly payload: Record<string, unknown>
  readonly body: string | null
  readonly score_applied: number
  readonly is_pinned: boolean
  readonly performed_at: string
  readonly created_at: string
}

export interface Changeset {
  readonly id: string
  readonly source: string
  readonly actor_id: string | null
  readonly summary: string
  readonly lead_count: number
  readonly is_undone: boolean
  readonly undone_at?: string | null
  readonly undone_by_id?: string | null
  /** Set when this changeset *is* an undo, naming what it reversed. */
  readonly undo_of_id?: string | null
  readonly created_at: string
}

export type TemplateChannel = 'WHATSAPP' | 'SMS' | 'EMAIL'

export interface MessageTemplate {
  readonly id: string
  readonly channel: TemplateChannel
  readonly name: string
  readonly subject: string | null
  readonly body: string
  readonly owner_id: string | null
  readonly template_id: string | null
  readonly visibility: 'personal' | 'shared' | 'role'
}

export interface RenderedTemplate {
  readonly id: string
  readonly channel: TemplateChannel
  readonly subject: string | null
  readonly body: string
  /** Placeholders that resolved to nothing — including fields the sender cannot view. */
  readonly unresolved: readonly string[]
}

// --- the filter DSL (M6) -----------------------------------------------------
//
// The mirror of `app/filters/dsl.py`. Two families of node in one union: field
// rules ask about a lead's current state, history predicates ask about its
// timeline. The builder renders both as first-class rules — a user never sees
// or edits the JSON these describe.

/** A window, relative or absolute. Exactly one form, matching the server. */
export interface FilterWindow {
  readonly last_days?: number
  readonly from?: string
  readonly to?: string
}

export interface FieldRuleNode {
  readonly type: 'field'
  readonly key: string
  readonly op: string
  readonly value?: unknown
}

export interface ActionPerformedNode {
  readonly type: 'action_performed'
  readonly action_kind?: ActionKind | null
  readonly action_type_id?: string | null
  readonly actor_id?: string | null
  readonly payload_match?: Record<string, unknown>
  readonly min_count?: number
  readonly within?: FilterWindow | null
}

export interface ActionNotPerformedNode {
  readonly type: 'action_not_performed'
  readonly action_kind?: ActionKind | null
  readonly action_type_id?: string | null
  readonly actor_id?: string | null
  readonly payload_match?: Record<string, unknown>
  readonly within?: FilterWindow | null
}

export interface StatusChangedNode {
  readonly type: 'status_changed'
  readonly from_stage_id?: string | null
  readonly to_stage_id?: string | null
  readonly within?: FilterWindow | null
}

export interface AssigneeChangedNode {
  readonly type: 'assignee_changed'
  readonly from_membership_id?: string | null
  readonly to_membership_id?: string | null
  readonly within?: FilterWindow | null
}

export interface GroupNode {
  readonly type: 'group'
  readonly op: 'AND' | 'OR'
  readonly children: readonly FilterNode[]
}

export type HistoryNode =
  ActionPerformedNode | ActionNotPerformedNode | StatusChangedNode | AssigneeChangedNode

export type FilterNode = GroupNode | FieldRuleNode | HistoryNode

export type SavedFilterVisibility = 'PERSONAL' | 'SHARED' | 'ROLE'

export interface SavedFilter {
  readonly id: string
  readonly name: string
  readonly description: string | null
  readonly definition: FilterNode
  readonly visibility: SavedFilterVisibility
  readonly template_id: string | null
  readonly owner_membership_id: string | null
  readonly sort_order: number
  readonly is_archived: boolean
  readonly created_at: string
}

export interface FilterStats {
  readonly filter_id: string
  readonly total: number
  readonly by_stage: Record<string, number>
}

export interface TableLayout {
  readonly id: string
  readonly filter_id: string | null
  readonly columns: readonly string[]
  readonly column_widths: Record<string, number>
  readonly sort_key: string | null
  readonly sort_desc: boolean
}

export interface LeadSearchRequest {
  readonly filter?: FilterNode | null
  readonly q?: string | null
  readonly sort?: string
  readonly limit?: number
  readonly offset?: number
  readonly columns?: readonly string[] | null
  /**
   * Quick filters. Separate from `filter` because these are columns on
   * `leads` — a DSL field rule references a workspace-defined field key by
   * definition, so stage and assignee are out of its reach.
   */
  readonly stage_id?: string | null
  readonly assignee_id?: string | null
  readonly unassigned?: boolean
  readonly rating?: number | null
  readonly stage_kinds?: readonly StageKind[]
}

// --- tasks, labels, undo and imports (M7) ------------------------------------

/** `upcoming` and `late` are computed against the *workspace's* timezone. */
export type TaskBucket = 'upcoming' | 'late' | 'done'

export interface Task {
  readonly id: string
  readonly lead_id: string | null
  readonly title: string
  readonly notes: string | null
  readonly due_at: string
  readonly assignee_id: string | null
  readonly completed_at: string | null
  readonly completed_by_id: string | null
  readonly created_at: string
}

export interface TaskCounts {
  readonly upcoming: number
  readonly late: number
  readonly done: number
}

export interface Label {
  readonly id: string
  readonly name: string
  readonly color: string | null
  readonly sort_order: number
  readonly is_archived: boolean
}

/**
 * One value an undo would put back, and whether it still can.
 *
 * `expected` is what the changeset set; `current` is what the lead holds now.
 * When they differ somebody has edited since, and reverting would discard
 * their work — which is what `conflicted` means.
 */
export interface Reversal {
  readonly target: string
  readonly label: string
  readonly revert_to: unknown
  readonly expected: unknown
  readonly current: unknown
  readonly conflicted: boolean
}

export type UndoOutcome = 'REVERSIBLE' | 'CONFLICTED' | 'ALREADY_UNDONE' | 'DELETED'

export interface LeadUndoPlan {
  readonly lead_id: string
  readonly identity_value: string
  readonly outcome: UndoOutcome
  readonly reversals: readonly Reversal[]
}

export interface UndoPreview {
  readonly changeset_id: string
  readonly summary: string
  readonly is_undone: boolean
  readonly counts: {
    readonly total: number
    readonly reversible: number
    readonly conflicted: number
    readonly deleted: number
  }
  readonly leads: readonly LeadUndoPlan[]
}

export interface UndoResult {
  readonly undo_changeset_id: string
  readonly undone_changeset_id: string
  readonly leads_reverted: number
  readonly leads_skipped: number
  readonly skipped: readonly LeadUndoPlan[]
}

export type ImportJobKind = 'LEAD_IMPORT' | 'LEAD_UPDATE' | 'ACTION_IMPORT' | 'EXPORT'

export type ImportJobStatus =
  'UPLOADED' | 'MAPPED' | 'PREVIEWED' | 'RUNNING' | 'COMPLETED' | 'FAILED'

/** What a dry run found, and later what the commit did. */
export interface ImportResult {
  readonly counts?: Record<string, number>
  readonly total?: number
  readonly errors?: ReadonlyArray<{
    readonly row_number: number
    readonly status: string
    readonly identity: string | null
    readonly message: string | null
  }>
  readonly errors_truncated?: boolean
  readonly columns?: readonly string[]
}

export interface ImportJob {
  readonly id: string
  readonly kind: ImportJobKind
  readonly status: ImportJobStatus
  readonly filename: string
  readonly source_columns: readonly string[]
  readonly mapping: Record<string, string>
  readonly options: Record<string, unknown>
  readonly result: ImportResult
  readonly row_count: number
  readonly changeset_id: string | null
  readonly error: string | null
  readonly created_at: string
}

/** A field the caller may map a column onto. */
export interface ImportableField {
  readonly key: string
  readonly label: string
  readonly field_type: string
}

export interface DuplicateGroup {
  readonly value: string
  readonly lead_ids: readonly string[]
  readonly identity_values: readonly string[]
}

// --- M8: routing and scheduling ---------------------------------------------

export type AssignmentStrategy =
  'ROUND_ROBIN' | 'WEIGHTED' | 'FIELD_VALUE' | 'SALES_GROUP' | 'FIXED' | 'UNASSIGNED'

export interface SalesGroup {
  readonly id: string
  readonly name: string
  readonly description: string | null
  readonly is_archived: boolean
}

export interface SalesGroupMember {
  readonly membership_id: string
  /** A member on weight 3 is dealt three times per round-robin cycle. */
  readonly weight: number
}

export interface AssignmentRule {
  readonly id: string
  readonly name: string
  /** Lowest number wins. */
  readonly priority: number
  readonly strategy: AssignmentStrategy
  /** Strategy-dependent; the server validates it against `strategy`. */
  readonly config: Record<string, unknown>
  /** The M6 filter DSL. `{}` is a catch-all. */
  readonly conditions: Record<string, unknown>
  readonly skip_unavailable: boolean
  readonly is_active: boolean
  readonly created_at: string
}

/** Why the engine decided what it decided. */
export type AssignmentReason =
  'matched' | 'no_rule_matched' | 'no_candidates' | 'rule_assigns_nobody'

export interface AssignmentPreview {
  readonly rule_id: string | null
  readonly rule_name: string | null
  readonly membership_id: string | null
  readonly reason: AssignmentReason
}

export interface DistributionResult {
  /** The undo handle. One redistribution is one changeset. */
  readonly changeset_id: string | null
  readonly assigned: number
  /** Already with that member, so nothing was written. */
  readonly skipped: number
  readonly total: number
}

export type ScheduledReportFormat = 'CSV' | 'XLSX'

export interface ScheduledReport {
  readonly id: string
  readonly name: string
  readonly report_type: string
  /** Five-field cron, evaluated in the workspace's timezone. */
  readonly cron: string
  readonly recipients: readonly string[]
  readonly params: Record<string, unknown>
  readonly format: ScheduledReportFormat
  readonly is_active: boolean
  readonly last_run_at: string | null
  /** Surfaced so a broken schedule is visible instead of failing quietly. */
  readonly last_error: string | null
  /** Whose field permissions the render uses. Null means it cannot run. */
  readonly created_by: string | null
}

export interface RecurringOccurrence {
  readonly lead_id: string
  readonly identity_value: string
  readonly field_key: string
  readonly occurs_on: string
}

// --- M10: intake and the event bus ------------------------------------------

export interface ApiKey {
  readonly id: string
  readonly name: string
  /** The only plaintext kept — identifies a key without reproducing it. */
  readonly prefix: string
  readonly permission_template_id: string
  readonly last_used_at: string | null
  readonly revoked_at: string | null
  readonly created_at: string
}

/** The one and only response that carries the plaintext key. */
export interface ApiKeyCreated extends ApiKey {
  readonly key: string
}

export interface WebhookEndpoint {
  readonly id: string
  readonly name: string
  readonly url: string
  /** Empty means every event. */
  readonly events: readonly string[]
  /** Whose field permissions the payload is projected through. */
  readonly permission_template_id: string
  readonly is_active: boolean
  readonly created_at: string
}

/** Carries the signing secret. Shown once, like a key. */
export interface WebhookCreated extends WebhookEndpoint {
  readonly secret: string
}

export interface WebhookTestResult {
  readonly delivered: boolean
  readonly status_code: number | null
  readonly error: string | null
  readonly signature: string
}

export type OutboxStatus = 'PENDING' | 'DELIVERING' | 'DELIVERED' | 'FAILED' | 'DEAD'

export interface OutboxEvent {
  readonly id: string
  readonly event: string
  /** Stable across retries — the consumer's dedupe key. */
  readonly event_id: string
  readonly endpoint_id: string
  readonly status: OutboxStatus
  readonly attempts: number
  readonly occurred_at: string
  readonly next_attempt_at: string
  readonly last_error: string | null
  readonly last_status_code: number | null
  readonly delivered_at: string | null
}

export type IntakeOutcome = 'CREATED' | 'UPDATED' | 'SKIPPED' | 'REJECTED'

export interface IntakeLogEntry {
  readonly id: string
  readonly api_key_id: string | null
  readonly endpoint: string
  readonly outcome: IntakeOutcome
  readonly status_code: number
  /** Unknown fields stored, quarantined templates, an unrecognised assignee. */
  readonly warnings: readonly string[]
  readonly lead_id: string | null
  readonly error: string | null
  readonly created_at: string
  readonly request_body: Record<string, unknown>
}

// --- M9: reports and dashboards ---------------------------------------------

/**
 * One bar.
 *
 * `key` is what a drill-through filters on; `label` is what a person reads.
 * They differ for a stage (uuid vs name) and coincide for a field value.
 */
export interface Bucket {
  readonly key: string
  readonly label: string
  readonly count: number
}

export interface FollowUpCounts {
  readonly late: number
  readonly upcoming: number
  readonly never_contacted: number
}

export interface LeaderboardRow {
  readonly membership_id: string
  readonly name: string
  /** Only the metrics this workspace turned on, plus leads and calls. */
  readonly metrics: Record<string, number>
}

/** A widget the editor can render a config form for without knowing it. */
export interface WidgetSpec {
  readonly key: string
  readonly label: string
  readonly description: string
  /** stats | bar | funnel | table */
  readonly shape: string
  readonly config: Record<
    string,
    { type: string; label: string; required?: boolean; help?: string }
  >
  readonly default_size: { w: number; h: number }
}

export interface DashboardWidget {
  readonly widget: string
  readonly x: number
  readonly y: number
  readonly w: number
  readonly h: number
  readonly config?: Record<string, string>
}

export interface Dashboard {
  readonly id: string
  readonly name: string
  /** Null means shared with the whole workspace. */
  readonly owner_id: string | null
  /** Non-null means everyone on that permission template gets it. */
  readonly template_id: string | null
  readonly layout: readonly DashboardWidget[]
  readonly is_default: boolean
  readonly created_at: string
}

/**
 * `POST /workspaces/{id}/voice/calls` — ask the CRM to place a Bolna call.
 *
 * Exactly one of `lead_id` or `phone` is required. The CRM UI always sends
 * `lead_id`: the backend resolves the lead, projects the fields this caller
 * may view, and assembles the Bolna payload itself. A browser that assembled
 * that payload would be reimplementing the permission projection outside the
 * one place that owns it.
 */
export interface VoiceCallTrigger {
  readonly lead_id?: string
  readonly phone?: string
  /** Overrides the deployment's configured agent for this one call. */
  readonly agent_id?: string
}

/**
 * What the CRM answers once it has spoken to Bolna.
 *
 * **A 2xx does not mean the call was placed.** The trigger records the attempt
 * before dialling and reports a vendor refusal in `error` with `status`
 * `FAILED`, so callers must read those rather than treating the response code
 * as the outcome.
 */
export interface VoiceCallTriggerResult {
  /** The CRM's own row id for the attempt. */
  readonly call_id: string
  readonly lead_id: string
  /** Bolna's execution id — the join key for the completion webhook. */
  readonly execution_id: string | null
  /** The CRM's view: QUEUED, DISPATCHED, COMPLETED, FAILED. */
  readonly status: string
  /** Bolna's own status string, passed through unmapped. */
  readonly bolna_status: string | null
  readonly agent_id: string | null
  readonly recipient_phone: string
  /**
   * The exact `user_data` sent to Bolna: projected, rendered lead values plus
   * the reserved `crm_*` keys. Carries no credential by construction — the
   * Bolna key never leaves the server.
   */
  readonly user_data: Record<string, unknown>
  /** What this call was told about the previous one. */
  readonly previous_call_summary: string | null
  readonly call_count: number
  readonly error: string | null
}
