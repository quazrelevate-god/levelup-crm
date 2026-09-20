/**
 * The lead panel's AI Call Summary card (docs/13).
 *
 * Small on purpose. It answers one question — "what happened on the last AI
 * call to this lead?" — and hands everything else to the dedicated call page.
 * The transcript, the extracted data and the raw provider payload are
 * deliberately *not* here: a lead panel is for working the lead, not for
 * reading a vendor body.
 *
 * The summary shown is the one the backend already stored (Bolna's own, or its
 * recorded fallback). Nothing here generates or reformats a summary.
 */

import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { useVoiceCalls } from '@/features/calls/api'
import { formatCallDuration, formatCallStatus } from '@/features/calls/format'

interface AiCallSummaryCardProps {
  readonly workspaceId: string
  readonly leadId: string
}

export function AiCallSummaryCard({ workspaceId, leadId }: AiCallSummaryCardProps) {
  // The most recent *completed* call for this lead, and only this lead: the
  // server filters by lead id, so nothing another lead's call can reach here.
  const calls = useVoiceCalls(workspaceId, { leadId, completedOnly: true, limit: 1 })
  const call = calls.data?.items[0]

  if (calls.isPending) {
    return (
      <div className="rounded-md border p-3 text-sm" data-testid="ai-call-summary-card">
        <p className="text-muted-foreground">Loading the latest AI call…</p>
      </div>
    )
  }

  // An error here is not the lead panel's problem to shout about: the rest of
  // the page still works, so the card says the same thing as "nothing yet".
  if (!call) {
    return (
      <div className="rounded-md border p-3 text-sm" data-testid="ai-call-summary-card">
        <p className="mb-1 font-medium">🤖 AI Call Summary</p>
        <p className="text-muted-foreground">No completed AI call yet.</p>
      </div>
    )
  }

  const isFallback = call.summary_source === 'FALLBACK'

  return (
    <div className="space-y-2 rounded-md border p-3 text-sm" data-testid="ai-call-summary-card">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">🤖 AI Call Summary</span>
        <Badge variant="outline">Call placed by AI</Badge>
      </div>

      <div>
        <p className="text-muted-foreground text-xs">
          Summary{isFallback ? ' · no automatic summary was available' : ''}
        </p>
        <p className="whitespace-pre-wrap" data-testid="ai-call-summary-text">
          {call.summary ?? '—'}
        </p>
      </div>

      <p className="text-muted-foreground text-xs">
        Duration: {formatCallDuration(call.duration_seconds)} · Status:{' '}
        {formatCallStatus(call.bolna_status ?? call.status)}
      </p>

      {/* The bridge to the Call Details module, by this call's own CRM id —
          never by lead name or phone number. */}
      <Link
        to={`/calls/${call.id}`}
        className="text-primary inline-block text-sm underline underline-offset-4"
        data-testid="view-raw-call-details"
      >
        View Raw Call Details →
      </Link>
    </div>
  )
}
