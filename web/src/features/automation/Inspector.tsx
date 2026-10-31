/**
 * The panel beside the canvas: everything the selected block needs, in plain
 * questions. Every change is applied to the drawing at once; nothing is sent
 * to the server until Save.
 */

import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Plus, Trash2 } from 'lucide-react'

import { api } from '@/api/client'
import type { GroupNode, LeadField, MemberDetail, Page } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { useFieldTypes, useLeadFields } from '@/features/fields/api'
import { FilterBuilder } from '@/features/filters/FilterBuilder'
import { useCustomActions, useLostReasons, useStages } from '@/features/pipeline/api'

import type { ConditionRule, NodeData, NodeType, OperatorSpec, TriggerSpec } from './api'
import { BLOCKS } from './blockSpecs'
import { allStages } from './helpers'

const EMPTY_GROUP: GroupNode = { type: 'group', op: 'AND', children: [] }

/** `{{amount}}` chips: click to drop a payment detail into the text. */
function VariableChips({
  trigger,
  onPick,
}: {
  readonly trigger: TriggerSpec | undefined
  readonly onPick: (token: string) => void
}) {
  if (!trigger) return null
  return (
    <div className="flex flex-wrap gap-1 pt-1">
      <span className="text-muted-foreground text-[11px]">Insert:</span>
      {trigger.variables.map((v) => (
        <button
          key={v.key}
          type="button"
          onClick={() => onPick(`{{${v.key}}}`)}
          className="bg-muted hover:bg-primary/10 rounded-md px-1.5 py-0.5 text-[11px]"
          title={`Puts the payment's ${v.label.toLowerCase()} here`}
        >
          {v.label}
        </button>
      ))}
    </div>
  )
}

export function Inspector({
  workspaceId,
  nodeId,
  type,
  data,
  trigger,
  triggers,
  operators,
  onChange,
  onDelete,
}: {
  readonly workspaceId: string
  readonly nodeId: string
  readonly type: NodeType
  readonly data: NodeData
  readonly trigger: TriggerSpec | undefined
  readonly triggers: readonly TriggerSpec[]
  readonly operators: readonly OperatorSpec[]
  readonly onChange: (next: NodeData) => void
  readonly onDelete: () => void
}) {
  const spec = BLOCKS[type]
  const set = (patch: Partial<NodeData>) => onChange({ ...data, ...patch })

  return (
    <div className="space-y-4" data-testid="block-inspector" data-node={nodeId}>
      <div>
        <p className="text-sm font-semibold">{spec.title}</p>
        <p className="text-muted-foreground text-xs">{spec.hint}</p>
      </div>

      {type !== 'trigger' ? (
        <div className="space-y-1.5">
          <Label htmlFor="block-title">Name on the canvas (optional)</Label>
          <Input
            id="block-title"
            maxLength={60}
            placeholder={spec.title}
            value={data.title ?? ''}
            onChange={(event) => set({ title: event.target.value })}
          />
        </div>
      ) : null}

      {type === 'trigger' ? (
        <div className="space-y-1.5">
          <Label htmlFor="block-trigger">Start this workflow when</Label>
          <Select
            id="block-trigger"
            value={data.trigger ?? ''}
            onChange={(event) => set({ trigger: event.target.value || null })}
          >
            <option value="">Choose…</option>
            {triggers.map((t) => (
              <option key={t.key} value={t.key}>
                {t.app === 'razorpay' ? 'Razorpay — ' : ''}
                {t.label}
              </option>
            ))}
          </Select>
          {trigger ? <p className="text-muted-foreground text-xs">{trigger.description}</p> : null}
        </div>
      ) : null}

      {type === 'condition' ? (
        <ConditionEditor data={data} trigger={trigger} operators={operators} onChange={set} />
      ) : null}
      {type === 'lead_condition' ? (
        <LeadConditionEditor workspaceId={workspaceId} data={data} onChange={set} />
      ) : null}
      {type === 'update_fields' ? (
        <FieldsEditor workspaceId={workspaceId} data={data} trigger={trigger} onChange={set} />
      ) : null}
      {type === 'update_stage' ? (
        <StageEditor workspaceId={workspaceId} data={data} onChange={set} />
      ) : null}
      {type === 'add_note' ? (
        <div className="space-y-1.5">
          <Label htmlFor="block-note">Note</Label>
          <Textarea
            id="block-note"
            rows={4}
            maxLength={2000}
            placeholder="e.g. Paid {{amount}} by {{method}} ({{payment_id}})"
            value={data.text ?? ''}
            onChange={(event) => set({ text: event.target.value })}
          />
          <VariableChips
            trigger={trigger}
            onPick={(token) => set({ text: `${data.text ?? ''}${token}` })}
          />
        </div>
      ) : null}

      {type !== 'trigger' ? (
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="text-destructive"
          onClick={onDelete}
        >
          <Trash2 aria-hidden="true" /> Remove this block
        </Button>
      ) : null}
    </div>
  )
}

function ConditionEditor({
  data,
  trigger,
  operators,
  onChange,
}: {
  readonly data: NodeData
  readonly trigger: TriggerSpec | undefined
  readonly operators: readonly OperatorSpec[]
  readonly onChange: (patch: Partial<NodeData>) => void
}) {
  const rules = data.rules ?? []
  const setRule = (index: number, patch: Partial<ConditionRule>) =>
    onChange({ rules: rules.map((rule, i) => (i === index ? { ...rule, ...patch } : rule)) })

  if (!trigger) {
    return (
      <p className="text-muted-foreground text-sm">
        Choose the trigger first — it decides what can be checked.
      </p>
    )
  }
  return (
    <div className="space-y-3">
      {rules.length > 1 ? (
        <div className="space-y-1.5">
          <Label htmlFor="block-match">Go down “Yes” when</Label>
          <Select
            id="block-match"
            value={data.match ?? 'all'}
            onChange={(event) => onChange({ match: event.target.value as 'all' | 'any' })}
          >
            <option value="all">every check is true</option>
            <option value="any">any one check is true</option>
          </Select>
        </div>
      ) : null}
      {rules.map((rule, index) => {
        const isNote = rule.variable.startsWith('notes.')
        const operator = operators.find((o) => o.key === rule.operator)
        return (
          <div key={index} className="border-border/70 space-y-2 rounded-lg border p-2.5">
            <div className="flex items-center gap-2">
              <Select
                aria-label={`Check ${index + 1}: what`}
                value={isNote ? '__note' : rule.variable}
                onChange={(event) =>
                  setRule(index, {
                    variable: event.target.value === '__note' ? 'notes.' : event.target.value,
                  })
                }
              >
                {trigger.variables.map((v) => (
                  <option key={v.key} value={v.key}>
                    {v.label}
                  </option>
                ))}
                <option value="__note">A note on the payment…</option>
              </Select>
              <Button
                type="button"
                size="icon"
                variant="ghost"
                aria-label={`Remove check ${index + 1}`}
                onClick={() => onChange({ rules: rules.filter((_, i) => i !== index) })}
              >
                <Trash2 aria-hidden="true" />
              </Button>
            </div>
            {isNote ? (
              <Input
                aria-label={`Check ${index + 1}: note name`}
                placeholder="The note's name in Razorpay, e.g. course"
                value={rule.variable.slice(6)}
                onChange={(event) =>
                  setRule(index, { variable: `notes.${event.target.value.trim()}` })
                }
              />
            ) : null}
            <Select
              aria-label={`Check ${index + 1}: how`}
              value={rule.operator}
              onChange={(event) => setRule(index, { operator: event.target.value })}
            >
              {operators.map((o) => (
                <option key={o.key} value={o.key}>
                  {o.label}
                </option>
              ))}
            </Select>
            {operator?.needs_value !== false ? (
              <Input
                aria-label={`Check ${index + 1}: value`}
                placeholder={rule.variable === 'amount' ? 'e.g. 400' : 'Value'}
                value={rule.value}
                onChange={(event) => setRule(index, { value: event.target.value })}
              />
            ) : null}
          </div>
        )
      })}
      <Button
        type="button"
        size="sm"
        variant="outline"
        onClick={() =>
          onChange({
            rules: [
              ...rules,
              { variable: trigger.variables[0]?.key ?? '', operator: 'equals', value: '' },
            ],
          })
        }
      >
        <Plus aria-hidden="true" /> Add a check
      </Button>
      {trigger.variables.some((v) => v.key === 'amount') ? (
        <p className="text-muted-foreground text-xs">
          Amounts are in rupees, as the lead paid them: 400 means ₹400.
        </p>
      ) : null}
    </div>
  )
}

function LeadConditionEditor({
  workspaceId,
  data,
  onChange,
}: {
  readonly workspaceId: string
  readonly data: NodeData
  readonly onChange: (patch: Partial<NodeData>) => void
}) {
  const fields = useLeadFields(workspaceId)
  const fieldTypes = useFieldTypes(workspaceId)
  const stages = useStages(workspaceId)
  const customActions = useCustomActions(workspaceId)
  const members = useQuery({
    queryKey: ['members', workspaceId],
    queryFn: () =>
      api.get<Page<MemberDetail>>(`/workspaces/${workspaceId}/members`, { query: { limit: 100 } }),
  })
  const node =
    data.conditions && 'type' in data.conditions ? (data.conditions as GroupNode) : EMPTY_GROUP
  return (
    <div className="space-y-1.5">
      <Label>Go down “Yes” when the lead matches</Label>
      <div className="border-border/70 rounded-lg border p-2">
        <FilterBuilder
          node={node}
          fields={fields.data ?? []}
          fieldTypes={fieldTypes.data ?? []}
          stages={allStages(stages.data)}
          members={members.data?.items ?? []}
          customActions={customActions.data ?? []}
          onChange={(next) => onChange({ conditions: next })}
        />
      </div>
    </div>
  )
}

function FieldValueInput({
  field,
  value,
  label,
  onChange,
}: {
  readonly field: LeadField | undefined
  readonly value: string
  readonly label: string
  readonly onChange: (value: string) => void
}) {
  const options = field?.options.filter((o) => !o.is_archived) ?? []
  if (field && options.length > 0 && !value.includes('{{')) {
    return (
      <Select aria-label={label} value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">Choose…</option>
        {options.map((option) => (
          <option key={option.id} value={option.code}>
            {option.label}
          </option>
        ))}
      </Select>
    )
  }
  return (
    <Input
      aria-label={label}
      placeholder="The new value"
      value={value}
      onChange={(event) => onChange(event.target.value)}
    />
  )
}

function FieldsEditor({
  workspaceId,
  data,
  trigger,
  onChange,
}: {
  readonly workspaceId: string
  readonly data: NodeData
  readonly trigger: TriggerSpec | undefined
  readonly onChange: (patch: Partial<NodeData>) => void
}) {
  const fields = useLeadFields(workspaceId)
  const values = data.values ?? []
  const writable = useMemo(() => (fields.data ?? []).filter((f) => !f.is_hidden), [fields.data])
  const setRow = (index: number, patch: Partial<{ field_key: string; value: string }>) =>
    onChange({ values: values.map((row, i) => (i === index ? { ...row, ...patch } : row)) })

  return (
    <div className="space-y-3">
      {values.map((row, index) => {
        const field = writable.find((f) => f.key === row.field_key)
        return (
          <div key={index} className="border-border/70 space-y-2 rounded-lg border p-2.5">
            <div className="flex items-center gap-2">
              <Select
                aria-label={`Field ${index + 1}`}
                value={row.field_key}
                onChange={(event) => setRow(index, { field_key: event.target.value, value: '' })}
              >
                <option value="">Choose a field…</option>
                {writable.map((f) => (
                  <option key={f.key} value={f.key}>
                    {f.label}
                  </option>
                ))}
              </Select>
              <Button
                type="button"
                size="icon"
                variant="ghost"
                aria-label={`Remove field ${index + 1}`}
                onClick={() => onChange({ values: values.filter((_, i) => i !== index) })}
              >
                <Trash2 aria-hidden="true" />
              </Button>
            </div>
            <FieldValueInput
              field={field}
              value={row.value}
              label={`Field ${index + 1}: new value`}
              onChange={(value) => setRow(index, { value })}
            />
            <VariableChips
              trigger={trigger}
              onPick={(token) => setRow(index, { value: `${row.value}${token}` })}
            />
          </div>
        )
      })}
      <Button
        type="button"
        size="sm"
        variant="outline"
        onClick={() => onChange({ values: [...values, { field_key: '', value: '' }] })}
      >
        <Plus aria-hidden="true" /> Add a field
      </Button>
      <p className="text-muted-foreground text-xs">
        A value that comes out blank leaves the field as it was.
      </p>
    </div>
  )
}

function StageEditor({
  workspaceId,
  data,
  onChange,
}: {
  readonly workspaceId: string
  readonly data: NodeData
  readonly onChange: (patch: Partial<NodeData>) => void
}) {
  const stages = useStages(workspaceId)
  const reasons = useLostReasons(workspaceId)
  const list = allStages(stages.data)
  const chosen = list.find((s) => s.id === data.stage_id)
  return (
    <div className="space-y-3">
      <div className="space-y-1.5">
        <Label htmlFor="block-stage">Move the lead to</Label>
        <Select
          id="block-stage"
          value={data.stage_id ?? ''}
          onChange={(event) => onChange({ stage_id: event.target.value, lost_reason_id: '' })}
        >
          <option value="">Choose a stage…</option>
          {list.map((stage) => (
            <option key={stage.id} value={stage.id}>
              {stage.label}
            </option>
          ))}
        </Select>
      </div>
      {chosen?.kind === 'LOST' ? (
        <div className="space-y-1.5">
          <Label htmlFor="block-lost-reason">Lost reason</Label>
          <Select
            id="block-lost-reason"
            value={data.lost_reason_id ?? ''}
            onChange={(event) => onChange({ lost_reason_id: event.target.value })}
          >
            <option value="">Choose…</option>
            {(reasons.data ?? []).map((reason) => (
              <option key={reason.id} value={reason.id}>
                {reason.label}
              </option>
            ))}
          </Select>
        </div>
      ) : null}
      <p className="text-muted-foreground text-xs">
        A lead already in that stage is left where it is.
      </p>
    </div>
  )
}
