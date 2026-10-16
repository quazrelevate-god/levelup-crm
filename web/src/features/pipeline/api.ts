/**
 * Pipeline, taxonomy and preferences queries (M3).
 *
 * Nothing here names a stage, a reason or a disposition. Every label the UI
 * renders is a row this module fetched.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/api/client'
import type {
  CallDisposition,
  CustomActionType,
  LostReason,
  Stage,
  StagePipeline,
  WorkspacePreferences,
} from '@/api/types'

const stagesKey = (workspaceId: string) => ['stages', workspaceId] as const
const reasonsKey = (workspaceId: string) => ['lost-reasons', workspaceId] as const
const dispositionsKey = (workspaceId: string) => ['dispositions', workspaceId] as const
const actionsKey = (workspaceId: string) => ['custom-actions', workspaceId] as const
const prefsKey = (workspaceId: string) => ['preferences', workspaceId] as const
const outcomeStagesKey = (workspaceId: string) => ['voice-outcome-stages', workspaceId] as const

function base(workspaceId: string): string {
  return `/workspaces/${workspaceId}/settings`
}

export function useStages(workspaceId: string) {
  return useQuery({
    queryKey: stagesKey(workspaceId),
    queryFn: () => api.get<StagePipeline>(`${base(workspaceId)}/stages`),
  })
}

export function useLostReasons(workspaceId: string, includeArchived = false) {
  return useQuery({
    queryKey: [...reasonsKey(workspaceId), includeArchived] as const,
    queryFn: () =>
      api.get<LostReason[]>(`${base(workspaceId)}/lost-reasons`, {
        query: { include_archived: includeArchived },
      }),
  })
}

export function useDispositions(workspaceId: string, includeArchived = false) {
  return useQuery({
    queryKey: [...dispositionsKey(workspaceId), includeArchived] as const,
    queryFn: () =>
      api.get<CallDisposition[]>(`${base(workspaceId)}/call-dispositions`, {
        query: { include_archived: includeArchived },
      }),
  })
}

/**
 * Custom actions sit behind the `custom_actions` feature flag, so a 403 here is
 * an expected state rather than a bug. The caller renders the flag prompt.
 */
export function useCustomActions(workspaceId: string, enabled = true) {
  return useQuery({
    queryKey: actionsKey(workspaceId),
    enabled,
    retry: false,
    queryFn: () => api.get<CustomActionType[]>(`${base(workspaceId)}/custom-actions`),
  })
}

export function usePreferences(workspaceId: string) {
  return useQuery({
    queryKey: prefsKey(workspaceId),
    queryFn: () => api.get<WorkspacePreferences>(`${base(workspaceId)}/preferences`),
  })
}

function useTaxonomyMutation<TVariables, TResult>(
  workspaceId: string,
  mutationFn: (variables: TVariables) => Promise<TResult>,
) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn,
    onSuccess: async () => {
      // Broad on purpose: turning a feature flag off changes what the custom
      // actions query is even allowed to return.
      await Promise.all(
        // `outcomeStagesKey` is in the list because archiving a stage can
        // empty the mapping that names it, and the mapping is read back
        // resolved to stage rows — so a stage rename changes what it shows.
        [stagesKey, reasonsKey, dispositionsKey, actionsKey, prefsKey, outcomeStagesKey].map(
          (key) => queryClient.invalidateQueries({ queryKey: key(workspaceId) }),
        ),
      )
    },
  })
}

// --- stages ------------------------------------------------------------------

export function useCreateStage(workspaceId: string) {
  return useTaxonomyMutation(workspaceId, (body: { label: string; color?: string }) =>
    api.post<Stage>(`${base(workspaceId)}/stages`, body),
  )
}

export function useUpdateStage(workspaceId: string) {
  return useTaxonomyMutation(
    workspaceId,
    ({ stageId, ...body }: { stageId: string; label?: string; color?: string }) =>
      api.patch<Stage>(`${base(workspaceId)}/stages/${stageId}`, body),
  )
}

export function useArchiveStage(workspaceId: string) {
  return useTaxonomyMutation(workspaceId, ({ stageId }: { stageId: string }) =>
    api.delete<Stage>(`${base(workspaceId)}/stages/${stageId}`),
  )
}

export function useReorderStages(workspaceId: string) {
  return useTaxonomyMutation(workspaceId, ({ orderedIds }: { orderedIds: string[] }) =>
    api.patch<Stage[]>(`${base(workspaceId)}/stages/reorder`, { ordered_ids: orderedIds }),
  )
}

// --- lost reasons ------------------------------------------------------------

export function useCreateLostReason(workspaceId: string) {
  return useTaxonomyMutation(workspaceId, (body: { label: string }) =>
    api.post<LostReason>(`${base(workspaceId)}/lost-reasons`, body),
  )
}

export function useUpdateLostReason(workspaceId: string) {
  return useTaxonomyMutation(
    workspaceId,
    ({ reasonId, ...body }: { reasonId: string; label?: string }) =>
      api.patch<LostReason>(`${base(workspaceId)}/lost-reasons/${reasonId}`, body),
  )
}

export function useArchiveLostReason(workspaceId: string) {
  return useTaxonomyMutation(workspaceId, ({ reasonId }: { reasonId: string }) =>
    api.delete<LostReason>(`${base(workspaceId)}/lost-reasons/${reasonId}`),
  )
}

// --- call dispositions -------------------------------------------------------

export function useCreateDisposition(workspaceId: string) {
  return useTaxonomyMutation(workspaceId, (body: { label: string }) =>
    api.post<CallDisposition>(`${base(workspaceId)}/call-dispositions`, body),
  )
}

export function useUpdateDisposition(workspaceId: string) {
  return useTaxonomyMutation(
    workspaceId,
    ({ dispositionId, ...body }: { dispositionId: string; label?: string }) =>
      api.patch<CallDisposition>(`${base(workspaceId)}/call-dispositions/${dispositionId}`, body),
  )
}

export function useSetDefaultDisposition(workspaceId: string) {
  return useTaxonomyMutation(workspaceId, ({ dispositionId }: { dispositionId: string }) =>
    api.post<CallDisposition>(
      `${base(workspaceId)}/call-dispositions/${dispositionId}/set-default`,
    ),
  )
}

export function useArchiveDisposition(workspaceId: string) {
  return useTaxonomyMutation(workspaceId, ({ dispositionId }: { dispositionId: string }) =>
    api.post<CallDisposition>(`${base(workspaceId)}/call-dispositions/${dispositionId}/archive`),
  )
}

// --- custom actions ----------------------------------------------------------

export function useCreateCustomAction(workspaceId: string) {
  return useTaxonomyMutation(
    workspaceId,
    (body: {
      name: string
      score?: number
      direction?: string
      allow_predated?: boolean
      description?: string | null
    }) => api.post<CustomActionType>(`${base(workspaceId)}/custom-actions`, body),
  )
}

export function useUpdateCustomAction(workspaceId: string) {
  return useTaxonomyMutation(
    workspaceId,
    ({
      typeId,
      ...body
    }: {
      typeId: string
      name?: string
      score?: number
      direction?: string
      allow_predated?: boolean
    }) => api.patch<CustomActionType>(`${base(workspaceId)}/custom-actions/${typeId}`, body),
  )
}

export function useArchiveCustomAction(workspaceId: string) {
  return useTaxonomyMutation(workspaceId, ({ typeId }: { typeId: string }) =>
    api.post<CustomActionType>(`${base(workspaceId)}/custom-actions/${typeId}/archive`),
  )
}

export function useAddActionField(workspaceId: string) {
  return useTaxonomyMutation(
    workspaceId,
    ({
      typeId,
      ...body
    }: {
      typeId: string
      label: string
      field_type: string
      is_required?: boolean
      options?: string[]
    }) => api.post(`${base(workspaceId)}/custom-actions/${typeId}/fields`, body),
  )
}

// --- preferences -------------------------------------------------------------

export function useUpdatePreferences(workspaceId: string) {
  return useTaxonomyMutation(workspaceId, (body: Partial<WorkspacePreferences>) =>
    api.patch<WorkspacePreferences>(`${base(workspaceId)}/preferences`, body),
  )
}

// --- which stages a finished AI call moves a lead to --------------------------

/**
 * The pair an admin configures, read back in full so the UI can name the
 * stages rather than show two ids. Null for either when nothing is set, which
 * is every workspace until someone configures it — and an unset mapping means
 * a finished call moves no lead, which is the safe default.
 */
export interface VoiceOutcomeStages {
  readonly answered: Stage | null
  readonly unanswered: Stage | null
}

export function useVoiceOutcomeStages(workspaceId: string) {
  return useQuery({
    queryKey: outcomeStagesKey(workspaceId),
    queryFn: () => api.get<VoiceOutcomeStages>(`${base(workspaceId)}/voice-outcome-stages`),
  })
}

export function useSetVoiceOutcomeStages(workspaceId: string) {
  return useTaxonomyMutation(
    workspaceId,
    (body: { readonly answered: string; readonly unanswered: string }) =>
      api.put<VoiceOutcomeStages>(`${base(workspaceId)}/voice-outcome-stages`, body),
  )
}

export function useClearVoiceOutcomeStages(workspaceId: string) {
  return useTaxonomyMutation(workspaceId, () =>
    api.delete<void>(`${base(workspaceId)}/voice-outcome-stages`),
  )
}
