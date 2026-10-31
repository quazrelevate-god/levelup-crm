/**
 * Try the workflow on a real lead with a pretend payment. Nothing is saved:
 * the server runs it and throws every change away, then says what it did.
 */

import { useState } from 'react'

import { ApiError } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useLeads } from '@/features/leads/api'

import { type TriggerSpec, type WorkflowGraph, useTestWorkflow } from './api'

export function TestDialog({
  workspaceId,
  workflowId,
  trigger,
  graph,
  onClose,
}: {
  readonly workspaceId: string
  readonly workflowId: string
  readonly trigger: TriggerSpec | undefined
  readonly graph: WorkflowGraph
  readonly onClose: () => void
}) {
  const [search, setSearch] = useState('')
  const [leadId, setLeadId] = useState<string | null>(null)
  const [data, setData] = useState<Record<string, string>>({ amount: '400' })
  const leads = useLeads(workspaceId, search)
  const test = useTestWorkflow(workspaceId)
  const chosen = leads.data?.items.find((lead) => lead.id === leadId)

  return (
    <Dialog
      open
      onClose={onClose}
      title="Test this workflow"
      description="Pick a lead and pretend a payment arrived. Nothing is changed — you only see what would happen."
      className="max-w-2xl"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
          <Button
            disabled={!leadId || test.isPending}
            onClick={() =>
              leadId && test.mutate({ id: workflowId, body: { lead_id: leadId, data, graph } })
            }
          >
            {test.isPending ? 'Running…' : 'Run test'}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="test-lead-search">Lead</Label>
          <Input
            id="test-lead-search"
            placeholder="Search by name or phone"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          <div className="border-border max-h-40 overflow-y-auto rounded-lg border">
            {(leads.data?.items ?? []).slice(0, 20).map((lead) => {
              const name = typeof lead.values.name === 'string' ? lead.values.name : ''
              return (
                <button
                  key={lead.id}
                  type="button"
                  onClick={() => setLeadId(lead.id)}
                  className={`hover:bg-muted/50 block w-full px-3 py-1.5 text-left text-sm ${
                    lead.id === leadId ? 'bg-primary/10 font-medium' : ''
                  }`}
                >
                  {name || lead.identity_value}{' '}
                  <span className="text-muted-foreground">{lead.identity_value}</span>
                </button>
              )
            })}
            {leads.data && leads.data.items.length === 0 ? (
              <p className="text-muted-foreground px-3 py-2 text-sm">No leads match.</p>
            ) : null}
          </div>
          {chosen ? (
            <p className="text-muted-foreground text-xs">Testing on {chosen.identity_value}</p>
          ) : null}
        </div>

        {trigger ? (
          <div className="space-y-1.5">
            <Label>The pretend {trigger.label.toLowerCase()}</Label>
            <div className="grid grid-cols-2 gap-2">
              {trigger.variables.map((v) => (
                <Input
                  key={v.key}
                  aria-label={v.label}
                  placeholder={v.label}
                  value={data[v.key] ?? ''}
                  onChange={(event) => setData({ ...data, [v.key]: event.target.value })}
                />
              ))}
            </div>
          </div>
        ) : (
          <p className="text-muted-foreground text-sm">Choose the trigger first.</p>
        )}

        {test.error ? (
          <p role="alert" className="text-destructive text-sm">
            {test.error instanceof ApiError ? test.error.message : 'The test could not run.'}
          </p>
        ) : null}

        {test.data ? (
          <div className="border-border space-y-2 rounded-lg border p-3" data-testid="test-result">
            <p className="text-sm font-semibold">
              Result:{' '}
              <Badge variant={test.data.status === 'FAILED' ? 'destructive' : 'success'}>
                {test.data.status === 'FAILED' ? 'A step would fail' : 'Would work'}
              </Badge>
            </p>
            <ol className="space-y-1.5 text-sm">
              {test.data.steps.map((step, i) => (
                <li key={`${step.node_id}-${i}`}>
                  <span className="font-medium">{step.label}</span>{' '}
                  <Badge
                    variant={
                      step.result === 'failed'
                        ? 'destructive'
                        : step.result === 'no'
                          ? 'outline'
                          : 'success'
                    }
                  >
                    {step.result}
                  </Badge>
                  <span className="text-muted-foreground"> — {step.detail}</span>
                </li>
              ))}
              {test.data.steps.length === 0 ? (
                <li className="text-muted-foreground">Nothing is linked after the trigger yet.</li>
              ) : null}
            </ol>
          </div>
        ) : null}
      </div>
    </Dialog>
  )
}
