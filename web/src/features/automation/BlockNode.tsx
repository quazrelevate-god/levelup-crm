/**
 * One block on the canvas: a coloured header, a line or two saying what it
 * does, and its exits — green "yes" and red "no" for a check, one for the rest.
 */

import { memo, useContext } from 'react'
import { Handle, Position, type NodeProps } from '@xyflow/react'
import { AlertTriangle, CheckCircle2, XCircle } from 'lucide-react'

import { cn } from '@/lib/utils'

import type { NodeData, NodeType } from './api'
import { BLOCKS, BlockLookupsContext, describeBlock } from './blockSpecs'

function exitClass(exit: string) {
  if (exit === 'yes') return '!bg-emerald-500 !border-emerald-600'
  if (exit === 'no') return '!bg-rose-500 !border-rose-600'
  return '!bg-slate-400 !border-slate-500'
}

export const BlockNode = memo(function BlockNode({ id, type, data, selected }: NodeProps) {
  const lookups = useContext(BlockLookupsContext)
  const spec = BLOCKS[type as NodeType]
  if (!spec) return null
  const nodeData = data as NodeData
  const Icon = spec.icon
  const trigger = lookups.triggers.find((t) => t.key === (data as { trigger?: string }).trigger)
  const lines = describeBlock(spec.type, nodeData, lookups, trigger)
  const problem = lookups.problems.get(id)
  const visit = lookups.visited.get(id)
  const dimmed = lookups.visited.size > 0 && !visit && spec.type !== 'trigger'

  return (
    <div
      className={cn(
        'bg-card relative w-60 rounded-xl border text-left shadow-sm transition-[box-shadow,opacity]',
        selected ? 'ring-primary/60 ring-2' : 'border-border',
        problem && !visit ? 'border-amber-500/70' : '',
        visit === 'failed' ? 'ring-2 ring-rose-500' : '',
        dimmed ? 'opacity-40' : '',
      )}
    >
      {spec.type !== 'trigger' ? (
        <Handle
          type="target"
          position={Position.Left}
          className="!size-3 !border-2 !border-slate-500 !bg-white"
        />
      ) : null}
      <div
        className={cn(
          'flex items-center gap-2 rounded-t-[11px] px-3 py-2 text-xs font-semibold',
          spec.header,
        )}
      >
        <Icon aria-hidden="true" className="size-3.5 shrink-0" />
        <span className="truncate">{nodeData.title || spec.title}</span>
        {visit ? <VisitMark result={visit} /> : null}
      </div>
      <div className="space-y-0.5 px-3 py-2 text-xs">
        {lines.map((line, i) => (
          <p key={i} className="text-foreground/85 truncate">
            {line}
          </p>
        ))}
        {problem && !visit ? (
          <p className="flex items-center gap-1 pt-1 text-[11px] text-amber-700 dark:text-amber-400">
            <AlertTriangle aria-hidden="true" className="size-3" /> {problem}
          </p>
        ) : null}
      </div>
      {spec.exits.length === 2 ? (
        <>
          <span className="pointer-events-none absolute top-[38%] -right-7 text-[10px] font-semibold text-emerald-600">
            Yes
          </span>
          <Handle
            id="yes"
            type="source"
            position={Position.Right}
            style={{ top: '45%' }}
            className={cn('!size-3 !border-2', exitClass('yes'))}
          />
          <span className="pointer-events-none absolute top-[68%] -right-6 text-[10px] font-semibold text-rose-600">
            No
          </span>
          <Handle
            id="no"
            type="source"
            position={Position.Right}
            style={{ top: '75%' }}
            className={cn('!size-3 !border-2', exitClass('no'))}
          />
        </>
      ) : (
        <Handle
          id="out"
          type="source"
          position={Position.Right}
          className={cn('!size-3 !border-2', exitClass('out'))}
        />
      )}
    </div>
  )
})

function VisitMark({ result }: { readonly result: string }) {
  if (result === 'failed') {
    return <XCircle aria-label="Failed" className="ml-auto size-4 shrink-0" />
  }
  return (
    <span className="ml-auto flex items-center gap-1 text-[10px] font-semibold tracking-wide uppercase">
      <CheckCircle2 aria-hidden="true" className="size-3.5" />
      {result === 'yes' || result === 'no' ? result : ''}
    </span>
  )
}
