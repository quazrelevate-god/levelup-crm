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
