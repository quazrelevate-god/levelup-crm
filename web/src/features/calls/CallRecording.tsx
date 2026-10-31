/**
 * Play an AI call's recording (docs/20).
 *
 * The audio comes through the CRM with the session's token — Bolna's own link
 * opens for anyone who has it, so the browser never sees it. Loaded on press
 * rather than with the page: a call list should not download every call.
 */

import { useEffect, useState } from 'react'
import { Download, Play } from 'lucide-react'

import { ApiError, fetchBlob } from '@/api/client'
import { Button } from '@/components/ui/button'

import type { VoiceCallSummary } from './api'

export function CallRecording({
  workspaceId,
  call,
  compact = false,
}: {
  readonly workspaceId: string
  readonly call: VoiceCallSummary
  readonly compact?: boolean
}) {
  const [src, setSrc] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    return () => {
      if (src) URL.revokeObjectURL(src)
    }
  }, [src])

  if (!call.has_recording) {
    return compact ? null : (
      <p className="text-muted-foreground text-sm">No recording for this call.</p>
    )
  }

  const load = () => {
    setLoading(true)
    setError(null)
    fetchBlob(`/workspaces/${workspaceId}/voice/calls/${call.id}/recording`)
      .then((blob) => setSrc(URL.createObjectURL(blob)))
      .catch((cause: unknown) =>
        setError(cause instanceof ApiError ? cause.message : 'The recording could not be loaded.'),
      )
      .finally(() => setLoading(false))
  }

  return (
    <div className="space-y-2" data-testid="call-recording">
      {src ? (
        <div className="flex flex-wrap items-center gap-2">
          {/* The recording is the call itself; there is no separate caption track. */}
          <audio controls autoPlay src={src} className="h-10 w-full max-w-md" />
          <Button asChild size="sm" variant="ghost">
            <a href={src} download={`call-${call.id}.mp3`}>
              <Download aria-hidden="true" /> Download
            </a>
          </Button>
        </div>
      ) : (
        <Button size="sm" variant="outline" disabled={loading} onClick={load}>
          <Play aria-hidden="true" /> {loading ? 'Loading…' : 'Play recording'}
        </Button>
      )}
      {error ? (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      ) : null}
    </div>
  )
}

const WHO: Record<string, string> = { lead: 'the lead', agent: 'the AI agent' }

/** The carrier's words for why a call ended, said plainly. */
const REASONS: Record<string, string> = {
  inactivity_timeout: 'Nobody spoke for a while (silence timeout)',
  normal_hangup: 'Call ended normally',
  'normal hangup': 'Call ended normally',
  normal_clearing: 'Call ended normally',
  user_busy: 'Line busy',
  'user busy': 'Line busy',
  no_answer: 'Not answered',
  'no answer': 'Not answered',
  call_rejected: 'Call rejected',
  unallocated_number: 'Number does not exist',
  'unallocated number': 'Number does not exist',
  subscriber_absent: 'Phone switched off or not reachable',
  'subscriber absent': 'Phone switched off or not reachable',
  network_out_of_order: 'Network problem',
  max_duration_reached: 'Hit the maximum call length',
}

function reasonText(raw: string): string {
  const known = REASONS[raw.trim().toLowerCase()]
  if (known) return known
  const words = raw.replace(/[_-]+/g, ' ').trim()
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/** "Hung up by the lead · Normal Hangup · rang 7 s" — or nothing if unknown. */
export function CallEnding({ call }: { readonly call: VoiceCallSummary }) {
  const parts = [
    call.hangup_by ? `Hung up by ${WHO[call.hangup_by] ?? call.hangup_by}` : null,
    call.hangup_reason ? reasonText(call.hangup_reason) : null,
    typeof call.ring_seconds === 'number' ? `rang ${call.ring_seconds} s` : null,
  ].filter(Boolean)
  if (parts.length === 0) return null
  return <p className="text-muted-foreground text-xs">{parts.join(' · ')}</p>
}
