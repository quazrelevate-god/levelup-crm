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
import { ShieldBan } from 'lucide-react'

import { api, ApiError } from '@/api/client'
import type { GroupNode, MemberDetail, Page } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { PageHeader } from '@/components/ui/page-header'
import { useAuth } from '@/features/auth/context'
import { useNeverCallRule, useSetNeverCallRule } from '@/features/automation/api'
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
        description="Leads the AI agent must never call, whoever presses the button or runs a batch."
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
    </div>
  )
}
