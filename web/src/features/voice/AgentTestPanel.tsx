/**
 * "Test this call" on the lead panel (docs/15).
 *
 * Two ways to check a call before a real lead hears it:
 *
 * - **What the agent will know** — every variable this lead's call would send,
 *   with the empty ones called out, because an empty variable is a fact the
 *   agent must not invent. Built by the server's own call code; nothing is
 *   called and nothing is written.
 * - **Test call to my phone** — the agent rings the tester instead of the lead
 *   and behaves exactly as it would with this lead. Recorded on the timeline as
 *   a test, charged like any call, and it never changes the lead's stage.
 */

import { useState } from 'react'
import { Eye, PhoneCall } from 'lucide-react'

import { ApiError } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { cn } from '@/lib/utils'
import { useCallPreview, type CallPreview } from '@/features/voice/agentContext'

const TEST_PHONE_KEY = 'marbi.testPhone'

function rememberedPhone(): string {
  try {
    return window.localStorage.getItem(TEST_PHONE_KEY) ?? ''
  } catch {
    return ''
  }
}

function rememberPhone(phone: string): void {
  try {
    window.localStorage.setItem(TEST_PHONE_KEY, phone)
  } catch {
    // Private mode or blocked storage: the number is simply not remembered.
  }
}

function display(value: unknown): string {
  if (value === null || value === undefined) return ''
  return typeof value === 'string' ? value : JSON.stringify(value)
}

function PreviewTable({ preview }: { readonly preview: CallPreview }) {
  const blank = new Set(preview.blank_variables)
  const entries = Object.entries(preview.user_data)
  const own = entries.filter(([name]) => !name.startsWith('crm_') && name !== 'call_opening')
  const reserved = entries.filter(([name]) => name.startsWith('crm_') || name === 'call_opening')

  const rows = (list: [string, unknown][]) =>
    list.map(([name, value]) => (
      <tr key={name} className="border-border/50 border-b last:border-b-0">
        <td className="py-1.5 pr-3 align-top font-mono text-xs whitespace-nowrap">{`{${name}}`}</td>
        <td className={cn('py-1.5 align-top text-xs', blank.has(name) && 'text-muted-foreground')}>
          {blank.has(name) ? (
            <span className="text-warning">blank — the agent will not know this</span>
          ) : (
            <span className="break-words whitespace-pre-wrap">{display(value)}</span>
          )}
        </td>
      </tr>
    ))

  return (
    <div className="space-y-3" data-testid="agent-preview">
      <p className="text-muted-foreground text-xs">
        Would dial {preview.recipient_phone ?? 'no number (the lead has none)'} ·{' '}
        {preview.call_count === 0
          ? 'first call'
          : `${preview.call_count} earlier call${preview.call_count === 1 ? '' : 's'}`}
        {preview.blank_variables.length > 0
          ? ` · ${preview.blank_variables.length} blank`
          : ' · nothing blank'}
      </p>
      <table className="w-full">
        <tbody>{rows(own)}</tbody>
      </table>
      <details>
        <summary className="text-muted-foreground cursor-pointer text-xs">
          Call memory and CRM details ({reserved.length})
        </summary>
        <table className="mt-2 w-full">
          <tbody>{rows(reserved)}</tbody>
        </table>
      </details>
    </div>
  )
}

export function AgentTestPanel({
  workspaceId,
  leadId,
  busy,
  onTestCall,
}: {
  readonly workspaceId: string
  readonly leadId: string
  readonly busy: boolean
  readonly onTestCall: (phone: string) => void
}) {
  const preview = useCallPreview(workspaceId)
  const [followup, setFollowup] = useState(false)
  const [phone, setPhone] = useState(rememberedPhone)

  const previewError =
    preview.error instanceof ApiError
      ? preview.error.message
      : preview.error
        ? 'Preview failed.'
        : null

  return (
    <div
      className="border-border/60 space-y-3 rounded-xl border p-3"
      data-testid="agent-test-panel"
    >
      <div className="flex items-center justify-between gap-2">
        <h4 className="text-sm font-medium">Test this call</h4>
        <Badge variant="outline">Nobody real is called</Badge>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <Button
          size="sm"
          variant="outline"
          disabled={preview.isPending}
          onClick={() => preview.mutate({ lead_id: leadId, followup })}
        >
          <Eye aria-hidden="true" />
          {preview.isPending ? 'Loading…' : 'What the agent will know'}
        </Button>
        <Label className="text-muted-foreground flex items-center gap-2 text-xs font-normal">
          <Checkbox checked={followup} onChange={(event) => setFollowup(event.target.checked)} />
          as a follow-up call
        </Label>
      </div>
      {previewError ? (
        <p role="alert" className="text-destructive text-xs">
          {previewError}
        </p>
      ) : null}
      {preview.data ? <PreviewTable preview={preview.data} /> : null}

      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          if (!phone.trim()) return
          rememberPhone(phone.trim())
          onTestCall(phone.trim())
        }}
      >
        <div className="min-w-48 flex-1 space-y-1.5">
          <Label htmlFor="test-phone" className="text-xs">
            Your phone number
          </Label>
          <Input
            id="test-phone"
            type="tel"
            inputMode="tel"
            autoComplete="tel"
            placeholder="e.g. 90000 12345"
            value={phone}
            onChange={(event) => setPhone(event.target.value)}
          />
        </div>
        <Button size="sm" type="submit" disabled={busy || !phone.trim()}>
          <PhoneCall aria-hidden="true" />
          {busy ? 'Starting…' : 'Test call to my phone'}
        </Button>
      </form>
      <p className="text-muted-foreground text-xs">
        The agent calls you instead of this lead and talks to you exactly as it would to them —
        their name, programme and what earlier calls said. Call twice to hear it remember. Shown as
        a test on the timeline, never changes the lead&apos;s stage, uses wallet minutes like any
        call, and only works between 9am and 9pm.
      </p>
    </div>
  )
}
