/**
 * AI calling rules (docs/18): leads the voice agent must never ring.
 *
 * One rule, written with the same filter builder as saved views. It is checked
 * on every AI call the CRM places — the lead panel's button, a batch, anything
 * added later — so "a lead who has already paid is never asked to pay" holds
 * whoever starts the call.
 */

import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { CalendarClock, ShieldBan } from 'lucide-react'

import { api, ApiError } from '@/api/client'
import type { GroupNode, MemberDetail, Page } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { PageHeader } from '@/components/ui/page-header'
import { Select } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { useAuth } from '@/features/auth/context'
import {
  useAutoCallback,
  useNeverCallRule,
  useSetAutoCallback,
  useSetNeverCallRule,
} from '@/features/automation/api'
import { allStages } from '@/features/automation/helpers'
import { useFieldTypes, useLeadFields } from '@/features/fields/api'
import { FilterBuilder } from '@/features/filters/FilterBuilder'
import { useCustomActions, useStages } from '@/features/pipeline/api'

const EMPTY: GroupNode = { type: 'group', op: 'OR', children: [] }

export function AiCallingRulesPage() {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string
  const rule = useNeverCallRule(workspaceId)
  const save = useSetNeverCallRule(workspaceId)
  const fields = useLeadFields(workspaceId)
  const fieldTypes = useFieldTypes(workspaceId)
  const stages = useStages(workspaceId)
  const customActions = useCustomActions(workspaceId)
  const members = useQuery({
    queryKey: ['members', workspaceId],
    queryFn: () =>
      api.get<Page<MemberDetail>>(`/workspaces/${workspaceId}/members`, { query: { limit: 100 } }),
  })
  const [draft, setDraft] = useState<GroupNode>(EMPTY)
  const [dirty, setDirty] = useState(false)

  useEffect(() => {
    if (!rule.data || dirty) return
    const stored = rule.data.conditions
    setDraft('type' in stored ? (stored as GroupNode) : EMPTY)
  }, [rule.data, dirty])

  const stageList = useMemo(() => allStages(stages.data), [stages.data])
  const active = draft.children.length > 0

  return (
    <div className="space-y-5">
      <PageHeader
        title="AI calling rules"
        description="Who the AI agent must never call, and when it calls back by itself."
      />
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ShieldBan aria-hidden="true" className="size-4" /> Never AI-call a lead when
          </CardTitle>
          <CardDescription>
            For example “Application Fee Status is Paid” or “Stage is Enrolled”, so a lead who
            already paid is never asked to pay again. Leave it empty to allow every approved lead.
            Test calls to your own phone are not affected.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="border-border/70 rounded-xl border p-3">
            <FilterBuilder
              node={draft}
              fields={fields.data ?? []}
              fieldTypes={fieldTypes.data ?? []}
              stages={stageList}
              members={members.data?.items ?? []}
              customActions={customActions.data ?? []}
              emptyText="No rules yet — no lead is blocked."
              onChange={(next) => {
                setDraft(next)
                setDirty(true)
              }}
            />
          </div>
          <p className="text-muted-foreground text-sm">
            {active
              ? 'The AI will refuse to call any lead that matches.'
              : 'No rule: the AI may call every approved lead.'}
          </p>
          {save.error ? (
            <p role="alert" className="text-destructive text-sm">
              {save.error instanceof ApiError ? save.error.message : 'The rule could not be saved.'}
            </p>
          ) : null}
          <Button
            disabled={!dirty || save.isPending}
            onClick={() => save.mutate(active ? draft : {}, { onSuccess: () => setDirty(false) })}
          >
            {save.isPending ? 'Saving…' : save.isSuccess && !dirty ? 'Saved' : 'Save rule'}
          </Button>
        </CardContent>
      </Card>
      <AutoCallbackCard workspaceId={workspaceId} />
    </div>
  )
}

/**
 * Book the call a lead asks for (docs/22). Off until someone switches it on —
 * nothing dials a lead on its own until the company decides it may.
 */
function AutoCallbackCard({ workspaceId }: { readonly workspaceId: string }) {
  const setting = useAutoCallback(workspaceId)
  const save = useSetAutoCallback(workspaceId)
  const fields = useLeadFields(workspaceId)
  const [fieldKey, setFieldKey] = useState<string | null>(null)
  const current = setting.data
  const chosen = fieldKey ?? current?.field_key ?? ''
  const textFields = (fields.data ?? []).filter((f) => ['TEXT', 'DATE'].includes(f.field_type))

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <CalendarClock aria-hidden="true" className="size-4" /> Call back when the lead asks
        </CardTitle>
        <CardDescription>
          When a lead says “call me tomorrow at 5:30” on an AI call, book that call automatically.
          Times outside calling hours move to the next allowed time; anything unclear (“sometime
          next week”) becomes a task for a person instead.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <label className="block space-y-1.5">
          <span className="text-sm font-medium">
            The field the AI writes the asked-for time into
          </span>
          <Select value={chosen} onChange={(event) => setFieldKey(event.target.value || null)}>
            <option value="">Choose…</option>
            {textFields.map((f) => (
              <option key={f.key} value={f.key}>
                {f.label}
              </option>
            ))}
          </Select>
        </label>
        <label className="flex items-center gap-3">
          <Switch
            aria-label="Book callbacks automatically"
            checked={Boolean(current?.enabled)}
            disabled={save.isPending || !chosen}
            onCheckedChange={(on) => save.mutate({ enabled: on, field_key: chosen || null })}
          />
          <span className="text-sm">
            {current?.enabled ? 'On — callbacks are booked automatically' : 'Off'}
          </span>
        </label>
        {fieldKey !== null && current?.enabled && fieldKey !== current.field_key ? (
          <Button
            size="sm"
            variant="outline"
            disabled={save.isPending}
            onClick={() => save.mutate({ enabled: true, field_key: fieldKey })}
          >
            Save field
          </Button>
        ) : null}
        {save.error ? (
          <p role="alert" className="text-destructive text-sm">
            {save.error instanceof ApiError ? save.error.message : 'It could not be saved.'}
          </p>
        ) : null}
      </CardContent>
    </Card>
  )
}
