/**
 * Voice extraction mapping queries and mutations (docs/12).
 *
 * One workspace-scoped list plus four writes. The writes each invalidate the
 * list — a small enough set that a per-row cache would be more machinery than
 * it earns.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/api/client'

export interface VoiceExtractionMapping {
  readonly id: string
  readonly disposition_name: string
  readonly target_field_key: string
  readonly min_confidence: number
  readonly is_enabled: boolean
  readonly created_at: string
  readonly updated_at: string
}

export interface VoiceExtractionMappingCreate {
  readonly disposition_name: string
  readonly target_field_key: string
  readonly min_confidence: number
  readonly is_enabled: boolean
}

export interface VoiceExtractionMappingUpdate {
  readonly disposition_name?: string
  readonly target_field_key?: string
  readonly min_confidence?: number
  readonly is_enabled?: boolean
}

const listKey = (workspaceId: string) =>
  ['voice-extraction-mappings', workspaceId] as const

function base(workspaceId: string): string {
  return `/workspaces/${workspaceId}/voice/extraction-mappings`
}

export function useVoiceExtractionMappings(workspaceId: string) {
  return useQuery({
    queryKey: listKey(workspaceId),
    queryFn: () => api.get<VoiceExtractionMapping[]>(base(workspaceId)),
  })
}

function useInvalidate(workspaceId: string) {
  const queryClient = useQueryClient()
  return () => queryClient.invalidateQueries({ queryKey: listKey(workspaceId) })
}

export function useCreateVoiceExtractionMapping(workspaceId: string) {
  const invalidate = useInvalidate(workspaceId)
  return useMutation({
    mutationFn: (body: VoiceExtractionMappingCreate) =>
      api.post<VoiceExtractionMapping>(base(workspaceId), body),
    onSuccess: invalidate,
  })
}

export function useUpdateVoiceExtractionMapping(workspaceId: string) {
  const invalidate = useInvalidate(workspaceId)
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: VoiceExtractionMappingUpdate }) =>
      api.patch<VoiceExtractionMapping>(`${base(workspaceId)}/${id}`, body),
    onSuccess: invalidate,
  })
}

export function useDeleteVoiceExtractionMapping(workspaceId: string) {
  const invalidate = useInvalidate(workspaceId)
  return useMutation({
    mutationFn: (id: string) => api.delete<void>(`${base(workspaceId)}/${id}`),
    onSuccess: invalidate,
  })
}
