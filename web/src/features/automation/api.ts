/**
 * Connected apps, workflows and the never-AI-call rule (docs/18).
 *
 * A workflow's drawing is stored exactly as the canvas holds it — nodes with a
 * position and a `data` bag, edges with a source exit — so saving is sending
 * the canvas and loading is handing it back.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/api/client'
import type { GroupNode, Page } from '@/api/types'

// --- types -------------------------------------------------------------------

export interface TriggerVariable {
  readonly key: string
  readonly label: string
  /** `number` or `text`. */
  readonly kind: string
}

export interface TriggerSpec {
  readonly key: string
  readonly label: string
  readonly app: string | null
  readonly description: string
  readonly variables: readonly TriggerVariable[]
}

export interface OperatorSpec {
  readonly key: string
  readonly label: string
  readonly needs_value: boolean
}

export interface AutomationCatalogue {
  readonly triggers: readonly TriggerSpec[]
  readonly operators: readonly OperatorSpec[]
}

export type NodeType =
  'trigger' | 'condition' | 'lead_condition' | 'update_fields' | 'update_stage' | 'add_note'

export interface ConditionRule {
  variable: string
  operator: string
  value: string
}

/** Everything any block can hold; each type reads its own part. */
export interface NodeData extends Record<string, unknown> {
  title?: string
  trigger?: string | null
  match?: 'all' | 'any'
  rules?: ConditionRule[]
  conditions?: GroupNode | Record<string, never>
  values?: { field_key: string; value: string }[]
  stage_id?: string
  lost_reason_id?: string
  text?: string
}

export interface GraphNode {
  readonly id: string
  readonly type: NodeType
  readonly position: { readonly x: number; readonly y: number }
  readonly data: NodeData
}

export interface GraphEdge {
  readonly id: string
  readonly source: string
  readonly sourceHandle: string | null
  readonly target: string
}

export interface WorkflowGraph {
  readonly nodes: readonly GraphNode[]
  readonly edges: readonly GraphEdge[]
}

export interface GraphProblem {
  readonly node_id: string | null
  readonly message: string
}

export interface Workflow {
  readonly id: string
  readonly name: string
  readonly trigger: string | null
  readonly graph: WorkflowGraph
  readonly is_active: boolean
  readonly total_runs: number
  readonly runs_24h: number
  readonly failed_24h: number
  readonly last_run_at: string | null
  /** What must still be filled in before it can be switched on. */
  readonly problems: readonly GraphProblem[]
  readonly created_at: string
  readonly updated_at: string
}

export interface RunStep {
  readonly node_id: string
  readonly type: NodeType
  readonly label: string
  /** `yes` / `no` for a check; `done` / `failed` for an action. */
  readonly result: string
  readonly detail: string
}

export interface WorkflowRun {
  readonly id: string
  readonly lead_id: string | null
  readonly lead_name: string | null
  /** `SUCCESS` or `FAILED`. */
  readonly status: string
  readonly steps: readonly RunStep[]
  readonly data: Record<string, unknown>
  readonly duration_ms: number
  readonly error: string | null
  readonly created_at: string
}

export interface TestResult {
  readonly status: string
  readonly steps: readonly RunStep[]
  readonly error: string | null
}

export interface ConnectedApp {
  readonly provider: string
  readonly label: string
  readonly description: string
  readonly connected: boolean
  readonly is_active: boolean
  readonly webhook_path: string | null
  readonly secret_set: boolean
  readonly settings: Record<string, unknown>
  readonly last_event_at: string | null
}

export type AppEventOutcome =
  'MATCHED' | 'CREATED_LEAD' | 'NO_LEAD' | 'DUPLICATE' | 'IGNORED' | 'REJECTED'

export interface AppEvent {
  readonly id: string
  readonly event_type: string
  readonly trigger: string | null
  /** One of `AppEventOutcome`. */
  readonly outcome: string
  readonly data: Record<string, unknown>
  readonly lead_id: string | null
  readonly lead_name: string | null
  readonly workflows_run: number
  readonly error: string | null
  readonly created_at: string
}

// --- keys --------------------------------------------------------------------

const base = (workspaceId: string) => `/workspaces/${workspaceId}`
const workflowsKey = (workspaceId: string) => ['workflows', workspaceId] as const
const appsKey = (workspaceId: string) => ['apps', workspaceId] as const

/** Runs and payments arrive on their own; the screens look again. */
const LIVE_REFRESH_MS = 15_000

// --- catalogue -----------------------------------------------------------------

export function useAutomationCatalogue(workspaceId: string) {
  return useQuery({
    queryKey: ['automation-catalogue', workspaceId],
    queryFn: () => api.get<AutomationCatalogue>(`${base(workspaceId)}/automation/catalogue`),
    staleTime: Infinity,
  })
}

// --- workflows -------------------------------------------------------------------

export function useWorkflows(workspaceId: string) {
  return useQuery({
    queryKey: workflowsKey(workspaceId),
    queryFn: () => api.get<Workflow[]>(`${base(workspaceId)}/workflows`),
    refetchInterval: LIVE_REFRESH_MS,
  })
}

export function useWorkflow(workspaceId: string, workflowId: string) {
  return useQuery({
    queryKey: [...workflowsKey(workspaceId), workflowId],
    queryFn: () => api.get<Workflow>(`${base(workspaceId)}/workflows/${workflowId}`),
  })
}

export function useCreateWorkflow(workspaceId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: { name: string; trigger?: string | null; graph?: WorkflowGraph }) =>
      api.post<Workflow>(`${base(workspaceId)}/workflows`, body),
    onSuccess: () => client.invalidateQueries({ queryKey: workflowsKey(workspaceId) }),
  })
}

export function useUpdateWorkflow(workspaceId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({
      id,
      body,
    }: {
      id: string
      body: { name?: string; graph?: WorkflowGraph; is_active?: boolean }
    }) => api.patch<Workflow>(`${base(workspaceId)}/workflows/${id}`, body),
    onSuccess: (saved) => {
      client.setQueryData([...workflowsKey(workspaceId), saved.id], saved)
      void client.invalidateQueries({ queryKey: workflowsKey(workspaceId) })
    },
  })
}

export function useDeleteWorkflow(workspaceId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.delete<void>(`${base(workspaceId)}/workflows/${id}`),
    onSuccess: () => client.invalidateQueries({ queryKey: workflowsKey(workspaceId) }),
  })
}

export function useTestWorkflow(workspaceId: string) {
  return useMutation({
    mutationFn: ({
      id,
      body,
    }: {
      id: string
      body: { lead_id: string; data: Record<string, string>; graph?: WorkflowGraph }
    }) => api.post<TestResult>(`${base(workspaceId)}/workflows/${id}/test`, body),
  })
}

export function useWorkflowRuns(
  workspaceId: string,
  workflowId: string,
  { offset, status }: { offset: number; status: string | null },
) {
  return useQuery({
    queryKey: [...workflowsKey(workspaceId), workflowId, 'runs', offset, status],
    queryFn: () =>
      api.get<Page<WorkflowRun>>(`${base(workspaceId)}/workflows/${workflowId}/runs`, {
        query: { limit: 20, offset, ...(status ? { status } : {}) },
      }),
    refetchInterval: LIVE_REFRESH_MS,
  })
}

// --- apps --------------------------------------------------------------------------

export function useApps(workspaceId: string) {
  return useQuery({
    queryKey: appsKey(workspaceId),
    queryFn: () => api.get<ConnectedApp[]>(`${base(workspaceId)}/apps`),
  })
}

export function useConnectApp(workspaceId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (provider: string) =>
      api.post<ConnectedApp>(`${base(workspaceId)}/apps/${provider}/connect`, {}),
    onSuccess: () => client.invalidateQueries({ queryKey: appsKey(workspaceId) }),
  })
}

export function useUpdateApp(workspaceId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({
      provider,
      body,
    }: {
      provider: string
      body: { is_active?: boolean; secret?: string; settings?: Record<string, unknown> }
    }) => api.patch<ConnectedApp>(`${base(workspaceId)}/apps/${provider}`, body),
    onSuccess: () => client.invalidateQueries({ queryKey: appsKey(workspaceId) }),
  })
}

export function useRotateAppUrl(workspaceId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (provider: string) =>
      api.post<ConnectedApp>(`${base(workspaceId)}/apps/${provider}/rotate-url`, {}),
    onSuccess: () => client.invalidateQueries({ queryKey: appsKey(workspaceId) }),
  })
}

export function useAppEvents(workspaceId: string, provider: string, offset: number) {
  return useQuery({
    queryKey: [...appsKey(workspaceId), provider, 'events', offset],
    queryFn: () =>
      api.get<Page<AppEvent>>(`${base(workspaceId)}/apps/${provider}/events`, {
        query: { limit: 20, offset },
      }),
    refetchInterval: LIVE_REFRESH_MS,
  })
}

// --- never AI-call -------------------------------------------------------------------

export function useNeverCallRule(workspaceId: string) {
  return useQuery({
    queryKey: ['voice-never-call', workspaceId],
    queryFn: () =>
      api.get<{ conditions: GroupNode | Record<string, never> }>(
        `${base(workspaceId)}/settings/voice-never-call`,
      ),
  })
}

export function useSetNeverCallRule(workspaceId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (conditions: GroupNode | Record<string, never>) =>
      api.put<{ conditions: GroupNode | Record<string, never> }>(
        `${base(workspaceId)}/settings/voice-never-call`,
        { conditions },
      ),
    onSuccess: (saved) => client.setQueryData(['voice-never-call', workspaceId], saved),
  })
}
