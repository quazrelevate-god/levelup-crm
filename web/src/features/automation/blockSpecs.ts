/**
 * The blocks a workflow is drawn from, and how each one is described in words.
 *
 * Colour says what a block is at a glance, as in TeleCRM's builder: the trigger
 * is dark, checks are violet, and each kind of action has its own colour. A
 * check has two exits — green "yes" and red "no" — and everything else one.
 */

import { createContext } from 'react'
import { Filter, type LucideIcon, NotebookPen, PenLine, Route, UserSearch, Zap } from 'lucide-react'

import type { LeadField, Stage } from '@/api/types'
import type { ConditionRule, NodeData, NodeType, OperatorSpec, TriggerSpec } from './api'

export interface BlockSpec {
  readonly type: NodeType
  readonly title: string
  readonly hint: string
  readonly icon: LucideIcon
  /** Header colour. Saturated enough to read on both themes. */
  readonly header: string
  readonly group: 'trigger' | 'check' | 'action'
  readonly exits: readonly ('out' | 'yes' | 'no')[]
  readonly initial: () => NodeData
}

export const BLOCKS: Record<NodeType, BlockSpec> = {
  trigger: {
    type: 'trigger',
    title: 'When this happens',
    hint: 'What starts the workflow.',
    icon: Zap,
    header: 'bg-slate-800 text-white dark:bg-slate-700',
    group: 'trigger',
    exits: ['out'],
    initial: () => ({ trigger: null }),
  },
  condition: {
    type: 'condition',
    title: 'If the payment…',
    hint: 'Check the amount, method or anything else the payment carries.',
    icon: Filter,
    header: 'bg-violet-600 text-white',
    group: 'check',
    exits: ['yes', 'no'],
    initial: () => ({
      match: 'all',
      rules: [{ variable: 'amount', operator: 'equals', value: '' }],
    }),
  },
  lead_condition: {
    type: 'lead_condition',
    title: 'If the lead…',
    hint: 'Check the lead itself: its stage, a field, its owner.',
    icon: UserSearch,
    header: 'bg-indigo-600 text-white',
    group: 'check',
    exits: ['yes', 'no'],
    initial: () => ({ conditions: { type: 'group', op: 'AND', children: [] } }),
  },
  update_fields: {
    type: 'update_fields',
    title: 'Update lead fields',
    hint: 'Set one or more fields, e.g. Fee Status → Paid.',
    icon: PenLine,
    header: 'bg-sky-600 text-white',
    group: 'action',
    exits: ['out'],
    initial: () => ({ values: [{ field_key: '', value: '' }] }),
  },
  update_stage: {
    type: 'update_stage',
    title: 'Change stage',
    hint: 'Move the lead to a pipeline stage.',
    icon: Route,
    header: 'bg-amber-500 text-white',
    group: 'action',
    exits: ['out'],
    initial: () => ({ stage_id: '' }),
  },
  add_note: {
    type: 'add_note',
    title: 'Add a note',
    hint: "Write a note on the lead's timeline.",
    icon: NotebookPen,
    header: 'bg-emerald-600 text-white',
    group: 'action',
    exits: ['out'],
    initial: () => ({ text: '' }),
  },
}

/** What the canvas needs to describe a block in words. */
export interface BlockLookups {
  readonly triggers: readonly TriggerSpec[]
  readonly operators: readonly OperatorSpec[]
  readonly fields: readonly LeadField[]
  readonly stages: readonly Stage[]
  /** Node id → what is still missing, from the server's own check. */
  readonly problems: ReadonlyMap<string, string>
  /** Node id → how the run being inspected went through it. */
  readonly visited: ReadonlyMap<string, string>
}

export const BlockLookupsContext = createContext<BlockLookups>({
  triggers: [],
  operators: [],
  fields: [],
  stages: [],
  problems: new Map(),
  visited: new Map(),
})

function ruleText(rule: ConditionRule, lookups: BlockLookups, trigger: TriggerSpec | undefined) {
  const variable =
    trigger?.variables.find((v) => v.key === rule.variable)?.label ??
    (rule.variable.startsWith('notes.') ? `Note “${rule.variable.slice(6)}”` : rule.variable)
  const operator = lookups.operators.find((o) => o.key === rule.operator)
  const value = operator && !operator.needs_value ? '' : ` ${rule.value || '…'}`
  return `${variable || '…'} ${operator?.label ?? rule.operator}${value}`
}

/** One or two lines saying what the block does, for the canvas and the steps. */
export function describeBlock(
  type: NodeType,
  data: NodeData,
  lookups: BlockLookups,
  trigger: TriggerSpec | undefined,
): string[] {
  switch (type) {
    case 'trigger': {
      const spec = lookups.triggers.find((t) => t.key === data.trigger)
      return [spec ? `${spec.app ? 'Razorpay · ' : ''}${spec.label}` : 'Choose a trigger']
    }
    case 'condition': {
      const rules = data.rules ?? []
      if (rules.length === 0) return ['Add a check']
      const joiner = data.match === 'any' ? 'or' : 'and'
      return rules.map((rule, i) => (i ? `${joiner} ` : '') + ruleText(rule, lookups, trigger))
    }
    case 'lead_condition': {
      const children =
        data.conditions && 'children' in data.conditions ? data.conditions.children.length : 0
      return [children ? `${children} lead condition${children > 1 ? 's' : ''}` : 'Add a condition']
    }
    case 'update_fields': {
      const values = (data.values ?? []).filter((v) => v.field_key)
      if (values.length === 0) return ['Choose a field']
      return values.map((v) => {
        const field = lookups.fields.find((f) => f.key === v.field_key)
        const option = field?.options.find((o) => o.code === v.value)
        return `${field?.label ?? v.field_key} → ${option?.label ?? (v.value || '…')}`
      })
    }
    case 'update_stage': {
      const stage = lookups.stages.find((s) => s.id === data.stage_id)
      return [stage ? `→ ${stage.label}` : 'Choose a stage']
    }
    case 'add_note': {
      const text = (data.text ?? '').trim()
      return [text ? (text.length > 60 ? `${text.slice(0, 60)}…` : text) : 'Write the note']
    }
  }
}
