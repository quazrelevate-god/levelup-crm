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
import { APP_LABELS, BLOCKS } from './blockSpecs'
import { allStages } from './helpers'

const EMPTY_GROUP: GroupNode = { type: 'group', op: 'AND', children: [] }

/** `{{amount}}` chips: click to drop a payment detail into the text. */
/** The lead's own details, usable in any message. Matches the server's list. */
const LEAD_CHIPS = [
  { key: 'lead.name', label: "Lead's name" },
  { key: 'lead.phone', label: "Lead's phone" },
  { key: 'lead.email', label: "Lead's email" },
]

/** `{{amount}}` chips: click to drop a detail into the text. */
function VariableChips({
  trigger,
  onPick,
}: {
  readonly trigger: TriggerSpec | undefined
  readonly onPick: (token: string) => void
}) {
  const chips = [...(trigger?.variables ?? []), ...LEAD_CHIPS]
  return (
    <div className="flex flex-wrap gap-1 pt-1">
      <span className="text-muted-foreground text-[11px]">Insert:</span>
      {chips.map((v) => (
        <button
          key={v.key}
          type="button"
          onClick={() => onPick(`{{${v.key}}}`)}
          className="bg-muted hover:bg-primary/10 rounded-md px-1.5 py-0.5 text-[11px]"
          title={`Puts the ${v.label.toLowerCase()} here`}
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
  purposes,
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
  readonly purposes: readonly { readonly key: string; readonly label: string }[]
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
                {t.app ? `${APP_LABELS[t.app] ?? t.app} — ` : ''}
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

      {type === 'wait' ? (
        <div className="space-y-1.5">
          <Label>Wait for</Label>
          <div className="flex gap-2">
            <Input
              aria-label="How long"
              type="number"
              min={1}
              className="w-24"
              value={data.amount ?? 10}
              onChange={(event) => set({ amount: Number(event.target.value) || 0 })}
            />
            <Select
              aria-label="Unit"
              value={data.unit ?? 'minutes'}
              onChange={(event) => set({ unit: event.target.value as NodeData['unit'] })}
            >
              <option value="minutes">minutes</option>
              <option value="hours">hours</option>
              <option value="days">days</option>
            </Select>
          </div>
          <p className="text-muted-foreground text-xs">
            Then the next step runs. Put an “If the lead…” check after it to act only if nothing has
            changed — e.g. they still have not booked.
          </p>
        </div>
      ) : null}

      {type === 'ai_call' ? (
        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor="block-purpose">What kind of call</Label>
            <Select
              id="block-purpose"
              value={data.purpose ?? ''}
              onChange={(event) => set({ purpose: event.target.value })}
            >
              {purposes.map((p) => (
                <option key={p.key} value={p.key}>
                  {p.label}
                </option>
              ))}
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="block-reason">Why (shown on the lead’s timeline)</Label>
            <Input
              id="block-reason"
              maxLength={300}
              placeholder="e.g. paid {{amount}}, not booked yet"
              value={data.reason ?? ''}
              onChange={(event) => set({ reason: event.target.value })}
            />
            <VariableChips
              trigger={trigger}
              onPick={(token) => set({ reason: `${data.reason ?? ''}${token}` })}
            />
          </div>
          <p className="text-muted-foreground text-xs">
            The call is placed within calling hours, one at a time, and never to a lead your “Never
            AI-call” rule covers.
          </p>
        </div>
      ) : null}

      {type === 'send_whatsapp' ? (
        <div className="space-y-3">
          <div className="grid grid-cols-[1fr_6rem] gap-2">
            <div className="space-y-1.5">
              <Label htmlFor="block-template">Template name</Label>
              <Input
                id="block-template"
                placeholder="e.g. fee_received"
                value={data.template ?? ''}
                onChange={(event) => set({ template: event.target.value.trim() })}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="block-language">Language</Label>
              <Input
                id="block-language"
                placeholder="en"
                value={data.language ?? 'en'}
                onChange={(event) => set({ language: event.target.value.trim() })}
              />
            </div>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="block-to">Send to</Label>
            <Input
              id="block-to"
              value={data.to ?? '{{lead.phone}}'}
              onChange={(event) => set({ to: event.target.value })}
            />
            <VariableChips trigger={trigger} onPick={(token) => set({ to: token })} />
          </div>
          <div className="space-y-2">
            <Label>
              Values for the template’s {'{{1}}'}, {'{{2}}'}…
            </Label>
            {(data.params ?? []).map((param, index) => (
              <div key={index} className="flex items-center gap-2">
                <span className="text-muted-foreground w-10 text-xs">{`{{${index + 1}}}`}</span>
                <Input
                  aria-label={`Value ${index + 1}`}
                  value={param}
                  onChange={(event) =>
                    set({
                      params: (data.params ?? []).map((p, i) =>
                        i === index ? event.target.value : p,
                      ),
                    })
                  }
                />
                <Button
                  type="button"
                  size="icon"
                  variant="ghost"
                  aria-label={`Remove value ${index + 1}`}
                  onClick={() => set({ params: (data.params ?? []).filter((_, i) => i !== index) })}
                >
                  <Trash2 aria-hidden="true" />
                </Button>
              </div>
            ))}
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => set({ params: [...(data.params ?? []), ''] })}
            >
              <Plus aria-hidden="true" /> Add a value
            </Button>
            <VariableChips
              trigger={trigger}
              onPick={(token) => set({ params: [...(data.params ?? []), token] })}
            />
          </div>
          <p className="text-muted-foreground text-xs">
            WhatsApp only allows templates Meta has approved. Create them in WhatsApp Manager;
            connect your number in Apps → WhatsApp.
          </p>
        </div>
      ) : null}

      {type === 'call_api' ? (
        <div className="space-y-3">
          <div className="grid grid-cols-[6rem_1fr] gap-2">
            <Select
              aria-label="Method"
              value={(data.method ?? 'POST').toUpperCase()}
              onChange={(event) => set({ method: event.target.value })}
            >
              {['POST', 'GET', 'PUT', 'PATCH'].map((m) => (
                <option key={m}>{m}</option>
              ))}
            </Select>
            <Input
              aria-label="Address"
              placeholder="https://…"
              value={data.url ?? ''}
              onChange={(event) => set({ url: event.target.value.trim() })}
            />
          </div>
          <div className="space-y-2">
            <Label>Headers (optional)</Label>
            {(data.headers ?? []).map((header, index) => (
              <div key={index} className="flex gap-2">
                <Input
                  aria-label={`Header ${index + 1} name`}
                  placeholder="Name"
                  value={header.key}
                  onChange={(event) =>
                    set({
                      headers: (data.headers ?? []).map((h, i) =>
                        i === index ? { ...h, key: event.target.value } : h,
                      ),
                    })
                  }
                />
                <Input
                  aria-label={`Header ${index + 1} value`}
                  placeholder="Value"
                  value={header.value}
                  onChange={(event) =>
                    set({
                      headers: (data.headers ?? []).map((h, i) =>
                        i === index ? { ...h, value: event.target.value } : h,
                      ),
                    })
                  }
                />
                <Button
                  type="button"
                  size="icon"
                  variant="ghost"
                  aria-label={`Remove header ${index + 1}`}
                  onClick={() =>
                    set({ headers: (data.headers ?? []).filter((_, i) => i !== index) })
                  }
                >
                  <Trash2 aria-hidden="true" />
                </Button>
              </div>
            ))}
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => set({ headers: [...(data.headers ?? []), { key: '', value: '' }] })}
            >
              <Plus aria-hidden="true" /> Add a header
            </Button>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="block-body">Body (JSON)</Label>
            <Textarea
              id="block-body"
              rows={5}
              className="font-mono text-xs"
              placeholder={'{"phone": "{{lead.phone}}", "amount": "{{amount}}"}'}
              value={data.body ?? ''}
              onChange={(event) => set({ body: event.target.value })}
            />
            <VariableChips
              trigger={trigger}
              onPick={(token) => set({ body: `${data.body ?? ''}${token}` })}
            />
          </div>
          <p className="text-muted-foreground text-xs">
            Sent by the background worker within a minute. Only https:// addresses on the public
            internet.
          </p>
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
