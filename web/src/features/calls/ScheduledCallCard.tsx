/**
 * The lead panel's "Next AI call" (docs/22): what is booked, why, and a way to
 * cancel it or book one by hand.
 */

import { useState } from 'react'
import { CalendarClock, X } from 'lucide-react'

import { ApiError } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

import {
  SOURCE_LABELS,
  useCancelScheduledCall,
  useScheduleCall,
  useScheduledCalls,
} from './scheduled'

function when(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    hour: 'numeric',
    minute: '2-digit',
  })
}

export function ScheduledCallCard({
  workspaceId,
  leadId,
}: {
  readonly workspaceId: string
  readonly leadId: string
}) {
  const scheduled = useScheduledCalls(workspaceId, { leadId })
  const book = useScheduleCall(workspaceId)
  const cancel = useCancelScheduledCall(workspaceId)
  const [open, setOpen] = useState(false)
  const [at, setAt] = useState('')
  const next = scheduled.data?.items[0]
  const error = [book.error, cancel.error].find(Boolean)

  return (
    <div className="space-y-2 rounded-md border p-3 text-sm" data-testid="scheduled-call-card">
      <div className="flex items-center gap-2">
        <CalendarClock aria-hidden="true" className="size-4" />
        <span className="font-medium">Next AI call</span>
        {!open ? (
          <Button size="sm" variant="ghost" className="ml-auto h-7" onClick={() => setOpen(true)}>
            {next ? 'Change' : 'Schedule'}
          </Button>
        ) : null}
      </div>
      {next ? (
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <p className="font-medium">{when(next.due_at)}</p>
            <p className="text-muted-foreground text-xs">
              {SOURCE_LABELS[next.source] ?? next.source}
              {next.reason ? ` · ${next.reason}` : ''}
            </p>
          </div>
          <Button
            size="sm"
            variant="ghost"
            className="h-7"
            disabled={cancel.isPending}
            onClick={() => cancel.mutate(next.id)}
          >
            <X aria-hidden="true" /> Cancel
          </Button>
        </div>
      ) : !open ? (
        <p className="text-muted-foreground text-xs">Nothing booked.</p>
      ) : null}
      {open ? (
        <form
          className="flex flex-wrap items-center gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            book.mutate(
              { lead_id: leadId, due_at: new Date(at).toISOString() },
              {
                onSuccess: () => {
                  setOpen(false)
                  setAt('')
                },
              },
            )
          }}
        >
          <Input
            type="datetime-local"
            aria-label="When to call"
            className="max-w-56"
            value={at}
            onChange={(event) => setAt(event.target.value)}
          />
          <Button type="submit" size="sm" disabled={!at || book.isPending}>
            Book AI call
          </Button>
          <Button type="button" size="sm" variant="ghost" onClick={() => setOpen(false)}>
            Close
          </Button>
          <p className="text-muted-foreground w-full text-xs">
            Placed within calling hours; replaces anything already booked for this lead.
          </p>
        </form>
      ) : null}
      {error ? (
        <p role="alert" className="text-destructive text-xs">
          {error instanceof ApiError ? error.message : 'That did not work.'}
        </p>
      ) : null}
    </div>
  )
}
