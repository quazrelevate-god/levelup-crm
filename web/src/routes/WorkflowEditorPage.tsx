/**
 * One workflow: draw it, test it, switch it on, and see every run (docs/18).
 *
 * Laid out like TeleCRM's builder — blocks on the left, the canvas in the
 * middle, the selected block's settings on the right — with two tabs: the
 * editor, and the runs, where any run can be shown on the canvas as the path
 * it took.
 *
 * The drawing is local until Save. Switching a workflow on saves the drawing in
 * the same request, so what runs is always what is on screen.
 */

import '@xyflow/react/dist/style.css'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  addEdge,
  Background,
  type Connection,
  Controls,
  type Edge,
  MarkerType,
  type Node,
  type NodeChange,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
} from '@xyflow/react'
import { ArrowLeft, FlaskConical, Play, Save, X } from 'lucide-react'

import { ApiError } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { useAuth } from '@/features/auth/context'
import {
  type GraphEdge,
  type GraphNode,
  type NodeData,
  type NodeType,
  type WorkflowGraph,
  type WorkflowRun,
  useAutomationCatalogue,
  useUpdateWorkflow,
  useWorkflow,
} from '@/features/automation/api'
import { BlockNode } from '@/features/automation/BlockNode'
import { BLOCKS, BlockLookupsContext } from '@/features/automation/blockSpecs'
import { allStages } from '@/features/automation/helpers'
import { Inspector } from '@/features/automation/Inspector'
import { RunsPanel } from '@/features/automation/RunsPanel'
import { TestDialog } from '@/features/automation/TestDialog'
import { useLeadFields } from '@/features/fields/api'
import { useStages } from '@/features/pipeline/api'
import { cn } from '@/lib/utils'

type FlowNode = Node<NodeData>

/** Every block type draws with the same component; its spec does the rest. */
const NODE_TYPES_MAP = Object.fromEntries(
  (Object.keys(BLOCKS) as NodeType[]).map((type) => [type, BlockNode]),
)

function edgeStyle(handle: string | null | undefined): Partial<Edge> {
  const colour = handle === 'yes' ? '#10b981' : handle === 'no' ? '#f43f5e' : '#94a3b8'
  return {
    style: { stroke: colour, strokeWidth: 2 },
    markerEnd: { type: MarkerType.ArrowClosed, color: colour },
  }
}

function toFlow(graph: WorkflowGraph): { nodes: FlowNode[]; edges: Edge[] } {
  return {
    nodes: (graph.nodes ?? []).map((n) => ({
      id: n.id,
      type: n.type,
      position: n.position,
      data: n.data ?? {},
      deletable: n.type !== 'trigger',
    })),
    edges: (graph.edges ?? []).map((e) => ({
      id: e.id,
      source: e.source,
      sourceHandle: e.sourceHandle,
      target: e.target,
      ...edgeStyle(e.sourceHandle),
    })),
  }
}

function toGraph(nodes: FlowNode[], edges: Edge[]): WorkflowGraph {
  return {
    nodes: nodes.map((n): GraphNode => ({
      id: n.id,
      type: n.type as NodeType,
      position: { x: Math.round(n.position.x), y: Math.round(n.position.y) },
      data: n.data,
    })),
    edges: edges.map((e): GraphEdge => ({
      id: e.id,
      source: e.source,
      sourceHandle: e.sourceHandle ?? 'out',
      target: e.target,
    })),
  }
}

/** Follows the `dark` class the theme toggle sets, so the canvas controls match. */
function useDarkClass(): boolean {
  const [dark, setDark] = useState(() => document.documentElement.classList.contains('dark'))
  useEffect(() => {
    const root = document.documentElement
    const observer = new MutationObserver(() => setDark(root.classList.contains('dark')))
    observer.observe(root, { attributes: true, attributeFilter: ['class'] })
    return () => observer.disconnect()
  }, [])
  return dark
}

function newId(type: string) {
  return `${type}-${Math.random().toString(36).slice(2, 8)}`
}

function errorText(error: unknown): string | null {
  if (!error) return null
  return error instanceof ApiError ? error.message : 'Something went wrong. Please try again.'
}

export function WorkflowEditorPage() {
  const { workflowId = '' } = useParams()
  return (
    <ReactFlowProvider>
      <Editor key={workflowId} workflowId={workflowId} />
    </ReactFlowProvider>
  )
}

function Editor({ workflowId }: { readonly workflowId: string }) {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string
  const navigate = useNavigate()
  const workflow = useWorkflow(workspaceId, workflowId)
  const catalogue = useAutomationCatalogue(workspaceId)
  const fields = useLeadFields(workspaceId)
  const stages = useStages(workspaceId)
  const update = useUpdateWorkflow(workspaceId)
  const dark = useDarkClass()

  const [nodes, setNodes, onNodesChange] = useNodesState<FlowNode>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])
  const [name, setName] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const [tab, setTab] = useState<'editor' | 'runs'>('editor')
  const [testing, setTesting] = useState(false)
  const [shownRun, setShownRun] = useState<WorkflowRun | null>(null)

  useEffect(() => {
    if (!workflow.data || loaded) return
    const flow = toFlow(workflow.data.graph)
    setNodes(flow.nodes)
    setEdges(flow.edges)
    setName(workflow.data.name)
    setLoaded(true)
  }, [workflow.data, loaded, setNodes, setEdges])

  const triggerNode = nodes.find((n) => n.type === 'trigger')
  const trigger = catalogue.data?.triggers.find((t) => t.key === triggerNode?.data.trigger)

  const lookups = useMemo(
    () => ({
      triggers: catalogue.data?.triggers ?? [],
      operators: catalogue.data?.operators ?? [],
      fields: fields.data ?? [],
      stages: allStages(stages.data),
      problems: dirty
        ? new Map<string, string>()
        : new Map(
            (workflow.data?.problems ?? [])
              .filter((p) => p.node_id)
              .map((p) => [p.node_id as string, p.message]),
          ),
      visited: new Map((shownRun?.steps ?? []).map((s) => [s.node_id, s.result])),
    }),
    [catalogue.data, fields.data, stages.data, workflow.data, dirty, shownRun],
  )

  const handleNodesChange = useCallback(
    (changes: NodeChange<FlowNode>[]) => {
      const kept = changes.filter(
        (c) => !(c.type === 'remove' && nodes.find((n) => n.id === c.id)?.type === 'trigger'),
      )
      if (kept.some((c) => c.type !== 'select' && c.type !== 'dimensions')) setDirty(true)
      onNodesChange(kept)
    },
    [nodes, onNodesChange],
  )

  const onConnect = useCallback(
    (connection: Connection) => {
      setEdges((current) =>
        addEdge(
          { ...connection, id: newId('edge'), ...edgeStyle(connection.sourceHandle) },
          // One link per exit-to-block pair; a second identical drag is a no-op.
          current,
        ),
      )
      setDirty(true)
    },
    [setEdges],
  )

  const addBlock = (type: NodeType) => {
    const from = nodes.find((n) => n.id === selected) ?? triggerNode
    const id = newId(type)
    const siblings = from ? edges.filter((e) => e.source === from.id).length : 0
    const position = from
      ? { x: from.position.x + 320, y: from.position.y + siblings * 140 }
      : { x: 400, y: 160 }
    setNodes((current) => [
      ...current.map((n) => ({ ...n, selected: false })),
      { id, type, position, data: BLOCKS[type].initial(), selected: true },
    ])
    if (from) {
      const exits = BLOCKS[from.type as NodeType].exits
      const used = new Set(edges.filter((e) => e.source === from.id).map((e) => e.sourceHandle))
      const exit = exits.find((x) => !used.has(x)) ?? exits[0]
      setEdges((current) => [
        ...current,
        { id: newId('edge'), source: from.id, sourceHandle: exit, target: id, ...edgeStyle(exit) },
      ])
    }
    setSelected(id)
    setDirty(true)
  }

  const changeData = (id: string, next: NodeData) => {
    setNodes((current) => current.map((n) => (n.id === id ? { ...n, data: next } : n)))
    setDirty(true)
  }

  const removeBlock = (id: string) => {
    setNodes((current) => current.filter((n) => n.id !== id))
    setEdges((current) => current.filter((e) => e.source !== id && e.target !== id))
    setSelected(null)
    setDirty(true)
  }

  const save = (extra: { is_active?: boolean } = {}) =>
    update.mutate(
      { id: workflowId, body: { name: name.trim(), graph: toGraph(nodes, edges), ...extra } },
      { onSuccess: () => setDirty(false) },
    )

  if (workflow.isPending) return <p className="text-muted-foreground text-sm">Loading…</p>
  if (workflow.isError) {
    return (
      <p className="text-muted-foreground text-sm">
        This workflow could not be loaded. <Link to="/automations">Back to workflows</Link>
      </p>
    )
  }

  const current = workflow.data
  const selectedNode = nodes.find((n) => n.id === selected)
  const blocking = dirty ? [] : current.problems
  const error = errorText(update.error)

  return (
    <div className="flex h-[calc(100vh-7rem)] min-h-[560px] flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="icon"
          variant="ghost"
          aria-label="Back to workflows"
          onClick={() => void navigate('/automations')}
        >
          <ArrowLeft aria-hidden="true" />
        </Button>
        <Input
          aria-label="Workflow name"
          className="h-9 max-w-80 text-base font-semibold"
          value={name}
          maxLength={120}
          onChange={(event) => {
            setName(event.target.value)
            setDirty(true)
          }}
        />
        <label className="ml-2 flex items-center gap-2 text-sm">
          <Switch
            aria-label="Workflow on"
            checked={current.is_active}
            disabled={update.isPending}
            onCheckedChange={(on) => save({ is_active: on })}
          />
          {current.is_active ? (
            <Badge variant="success">On</Badge>
          ) : (
            <Badge variant="outline">Off</Badge>
          )}
        </label>
        <div className="ml-auto flex items-center gap-2">
          {dirty ? <span className="text-muted-foreground text-xs">Unsaved changes</span> : null}
          <Button size="sm" variant="outline" onClick={() => setTesting(true)}>
            <FlaskConical aria-hidden="true" /> Test
          </Button>
          <Button
            size="sm"
            disabled={!dirty || update.isPending || !name.trim()}
            onClick={() => save()}
          >
            <Save aria-hidden="true" /> {update.isPending ? 'Saving…' : 'Save'}
          </Button>
        </div>
      </div>

      {error ? (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      ) : null}
      {!current.is_active && blocking.length > 0 ? (
        <p className="text-sm text-amber-700 dark:text-amber-400">
          To switch it on: {blocking.map((p) => p.message).join(' · ')}
        </p>
      ) : null}

      <div className="border-border flex gap-1 border-b" role="tablist">
        {(['editor', 'runs'] as const).map((key) => (
          <button
            key={key}
            role="tab"
            aria-selected={tab === key}
            onClick={() => setTab(key)}
            className={cn(
              '-mb-px border-b-2 px-4 py-2 text-sm font-medium',
              tab === key
                ? 'border-primary text-foreground'
                : 'text-muted-foreground border-transparent',
            )}
          >
            {key === 'editor' ? 'Editor' : `Runs (${current.total_runs})`}
          </button>
        ))}
      </div>

      {tab === 'runs' ? (
        <RunsPanel
          workspaceId={workspaceId}
          workflowId={workflowId}
          onShow={(run) => {
            setShownRun(run)
            setSelected(null)
            setTab('editor')
          }}
        />
      ) : (
        <div className="flex min-h-0 flex-1 gap-3">
          <Palette onAdd={addBlock} disabled={shownRun !== null} />
          <div className="border-border bg-muted/30 relative min-w-0 flex-1 overflow-hidden rounded-xl border">
            {shownRun ? (
              <div className="bg-card/95 absolute top-2 left-2 z-10 flex items-center gap-2 rounded-lg border px-3 py-1.5 text-xs shadow-sm">
                <Play aria-hidden="true" className="size-3.5" />
                Path taken for {shownRun.lead_name ?? 'a lead'} on{' '}
                {new Date(shownRun.created_at).toLocaleString()}
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-6 px-2"
                  onClick={() => setShownRun(null)}
                >
                  <X aria-hidden="true" /> Back to editing
                </Button>
              </div>
            ) : null}
            <BlockLookupsContext.Provider value={lookups}>
              <ReactFlow
                nodes={nodes}
                edges={edges}
                nodeTypes={NODE_TYPES_MAP}
                onNodesChange={handleNodesChange}
                onEdgesChange={(changes) => {
                  if (changes.some((c) => c.type === 'remove')) setDirty(true)
                  onEdgesChange(changes)
                }}
                onConnect={onConnect}
                isValidConnection={(c) =>
                  c.source !== c.target && nodes.find((n) => n.id === c.target)?.type !== 'trigger'
                }
                onSelectionChange={({ nodes: chosen }) => setSelected(chosen[0]?.id ?? null)}
                nodesDraggable={shownRun === null}
                nodesConnectable={shownRun === null}
                elementsSelectable={shownRun === null}
                deleteKeyCode={['Backspace', 'Delete']}
                fitView
                fitViewOptions={{ padding: 0.25, maxZoom: 1 }}
                proOptions={{ hideAttribution: true }}
                colorMode={dark ? 'dark' : 'light'}
              >
                <Background gap={18} />
                <Controls showInteractive={false} />
              </ReactFlow>
            </BlockLookupsContext.Provider>
          </div>
          <aside className="w-80 shrink-0 overflow-y-auto">
            <Card>
              <CardContent className="pt-5">
                {selectedNode && shownRun === null ? (
                  <Inspector
                    key={selectedNode.id}
                    workspaceId={workspaceId}
                    nodeId={selectedNode.id}
                    type={selectedNode.type as NodeType}
                    data={selectedNode.data}
                    trigger={trigger}
                    triggers={catalogue.data?.triggers ?? []}
                    operators={catalogue.data?.operators ?? []}
                    onChange={(next) => changeData(selectedNode.id, next)}
                    onDelete={() => removeBlock(selectedNode.id)}
                  />
                ) : shownRun ? (
                  <RunSteps run={shownRun} />
                ) : (
                  <HowTo />
                )}
              </CardContent>
            </Card>
          </aside>
        </div>
      )}

      {testing ? (
        <TestDialog
          workspaceId={workspaceId}
          workflowId={workflowId}
          trigger={trigger}
          graph={toGraph(nodes, edges)}
          onClose={() => setTesting(false)}
        />
      ) : null}
    </div>
  )
}

function Palette({
  onAdd,
  disabled,
}: {
  readonly onAdd: (type: NodeType) => void
  readonly disabled: boolean
}) {
  const groups: { title: string; hint: string; group: 'check' | 'action' }[] = [
    { title: 'Checks', hint: 'If… then go Yes or No', group: 'check' },
    { title: 'Actions', hint: 'Do this to the lead', group: 'action' },
  ]
  return (
    <aside className="w-52 shrink-0 space-y-4 overflow-y-auto" aria-label="Blocks">
      {groups.map(({ title, hint, group }) => (
        <div key={group} className="space-y-1.5">
          <div>
            <p className="text-sm font-semibold">{title}</p>
            <p className="text-muted-foreground text-xs">{hint}</p>
          </div>
          {Object.values(BLOCKS)
            .filter((b) => b.group === group)
            .map((block) => {
              const Icon = block.icon
              return (
                <button
                  key={block.type}
                  type="button"
                  disabled={disabled}
                  onClick={() => onAdd(block.type)}
                  className="border-border bg-card hover:border-primary/50 flex w-full items-center gap-2 rounded-lg border px-2.5 py-2 text-left text-sm disabled:opacity-50"
                  title={block.hint}
                >
                  <span className={cn('rounded-md p-1', block.header)}>
                    <Icon aria-hidden="true" className="size-3.5" />
                  </span>
                  {block.title}
                </button>
              )
            })}
        </div>
      ))}
      <p className="text-muted-foreground text-xs">
        A new block is linked after the one you have selected. Drag from a dot to a block to link
        them yourself; select a link and press Delete to remove it.
      </p>
    </aside>
  )
}

function HowTo() {
  return (
    <div className="text-muted-foreground space-y-2 text-sm">
      <p className="text-foreground font-semibold">How to build it</p>
      <ol className="list-decimal space-y-1.5 pl-4">
        <li>Click the dark “When this happens” block and choose the trigger.</li>
        <li>Add a check, e.g. “If the payment…” amount equals 400.</li>
        <li>From its green Yes dot, add what should happen: change stage, update fields.</li>
        <li>Save, press Test to try it on a lead, then switch it On.</li>
      </ol>
      <p>Click any block to change its settings here.</p>
    </div>
  )
}

export function RunSteps({ run }: { readonly run: WorkflowRun }) {
  return (
    <div className="space-y-2">
      <p className="text-sm font-semibold">What happened</p>
      <ol className="space-y-2">
        {run.steps.map((step, i) => (
          <li key={`${step.node_id}-${i}`} className="text-sm">
            <span className="font-medium">{step.label}</span>{' '}
            <Badge
              variant={
                step.result === 'failed'
                  ? 'destructive'
                  : step.result === 'no'
                    ? 'outline'
                    : 'success'
              }
            >
              {step.result}
            </Badge>
            <p className="text-muted-foreground text-xs">{step.detail}</p>
          </li>
        ))}
        {run.steps.length === 0 ? (
          <li className="text-muted-foreground text-sm">Nothing after the trigger ran.</li>
        ) : null}
      </ol>
    </div>
  )
}
