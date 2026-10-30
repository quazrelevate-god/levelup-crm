/**
 * What the voice agent is told about a lead (docs/15).
 *
 * Two reads, both safe to use as often as needed:
 *
 * - **Preview** — the exact variables a call to one lead would send, built by
 *   the same server code as a real call. Nothing is written and nobody is
 *   called.
 * - **Variables** — every variable an agent's prompt can use, written `{name}`
 *   in the Bolna script, with what each one holds.
 */

import { useMutation, useQuery } from '@tanstack/react-query'

import { api } from '@/api/client'

export interface CallPreview {
  readonly lead_id: string
  readonly recipient_phone: string | null
  readonly agent_id: string | null
  /** Exactly the `user_data` a real call would send. */
  readonly user_data: Record<string, unknown>
  /** Variables that would reach the agent empty: what it will not know. */
  readonly blank_variables: readonly string[]
  readonly previous_call_summary: string | null
  readonly call_count: number
}

export interface VoiceVariable {
  readonly name: string
  /** `field` for one of the workspace's lead fields, `crm` for a reserved one. */
  readonly kind: 'field' | 'fact' | 'crm'
  readonly label: string
  readonly description: string
  readonly always_sent: boolean
  readonly field_type: string | null
}

const base = (workspaceId: string) => `/workspaces/${workspaceId}/voice`

export function useCallPreview(workspaceId: string) {
  return useMutation({
    mutationFn: (body: { readonly lead_id: string; readonly followup?: boolean }) =>
      api.post<CallPreview>(`${base(workspaceId)}/calls/preview`, body),
  })
}

export function useVoiceVariables(workspaceId: string) {
  return useQuery({
    queryKey: ['voice-variables', workspaceId] as const,
    queryFn: () => api.get<VoiceVariable[]>(`${base(workspaceId)}/variables`),
    retry: false,
  })
}
