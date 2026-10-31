/**
 * The visual filter builder (M6).
 *
 * Two things this component exists to guarantee:
 *
 * 1. **Users never see raw JSON.** Every node — including the four history
 *    predicates — renders as a row of labelled controls. The DSL document is an
 *    implementation detail of the wire, not something anyone types.
 * 2. **Operators come from the registry.** When you pick a field, the operator
 *    list is whatever `GET /settings/field-types` said that field's type
 *    supports. There is no table of operators in this file, because a second
 *    table would disagree with the first one inside a milestone.
 *
 * `04-feature-coverage.md` calls history filtering "the worst miss in the
 * audit": four of the ten filters people actually used ask about a lead's
 * timeline rather than its current state. So "no outgoing call in 14 days" is a
 * rule you build from dropdowns here, exactly like "city is Chennai" — not an
 * advanced mode, not a JSON escape hatch.
 */

import { useMemo } from 'react'

import type {
  ActionKind,
  CustomActionType,
  FieldTypeSpec,
  FilterNode,
  FilterWindow,
  GroupNode,
  LeadField,
  MemberDetail,
  Stage,
} from '@/api/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { FieldInput } from '@/features/fields/FieldInput'

/** How each operator reads in a sentence. Presentation only — the *set* of
 * operators offered always comes from the registry. */
const OPERATOR_LABELS: Record<string, string> = {
  eq: 'is',
  ne: 'is not',
  contains: 'contains',
  not_contains: 'does not contain',
  starts_with: 'starts with',
  ends_with: 'ends with',
  in: 'is any of',
  not_in: 'is none of',
  gt: 'is greater than',
  gte: 'is at least',
  lt: 'is less than',
  lte: 'is at most',
  between: 'is between',
  is_empty: 'is empty',
  is_not_empty: 'is not empty',
  has_any: 'has any of',
  has_all: 'has all of',
  in_last_days: 'in the last (days)',
  in_next_days: 'in the next (days)',
}

/** Operators that take no operand, so the value control is hidden entirely. */
const NULLARY = new Set(['is_empty', 'is_not_empty'])

/** The timeline events worth offering as a filter.
 *
 * A subset of `ActionKind` on purpose: filtering on FIELD_CHANGE or
 * LEAD_CREATED is expressible in the DSL but answers a question nobody asks of
 * a worklist, and a picker with thirteen entries buries the four that matter.
 */
const FILTERABLE_KINDS: ReadonlyArray<{ kind: ActionKind; label: string }> = [
  { kind: 'CALL_LOGGED', label: 'a call' },
  { kind: 'NOTE', label: 'a note' },
  { kind: 'WHATSAPP_SENT', label: 'a WhatsApp message' },
  { kind: 'EMAIL_SENT', label: 'an email' },
  { kind: 'SMS_SENT', label: 'an SMS' },
  { kind: 'CUSTOM', label: 'a custom action' },
]

const CALL_DIRECTIONS = [
  { value: '', label: 'in any direction' },
  { value: 'OUTGOING', label: 'outgoing' },
  { value: 'INCOMING', label: 'incoming' },
  { value: 'MISSED', label: 'missed' },
]

export interface FilterBuilderProps {
  readonly node: GroupNode
  readonly fields: readonly LeadField[]
  readonly fieldTypes: readonly FieldTypeSpec[]
  readonly stages: readonly Stage[]
  readonly members: readonly MemberDetail[]
  readonly customActions: readonly CustomActionType[]
  readonly onChange: (next: GroupNode) => void
  /** What an empty rule means here. Defaults to the saved-filter meaning. */
  readonly emptyText?: string
}

function emptyGroup(): GroupNode {
  return { type: 'group', op: 'AND', children: [] }
}

function replaceChild(group: GroupNode, index: number, child: FilterNode): GroupNode {
  const children = group.children.slice()
  children[index] = child
  return { ...group, children }
}

function removeChild(group: GroupNode, index: number): GroupNode {
  return { ...group, children: group.children.filter((_, i) => i !== index) }
}

/** "in the last N days" or a date range — the two shapes `Within` accepts. */
function WindowControl({
  value,
  onChange,
}: {
  readonly value: FilterWindow | null | undefined
  readonly onChange: (next: FilterWindow | null) => void
}) {
  const mode = value == null ? 'any' : value.last_days != null ? 'relative' : 'absolute'

  return (
    <span className="flex flex-wrap items-center gap-2">
      <Select
        aria-label="Time window"
        className="w-40"
        value={mode}
        onChange={(event) => {
          const next = event.target.value
          if (next === 'any') onChange(null)
          else if (next === 'relative') onChange({ last_days: 14 })
          else onChange({ from: '', to: '' })
        }}
      >
        <option value="any">at any time</option>
        <option value="relative">in the last…</option>
        <option value="absolute">between…</option>
      </Select>

      {mode === 'relative' && (
        <span className="flex items-center gap-2">
          <Input
            aria-label="Number of days"
            className="w-20"
            type="number"
            min={1}
            value={value?.last_days ?? 14}
            onChange={(event) => onChange({ last_days: Number(event.target.value) || 1 })}
          />
          <span className="text-muted-foreground text-sm">days</span>
        </span>
      )}

      {mode === 'absolute' && (
        <span className="flex items-center gap-2">
          <Input
            aria-label="From date"
            className="w-40"
            type="date"
            value={value?.from ?? ''}
            onChange={(event) => onChange({ ...value, from: event.target.value })}
          />
          <span className="text-muted-foreground text-sm">to</span>
          <Input
            aria-label="To date"
            className="w-40"
            type="date"
            value={value?.to ?? ''}
            onChange={(event) => onChange({ ...value, to: event.target.value })}
          />
        </span>
      )}
    </span>
  )
}

function FieldRuleRow({
  node,
  fields,
  fieldTypes,
  onChange,
}: {
  readonly node: Extract<FilterNode, { type: 'field' }>
  readonly fields: readonly LeadField[]
  readonly fieldTypes: readonly FieldTypeSpec[]
  readonly onChange: (next: FilterNode) => void
}) {
  const field = fields.find((candidate) => candidate.key === node.key) ?? fields[0]
  const spec = fieldTypes.find((candidate) => candidate.key === field?.field_type)
  // Straight from the registry. A DATE field offers temporal operators and a
  // TAGS field offers set operators because the server said so.
  const operators = spec?.operators ?? []

  return (
    <>
      <Select
        aria-label="Field"
        className="w-48"
        value={node.key}
        onChange={(event) => {
          const nextField = fields.find((candidate) => candidate.key === event.target.value)
          const nextSpec = fieldTypes.find((c) => c.key === nextField?.field_type)
          // The old operator may not exist on the new type, so fall back to the
          // first one the new type offers rather than sending a rule the server
          // will refuse.
          const nextOp = nextSpec?.operators.includes(node.op)
            ? node.op
            : (nextSpec?.operators[0] ?? 'eq')
          onChange({ type: 'field', key: event.target.value, op: nextOp, value: null })
        }}
      >
        {fields.map((candidate) => (
          <option key={candidate.key} value={candidate.key}>
            {candidate.label}
          </option>
        ))}
      </Select>

      <Select
        aria-label="Operator"
        className="w-44"
        value={node.op}
        onChange={(event) => onChange({ ...node, op: event.target.value, value: null })}
      >
        {operators.map((operator) => (
          <option key={operator} value={operator}>
            {OPERATOR_LABELS[operator] ?? operator}
          </option>
        ))}
      </Select>

      {!NULLARY.has(node.op) && field && spec && (
        <span className="min-w-48 flex-1">
          {/* The same renderer the lead form uses, so a DROPDOWN filter offers
              the workspace's own options rather than a free-text box. */}
          <FieldInput
            field={field}
            renderer={spec.renderer}
            value={node.value ?? null}
            onChange={(value) => onChange({ ...node, value })}
          />
        </span>
      )}
    </>
  )
}

function ActionRow({
  node,
  customActions,
  onChange,
}: {
  readonly node: Extract<FilterNode, { type: 'action_performed' | 'action_not_performed' }>
  readonly customActions: readonly CustomActionType[]
  readonly onChange: (next: FilterNode) => void
}) {
  const direction = (node.payload_match?.direction as string | undefined) ?? ''

  return (
    <>
      <Select
        aria-label="Action"
        className="w-48"
        value={node.action_kind ?? ''}
        onChange={(event) =>
          onChange({
            ...node,
            action_kind: (event.target.value || null) as ActionKind | null,
            action_type_id: null,
          })
        }
      >
        <option value="">any activity</option>
        {FILTERABLE_KINDS.map((entry) => (
          <option key={entry.kind} value={entry.kind}>
            {entry.label}
          </option>
        ))}
      </Select>

      {node.action_kind === 'CUSTOM' && (
        <Select
          aria-label="Custom action type"
          className="w-48"
          value={node.action_type_id ?? ''}
          onChange={(event) => onChange({ ...node, action_type_id: event.target.value || null })}
        >
          <option value="">any type</option>
          {customActions.map((type) => (
            <option key={type.id} value={type.id}>
              {type.name}
            </option>
          ))}
        </Select>
      )}

      {node.action_kind === 'CALL_LOGGED' && (
        <Select
          aria-label="Call direction"
          className="w-40"
          value={direction}
          onChange={(event) => {
            const next = { ...(node.payload_match ?? {}) }
            if (event.target.value) next.direction = event.target.value
            else delete next.direction
            onChange({ ...node, payload_match: next })
          }}
        >
          {CALL_DIRECTIONS.map((entry) => (
            <option key={entry.value} value={entry.value}>
              {entry.label}
            </option>
          ))}
        </Select>
      )}

      {node.type === 'action_performed' && (
        <span className="flex items-center gap-2">
          <span className="text-muted-foreground text-sm">at least</span>
          <Input
            aria-label="Minimum count"
            className="w-20"
            type="number"
            min={1}
            value={node.min_count ?? 1}
            onChange={(event) => onChange({ ...node, min_count: Number(event.target.value) || 1 })}
          />
          <span className="text-muted-foreground text-sm">times</span>
        </span>
      )}

      <WindowControl value={node.within} onChange={(within) => onChange({ ...node, within })} />
    </>
  )
}

function StatusChangedRow({
  node,
  stages,
  onChange,
}: {
  readonly node: Extract<FilterNode, { type: 'status_changed' }>
  readonly stages: readonly Stage[]
  readonly onChange: (next: FilterNode) => void
}) {
  return (
    <>
      <span className="text-muted-foreground text-sm">from</span>
      <Select
        aria-label="From stage"
        className="w-40"
        value={node.from_stage_id ?? ''}
        onChange={(event) => onChange({ ...node, from_stage_id: event.target.value || null })}
      >
        <option value="">any stage</option>
        {stages.map((stage) => (
          <option key={stage.id} value={stage.id}>
            {stage.label}
          </option>
        ))}
      </Select>

      <span className="text-muted-foreground text-sm">to</span>
      <Select
        aria-label="To stage"
        className="w-40"
        value={node.to_stage_id ?? ''}
        onChange={(event) => onChange({ ...node, to_stage_id: event.target.value || null })}
      >
        <option value="">any stage</option>
        {stages.map((stage) => (
          <option key={stage.id} value={stage.id}>
            {stage.label}
          </option>
        ))}
      </Select>

      <WindowControl value={node.within} onChange={(within) => onChange({ ...node, within })} />
    </>
  )
}

function AssigneeChangedRow({
  node,
  members,
  onChange,
}: {
  readonly node: Extract<FilterNode, { type: 'assignee_changed' }>
  readonly members: readonly MemberDetail[]
  readonly onChange: (next: FilterNode) => void
}) {
  return (
    <>
      <span className="text-muted-foreground text-sm">from</span>
      <Select
        aria-label="From member"
        className="w-44"
        value={node.from_membership_id ?? ''}
        onChange={(event) => onChange({ ...node, from_membership_id: event.target.value || null })}
      >
        <option value="">anyone</option>
        {members.map((member) => (
          <option key={member.id} value={member.id}>
            {member.user.full_name}
          </option>
        ))}
      </Select>

      <span className="text-muted-foreground text-sm">to</span>
      <Select
        aria-label="To member"
        className="w-44"
        value={node.to_membership_id ?? ''}
        onChange={(event) => onChange({ ...node, to_membership_id: event.target.value || null })}
      >
        <option value="">anyone</option>
        {members.map((member) => (
          <option key={member.id} value={member.id}>
            {member.user.full_name}
          </option>
        ))}
      </Select>

      <WindowControl value={node.within} onChange={(within) => onChange({ ...node, within })} />
    </>
  )
}

/** The verb each node kind reads as, so a rule row starts like a sentence. */
function ruleLead(node: FilterNode): string {
  switch (node.type) {
    case 'action_performed':
      return 'Has done'
    case 'action_not_performed':
      return 'Has not done'
    case 'status_changed':
      return 'Stage moved'
    case 'assignee_changed':
      return 'Was reassigned'
    default:
      return ''
  }
}

function RuleRow({
  node,
  depth,
  props,
  onChange,
  onRemove,
}: {
  readonly node: FilterNode
  readonly depth: number
  readonly props: FilterBuilderProps
  readonly onChange: (next: FilterNode) => void
  readonly onRemove: () => void
}) {
  if (node.type === 'group') {
    return (
      <GroupEditor
        node={node}
        depth={depth + 1}
        props={props}
        onChange={onChange}
        onRemove={onRemove}
      />
    )
  }

  const lead = ruleLead(node)

  return (
    <div className="bg-background flex flex-wrap items-center gap-2 rounded-md border p-2">
      {lead && <span className="text-sm font-medium">{lead}</span>}

      {node.type === 'field' && (
        <FieldRuleRow
          node={node}
          fields={props.fields}
          fieldTypes={props.fieldTypes}
          onChange={onChange}
        />
      )}
      {(node.type === 'action_performed' || node.type === 'action_not_performed') && (
        <ActionRow node={node} customActions={props.customActions} onChange={onChange} />
      )}
      {node.type === 'status_changed' && (
        <StatusChangedRow node={node} stages={props.stages} onChange={onChange} />
      )}
      {node.type === 'assignee_changed' && (
        <AssigneeChangedRow node={node} members={props.members} onChange={onChange} />
      )}

      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="ml-auto"
        onClick={onRemove}
        aria-label="Remove rule"
      >
        Remove
      </Button>
    </div>
  )
}

function GroupEditor({
  node,
  depth,
  props,
  onChange,
  onRemove,
}: {
  readonly node: GroupNode
  readonly depth: number
  readonly props: FilterBuilderProps
  readonly onChange: (next: GroupNode) => void
  readonly onRemove?: () => void
}) {
  const { fields, fieldTypes } = props
  const firstField = fields[0]
  const firstOperator =
    fieldTypes.find((spec) => spec.key === firstField?.field_type)?.operators[0] ?? 'eq'

  function add(kind: FilterNode['type']) {
    const child: FilterNode =
      kind === 'field'
        ? { type: 'field', key: firstField?.key ?? '', op: firstOperator, value: null }
        : kind === 'group'
          ? emptyGroup()
          : kind === 'action_performed'
            ? { type: 'action_performed', action_kind: 'CALL_LOGGED', min_count: 1, within: null }
            : kind === 'action_not_performed'
              ? {
                  type: 'action_not_performed',
                  action_kind: 'CALL_LOGGED',
                  payload_match: { direction: 'OUTGOING' },
                  within: { last_days: 14 },
                }
              : kind === 'status_changed'
                ? { type: 'status_changed', within: { last_days: 7 } }
                : { type: 'assignee_changed', within: { last_days: 7 } }

    onChange({ ...node, children: [...node.children, child] })
  }

  return (
    <div className={depth > 0 ? 'bg-muted/40 space-y-2 rounded-md border p-3' : 'space-y-2'}>
      <div className="flex items-center gap-2">
        <Select
          aria-label="Match"
          className="w-36"
          value={node.op}
          onChange={(event) => onChange({ ...node, op: event.target.value as 'AND' | 'OR' })}
        >
          <option value="AND">Match all of</option>
          <option value="OR">Match any of</option>
        </Select>
        {onRemove && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="ml-auto"
            onClick={onRemove}
            aria-label="Remove group"
          >
            Remove group
          </Button>
        )}
      </div>

      {node.children.length === 0 && (
        <p className="text-muted-foreground px-1 text-sm">
          {props.emptyText ?? 'No rules yet — this matches every lead.'}
        </p>
      )}

      <div className="space-y-2">
        {node.children.map((child, index) => (
          <RuleRow
            // Index as key: rules have no id, and reordering is done by
            // removing and re-adding rather than dragging, so an index is
            // stable for as long as the row exists.
            key={index}
            node={child}
            depth={depth}
            props={props}
            onChange={(next) => onChange(replaceChild(node, index, next))}
            onRemove={() => onChange(removeChild(node, index))}
          />
        ))}
      </div>

      <div className="flex flex-wrap gap-2 pt-1">
        <Button type="button" variant="outline" size="sm" onClick={() => add('field')}>
          + Field
        </Button>
        {/* History predicates sit beside field rules, not behind an "advanced"
            toggle. Four of the ten filters the audit observed are these. */}
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => add('action_not_performed')}
        >
          + Has not done
        </Button>
        <Button type="button" variant="outline" size="sm" onClick={() => add('action_performed')}>
          + Has done
        </Button>
        <Button type="button" variant="outline" size="sm" onClick={() => add('status_changed')}>
          + Stage moved
        </Button>
        <Button type="button" variant="outline" size="sm" onClick={() => add('assignee_changed')}>
          + Reassigned
        </Button>
        <Button type="button" variant="ghost" size="sm" onClick={() => add('group')}>
          + Group
        </Button>
      </div>
    </div>
  )
}

export function FilterBuilder(props: FilterBuilderProps) {
  // Only fields the caller can view are offered. The server refuses a filter on
  // anything else, so offering it would build a rule that cannot run.
  const visible = useMemo(() => props.fields.filter((field) => !field.is_hidden), [props.fields])

  return (
    <GroupEditor
      node={props.node}
      depth={0}
      props={{ ...props, fields: visible }}
      onChange={props.onChange}
    />
  )
}
