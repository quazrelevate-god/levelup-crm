/**
 * The import wizard (M7).
 *
 * Four steps because the API has four, and the API has four because an
 * operator has to be able to see what a file would do *before* it does it, and
 * to walk away between the preview and the decision.
 *
 * Upload → map → preview → commit.
 *
 * The target list comes from `GET /imports/fields`, which returns only fields
 * the caller may import *and* that the admin marked `show_in_import`. The
 * screen therefore cannot offer a mapping the commit would refuse — the two
 * agree because they read the same list.
 *
 * The kind picker is the audit's four flows made visible. "Create or update"
 * and "Update only" look similar and are not: in an update run a row matching
 * nothing is an error rather than a new lead, which is exactly what somebody
 * correcting 400 records wants and exactly what somebody importing a new list
 * does not.
 */

import { useState } from 'react'

import { ApiError, api } from '@/api/client'
import type { ImportJob, ImportJobKind, MemberDetail, Page } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label as FieldLabel } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { useAuth } from '@/features/auth/context'
import {
  useCommitImport,
  useImportableFields,
  usePreviewImport,
  useSetImportMapping,
  useUploadImport,
} from '@/features/work/api'
import { useQuery } from '@tanstack/react-query'

const KINDS: ReadonlyArray<{ value: ImportJobKind; label: string; hint: string }> = [
  {
    value: 'LEAD_IMPORT',
    label: 'Create or update leads',
    hint: 'Rows matching an existing identifier update it; the rest create new leads.',
  },
  {
    value: 'LEAD_UPDATE',
    label: 'Update existing leads only',
    hint: 'A row matching nothing is reported as an error rather than creating a lead.',
  },
  {
    value: 'ACTION_IMPORT',
    label: 'Import past activity',
    hint: 'Migrate a timeline from another system. Events keep their original dates.',
  },
]

/** Targets the action import maps onto — not fields, but parts of an event. */
const ACTION_TARGETS = [
  { key: 'identity', label: 'Lead identifier' },
  { key: 'kind', label: 'Activity type' },
  { key: 'performed_at', label: 'When it happened' },
  { key: 'body', label: 'Note' },
  { key: 'direction', label: 'Call direction' },
  { key: 'action_type', label: 'Custom action name' },
]

const STRATEGIES = [
  { value: 'NONE', label: 'Leave unassigned' },
  { value: 'ROUND_ROBIN', label: 'Share evenly between members' },
  { value: 'WEIGHTED', label: 'Share by weight' },
  { value: 'AVAILABILITY', label: 'Share between members who are working' },
  { value: 'COLUMN', label: 'Take the owner from a column' },
]

function message(cause: unknown): string {
  if (!(cause instanceof ApiError)) return 'That upload failed.'
  if (cause.code === 'field_not_importable') {
    return 'One of those fields cannot be imported with your permissions.'
  }
  if (cause.code === 'unsupported_file_type') return 'Upload a .csv or .xlsx file.'
  if (cause.code === 'too_many_rows') return cause.message
  if (cause.code === 'mapping_required') return cause.message
  return cause.message
}

export function ImportPage() {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string

  const [kind, setKind] = useState<ImportJobKind>('LEAD_IMPORT')
  const [job, setJob] = useState<ImportJob | null>(null)
  const [mapping, setMapping] = useState<Record<string, string>>({})
  const [strategy, setStrategy] = useState('NONE')
  const [ownerColumn, setOwnerColumn] = useState('')
  const [members, setMembers] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)

  const fields = useImportableFields(workspaceId)
  const upload = useUploadImport(workspaceId)
  const setMappingMutation = useSetImportMapping(workspaceId)
  const preview = usePreviewImport(workspaceId)
  const commit = useCommitImport(workspaceId)

  const team = useQuery({
    queryKey: ['members', workspaceId],
    queryFn: () =>
      api.get<Page<MemberDetail>>(`/workspaces/${workspaceId}/members`, { query: { limit: 100 } }),
  })

  const isActionImport = kind === 'ACTION_IMPORT'
  const targets = isActionImport
    ? ACTION_TARGETS
    : (fields.data ?? []).map((field) => ({ key: field.key, label: field.label }))

  function reset() {
    setJob(null)
    setMapping({})
    setStrategy('NONE')
    setOwnerColumn('')
    setMembers([])
    setError(null)
  }

  const counts = job?.result?.counts ?? {}
  const errors = job?.result?.errors ?? []

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Import</h1>
        <p className="text-muted-foreground text-sm">
          Upload a spreadsheet, choose what the columns mean, see what it would do, then commit.
        </p>
      </header>

      {error ? (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      ) : null}

      {/* --- step 1: upload --- */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">1 &middot; Choose a file</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="space-y-1.5">
            <FieldLabel htmlFor="import-kind">What are you importing?</FieldLabel>
            <Select
              id="import-kind"
              value={kind}
              disabled={job !== null}
              onChange={(event) => {
                setKind(event.target.value as ImportJobKind)
                reset()
              }}
            >
              {KINDS.map((entry) => (
                <option key={entry.value} value={entry.value}>
                  {entry.label}
                </option>
              ))}
            </Select>
            <p className="text-muted-foreground text-xs">
              {KINDS.find((entry) => entry.value === kind)?.hint}
            </p>
          </div>

          {job === null ? (
            <Input
              type="file"
              aria-label="Spreadsheet"
              accept=".csv,.xlsx,.xlsm"
              onChange={(event) => {
                const file = event.target.files?.[0]
                if (!file) return
                void (async () => {
                  setError(null)
                  try {
                    const created = await upload.mutateAsync({ file, kind })
                    setJob(created)
                  } catch (cause) {
                    setError(message(cause))
                  }
                })()
              }}
            />
          ) : (
            <p className="text-sm">
              <span className="font-medium">{job.filename}</span>{' '}
              <span className="text-muted-foreground">
                — {job.row_count} row{job.row_count === 1 ? '' : 's'}, {job.source_columns.length}{' '}
                columns
              </span>{' '}
              <Button variant="ghost" size="sm" onClick={reset}>
                Start over
              </Button>
            </p>
          )}
        </CardContent>
      </Card>

      {/* --- step 2: map --- */}
      {job ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">2 &middot; Match the columns</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {fields.isError ? (
              <p role="alert" className="text-destructive text-sm">
                {message(fields.error)}
              </p>
            ) : (
              <p className="text-muted-foreground text-sm">
                Only fields you may import and that this workspace allows in a sheet are offered.
                Leave a column unmatched to ignore it.
              </p>
            )}
            {!isActionImport && !fields.isError && targets.length === 0 && !fields.isLoading ? (
              // An empty target list is a real state — an admin can turn
              // `show_in_import` off for every field — and it needs saying, or
              // the screen looks broken.
              <p className="text-sm">
                No fields in this workspace are available for import. Turn on &ldquo;show in
                import&rdquo; for at least one field first.
              </p>
            ) : null}

            <div className="grid gap-2 sm:grid-cols-2">
              {job.source_columns.map((column) => (
                <div key={column} className="flex items-center gap-2">
                  <span className="w-40 truncate text-sm font-medium" title={column}>
                    {column}
                  </span>
                  <Select
                    aria-label={`Match ${column}`}
                    value={mapping[column] ?? ''}
                    onChange={(event) =>
                      setMapping((current) => {
                        const next = { ...current }
                        if (event.target.value) next[column] = event.target.value
                        else delete next[column]
                        return next
                      })
                    }
                  >
                    <option value="">Ignore this column</option>
                    {targets.map((target) => (
                      <option key={target.key} value={target.key}>
                        {target.label}
                      </option>
                    ))}
                  </Select>
                </div>
              ))}
            </div>

            {!isActionImport ? (
              <div className="space-y-3 border-t pt-3">
                <div className="space-y-1.5">
                  <FieldLabel htmlFor="import-strategy">Who gets these leads?</FieldLabel>
                  <Select
                    id="import-strategy"
                    value={strategy}
                    onChange={(event) => setStrategy(event.target.value)}
                  >
                    {STRATEGIES.map((entry) => (
                      <option key={entry.value} value={entry.value}>
                        {entry.label}
                      </option>
                    ))}
                  </Select>
                </div>

                {strategy === 'COLUMN' ? (
                  <div className="space-y-1.5">
                    <FieldLabel htmlFor="import-owner-column">Owner column</FieldLabel>
                    <Select
                      id="import-owner-column"
                      value={ownerColumn}
                      onChange={(event) => setOwnerColumn(event.target.value)}
                    >
                      <option value="">Choose a column…</option>
                      {job.source_columns.map((column) => (
                        <option key={column} value={column}>
                          {column}
                        </option>
                      ))}
                    </Select>
                    <p className="text-muted-foreground text-xs">
                      Matched on the member&rsquo;s email. A value nobody matches leaves the lead
                      unassigned rather than failing the row.
                    </p>
                  </div>
                ) : null}

                {['ROUND_ROBIN', 'WEIGHTED', 'AVAILABILITY'].includes(strategy) ? (
                  <div className="space-y-1.5">
                    <FieldLabel htmlFor="import-members">Share between</FieldLabel>
                    <select
                      id="import-members"
                      multiple
                      className="border-input bg-background h-32 w-full rounded-md border p-2 text-sm"
                      value={members}
                      onChange={(event) =>
                        setMembers([...event.target.selectedOptions].map((option) => option.value))
                      }
                    >
                      {(team.data?.items ?? []).map((member) => (
                        <option key={member.id} value={member.id}>
                          {member.user.full_name}
                        </option>
                      ))}
                    </select>
                    {strategy === 'AVAILABILITY' ? (
                      <p className="text-muted-foreground text-xs">
                        Anyone not currently working is skipped.
                      </p>
                    ) : null}
                  </div>
                ) : null}
              </div>
            ) : null}

            <div className="flex justify-end">
              <Button
                disabled={Object.keys(mapping).length === 0 || preview.isPending}
                onClick={() => {
                  void (async () => {
                    setError(null)
                    try {
                      await setMappingMutation.mutateAsync({
                        jobId: job.id,
                        mapping,
                        options: isActionImport
                          ? {}
                          : {
                              strategy,
                              ...(ownerColumn ? { owner_column: ownerColumn } : {}),
                              ...(members.length ? { membership_ids: members } : {}),
                            },
                      })
                      setJob(await preview.mutateAsync(job.id))
                    } catch (cause) {
                      setError(message(cause))
                    }
                  })()
                }}
              >
                Preview
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : null}

      {/* --- step 3 and 4: preview, then commit --- */}
      {job && job.status === 'PREVIEWED' ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">3 &middot; What this would do</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex flex-wrap gap-3">
              {Object.entries(counts).map(([status, count]) => (
                <Badge
                  key={status}
                  variant="outline"
                  className={status === 'error' ? 'border-destructive text-destructive' : ''}
                >
                  {count} {status}
                </Badge>
              ))}
            </div>

            {errors.length > 0 ? (
              <div className="space-y-1">
                <p className="text-sm font-medium">Rows that will be skipped</p>
                <ul className="max-h-56 space-y-1 overflow-y-auto text-sm">
                  {errors.map((row) => (
                    <li key={row.row_number} className="flex gap-2">
                      {/* Row numbers match Excel's own gutter, so the operator
                          can go and look at the row. */}
                      <span className="text-muted-foreground w-16 shrink-0">
                        Row {row.row_number}
                      </span>
                      <span>{row.message}</span>
                    </li>
                  ))}
                </ul>
                {job.result.errors_truncated ? (
                  <p className="text-muted-foreground text-xs">Only the first errors are listed.</p>
                ) : null}
              </div>
            ) : null}

            <div className="flex items-center justify-between border-t pt-3">
              <p className="text-muted-foreground text-sm">
                Committing writes one change you can undo as a unit.
              </p>
              <Button
                disabled={commit.isPending}
                onClick={() => {
                  void (async () => {
                    setError(null)
                    try {
                      setJob(await commit.mutateAsync(job.id))
                    } catch (cause) {
                      setError(message(cause))
                    }
                  })()
                }}
              >
                Import {counts.create ?? 0} new, update {counts.update ?? 0}
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : null}

      {job && job.status === 'COMPLETED' ? (
        <Card>
          <CardContent className="space-y-2 pt-4">
            <p className="text-sm font-medium">Import finished.</p>
            <p className="text-muted-foreground text-sm">
              {counts.create ?? 0} created, {counts.update ?? 0} updated
              {counts.error ? `, ${counts.error} skipped` : ''}. It is one entry in the edit report,
              so it can be undone in one go.
            </p>
            <Button variant="outline" onClick={reset}>
              Import another file
            </Button>
          </CardContent>
        </Card>
      ) : null}
    </div>
  )
}
