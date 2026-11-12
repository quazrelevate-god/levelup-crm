/**
 * Scheduled AI calls (docs/22): calls the background worker places later — a
 * callback the lead asked for, a workflow's call, one a person booked, or a
 * bulk AI call from the lead list (docs/23).
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/api/client'
import type { Page } from '@/api/types'

export interface ScheduledCall {
  readonly id: string
  readonly lead_id: string
  readonly lead_name: string
  readonly due_at: string
  /** PENDING, PLACING, PLACED, CANCELLED, SKIPPED, FAILED. */
  readonly status: string
  /** CALLBACK (the lead asked), WORKFLOW, MANUAL, or BULK. */
  readonly source: string
  readonly purpose: string | null
  readonly reason: string | null
  readonly call_id: string | null
  readonly attempts: number
  readonly last_error: string | null
  readonly created_at: string
}

const key = (workspaceId: string) => ['scheduled-calls', workspaceId] as const
const path = (workspaceId: string) => `/workspaces/${workspaceId}/voice/scheduled-calls`

export function useScheduledCalls(
  workspaceId: string,
  { leadId, upcomingOnly = true }: { leadId?: string; upcomingOnly?: boolean } = {},
) {
  return useQuery({
    queryKey: [...key(workspaceId), leadId ?? null, upcomingOnly],
    queryFn: () =>
      api.get<Page<ScheduledCall>>(path(workspaceId), {
        query: {
          limit: 50,
          ...(leadId ? { lead_id: leadId } : {}),
          upcoming_only: upcomingOnly,
        },
      }),
    // A call due now is placed by the worker within a minute.
    refetchInterval: 30_000,
  })
}

export function useScheduleCall(workspaceId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: { lead_id: string; due_at: string; reason?: string }) =>
      api.post<ScheduledCall>(path(workspaceId), body),
    onSuccess: () => client.invalidateQueries({ queryKey: key(workspaceId) }),
  })
}

export function useCancelScheduledCall(workspaceId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.delete<ScheduledCall>(`${path(workspaceId)}/${id}`),
    onSuccess: () => client.invalidateQueries({ queryKey: key(workspaceId) }),
  })
}

export const SOURCE_LABELS: Record<string, string> = {
  CALLBACK: 'The lead asked',
  WORKFLOW: 'A workflow',
  MANUAL: 'Booked by hand',
  BULK: 'Bulk AI call',
}

export interface BulkCallResult {
  readonly queued: number
  readonly already_waiting: number
  readonly never_call: number
  readonly not_found: number
  readonly starts_at: string | null
  readonly at_once: number
}

export interface BulkCallStatus {
  /** Leads still waiting their turn. */
  readonly waiting: number
  /** AI calls going right now. */
  readonly on_calls: number
  /** How many calls run at the same time. */
  readonly at_once: number
}

const bulkKey = (workspaceId: string) => [...key(workspaceId), 'bulk'] as const

export function useBulkCallStatus(workspaceId: string) {
  return useQuery({
    queryKey: bulkKey(workspaceId),
    queryFn: () => api.get<BulkCallStatus>(`${path(workspaceId)}/bulk`),
    // Quick while a bulk call is running, so the counts visibly go down.
    refetchInterval: (query) => {
      const data = query.state.data
      return data && (data.waiting > 0 || data.on_calls > 0) ? 10_000 : 60_000
    },
    retry: false,
  })
}

export function useBulkCall(workspaceId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (leadIds: readonly string[]) =>
      api.post<BulkCallResult>(`${path(workspaceId)}/bulk`, { lead_ids: leadIds }),
    onSuccess: () => client.invalidateQueries({ queryKey: key(workspaceId) }),
  })
}

export function useStopBulkCall(workspaceId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: () => api.post<{ cancelled: number }>(`${path(workspaceId)}/bulk/stop`, {}),
    onSuccess: () => client.invalidateQueries({ queryKey: key(workspaceId) }),
  })
}
