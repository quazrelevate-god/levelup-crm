/**
 * Calling numbers (docs/16): which of the operator's numbers AI calls go out
 * from, in rotation order. Read by workspace admins; set by the operator only.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/api/client'

export interface CallingNumbers {
  /** In rotation order. Empty: Bolna's default number. */
  readonly numbers: readonly string[]
  /** Only the platform operator may change them. */
  readonly editable: boolean
}

const key = (workspaceId: string) => ['calling-numbers', workspaceId] as const
const path = (workspaceId: string) => `/workspaces/${workspaceId}/voice/calling-numbers`

export function useCallingNumbers(workspaceId: string) {
  return useQuery({
    queryKey: key(workspaceId),
    queryFn: () => api.get<CallingNumbers>(path(workspaceId)),
    retry: false,
  })
}

export function useSetCallingNumbers(workspaceId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (numbers: readonly string[]) =>
      api.put<CallingNumbers>(path(workspaceId), { numbers }),
    onSuccess: (saved) => queryClient.setQueryData(key(workspaceId), saved),
  })
}

export type NumberRequestStatus = 'PENDING' | 'ACTIVE' | 'PAST_DUE' | 'CANCELLED'

export interface NumberRequest {
  readonly id: string
  readonly status: NumberRequestStatus
  readonly region: string | null
  readonly note: string | null
  readonly monthly_price: string
  readonly currency: string
  readonly phone_number: string | null
  readonly paid_until: string | null
  readonly created_at: string
  readonly fulfilled_at: string | null
  readonly cancelled_at: string | null
}

export interface NumberRequests {
  readonly items: readonly NumberRequest[]
  /** What a new request costs per 30 days. */
  readonly monthly_price: string
  readonly currency: string
  /** Only the platform operator may fulfil or cancel. */
  readonly is_operator: boolean
}

const requestsKey = (workspaceId: string) => ['number-requests', workspaceId] as const
const requestsPath = (workspaceId: string) => `/workspaces/${workspaceId}/voice/number-requests`

export function useNumberRequests(workspaceId: string) {
  return useQuery({
    queryKey: requestsKey(workspaceId),
    queryFn: () => api.get<NumberRequests>(requestsPath(workspaceId)),
    retry: false,
  })
}

/** Requesting, fulfilling or cancelling moves money and numbers: refresh all three. */
function useNumberRequestMutation<TVariables>(
  workspaceId: string,
  mutationFn: (variables: TVariables) => Promise<NumberRequest>,
) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn,
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: requestsKey(workspaceId) }),
        queryClient.invalidateQueries({ queryKey: key(workspaceId) }),
        queryClient.invalidateQueries({ queryKey: ['wallet', workspaceId] }),
      ])
    },
  })
}

export function useRequestNumber(workspaceId: string) {
  return useNumberRequestMutation(
    workspaceId,
    (body: { readonly region: string | null; readonly note: string | null }) =>
      api.post<NumberRequest>(requestsPath(workspaceId), body),
  )
}

export function useFulfilNumberRequest(workspaceId: string) {
  return useNumberRequestMutation(
    workspaceId,
    ({ id, phoneNumber }: { readonly id: string; readonly phoneNumber: string }) =>
      api.post<NumberRequest>(`${requestsPath(workspaceId)}/${id}/fulfil`, {
        phone_number: phoneNumber,
      }),
  )
}

export function useCancelNumberRequest(workspaceId: string) {
  return useNumberRequestMutation(workspaceId, (id: string) =>
    api.post<NumberRequest>(`${requestsPath(workspaceId)}/${id}/cancel`),
  )
}
