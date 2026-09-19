/**
 * Lead, action and message-template queries (M5).
 *
 * `values` is a map keyed by `lead_fields.key`, never a set of named
 * parameters — the schema belongs to the customer, so the payload has to be
 * open-ended. Anything the caller cannot view is simply absent from the map the
 * server returns.
 */

import { useCallback } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/api/client'
import type {
  Changeset,
  Lead,
  LeadAction,
  MessageTemplate,
  Page,
  RenderedTemplate,
  TemplateChannel,
  VoiceCallTrigger,
  VoiceCallTriggerResult,
} from '@/api/types'

const leadsKey = (workspaceId: string) => ['leads', workspaceId] as const
const timelineKey = (workspaceId: string, leadId: string) =>
  ['lead-actions', workspaceId, leadId] as const
const templatesKey = (workspaceId: string) => ['message-templates', workspaceId] as const

function base(workspaceId: string): string {
  return `/workspaces/${workspaceId}`
}

export function useLeads(workspaceId: string, search: string) {
  return useQuery({
    queryKey: [...leadsKey(workspaceId), search] as const,
    queryFn: () =>
      api.get<Page<Lead>>(`${base(workspaceId)}/leads`, {
        query: { limit: 50, ...(search.trim() ? { q: search.trim() } : {}) },
      }),
  })
}

export function useLead(workspaceId: string, leadId: string | null) {
  return useQuery({
    queryKey: [...leadsKey(workspaceId), 'detail', leadId] as const,
    enabled: leadId !== null,
    queryFn: () => api.get<Lead>(`${base(workspaceId)}/leads/${leadId as string}`),
  })
}

/**
 * `refetchInterval` is for one case: an AI call has been started and its result
 * arrives later, on Bolna's webhook, with nothing to push it to the browser.
 * The detail panel polls while it waits and stops once the call is logged.
 */
export function useLeadTimeline(
  workspaceId: string,
  leadId: string | null,
  options: { readonly refetchInterval?: number | false } = {},
) {
  return useQuery({
    queryKey: timelineKey(workspaceId, leadId ?? ''),
    enabled: leadId !== null,
    refetchInterval: options.refetchInterval ?? false,
    queryFn: () =>
      api.get<Page<LeadAction>>(`${base(workspaceId)}/leads/${leadId as string}/actions`, {
        query: { limit: 100 },
      }),
  })
}

export function useChangesets(workspaceId: string) {
  return useQuery({
    queryKey: ['changesets', workspaceId],
    queryFn: () =>
      api.get<Page<Changeset>>(`${base(workspaceId)}/changesets`, {
        query: { limit: 50 },
      }),
  })
}

export function useMessageTemplates(workspaceId: string, channel?: TemplateChannel) {
  return useQuery({
    queryKey: [...templatesKey(workspaceId), channel ?? 'all'] as const,
    queryFn: () =>
      api.get<MessageTemplate[]>(`${base(workspaceId)}/templates`, {
        query: channel ? { channel } : {},
      }),
  })
}

/**
 * Invalidates leads, the timeline and the changeset log together.
 *
 * They move as one: every mutation opens a changeset and appends actions in the
 * same transaction, so refreshing one without the others would show a lead
 * whose timeline disagrees with it.
 */
function useLeadMutation<TVariables, TResult>(
  workspaceId: string,
  mutationFn: (variables: TVariables) => Promise<TResult>,
) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn,
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: leadsKey(workspaceId) }),
        // M6 draws the list from `POST /leads/search`, which is a different
        // cache entry. Without this a newly created or edited lead is written
        // to the server and then not shown, which reads as the write failing.
        queryClient.invalidateQueries({ queryKey: ['lead-search', workspaceId] }),
        queryClient.invalidateQueries({ queryKey: ['lead-actions', workspaceId] }),
        queryClient.invalidateQueries({ queryKey: ['changesets', workspaceId] }),
      ])
    },
  })
}

export function useCreateLead(workspaceId: string) {
  return useLeadMutation(
    workspaceId,
    (body: {
      values: Record<string, unknown>
      stage_id?: string | null
      assignee_id?: string | null
    }) => api.post<Lead>(`${base(workspaceId)}/leads`, body),
  )
}

export function useUpdateLead(workspaceId: string) {
  return useLeadMutation(
    workspaceId,
    ({
      leadId,
      ...body
    }: {
      leadId: string
      values?: Record<string, unknown>
      stage_id?: string | null
      lost_reason_id?: string | null
      assignee_id?: string | null
      rating?: number | null
    }) => api.patch<Lead>(`${base(workspaceId)}/leads/${leadId}`, body),
  )
}

export function useDeleteLead(workspaceId: string) {
  return useLeadMutation(workspaceId, ({ leadId }: { leadId: string }) =>
    api.delete<void>(`${base(workspaceId)}/leads/${leadId}`),
  )
}

export function useAddNote(workspaceId: string) {
  return useLeadMutation(workspaceId, ({ leadId, body }: { leadId: string; body: string }) =>
    api.post<LeadAction>(`${base(workspaceId)}/leads/${leadId}/notes`, { body }),
  )
}

export function useLogCall(workspaceId: string) {
  return useLeadMutation(
    workspaceId,
    ({
      leadId,
      ...body
    }: {
      leadId: string
      direction: string
      disposition_id: string
      duration_seconds: number
      notes?: string | null
    }) => api.post<LeadAction>(`${base(workspaceId)}/leads/${leadId}/calls`, body),
  )
}

export function useLogCustomAction(workspaceId: string) {
  return useLeadMutation(
    workspaceId,
    ({
      leadId,
      ...body
    }: {
      leadId: string
      action_type_id: string
      values: Record<string, unknown>
    }) => api.post<LeadAction>(`${base(workspaceId)}/leads/${leadId}/custom-actions`, body),
  )
}

/** Records that a message was composed. Nothing here implies delivery. */
export function useRecordMessage(workspaceId: string) {
  return useLeadMutation(
    workspaceId,
    ({
      leadId,
      ...body
    }: {
      leadId: string
      channel: TemplateChannel
      body: string
      template_id?: string | null
    }) => api.post<LeadAction>(`${base(workspaceId)}/leads/${leadId}/messages`, body),
  )
}

export function useCreateMessageTemplate(workspaceId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: {
      channel: TemplateChannel
      name: string
      body: string
      subject?: string | null
      shared?: boolean
    }) => api.post<MessageTemplate>(`${base(workspaceId)}/templates`, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: templatesKey(workspaceId) }),
  })
}

export function useArchiveMessageTemplate(workspaceId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ templateId }: { templateId: string }) =>
      api.delete<void>(`${base(workspaceId)}/templates/${templateId}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: templatesKey(workspaceId) }),
  })
}

/**
 * Render a template against a lead.
 *
 * Substitution runs server-side through `FieldProjectionService`, so a
 * placeholder naming a field the sender cannot view comes back in
 * `unresolved` rather than resolving. The compose screen surfaces that list.
 */
export function useRenderTemplate(workspaceId: string) {
  return useMutation({
    mutationFn: ({ templateId, leadId }: { templateId: string; leadId: string }) =>
      api.post<RenderedTemplate>(`${base(workspaceId)}/templates/${templateId}/render`, {
        lead_id: leadId,
      }),
  })
}

/**
 * Ask the CRM to place a Bolna call for one lead.
 *
 * Deliberately sends nothing but `lead_id`. The server resolves the lead,
 * projects the fields this caller may view, renders them for speech and adds
 * the reserved `crm_*` context — so the browser never assembles that payload
 * and never learns a field the caller could not otherwise read. The Bolna
 * credential stays server-side; nothing here can reach the vendor directly.
 *
 * No cache invalidation: the trigger records a call attempt and does not touch
 * the lead or its timeline. The write-back arrives later on Bolna's completion
 * webhook, and invalidating now would only refetch unchanged rows.
 */
export function useRefreshLeadData(workspaceId: string) {
  const queryClient = useQueryClient()
  // After an AI call is logged: extraction write-back may have corrected a
  // field, so the list and the open lead must re-read, not just the timeline.
  return useCallback(
    () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: leadsKey(workspaceId) }),
        queryClient.invalidateQueries({ queryKey: ['lead-search', workspaceId] }),
        queryClient.invalidateQueries({ queryKey: ['changesets', workspaceId] }),
      ]),
    [queryClient, workspaceId],
  )
}

export function useTriggerVoiceCall(workspaceId: string) {
  return useMutation({
    mutationFn: (body: VoiceCallTrigger) =>
      api.post<VoiceCallTriggerResult>(`${base(workspaceId)}/voice/calls`, body),
  })
}
