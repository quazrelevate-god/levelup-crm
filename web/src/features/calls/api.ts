/**
 * Reading AI calls (docs/13 §6).
 *
 * Two queries against endpoints that already exist server-side: a paginated
 * list, and one call in full. Nothing here writes — a call record is the
 * webhook's to create and nobody else's to change, and the raw provider
 * payload is read-only by design.
 *
 * The lead panel's card and the Call Details list are the *same* list query
 * with different filters, so a lead's most recent completed call cannot
 * disagree with what the list shows for that lead.
 */

import { useQuery } from '@tanstack/react-query'

import { api } from '@/api/client'
import type { Page } from '@/api/types'

/** Who a call was with, in the workspace's own headline fields. */
export interface VoiceCallLeadRef {
  readonly lead_id: string
  readonly identity_value: string
  readonly primary_h1: unknown
  readonly primary_h2: unknown
  readonly primary_h1_label: string | null
  readonly primary_h2_label: string | null
}

export interface VoiceCallSummary {
  readonly id: string
  readonly lead: VoiceCallLeadRef
  readonly execution_id: string | null
  readonly status: string
  readonly bolna_status: string | null
  readonly duration_seconds: number | null
  readonly summary: string | null
  readonly summary_source: string | null
  readonly agent_id: string | null
  readonly created_at: string
  readonly dispatched_at: string | null
  readonly completed_at: string | null
  /** A test call to a tester's phone, not a call this lead received. */
  readonly is_test: boolean
  /** Bolna recorded it; it plays through the CRM (docs/20). */
  readonly has_recording?: boolean
  /** `user` (the lead) or `agent`, as Bolna reports who hung up. */
  readonly hangup_by?: string | null
  /** The carrier's reason the call ended. */
  readonly hangup_reason?: string | null
  readonly ring_seconds?: number | null
}

/** One extracted item, flattened out of Bolna's grouping by the server. */
export interface Extraction {
  readonly group: string | null
  readonly name: string
  readonly path: string
  readonly value: unknown
  readonly confidence: number | null
}

export interface VoiceCallDetail extends VoiceCallSummary {
  readonly recipient_phone: string
  /** The calling number it went out from; null on Bolna's default number. */
  readonly caller_phone: string | null
  readonly transcript: string | null
  readonly extracted_data: Record<string, unknown>
  /** The same extractions, flattened for display — grouped or not. */
  readonly extractions: readonly Extraction[]
  /** Sanitised server-side; credential-shaped fields arrive redacted. */
  readonly raw_payload: Record<string, unknown>
  readonly webhook_received_at: string | null
  readonly call_log_id: string | null
  readonly summary_error: string | null
  readonly last_error: string | null
}

const base = (workspaceId: string) => `/workspaces/${workspaceId}/voice`

export const callsKey = (workspaceId: string) => ['voice-calls', workspaceId] as const

/**
 * A poll interval that may depend on what the last fetch returned.
 *
 * A call is worth re-reading while it is still running, or while it has
 * finished but Bolna's summary has not arrived — neither of which the caller
 * can know before the query runs, so React Query hands it the query instead.
 */
type CallsPoll =
  number | false | ((query: { state: { data?: Page<VoiceCallSummary> } }) => number | false)

interface CallListOptions {
  /** Restrict to one lead — the lead panel's card always does. */
  readonly leadId?: string
  /** Only calls that finished, i.e. the ones that have something to say. */
  readonly completedOnly?: boolean
  readonly limit?: number
  readonly enabled?: boolean
  readonly refetchInterval?: CallsPoll
}

export function useVoiceCalls(workspaceId: string, options: CallListOptions = {}) {
  const { leadId, completedOnly = false, limit = 50, enabled = true } = options
  return useQuery({
    refetchInterval: options.refetchInterval ?? false,
    queryKey: [...callsKey(workspaceId), { leadId: leadId ?? null, completedOnly, limit }] as const,
    enabled,
    queryFn: () =>
      api.get<Page<VoiceCallSummary>>(`${base(workspaceId)}/calls`, {
        query: {
          limit,
          ...(leadId ? { lead_id: leadId } : {}),
          ...(completedOnly ? { completed_only: true } : {}),
        },
      }),
  })
}

export function useVoiceCall(workspaceId: string, callId: string | null) {
  return useQuery({
    queryKey: [...callsKey(workspaceId), 'detail', callId ?? ''] as const,
    enabled: callId !== null,
    // A call record never changes once written, so there is nothing to poll.
    queryFn: () => api.get<VoiceCallDetail>(`${base(workspaceId)}/calls/${callId as string}`),
    retry: false,
  })
}
