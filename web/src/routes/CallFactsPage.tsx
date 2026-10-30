/**
 * Call facts (docs/17) — reference details the voice agent tells each lead.
 *
 * Batch dates, mentors, fees: things that are not on the lead but differ by
 * lead. Each set has a rule, built like a lead filter; on every call the first
 * set the lead matches, top of the list first, supplies its facts. The agent
 * reads them in its lead notes, and a script can name them as `{{key}}`.
 */

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { BookOpenText, Plus, Trash2 } from 'lucide-react'

import { api, ApiError } from '@/api/client'
import type { GroupNode, MemberDetail, Page } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { PageHeader } from '@/components/ui/page-header'
import { useAuth } from '@/features/auth/context'
import {
  useArchiveCallFacts,
  useCallFacts,
  useCreateCallFacts,
  useUpdateCallFacts,
  type CallFactSet,
  type CallFactSetWrite,
} from '@/features/callFacts/api'
import { useFieldTypes, useLeadFields } from '@/features/fields/api'
import { FilterBuilder } from '@/features/filters/FilterBuilder'
import { useCustomActions, useStages } from '@/features/pipeline/api'

const EVERY_LEAD: GroupNode = { type: 'group', op: 'AND', children: [] }

interface Draft {
  readonly id: string | null
  readonly name: string
  readonly priority: string
  readonly rule: GroupNode
  readonly facts: readonly { readonly label: string; readonly value: string }[]
}

function draftOf(set: CallFactSet | null, nextPriority: number): Draft {
  if (!set) {
    return {
      id: null,
      name: '',
      priority: String(nextPriority),
      rule: EVERY_LEAD,
      facts: [{ label: '', value: '' }],
    }
  }
  const rule = 'type' in set.conditions ? (set.conditions as GroupNode) : EVERY_LEAD
  return {
    id: set.id,
    name: set.name,
    priority: String(set.priority),
    rule,
    facts: set.facts.map(({ label, value }) => ({ label, value })),
  }
}

function errorText(error: unknown): string | null {
  if (!error) return null
  return error instanceof ApiError ? error.message : 'Something went wrong. Please try again.'
}

export function CallFactsPage() {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string
  const sets = useCallFacts(workspaceId)
  const archive = useArchiveCallFacts(workspaceId)
  const [draft, setDraft] = useState<Draft | null>(null)

  const nextPriority = useMemo(
    () => (sets.data?.length ? Math.max(...sets.data.map((s) => s.priority)) + 10 : 10),
    [sets.data],
  )

  return (
    <div className="space-y-5">
      <PageHeader
        title="Call facts"
        description="Details your voice agent tells each lead — batch dates, mentors, fees — chosen by rules. On every call, the first set a lead matches (top of the list first) is used."
      />

      {draft ? (
        <FactSetEditor workspaceId={workspaceId} draft={draft} onDone={() => setDraft(null)} />
      ) : (
        <Button size="sm" onClick={() => setDraft(draftOf(null, nextPriority))}>
          <Plus aria-hidden="true" /> New fact set
        </Button>
      )}

      {sets.isPending ? (
        <p className="text-muted-foreground text-sm">Loading…</p>
      ) : sets.isError ? (
        <p className="text-muted-foreground text-sm">Call facts could not be loaded.</p>
      ) : sets.data.length === 0 ? (
        <p className="text-muted-foreground text-sm">
          No call facts yet. Add one to tell the agent, for example, when each batch starts.
        </p>
      ) : (
        <div className="space-y-3">
          {sets.data.map((set) => (
            <Card key={set.id} data-testid="call-fact-set">
              <CardHeader>
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0">
                    <CardTitle className="flex items-center gap-2">
                      <BookOpenText aria-hidden="true" className="size-4" />
                      {set.name}
                    </CardTitle>
                    <CardDescription>
                      Applies to: {set.rule} · checked {set.priority}
                    </CardDescription>
                  </div>
                  <div className="flex gap-1">
                    <Button size="sm" variant="outline" onClick={() => setDraft(draftOf(set, 0))}>
                      Edit
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      aria-label={`Archive ${set.name}`}
                      disabled={archive.isPending}
                      onClick={() => archive.mutate(set.id)}
                    >
                      <Trash2 aria-hidden="true" />
                    </Button>
                  </div>
                </div>
              </CardHeader>
              <CardContent>
                <table className="w-full text-sm">
                  <tbody>
                    {set.facts.map((fact) => (
                      <tr key={fact.key} className="border-border/50 border-b last:border-b-0">
                        <td className="py-1.5 pr-3 align-top font-medium whitespace-nowrap">
                          {fact.label}
                        </td>
                        <td className="py-1.5 pr-3 align-top">{fact.value || '—'}</td>
                        <td className="py-1.5 text-right align-top">
                          <Badge variant="outline" className="font-mono">
                            {`{{${fact.key}}}`}
                          </Badge>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}

function FactSetEditor({
  workspaceId,
  draft: initial,
  onDone,
}: {
  readonly workspaceId: string
  readonly draft: Draft
  readonly onDone: () => void
}) {
  const [draft, setDraft] = useState<Draft>(initial)
  const create = useCreateCallFacts(workspaceId)
  const update = useUpdateCallFacts(workspaceId)
  const saving = create.isPending || update.isPending
  const error = errorText(create.error) ?? errorText(update.error)

  const fields = useLeadFields(workspaceId)
  const fieldTypes = useFieldTypes(workspaceId)
  const stages = useStages(workspaceId)
  const customActions = useCustomActions(workspaceId)
  const members = useQuery({
    queryKey: ['members', workspaceId],
    queryFn: () =>
      api.get<Page<MemberDetail>>(`/workspaces/${workspaceId}/members`, { query: { limit: 100 } }),
  })
  const allStages = useMemo(() => {
    const pipeline = stages.data
    if (!pipeline) return []
    return [pipeline.initial, ...pipeline.active, pipeline.won, pipeline.lost].filter(
      (stage): stage is NonNullable<typeof stage> => stage !== null,
    )
  }, [stages.data])

  const setFact = (index: number, part: 'label' | 'value', text: string) =>
    setDraft({
      ...draft,
      facts: draft.facts.map((fact, i) => (i === index ? { ...fact, [part]: text } : fact)),
    })

  return (
    <Card>
      <CardHeader>
        <CardTitle>{draft.id ? 'Edit fact set' : 'New fact set'}</CardTitle>
        <CardDescription>
          Give it a name, choose which leads it is for, then list the facts. Leave the rule empty to
          use it for every lead that no set above matches.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault()
            const body: CallFactSetWrite = {
              name: draft.name.trim(),
              priority: Number(draft.priority) || 0,
              conditions: draft.rule.children.length > 0 ? draft.rule : {},
              facts: draft.facts.filter((fact) => fact.label.trim()),
            }
            if (draft.id) update.mutate({ id: draft.id, body }, { onSuccess: onDone })
            else create.mutate(body, { onSuccess: onDone })
          }}
        >
          <div className="grid gap-3 sm:grid-cols-[1fr_8rem]">
            <div className="space-y-1.5">
              <Label htmlFor="fact-set-name">Name</Label>
              <Input
                id="fact-set-name"
                required
                maxLength={120}
                placeholder="e.g. Video Editing — Batch 6"
                value={draft.name}
                onChange={(event) => setDraft({ ...draft, name: event.target.value })}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="fact-set-priority">Check order</Label>
              <Input
                id="fact-set-priority"
                type="number"
                min={0}
                max={10000}
                value={draft.priority}
                onChange={(event) => setDraft({ ...draft, priority: event.target.value })}
              />
            </div>
          </div>

          <div className="space-y-1.5">
            <Label>Which leads</Label>
            <div className="border-border/70 rounded-xl border p-3">
              <FilterBuilder
                node={draft.rule}
                fields={fields.data ?? []}
                fieldTypes={fieldTypes.data ?? []}
                stages={allStages}
                members={members.data?.items ?? []}
                customActions={customActions.data ?? []}
                onChange={(rule) => setDraft({ ...draft, rule })}
              />
            </div>
          </div>

          <div className="space-y-2">
            <Label>Facts</Label>
            {draft.facts.map((fact, index) => (
              <div key={index} className="flex flex-wrap items-center gap-2">
                <Input
                  aria-label={`Fact ${index + 1} name`}
                  className="max-w-56"
                  maxLength={60}
                  placeholder="e.g. Batch start date"
                  value={fact.label}
                  onChange={(event) => setFact(index, 'label', event.target.value)}
                />
                <Input
                  aria-label={`Fact ${index + 1} value`}
                  className="min-w-48 flex-1"
                  maxLength={500}
                  placeholder="e.g. 12 December 2026"
                  value={fact.value}
                  onChange={(event) => setFact(index, 'value', event.target.value)}
                />
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  aria-label={`Remove fact ${index + 1}`}
                  onClick={() =>
                    setDraft({ ...draft, facts: draft.facts.filter((_, i) => i !== index) })
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
              disabled={draft.facts.length >= 30}
              onClick={() =>
                setDraft({ ...draft, facts: [...draft.facts, { label: '', value: '' }] })
              }
            >
              <Plus aria-hidden="true" /> Add fact
            </Button>
          </div>

          {error ? (
            <p role="alert" className="text-destructive text-sm">
              {error}
            </p>
          ) : null}
          <div className="flex gap-2">
            <Button type="submit" size="sm" disabled={saving || !draft.name.trim()}>
              {saving ? 'Saving…' : 'Save'}
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={onDone}>
              Cancel
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  )
}
