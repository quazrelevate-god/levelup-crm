/**
 * The task list (M7).
 *
 * Three buckets, and the order they appear in is the order they matter:
 * **late** first, because overdue follow-up is the thing a telecalling team
 * loses money on; then upcoming; then done, which is a record rather than a
 * queue.
 *
 * The buckets are the server's answer, computed against the *workspace's*
 * timezone. Nothing here re-derives "is this late?" from the browser clock —
 * a rep in a different timezone from their workspace would otherwise see a
 * different list from their manager.
 */

import { useState } from 'react'

import { ApiError } from '@/api/client'
import type { MemberDetail, Page, Task, TaskBucket } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label as FieldLabel } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { useAuth } from '@/features/auth/context'
import {
  useCompleteTask,
  useCreateTask,
  useReopenTask,
  useTaskCounts,
  useTasks,
} from '@/features/work/api'
import { api } from '@/api/client'
import { useQuery } from '@tanstack/react-query'

const BUCKETS: ReadonlyArray<{ key: TaskBucket; label: string }> = [
  { key: 'late', label: 'Late' },
  { key: 'upcoming', label: 'Upcoming' },
  { key: 'done', label: 'Done' },
]

function describeDue(iso: string): string {
  const due = new Date(iso)
  return due.toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

function message(cause: unknown): string {
  if (!(cause instanceof ApiError)) return 'That action failed.'
  if (cause.code === 'task_completed') return 'That task is done. Reopen it before editing.'
  if (cause.code === 'insufficient_permissions') {
    return 'Your permission template does not allow tasks in this workspace.'
  }
  return cause.message
}

export function TasksPage() {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string

  const [bucket, setBucket] = useState<TaskBucket>('late')
  const [createOpen, setCreateOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const tasks = useTasks(workspaceId, { bucket })
  const counts = useTaskCounts(workspaceId)
  const complete = useCompleteTask(workspaceId)
  const reopen = useReopenTask(workspaceId)

  const members = useQuery({
    queryKey: ['members', workspaceId],
    queryFn: () =>
      api.get<Page<MemberDetail>>(`/workspaces/${workspaceId}/members`, { query: { limit: 100 } }),
  })
  const byId = new Map((members.data?.items ?? []).map((member) => [member.id, member]))

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Tasks</h1>
          <p className="text-muted-foreground text-sm">
            Follow-up work, in the workspace&rsquo;s own timezone.
          </p>
        </div>
        <Button onClick={() => setCreateOpen(true)}>Add task</Button>
      </header>

      {error ? (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      ) : null}

      <div className="flex flex-wrap gap-2" role="group" aria-label="Task buckets">
        {BUCKETS.map((entry) => {
          const count = counts.data?.[entry.key] ?? 0
          const active = bucket === entry.key
          return (
            <button
              key={entry.key}
              type="button"
              aria-pressed={active}
              onClick={() => setBucket(entry.key)}
              className={
                active
                  ? 'bg-foreground text-background rounded-full border border-transparent px-3 py-1 text-sm'
                  : 'hover:bg-muted rounded-full border px-3 py-1 text-sm'
              }
            >
              {entry.label}
              <span className="ml-2 opacity-70">{count}</span>
            </button>
          )
        })}
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            {BUCKETS.find((entry) => entry.key === bucket)?.label} tasks
          </CardTitle>
        </CardHeader>
        <CardContent>
          <table className="w-full text-sm">
            <thead className="text-muted-foreground border-b text-left text-xs uppercase">
              <tr>
                <th className="pb-2 font-medium">Task</th>
                <th className="pb-2 font-medium">Due</th>
                <th className="pb-2 font-medium">Assignee</th>
                <th className="pb-2 font-medium" />
              </tr>
            </thead>
            <tbody>
              {(tasks.data?.items ?? []).map((task: Task) => (
                <tr key={task.id} data-testid="task-row" className="border-b last:border-0">
                  <td className="py-2">
                    <span className="font-medium">{task.title}</span>
                    {task.notes ? (
                      <span className="text-muted-foreground block text-xs">{task.notes}</span>
                    ) : null}
                  </td>
                  <td className="py-2">
                    {bucket === 'late' ? (
                      <Badge variant="outline" className="border-destructive text-destructive">
                        {describeDue(task.due_at)}
                      </Badge>
                    ) : (
                      <span className="text-muted-foreground">{describeDue(task.due_at)}</span>
                    )}
                  </td>
                  <td className="text-muted-foreground py-2">
                    {task.assignee_id
                      ? (byId.get(task.assignee_id)?.user.full_name ?? 'Unknown')
                      : 'Unassigned'}
                  </td>
                  <td className="py-2 text-right">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => {
                        void (async () => {
                          setError(null)
                          try {
                            if (task.completed_at) await reopen.mutateAsync(task.id)
                            else await complete.mutateAsync(task.id)
                          } catch (cause) {
                            setError(message(cause))
                          }
                        })()
                      }}
                    >
                      {task.completed_at ? 'Reopen' : 'Done'}
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {/* A refused request is not an empty list. Rendering "Nothing
              overdue" when the server answered 403 tells the operator there is
              no work to do, which is the opposite of what happened. */}
          {tasks.isError ? (
            <p role="alert" className="text-destructive py-8 text-center text-sm">
              {message(tasks.error)}
            </p>
          ) : (tasks.data?.items ?? []).length === 0 && !tasks.isLoading ? (
            <p className="text-muted-foreground py-8 text-center text-sm">
              {bucket === 'late'
                ? 'Nothing overdue.'
                : bucket === 'upcoming'
                  ? 'Nothing scheduled.'
                  : 'Nothing completed yet.'}
            </p>
          ) : null}
        </CardContent>
      </Card>

      <CreateTaskDialog
        open={createOpen}
        members={members.data?.items ?? []}
        onClose={() => setCreateOpen(false)}
        onError={setError}
      />
    </div>
  )
}

function CreateTaskDialog({
  open,
  members,
  onClose,
  onError,
}: {
  readonly open: boolean
  readonly members: readonly MemberDetail[]
  readonly onClose: () => void
  readonly onError: (message: string | null) => void
}) {
  const { activeWorkspaceId } = useAuth()
  const create = useCreateTask(activeWorkspaceId as string)

  const [title, setTitle] = useState('')
  const [notes, setNotes] = useState('')
  const [dueAt, setDueAt] = useState('')
  const [assignee, setAssignee] = useState('')

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Add a task"
      description="A task on a lead also appears on that lead's timeline."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={!title.trim() || !dueAt || create.isPending}
            onClick={() => {
              void (async () => {
                onError(null)
                try {
                  await create.mutateAsync({
                    title: title.trim(),
                    // `datetime-local` has no zone; the browser's is the right
                    // guess for someone typing "3pm" into their own calendar.
                    due_at: new Date(dueAt).toISOString(),
                    ...(notes.trim() ? { notes: notes.trim() } : {}),
                    ...(assignee ? { assignee_id: assignee } : {}),
                  })
                  setTitle('')
                  setNotes('')
                  setDueAt('')
                  setAssignee('')
                  onClose()
                } catch (cause) {
                  onError(message(cause))
                }
              })()
            }}
          >
            Create task
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <div className="space-y-1.5">
          <FieldLabel htmlFor="task-title">Task</FieldLabel>
          <Input
            id="task-title"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="Call back about the fee structure"
          />
        </div>
        <div className="space-y-1.5">
          <FieldLabel htmlFor="task-due">Due</FieldLabel>
          <Input
            id="task-due"
            type="datetime-local"
            value={dueAt}
            onChange={(event) => setDueAt(event.target.value)}
          />
        </div>
        <div className="space-y-1.5">
          <FieldLabel htmlFor="task-assignee">Assignee</FieldLabel>
          <Select
            id="task-assignee"
            value={assignee}
            onChange={(event) => setAssignee(event.target.value)}
          >
            <option value="">Unassigned</option>
            {members.map((member) => (
              <option key={member.id} value={member.id}>
                {member.user.full_name}
              </option>
            ))}
          </Select>
        </div>
        <div className="space-y-1.5">
          <FieldLabel htmlFor="task-notes">Notes</FieldLabel>
          <Textarea
            id="task-notes"
            value={notes}
            onChange={(event) => setNotes(event.target.value)}
          />
        </div>
      </div>
    </Dialog>
  )
}
