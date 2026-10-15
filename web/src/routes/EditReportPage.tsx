/**
 * The edit report (M7).
 *
 * Every mutation batch this workspace has made, newest first, with undo on each
 * one. The two filters are the two questions anyone actually brings here:
 * "what did the 9am import do" (by source) and "what has Priya changed today"
 * (by actor).
 *
 * An undo is itself a changeset, marked by `undo_of_id` rather than by a
 * distinct source — so an undone batch and the undo that reversed it both
 * appear in the list, which is what makes the history readable rather than
 * self-erasing.
 */

import { useState } from 'react'

import { ApiError, api } from '@/api/client'
import type { Changeset, MemberDetail, Page } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Select } from '@/components/ui/select'
import { useAuth } from '@/features/auth/context'
import { UndoDialog } from '@/features/work/UndoDialog'
import { useChangesets, useUndo, useUndoPreview } from '@/features/work/api'
import { useQuery } from '@tanstack/react-query'

/** Sources a person recognises, in the words they would use. */
const SOURCE_LABELS: Record<string, string> = {
  SINGLE_EDIT: 'Single edit',
  BULK_EDIT: 'Bulk edit',
  IMPORT: 'Import',
  DISTRIBUTION: 'Distribution',
  AUTOMATION: 'Automation',
  INTAKE: 'Intake',
}

function message(cause: unknown): string {
  if (!(cause instanceof ApiError)) return 'That action failed.'
  if (cause.code === 'undo_conflicts') {
    return 'Some of those leads changed after this edit. Choose whether to skip them.'
  }
  if (cause.code === 'already_undone') return 'That change has already been undone.'
  if (cause.code === 'nothing_to_undo') return 'There is nothing in that change to reverse.'
  if (cause.code === 'field_not_editable') {
    return 'Undoing this would change fields your permission template cannot edit.'
  }
  return cause.message
}

export function EditReportPage() {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string

  const [source, setSource] = useState('')
  const [actor, setActor] = useState('')
  const [undoing, setUndoing] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const changesets = useChangesets(workspaceId, {
    ...(source ? { source } : {}),
    ...(actor ? { actor_id: actor } : {}),
  })
  const preview = useUndoPreview(workspaceId, undoing)
  const undo = useUndo(workspaceId)

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
          <h1 className="text-2xl font-semibold tracking-tight">Edit report</h1>
          <p className="text-muted-foreground text-sm">
            Every batch of changes, and how to reverse one.
          </p>
        </div>
        <div className="flex gap-2">
          <Select
            aria-label="Source"
            className="w-40"
            value={source}
            onChange={(event) => setSource(event.target.value)}
          >
            <option value="">Any source</option>
            {Object.entries(SOURCE_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </Select>
          <Select
            aria-label="Changed by"
            className="w-48"
            value={actor}
            onChange={(event) => setActor(event.target.value)}
          >
            <option value="">Anyone</option>
            {(members.data?.items ?? []).map((member) => (
              <option key={member.id} value={member.id}>
                {member.user.full_name}
              </option>
            ))}
          </Select>
        </div>
      </header>

      {error ? (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      ) : null}
      {notice ? (
        <p role="status" className="text-sm">
          {notice}
        </p>
      ) : null}

      <Card>
        <CardContent className="p-0">
          <table className="w-full text-sm">
            <thead className="text-muted-foreground border-b text-left text-xs uppercase">
              <tr>
                <th className="px-3 py-2 font-medium">What changed</th>
                <th className="px-3 py-2 font-medium">Source</th>
                <th className="px-3 py-2 font-medium">By</th>
                <th className="px-3 py-2 font-medium">When</th>
                <th className="px-3 py-2 font-medium" />
              </tr>
            </thead>
            <tbody>
              {(changesets.data?.items ?? []).map((entry: Changeset) => (
                <tr key={entry.id} data-testid="changeset-row" className="border-b last:border-0">
                  <td className="px-3 py-2">
                    <span className="font-medium">{entry.summary}</span>
                    <span className="text-muted-foreground block text-xs">
                      {entry.lead_count} lead{entry.lead_count === 1 ? '' : 's'}
                    </span>
                  </td>
                  <td className="px-3 py-2">
                    <Badge variant="outline">{SOURCE_LABELS[entry.source] ?? entry.source}</Badge>
                    {/* An undo is a changeset like any other; `undo_of_id` is
                        the only thing that marks it. */}
                    {entry.undo_of_id ? (
                      <Badge variant="outline" className="ml-1">
                        undo
                      </Badge>
                    ) : null}
                  </td>
                  <td className="text-muted-foreground px-3 py-2">
                    {entry.actor_id
                      ? (byId.get(entry.actor_id)?.user.full_name ?? 'Unknown')
                      : 'System'}
                  </td>
                  <td className="text-muted-foreground px-3 py-2">
                    {new Date(entry.created_at).toLocaleString(undefined, {
                      dateStyle: 'medium',
                      timeStyle: 'short',
                    })}
                  </td>
                  <td className="px-3 py-2 text-right">
                    {entry.is_undone ? (
                      <span className="text-muted-foreground text-xs">Undone</span>
                    ) : (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => {
                          setError(null)
                          setNotice(null)
                          setUndoing(entry.id)
                        }}
                      >
                        Undo
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {(changesets.data?.items ?? []).length === 0 && !changesets.isLoading ? (
            <p className="text-muted-foreground py-10 text-center text-sm">
              No changes match those filters.
            </p>
          ) : null}
        </CardContent>
      </Card>

      <UndoDialog
        open={undoing !== null}
        preview={preview.data}
        loading={preview.isLoading}
        pending={undo.isPending}
        error={error}
        onClose={() => setUndoing(null)}
        onConfirm={(skipConflicts) => {
          void (async () => {
            setError(null)
            try {
              const result = await undo.mutateAsync({
                changesetId: undoing as string,
                skipConflicts,
              })
              setNotice(
                result.leads_skipped > 0
                  ? `Reverted ${result.leads_reverted} leads. ${result.leads_skipped} were left alone because they changed since.`
                  : `Reverted ${result.leads_reverted} lead${result.leads_reverted === 1 ? '' : 's'}.`,
              )
              setUndoing(null)
            } catch (cause) {
              setError(message(cause))
            }
          })()
        }}
      />
    </div>
  )
}
