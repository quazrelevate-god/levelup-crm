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
import { Bot, ChevronRight } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { PageHeader } from '@/components/ui/page-header'
import { cn } from '@/lib/utils'
import { useAuth } from '@/features/auth/context'
import { useVoiceCalls, type VoiceCallSummary } from '@/features/calls/api'
import { formatCallDuration, formatCallMoment, formatCallStatus } from '@/features/calls/format'
import { toDisplayStringOr } from '@/lib/format'

function CallRow({ call }: { readonly call: VoiceCallSummary }) {
  const failed = call.status === 'FAILED'

  return (
    <li
      className={cn(
        'border-border/60 bg-card/60 relative rounded-2xl border p-3.5 transition-shadow duration-200',
        'hover:shadow-e2 focus-within:ring-ring/45 focus-within:ring-[3px]',
      )}
      data-testid="call-row"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3.5">
          <span
            aria-hidden="true"
            className={cn(
              'flex size-10 shrink-0 items-center justify-center rounded-xl',
              failed ? 'bg-destructive/12 text-destructive' : 'bg-primary/12 text-primary',
            )}
          >
            <Bot className="size-5" />
          </span>
          <div className="min-w-0">
            <p className="truncate font-medium">
              {toDisplayStringOr(call.lead.primary_h1, call.lead.identity_value)}
            </p>
            {call.lead.primary_h2 ? (
              <p className="text-muted-foreground truncate text-sm">
                {toDisplayStringOr(call.lead.primary_h2)}
              </p>
            ) : null}
            <p className="text-muted-foreground mt-0.5 text-xs">
              {formatCallMoment(call.completed_at ?? call.created_at)} ·{' '}
              {formatCallDuration(call.duration_seconds)}
            </p>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <Badge variant={failed ? 'destructive' : 'outline'}>
            {formatCallStatus(call.bolna_status ?? call.status)}
          </Badge>
          {/* Stretched link: the whole row is the target, but only one anchor
              exists, so the accessibility tree still sees a single control. */}
          <Link
            to={`/calls/${call.id}`}
            className="text-muted-foreground hover:text-foreground after:absolute after:inset-0 after:content-[''] focus-visible:outline-none"
            data-testid="view-call-details"
          >
            <span className="sr-only">View Details</span>
            <ChevronRight aria-hidden="true" className="size-5" />
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
    <div className="space-y-5">
      <PageHeader
        title="Call details"
        description="Every call the AI agent has placed, newest first. Open one for its summary, transcript and the provider's own record."
      />

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
