/**
 * Pieces every app screen shares (docs/18, docs/22): numbered setup steps and
 * the list of what the app has sent, with what became of each.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

import { type AppEvent, useAppEvents } from './api'
import { amountText } from './helpers'

const OUTCOMES: Record<string, { label: string; variant: 'success' | 'outline' | 'destructive' }> =
  {
    MATCHED: { label: 'Matched a lead', variant: 'success' },
    CREATED_LEAD: { label: 'New lead created', variant: 'success' },
    NO_LEAD: { label: 'No matching lead', variant: 'outline' },
    DUPLICATE: { label: 'Already handled', variant: 'outline' },
    IGNORED: { label: 'Not a payment', variant: 'outline' },
    REJECTED: { label: 'Refused', variant: 'destructive' },
  }

export function Step({
  n,
  title,
  children,
}: {
  readonly n: number
  readonly title: string
  readonly children: React.ReactNode
}) {
  return (
    <section className="flex gap-3">
      <span className="bg-primary/10 text-primary flex size-7 shrink-0 items-center justify-center rounded-full text-sm font-semibold">
        {n}
      </span>
      <div className="min-w-0 flex-1 space-y-2">
        <p className="font-medium">{title}</p>
        {children}
      </div>
    </section>
  )
}

function who(event: AppEvent): string {
  const phone = typeof event.data.phone === 'string' ? event.data.phone : ''
  const email = typeof event.data.email === 'string' ? event.data.email : ''
  return phone || email || '—'
}

export function AppEvents({
  workspaceId,
  provider,
  title,
}: {
  readonly workspaceId: string
  readonly provider: string
  readonly title: string
}) {
  const [offset, setOffset] = useState(0)
  const events = useAppEvents(workspaceId, provider, offset)
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription>Newest first. Refreshes on its own.</CardDescription>
      </CardHeader>
      <CardContent>
        {events.isPending ? (
          <p className="text-muted-foreground text-sm">Loading…</p>
        ) : events.isError ? (
          <p className="text-muted-foreground text-sm">The list could not be loaded.</p>
        ) : events.data.items.length === 0 ? (
          <p className="text-muted-foreground text-sm">
            Nothing yet. After setup, the next one appears here within seconds.
          </p>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-muted-foreground text-left text-xs">
                  <tr>
                    <th className="py-2 pr-3 font-medium">When</th>
                    <th className="py-2 pr-3 font-medium">Event</th>
                    <th className="py-2 pr-3 font-medium">Amount</th>
                    <th className="py-2 pr-3 font-medium">From</th>
                    <th className="py-2 pr-3 font-medium">Lead</th>
                    <th className="py-2 pr-3 font-medium">Result</th>
                    <th className="py-2 font-medium">Workflows</th>
                  </tr>
                </thead>
                <tbody>
                  {events.data.items.map((event) => {
                    const outcome = OUTCOMES[event.outcome] ?? {
                      label: event.outcome,
                      variant: 'outline' as const,
                    }
                    return (
                      <tr key={event.id} className="border-border/60 border-t align-top">
                        <td className="py-2 pr-3 whitespace-nowrap">
                          {new Date(event.created_at).toLocaleString()}
                        </td>
                        <td className="py-2 pr-3 font-mono text-xs">{event.event_type}</td>
                        <td className="py-2 pr-3">{amountText(event.data) || '—'}</td>
                        <td className="py-2 pr-3">{who(event)}</td>
                        <td className="py-2 pr-3">
                          {event.lead_id ? (
                            <Link
                              to={`/leads?lead=${event.lead_id}`}
                              className="text-primary hover:underline"
                            >
                              {event.lead_name ?? 'Lead'}
                            </Link>
                          ) : (
                            '—'
                          )}
                        </td>
                        <td className="py-2 pr-3">
                          <Badge variant={outcome.variant}>{outcome.label}</Badge>
                          {event.error ? (
                            <p className="text-muted-foreground mt-1 max-w-64 text-xs">
                              {event.error}
                            </p>
                          ) : null}
                        </td>
                        <td className="py-2">{event.workflows_run}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
            <div className="mt-3 flex gap-2">
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
                disabled={offset + 20 >= events.data.total}
                onClick={() => setOffset(offset + 20)}
              >
                Older
              </Button>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}
