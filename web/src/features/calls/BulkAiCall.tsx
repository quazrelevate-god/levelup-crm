/**
 * Bulk AI call from the lead list (docs/23).
 *
 * Tick leads, press **AI call**, confirm: the leads are queued and the worker
 * calls a few at a time (10 by default), starting the next as each call ends.
 * While it runs, a small counter beside the button shows how many are waiting
 * and how many are on calls, with a Stop for the waiting ones.
 *
 * Hidden entirely for someone whose permission template cannot place calls —
 * the status read is refused for them, and a button that can only fail is
 * worse than none.
 */

import { useState } from 'react'
import { PhoneCall, Square } from 'lucide-react'

import { ApiError } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import {
  type BulkCallResult,
  useBulkCall,
  useBulkCallStatus,
  useStopBulkCall,
} from '@/features/calls/scheduled'

interface BulkAiCallProps {
  readonly workspaceId: string
  readonly selectedIds: ReadonlySet<string>
  /** The leads are queued: clear the selection and say what happened. */
  readonly onQueued: (message: string) => void
  /** Say what happened, replacing an earlier message. */
  readonly onNotice: (message: string) => void
}

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`
}

/** "Calling 12 leads, 10 at a time…" plus anyone left out and why. */
function queuedMessage(result: BulkCallResult): string {
  const parts: string[] = []
  if (result.queued > 0) {
    const later =
      result.starts_at && new Date(result.starts_at).getTime() > Date.now() + 60_000
        ? `They start at ${new Date(result.starts_at).toLocaleString()}, when calling hours open.`
        : `Calling now, ${result.at_once} at a time — the rest wait their turn.`
    parts.push(`${plural(result.queued, 'lead', 'leads')} queued for an AI call. ${later}`)
  } else {
    parts.push('Nobody was queued.')
  }
  if (result.already_waiting > 0) {
    parts.push(
      `${plural(result.already_waiting, 'lead already has', 'leads already have')} an AI call booked.`,
    )
  }
  if (result.never_call > 0) {
    parts.push(`${plural(result.never_call, 'lead is', 'leads are')} on your Never-AI-call rule.`)
  }
  if (result.not_found > 0) {
    parts.push(`${plural(result.not_found, 'lead is', 'leads are')} no longer available.`)
  }
  return parts.join(' ')
}

export function BulkAiCall({ workspaceId, selectedIds, onQueued, onNotice }: BulkAiCallProps) {
  const status = useBulkCallStatus(workspaceId)
  const start = useBulkCall(workspaceId)
  const stop = useStopBulkCall(workspaceId)
  const [open, setOpen] = useState(false)
  const count = selectedIds.size

  if (status.isError) return null
  const atOnce = status.data?.at_once ?? 10
  const waiting = status.data?.waiting ?? 0
  const onCalls = status.data?.on_calls ?? 0
  const failure =
    start.error instanceof ApiError
      ? start.error.message
      : start.error
        ? 'The calls could not be queued.'
        : null

  return (
    <>
      {waiting > 0 || onCalls > 0 ? (
        <span
          role="status"
          className="bg-muted text-muted-foreground flex items-center gap-2 rounded-full py-1 pr-1 pl-3 text-sm"
        >
          <span className="relative flex size-2">
            <span className="bg-success absolute inline-flex size-full animate-ping rounded-full opacity-60" />
            <span className="bg-success relative inline-flex size-2 rounded-full" />
          </span>
          {onCalls} on calls · {waiting} waiting
          {waiting > 0 ? (
            <Button
              size="sm"
              variant="ghost"
              className="h-7 rounded-full"
              disabled={stop.isPending}
              title="Cancel the calls still waiting. Calls already going will finish."
              onClick={() =>
                stop.mutate(undefined, {
                  onSuccess: ({ cancelled }) =>
                    onNotice(
                      `Stopped ${plural(cancelled, 'waiting AI call', 'waiting AI calls')}. ` +
                        'Calls already going will finish.',
                    ),
                })
              }
            >
              <Square aria-hidden="true" className="size-3" /> Stop
            </Button>
          ) : null}
        </span>
      ) : null}
      <Button
        disabled={count === 0}
        title={count === 0 ? 'Tick the leads you want the AI to call' : undefined}
        onClick={() => {
          start.reset()
          setOpen(true)
        }}
      >
        <PhoneCall aria-hidden="true" />
        {count === 0 ? 'AI call' : `AI call ${count}`}
      </Button>

      <Dialog
        open={open}
        onClose={() => setOpen(false)}
        title={`AI call ${plural(count, 'lead', 'leads')}?`}
        description="The AI calls them in the order you picked them."
        footer={
          <>
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              disabled={start.isPending || count === 0}
              onClick={() =>
                start.mutate([...selectedIds], {
                  onSuccess: (result) => {
                    setOpen(false)
                    onQueued(queuedMessage(result))
                  },
                })
              }
            >
              <PhoneCall aria-hidden="true" />
              {start.isPending ? 'Queueing…' : 'Start calling'}
            </Button>
          </>
        }
      >
        <ul className="text-muted-foreground list-disc space-y-1.5 pl-5 text-sm">
          <li>
            <b className="text-foreground">{atOnce} calls at the same time.</b>{' '}
            {count > atOnce
              ? `The other ${count - atOnce} wait their turn and start as soon as a call ends.`
              : 'All of them start straight away.'}
          </li>
          <li>Only within calling hours — outside them, the calls start when hours open.</li>
          <li>
            Each call is charged from your wallet. Leads on your Never-AI-call rule are skipped.
          </li>
          <li>You can stop the waiting calls at any time.</li>
        </ul>
        {failure ? (
          <p role="alert" className="text-destructive text-sm">
            {failure}
          </p>
        ) : null}
      </Dialog>
    </>
  )
}
