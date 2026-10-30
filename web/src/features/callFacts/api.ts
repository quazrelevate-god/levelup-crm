/**
 * Call facts (docs/17): reference details the voice agent tells a lead — batch
 * dates, mentors, fees — each set with a rule choosing which leads it is for.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/api/client'
import type { GroupNode } from '@/api/types'

export interface CallFact {
  /** What the agent's script can use: `{{batch_start_date}}`. */
  readonly key: string
  readonly label: string
  readonly value: string
}

export interface CallFactSet {
  readonly id: string
  readonly name: string
  readonly priority: number
  /** The filter rule; `{}` applies to every lead. */
  readonly conditions: GroupNode | Record<string, never>
  /** The rule in words, or "Every lead". */
  readonly rule: string
  readonly facts: readonly CallFact[]
  readonly is_active: boolean
}

export interface CallFactSetWrite {
  readonly name: string
  readonly priority: number
  readonly conditions: GroupNode | Record<string, never>
  readonly facts: readonly { readonly label: string; readonly value: string }[]
}

const key = (workspaceId: string) => ['call-facts', workspaceId] as const
const path = (workspaceId: string) => `/workspaces/${workspaceId}/settings/call-facts`

export function useCallFacts(workspaceId: string) {
  return useQuery({
    queryKey: key(workspaceId),
    queryFn: () => api.get<CallFactSet[]>(path(workspaceId)),
  })
}

function useCallFactMutation<TVariables>(
  workspaceId: string,
  mutationFn: (variables: TVariables) => Promise<CallFactSet>,
) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn,
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: key(workspaceId) }),
        // Facts appear in the agent's variables and every call preview.
        queryClient.invalidateQueries({ queryKey: ['voice-variables', workspaceId] }),
      ])
    },
  })
}

export function useCreateCallFacts(workspaceId: string) {
  return useCallFactMutation(workspaceId, (body: CallFactSetWrite) =>
    api.post<CallFactSet>(path(workspaceId), body),
  )
}

export function useUpdateCallFacts(workspaceId: string) {
  return useCallFactMutation(
    workspaceId,
    ({ id, body }: { readonly id: string; readonly body: CallFactSetWrite }) =>
      api.patch<CallFactSet>(`${path(workspaceId)}/${id}`, body),
  )
}

export function useArchiveCallFacts(workspaceId: string) {
  return useCallFactMutation(workspaceId, (id: string) =>
    api.delete<CallFactSet>(`${path(workspaceId)}/${id}`),
  )
}
