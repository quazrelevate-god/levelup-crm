/**
 * Every time a workflow ran: which lead, whether it worked, and step by step
 * what it did. Any run can be shown on the canvas as the path it took.
 */

import { Fragment, useState } from 'react'
import { Link } from 'react-router-dom'
import { ChevronDown, ChevronRight, Route } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Select } from '@/components/ui/select'

import { type WorkflowRun, useWorkflowRuns } from './api'
import { amountText } from './helpers'

export function RunsPanel({
  workspaceId,
  workflowId,
  onShow,
}: {
  readonly workspaceId: string
  readonly workflowId: string
  readonly onShow: (run: WorkflowRun) => void
}) {
  const [offset, setOffset] = useState(0)
  const [status, setStatus] = useState<string | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  const runs = useWorkflowRuns(workspaceId, workflowId, { offset, status })

  return (
    <div className="min-h-0 flex-1 space-y-3 overflow-y-auto">
      <div className="flex items-center gap-2">
        <Select
          aria-label="Show runs"
          className="w-44"
          value={status ?? ''}
          onChange={(event) => {
            setStatus(event.target.value || null)
            setOffset(0)
          }}
        >
          <option value="">All runs</option>
          <option value="SUCCESS">Successful</option>
          <option value="FAILED">Failed</option>
        </Select>
        {runs.data ? (
          <span className="text-muted-foreground text-sm">
            {runs.data.total === 0
              ? 'No runs yet'
              : `${offset + 1}–${Math.min(offset + runs.data.items.length, runs.data.total)} of ${runs.data.total}`}
          </span>
        ) : null}
        <div className="ml-auto flex gap-1">
          <Button
            size="sm"
            variant="outline"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - 20))}
          >
            Newer
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={!runs.data || offset + 20 >= runs.data.total}
            onClick={() => setOffset(offset + 20)}
          >
            Older
          </Button>
        </div>
      </div>

      {runs.isPending ? (
        <p className="text-muted-foreground text-sm">Loading…</p>
      ) : runs.isError ? (
        <p className="text-muted-foreground text-sm">Runs could not be loaded.</p>
      ) : runs.data.items.length === 0 ? (
        <p className="text-muted-foreground text-sm">
          Nothing has run yet. Once the workflow is on, every payment it sees shows up here.
        </p>
      ) : (
        <div className="border-border overflow-hidden rounded-xl border">
          <table className="w-full text-sm">
            <thead className="bg-muted/50 text-muted-foreground text-left text-xs">
              <tr>
                <th className="w-8" />
                <th className="px-3 py-2 font-medium">When</th>
                <th className="px-3 py-2 font-medium">Lead</th>
                <th className="px-3 py-2 font-medium">Amount</th>
                <th className="px-3 py-2 font-medium">Result</th>
                <th className="px-3 py-2 font-medium">Took</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody>
              {runs.data.items.map((run) => (
                <Fragment key={run.id}>
                  <tr
                    className="border-border/60 hover:bg-muted/30 cursor-pointer border-t"
                    onClick={() => setOpen(open === run.id ? null : run.id)}
                  >
                    <td className="pl-3">
                      {open === run.id ? (
                        <ChevronDown aria-hidden="true" className="size-4" />
                      ) : (
                        <ChevronRight aria-hidden="true" className="size-4" />
                      )}
                    </td>
                    <td className="px-3 py-2 whitespace-nowrap">
                      {new Date(run.created_at).toLocaleString()}
                    </td>
                    <td className="px-3 py-2">
                      {run.lead_id ? (
                        <Link
                          to={`/leads?lead=${run.lead_id}`}
                          className="text-primary hover:underline"
                          onClick={(event) => event.stopPropagation()}
                        >
                          {run.lead_name ?? 'Lead'}
                        </Link>
                      ) : (
                        '—'
                      )}
                    </td>
                    <td className="px-3 py-2">{amountText(run.data) || '—'}</td>
                    <td className="px-3 py-2">
                      <Badge variant={run.status === 'FAILED' ? 'destructive' : 'success'}>
                        {run.status === 'FAILED' ? 'Failed' : 'Success'}
                      </Badge>
                    </td>
                    <td className="text-muted-foreground px-3 py-2">{run.duration_ms} ms</td>
                    <td className="px-3 py-2 text-right">
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={(event) => {
                          event.stopPropagation()
                          onShow(run)
                        }}
                      >
                        <Route aria-hidden="true" /> Show path
                      </Button>
                    </td>
                  </tr>
                  {open === run.id ? (
                    <tr className="bg-muted/20">
                      <td />
                      <td colSpan={6} className="px-3 py-3">
                        <ol className="space-y-1.5">
                          {run.steps.map((step, i) => (
                            <li key={`${step.node_id}-${i}`} className="flex gap-2">
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
                              <span>
                                <span className="font-medium">{step.label}</span>
                                <span className="text-muted-foreground"> — {step.detail}</span>
                              </span>
                            </li>
                          ))}
                          {run.steps.length === 0 ? (
                            <li className="text-muted-foreground">
                              Nothing after the trigger ran.
                            </li>
                          ) : null}
                        </ol>
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
