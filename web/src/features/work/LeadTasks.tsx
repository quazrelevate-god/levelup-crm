/**
 * A lead's open follow-ups, on its detail view (M7).
 *
 * Separate from the Tasks page, which is a queue across every lead. Here the
 * question is "what did we promise this person", so the list is short, scoped
 * to them, and completing one is a single click without leaving the record.
 *
 * Completing a task writes to the lead's timeline, so the entry appears just
 * below without a reload — the mutation invalidates both.
 */

import { useState } from 'react'
import { ListChecks } from 'lucide-react'

import { DetailSection } from '@/components/ui/detail-section'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { useCompleteTask, useCreateTask, useLeadTasks } from '@/features/work/api'

export interface LeadTasksProps {
  readonly workspaceId: string
  readonly leadId: string
  /** Band colour in the lead panel's alternating sections. */
  readonly tone?: 'a' | 'b'
}

export function LeadTasks({ workspaceId, leadId, tone = 'a' }: LeadTasksProps) {
  const tasks = useLeadTasks(workspaceId, leadId)
  const create = useCreateTask(workspaceId)
  const complete = useCompleteTask(workspaceId)

  const [title, setTitle] = useState('')
  const [dueAt, setDueAt] = useState('')

  const open = (tasks.data ?? []).filter((task) => task.completed_at === null)

  return (
    <DetailSection title="Follow-ups" icon={ListChecks} tone={tone}>
      {open.length === 0 ? (
        <p className="text-muted-foreground text-sm">Nothing outstanding.</p>
      ) : (
        <ul className="space-y-1">
          {open.map((task) => (
            <li key={task.id} className="flex items-center justify-between gap-2 text-sm">
              <span>
                {task.title}
                <span className="text-muted-foreground ml-2 text-xs">
                  {new Date(task.due_at).toLocaleDateString()}
                </span>
              </span>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  void complete.mutateAsync(task.id).then(() => tasks.refetch())
                }}
              >
                Done
              </Button>
            </li>
          ))}
        </ul>
      )}

      <div className="flex flex-wrap gap-2 pt-1">
        <Input
          className="max-w-56"
          aria-label="New follow-up"
          placeholder="Call back about fees"
          value={title}
          onChange={(event) => setTitle(event.target.value)}
        />
        <Input
          className="max-w-48"
          type="datetime-local"
          aria-label="Follow-up due"
          value={dueAt}
          onChange={(event) => setDueAt(event.target.value)}
        />
        <Button
          size="sm"
          disabled={!title.trim() || !dueAt || create.isPending}
          onClick={() => {
            void (async () => {
              await create.mutateAsync({
                title: title.trim(),
                due_at: new Date(dueAt).toISOString(),
                lead_id: leadId,
              })
              await tasks.refetch()
              setTitle('')
              setDueAt('')
            })()
          }}
        >
          Add
        </Button>
      </div>
    </DetailSection>
  )
}
