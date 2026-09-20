/**
 * One AI call, in full (docs/13 §7).
 *
 * This is the only place raw call data appears. The lead panel links straight
 * here by the call's own CRM id, so arriving from a lead opens *that* call
 * with nothing to search for.
 *
 * The four heavy sections are collapsed by default: somebody opening this page
 * usually wants the summary and the identifiers, and only sometimes the
 * transcript or the provider's body.
 *
 * The raw payload is sanitised server-side — credential-shaped fields arrive
 * already redacted — and is read-only here. There is no endpoint that writes
 * it and no control on this page that pretends otherwise.
 */

import { Link, useParams } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useAuth } from '@/features/auth/context'
import { useVoiceCall, type VoiceCallDetail } from '@/features/calls/api'
import { formatCallDuration, formatCallMoment, formatCallStatus } from '@/features/calls/format'
import { toDisplayStringOr } from '@/lib/format'

/** A collapsible block. `<details>` because it is exactly this, natively. */
function Section({
  title,
  testId,
  children,
}: {
  readonly title: string
  readonly testId: string
  readonly children: React.ReactNode
}) {
  return (
    <details className="rounded-md border p-3" data-testid={testId}>
      <summary className="cursor-pointer text-sm font-medium select-none">{title}</summary>
      <div className="mt-3">{children}</div>
    </details>
  )
}

function Facts({ call }: { readonly call: VoiceCallDetail }) {
  const rows: readonly (readonly [string, string])[] = [
    [
      call.lead.primary_h1_label ?? 'Lead',
      toDisplayStringOr(call.lead.primary_h1, call.lead.identity_value),
    ],
    ...(call.lead.primary_h2_label
      ? ([[call.lead.primary_h2_label, toDisplayStringOr(call.lead.primary_h2)]] as const)
      : []),
    ['CRM call ID', call.id],
    ['CRM lead ID', call.lead.lead_id],
    ['Bolna execution ID', toDisplayStringOr(call.execution_id)],
    ['Agent ID', toDisplayStringOr(call.agent_id)],
    ['Recipient phone', call.recipient_phone],
    ['Status', `${formatCallStatus(call.bolna_status ?? call.status)} (${call.status})`],
    ['Duration', `${call.duration_seconds ?? 0} seconds`],
    ['Started at', formatCallMoment(call.dispatched_at ?? call.created_at)],
    ['Completed at', formatCallMoment(call.completed_at)],
    ['Webhook received at', formatCallMoment(call.webhook_received_at)],
  ]

  return (
    <dl className="grid gap-2 text-sm sm:grid-cols-2">
      {rows.map(([label, value]) => (
        <div key={label}>
          <dt className="text-muted-foreground text-xs">{label}</dt>
          <dd className="font-mono text-xs break-all">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

export function CallDetailPage() {
  const { callId } = useParams<{ callId: string }>()
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string
  const call = useVoiceCall(workspaceId, callId ?? null)

  const back = (
    <Link to="/calls" className="text-primary text-sm underline underline-offset-4">
      ← Back to Call details
    </Link>
  )

  if (call.isPending) {
    return (
      <div className="space-y-4">
        {back}
        <p className="text-muted-foreground text-sm">Loading the call…</p>
      </div>
    )
  }

  if (call.isError || !call.data) {
    // Never the raw API error: an unknown or inaccessible id reads the same,
    // which is also what the server's 404 is careful to do.
    return (
      <div className="space-y-4">
        {back}
        <Card>
          <CardContent className="pt-6">
            <p className="text-sm" data-testid="call-not-found">
              Call details not found.
            </p>
          </CardContent>
        </Card>
      </div>
    )
  }

  const detail = call.data

  return (
    <div className="space-y-4" data-testid="call-detail">
      {back}

      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-muted-foreground text-sm">🤖 AI Call</p>
          <h1 className="text-xl font-semibold">
            {toDisplayStringOr(detail.lead.primary_h1, detail.lead.identity_value)}
          </h1>
          {detail.lead.primary_h2 ? (
            <p className="text-muted-foreground text-sm">
              {toDisplayStringOr(detail.lead.primary_h2)}
            </p>
          ) : null}
          <p className="text-muted-foreground mt-1 text-sm">
            Status: {formatCallStatus(detail.bolna_status ?? detail.status)} · Duration:{' '}
            {formatCallDuration(detail.duration_seconds)} ·{' '}
            {formatCallMoment(detail.completed_at ?? detail.created_at)}
          </p>
        </div>
        <div className="flex flex-col items-end gap-2">
          <Badge variant={detail.status === 'FAILED' ? 'destructive' : 'outline'}>
            {detail.status}
          </Badge>
          <Link
            to={`/leads?lead=${detail.lead.lead_id}`}
            className="text-primary text-sm underline underline-offset-4"
          >
            Open lead →
          </Link>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>AI call summary</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          <p className="text-sm whitespace-pre-wrap" data-testid="call-summary">
            {detail.summary ?? 'No summary was stored for this call.'}
          </p>
          {detail.summary_source === 'FALLBACK' ? (
            <p className="text-muted-foreground text-xs">
              No automatic summary was available for this call
              {detail.summary_error ? ` (${detail.summary_error})` : ''}.
            </p>
          ) : null}
        </CardContent>
      </Card>

      <div className="space-y-2">
        <Section title="Call information" testId="section-call-information">
          <Facts call={detail} />
        </Section>

        <Section title="Transcript" testId="section-transcript">
          {detail.transcript ? (
            <p className="text-sm whitespace-pre-wrap" data-testid="call-transcript">
              {detail.transcript}
            </p>
          ) : (
            <p className="text-muted-foreground text-sm">No transcript was received.</p>
          )}
        </Section>

        <Section title="Extracted data" testId="section-extracted-data">
          {Object.keys(detail.extracted_data).length > 0 ? (
            <pre
              className="bg-muted max-h-96 overflow-auto rounded-md p-3 text-xs"
              data-testid="call-extracted-data"
            >
              {JSON.stringify(detail.extracted_data, null, 2)}
            </pre>
          ) : (
            <p className="text-muted-foreground text-sm">Nothing was extracted from this call.</p>
          )}
        </Section>

        <Section title="Raw provider payload" testId="section-raw-payload">
          <p className="text-muted-foreground mb-2 text-xs">
            The provider's own record of the call, read-only. Credential-shaped fields are redacted
            before it leaves the server.
          </p>
          {Object.keys(detail.raw_payload).length > 0 ? (
            <pre
              className="bg-muted max-h-96 overflow-auto rounded-md p-3 text-xs"
              data-testid="call-raw-payload"
            >
              {JSON.stringify(detail.raw_payload, null, 2)}
            </pre>
          ) : (
            <p className="text-muted-foreground text-sm">No provider payload was stored.</p>
          )}
        </Section>
      </div>
    </div>
  )
}
