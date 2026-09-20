/**
 * Call Details — every AI call in the workspace, newest first (docs/13).
 *
 * The list deliberately shows only what identifies a call: who it was with, in
 * the workspace's own headline fields, and how it went. Everything heavy lives
 * one click away on the call's own page, so a workspace with a thousand calls
 * does not ship a thousand transcripts to draw a list.
 *
 * Which leads appear is the server's decision, not this page's: the endpoint
 * returns only calls whose lead the caller may see.
 */

import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useAuth } from '@/features/auth/context'
import { useVoiceCalls, type VoiceCallSummary } from '@/features/calls/api'
import { formatCallDuration, formatCallMoment, formatCallStatus } from '@/features/calls/format'
import { toDisplayStringOr } from '@/lib/format'

function CallRow({ call }: { readonly call: VoiceCallSummary }) {
  return (
    <li className="rounded-md border p-3" data-testid="call-row">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-medium">
            🤖 {toDisplayStringOr(call.lead.primary_h1, call.lead.identity_value)}
          </p>
          {call.lead.primary_h2 ? (
            <p className="text-muted-foreground text-sm">
              {toDisplayStringOr(call.lead.primary_h2)}
            </p>
          ) : null}
          <p className="text-muted-foreground mt-1 text-xs">
            {formatCallMoment(call.completed_at ?? call.created_at)} ·{' '}
            {formatCallDuration(call.duration_seconds)}
          </p>
        </div>
        <div className="flex flex-col items-end gap-2">
          <Badge variant={call.status === 'FAILED' ? 'destructive' : 'outline'}>
            {formatCallStatus(call.bolna_status ?? call.status)}
          </Badge>
          <Link
            to={`/calls/${call.id}`}
            className="text-primary text-sm underline underline-offset-4"
            data-testid="view-call-details"
          >
            View Details
          </Link>
        </div>
      </div>
    </li>
  )
}

export function CallsPage() {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string
  const calls = useVoiceCalls(workspaceId)

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Call details</h1>
        <p className="text-muted-foreground text-sm">
          Every call the AI agent has placed, newest first. Open one for its summary, transcript and
          the provider's own record.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>AI calls</CardTitle>
        </CardHeader>
        <CardContent>
          {calls.isPending ? (
            <p className="text-muted-foreground text-sm">Loading calls…</p>
          ) : calls.isError ? (
            <p className="text-muted-foreground text-sm">
              Those calls could not be loaded. Your permission template may not allow viewing call
              history.
            </p>
          ) : calls.data && calls.data.items.length > 0 ? (
            <ol className="space-y-2" data-testid="call-list">
              {calls.data.items.map((call) => (
                <CallRow key={call.id} call={call} />
              ))}
            </ol>
          ) : (
            <p className="text-muted-foreground text-sm">No AI calls have been recorded yet.</p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
