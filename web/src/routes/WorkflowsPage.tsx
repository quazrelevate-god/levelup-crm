/**
 * Workflows (docs/18): every automation the workspace has drawn, whether it is
 * on, and how its last day went — the TeleCRM list, in this product's style.
 */

import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Plus, Trash2, Workflow as WorkflowIcon } from 'lucide-react'

import { ApiError } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Dialog } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { PageHeader } from '@/components/ui/page-header'
import { Switch } from '@/components/ui/switch'
import { useAuth } from '@/features/auth/context'
import {
  type WorkflowGraph,
  useAutomationCatalogue,
  useCreateWorkflow,
  useDeleteWorkflow,
  useUpdateWorkflow,
  useWorkflows,
} from '@/features/automation/api'
import { cn } from '@/lib/utils'

interface Starter {
  readonly key: string
  readonly title: string
  readonly description: string
  readonly trigger: string | null
  readonly graph?: WorkflowGraph
}

const STARTERS: readonly Starter[] = [
  {
    key: 'payment',
    title: 'When a lead pays on Razorpay',
    description:
      'Starts with a payment check (amount equals …) and a “change stage” step on its Yes path. You fill in the amount and the stage.',
    trigger: 'razorpay.payment_captured',
    graph: {
      nodes: [
        {
          id: 'trigger',
          type: 'trigger',
          position: { x: 40, y: 160 },
          data: { trigger: 'razorpay.payment_captured' },
        },
        {
          id: 'check-amount',
          type: 'condition',
          position: { x: 360, y: 160 },
          data: {
            title: 'Is it the application fee?',
            match: 'all',
            rules: [{ variable: 'amount', operator: 'equals', value: '' }],
          },
        },
        {
          id: 'move-stage',
          type: 'update_stage',
          position: { x: 700, y: 100 },
          data: { stage_id: '' },
        },
        {
          id: 'set-fields',
          type: 'update_fields',
          position: { x: 700, y: 260 },
          data: { values: [{ field_key: '', value: '' }] },
        },
      ],
      edges: [
        { id: 'e1', source: 'trigger', sourceHandle: 'out', target: 'check-amount' },
        { id: 'e2', source: 'check-amount', sourceHandle: 'yes', target: 'move-stage' },
        { id: 'e3', source: 'check-amount', sourceHandle: 'yes', target: 'set-fields' },
      ],
    },
  },
  {
    key: 'blank',
    title: 'Start from scratch',
    description: 'Just the trigger. Add checks and actions yourself.',
    trigger: null,
  },
]

function when(iso: string | null): string {
  if (!iso) return 'Never'
  return new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

export function WorkflowsPage() {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string
  const workflows = useWorkflows(workspaceId)
  const catalogue = useAutomationCatalogue(workspaceId)
  const update = useUpdateWorkflow(workspaceId)
  const remove = useDeleteWorkflow(workspaceId)
  const [creating, setCreating] = useState(false)
  const [toggleError, setToggleError] = useState<string | null>(null)

  const list = workflows.data ?? []
  const runs24h = list.reduce((sum, w) => sum + w.runs_24h, 0)
  const failed24h = list.reduce((sum, w) => sum + w.failed_24h, 0)
  const triggerLabel = (key: string | null) => {
    const spec = catalogue.data?.triggers.find((t) => t.key === key)
    if (!spec) return 'No trigger yet'
    return `${spec.app === 'razorpay' ? 'Razorpay · ' : ''}${spec.label}`
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Workflows"
        description="When something happens — like a lead paying — check it and act on the lead automatically."
      >
        <Button onClick={() => setCreating(true)}>
          <Plus aria-hidden="true" /> Create workflow
        </Button>
      </PageHeader>

      <div className="grid gap-3 sm:grid-cols-3">
        <Stat label="Runs in the last 24 hours" value={runs24h} />
        <Stat
          label="Failed in the last 24 hours"
          value={failed24h}
          tone={failed24h > 0 ? 'bad' : 'neutral'}
        />
        <Stat label="Switched on" value={list.filter((w) => w.is_active).length} />
      </div>

      {toggleError ? (
        <p role="alert" className="text-destructive text-sm">
          {toggleError}
        </p>
      ) : null}

      {workflows.isPending ? (
        <p className="text-muted-foreground text-sm">Loading…</p>
      ) : workflows.isError ? (
        <p className="text-muted-foreground text-sm">Workflows could not be loaded.</p>
      ) : list.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center gap-3 py-10 text-center">
            <WorkflowIcon aria-hidden="true" className="text-muted-foreground size-8" />
            <p className="font-medium">No workflows yet</p>
            <p className="text-muted-foreground max-w-md text-sm">
              Start with “When a lead pays on Razorpay” to move paid leads to the right stage the
              moment the money arrives.
            </p>
            <Button onClick={() => setCreating(true)}>
              <Plus aria-hidden="true" /> Create workflow
            </Button>
          </CardContent>
        </Card>
      ) : (
        <div className="border-border overflow-hidden rounded-xl border">
          <table className="w-full text-sm">
            <thead className="bg-muted/50 text-muted-foreground text-left text-xs">
              <tr>
                <th className="px-4 py-2 font-medium">Name</th>
                <th className="px-4 py-2 font-medium">Starts when</th>
                <th className="px-4 py-2 font-medium">On</th>
                <th className="px-4 py-2 font-medium">Total runs</th>
                <th className="px-4 py-2 font-medium">Last 24h runs / failed</th>
                <th className="px-4 py-2 font-medium">Last run</th>
                <th className="px-4 py-2" />
              </tr>
            </thead>
            <tbody>
              {list.map((workflow) => (
                <tr key={workflow.id} className="border-border/60 border-t">
                  <td className="px-4 py-3">
                    <Link
                      to={`/automations/${workflow.id}`}
                      className="font-medium hover:underline"
                    >
                      {workflow.name}
                    </Link>
                    {!workflow.is_active && workflow.problems.length > 0 ? (
                      <p className="text-xs text-amber-700 dark:text-amber-400">
                        Not finished: {workflow.problems[0]?.message}
                      </p>
                    ) : null}
                  </td>
                  <td className="px-4 py-3">
                    <Badge variant="outline">{triggerLabel(workflow.trigger)}</Badge>
                  </td>
                  <td className="px-4 py-3">
                    <Switch
                      aria-label={`${workflow.name} on`}
                      checked={workflow.is_active}
                      disabled={update.isPending}
                      onCheckedChange={(on) => {
                        setToggleError(null)
                        update.mutate(
                          { id: workflow.id, body: { is_active: on } },
                          {
                            onError: (error) =>
                              setToggleError(
                                error instanceof ApiError
                                  ? `${workflow.name}: ${error.message}`
                                  : 'That could not be changed.',
                              ),
                          },
                        )
                      }}
                    />
                  </td>
                  <td className="px-4 py-3">{workflow.total_runs}</td>
                  <td className="px-4 py-3">
                    {workflow.runs_24h} /{' '}
                    <span className={cn(workflow.failed_24h > 0 && 'text-destructive font-medium')}>
                      {workflow.failed_24h}
                    </span>
                  </td>
                  <td className="text-muted-foreground px-4 py-3">{when(workflow.last_run_at)}</td>
                  <td className="px-4 py-3 text-right">
                    <Button
                      size="icon"
                      variant="ghost"
                      aria-label={`Delete ${workflow.name}`}
                      disabled={remove.isPending}
                      onClick={() => {
                        if (window.confirm(`Delete “${workflow.name}”? Its run history is kept.`)) {
                          remove.mutate(workflow.id)
                        }
                      }}
                    >
                      <Trash2 aria-hidden="true" />
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {creating ? (
        <CreateDialog workspaceId={workspaceId} onClose={() => setCreating(false)} />
      ) : null}
    </div>
  )
}

function Stat({
  label,
  value,
  tone = 'neutral',
}: {
  readonly label: string
  readonly value: number
  readonly tone?: 'neutral' | 'bad'
}) {
  return (
    <Card>
      <CardContent className="pt-5">
        <p className="text-muted-foreground text-xs">{label}</p>
        <p className={cn('text-2xl font-semibold', tone === 'bad' && 'text-destructive')}>
          {value}
        </p>
      </CardContent>
    </Card>
  )
}

function CreateDialog({
  workspaceId,
  onClose,
}: {
  readonly workspaceId: string
  readonly onClose: () => void
}) {
  const navigate = useNavigate()
  const create = useCreateWorkflow(workspaceId)
  const [name, setName] = useState('')
  const [starter, setStarter] = useState<string>(STARTERS[0]?.key ?? 'blank')
  const chosen = STARTERS.find((s) => s.key === starter)

  return (
    <Dialog
      open
      onClose={onClose}
      title="Create a workflow"
      description="It starts switched off, so nothing happens until you turn it on."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={!name.trim() || create.isPending}
            onClick={() =>
              create.mutate(
                {
                  name: name.trim(),
                  trigger: chosen?.trigger ?? null,
                  ...(chosen?.graph ? { graph: chosen.graph } : {}),
                },
                { onSuccess: (created) => void navigate(`/automations/${created.id}`) },
              )
            }
          >
            {create.isPending ? 'Creating…' : 'Create and open'}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="workflow-name">Name</Label>
          <Input
            id="workflow-name"
            autoFocus
            maxLength={120}
            placeholder="e.g. Application fee paid"
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </div>
        <fieldset className="space-y-2">
          <legend className="text-sm font-medium">Start from</legend>
          {STARTERS.map((option) => (
            <label
              key={option.key}
              className={cn(
                'border-border flex cursor-pointer gap-3 rounded-lg border p-3',
                starter === option.key && 'border-primary bg-primary/5',
              )}
            >
              <input
                type="radio"
                name="starter"
                className="mt-1"
                checked={starter === option.key}
                onChange={() => setStarter(option.key)}
              />
              <span>
                <span className="block text-sm font-medium">{option.title}</span>
                <span className="text-muted-foreground block text-xs">{option.description}</span>
              </span>
            </label>
          ))}
        </fieldset>
        {create.error ? (
          <p role="alert" className="text-destructive text-sm">
            {create.error instanceof ApiError ? create.error.message : 'It could not be created.'}
          </p>
        ) : null}
      </div>
    </Dialog>
  )
}
