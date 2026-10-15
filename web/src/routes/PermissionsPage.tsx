/**
 * Permission templates and the field matrix (docs/03-configuration-model.md §6).
 *
 * The matrix is the important half: fields down, View/Edit/Import/Export across,
 * with column select-all, live counts and the Full/Partial/None rollup. The
 * counts and rollups come from the server so the badge cannot disagree with the
 * data it describes.
 *
 * Groups whose contents this codebase *proposed* rather than observed are
 * flagged in the Access section — §8 lists nine of the thirteen as "Not
 * inspected", and hiding that would let a proposal pass for a fact.
 */

import { useState } from 'react'

import { ApiError } from '@/api/client'
import type { FieldGrantRow, TemplateCapabilities } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { useAuth } from '@/features/auth/context'
import { usePermissions } from '@/features/auth/usePermissions'
import {
  useBulkSetGrant,
  useCapabilitySchema,
  useCreateTemplate,
  useFieldMatrix,
  usePermissionTemplate,
  usePermissionTemplates,
  useSetGrants,
  useUpdateCapabilities,
} from '@/features/permissions/api'

/** The four grants, in the order §6.4 renders them. */
const GRANTS = ['view', 'edit', 'import', 'export'] as const
type Grant = (typeof GRANTS)[number]

const ACTION_MESSAGES: Record<string, string> = {
  template_readonly: 'Root is read-only — it is the fallback when other templates break.',
  template_assigned: 'Members still hold this template. Move them first.',
  duplicate_template: 'A template with that name already exists.',
  insufficient_permissions: 'Your permission template does not allow editing permissions.',
}

function message(cause: unknown): string {
  if (!(cause instanceof ApiError)) return 'That action failed.'
  return ACTION_MESSAGES[cause.code] ?? cause.message
}

/**
 * Narrowing helpers for the capability blob.
 *
 * The ten Access groups are flat `{capability: boolean}`; `view` nests three
 * sub-groups inside itself. Reading through these keeps the casts in one place
 * rather than scattered through the JSX.
 */
function accessGroup(
  capabilities: TemplateCapabilities | undefined,
  group: string,
): Record<string, boolean> {
  const value = capabilities?.[group]
  return value && typeof value === 'object' ? (value as Record<string, boolean>) : {}
}

function viewGroups(
  capabilities: TemplateCapabilities | undefined,
): Record<string, Record<string, boolean>> {
  const value = capabilities?.view
  return value && typeof value === 'object'
    ? (value as Record<string, Record<string, boolean>>)
    : {}
}

function rollupVariant(rollup: string) {
  if (rollup === 'Full') return 'success' as const
  if (rollup === 'None') return 'secondary' as const
  return 'outline' as const
}

export function PermissionsPage() {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string
  const { isAdmin } = usePermissions()

  const [selectedId, setSelectedId] = useState<string>('')
  const [fieldSearch, setFieldSearch] = useState('')
  const [newTemplate, setNewTemplate] = useState('')
  const [error, setError] = useState<string | null>(null)

  const templates = usePermissionTemplates(workspaceId)
  const schema = useCapabilitySchema(workspaceId)

  // Default to the first non-Root template: Root cannot be edited, so landing
  // on it would show a matrix every control refuses.
  const effectiveId =
    selectedId || (templates.data ?? []).find((template) => !template.is_readonly)?.id || ''

  const template = usePermissionTemplate(workspaceId, effectiveId)
  const matrix = useFieldMatrix(workspaceId, effectiveId)

  const createTemplate = useCreateTemplate(workspaceId)
  const setGrants = useSetGrants(workspaceId)
  const bulkSet = useBulkSetGrant(workspaceId)
  const updateCapabilities = useUpdateCapabilities(workspaceId)

  const readOnly = template.data?.is_readonly === true || !isAdmin

  const run = async (work: Promise<unknown>) => {
    setError(null)
    try {
      await work
    } catch (cause) {
      setError(message(cause))
    }
  }

  const toggleGrant = (row: FieldGrantRow, grant: Grant, next: boolean) => {
    // Send the whole row: the API replaces the grants for the fields named, so
    // a partial row would clear the three columns not mentioned.
    void run(
      setGrants.mutateAsync({
        templateId: effectiveId,
        grants: [
          {
            field_id: row.field_id,
            view: grant === 'view' ? next : row.view,
            edit: grant === 'edit' ? next : row.edit,
            import: grant === 'import' ? next : row.import,
            export: grant === 'export' ? next : row.export,
          },
        ],
      }),
    )
  }

  const rows = (matrix.data?.fields ?? []).filter((row) =>
    fieldSearch.trim() === ''
      ? true
      : row.label.toLowerCase().includes(fieldSearch.trim().toLowerCase()),
  )

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Permission templates</h1>
          <p className="text-muted-foreground text-sm">
            Named permission sets, including the per-field View / Edit / Import / Export matrix.
          </p>
        </div>
        {isAdmin ? (
          <div className="flex gap-2">
            <Input
              placeholder="New template name"
              aria-label="New template name"
              value={newTemplate}
              onChange={(event) => setNewTemplate(event.target.value)}
            />
            <Button
              disabled={newTemplate.trim() === ''}
              onClick={() => {
                void (async () => {
                  await run(createTemplate.mutateAsync({ name: newTemplate.trim() }))
                  setNewTemplate('')
                })()
              }}
            >
              Add
            </Button>
          </div>
        ) : null}
      </header>

      {error ? (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      ) : null}

      <div className="space-y-1.5">
        <Label htmlFor="template-picker">Template</Label>
        <Select
          id="template-picker"
          className="max-w-sm"
          value={effectiveId}
          onChange={(event) => setSelectedId(event.target.value)}
        >
          {(templates.data ?? []).map((entry) => (
            <option key={entry.id} value={entry.id}>
              {entry.name}
              {entry.is_readonly ? ' (read-only)' : ''}
            </option>
          ))}
        </Select>
        {template.data?.is_readonly ? (
          <p className="text-muted-foreground text-xs">
            Root is deliberately read-only: it is the escape hatch that has to keep working when
            every other template is misconfigured.
          </p>
        ) : null}
      </div>

      <Card>
        <CardHeader className="gap-3">
          <CardTitle className="text-base">Field permissions</CardTitle>
          <Input
            className="max-w-56"
            placeholder="Search fields"
            aria-label="Search fields in matrix"
            value={fieldSearch}
            onChange={(event) => setFieldSearch(event.target.value)}
          />
        </CardHeader>
        <CardContent className="overflow-x-auto">
          <table className="w-full text-sm" data-testid="field-matrix">
            <thead className="border-b">
              <tr className="text-left">
                <th className="pb-2 font-medium">Fields</th>
                {GRANTS.map((grant) => {
                  const column = matrix.data?.columns[grant]
                  return (
                    <th key={grant} className="pb-2 font-medium">
                      <div className="flex items-center gap-2">
                        <Checkbox
                          aria-label={`Grant ${grant} on every field`}
                          checked={column?.rollup === 'Full'}
                          disabled={readOnly}
                          onChange={(event) =>
                            void run(
                              bulkSet.mutateAsync({
                                templateId: effectiveId,
                                grant: grant.toUpperCase(),
                                value: event.target.checked,
                              }),
                            )
                          }
                        />
                        <span className="capitalize">{grant}</span>
                        <span className="text-muted-foreground text-xs">
                          ({column?.count ?? 0})
                        </span>
                      </div>
                      {column ? (
                        <Badge variant={rollupVariant(column.rollup)} className="mt-1">
                          {column.rollup}
                        </Badge>
                      ) : null}
                    </th>
                  )
                })}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.field_id} className="border-b last:border-0" data-testid="matrix-row">
                  <td className="py-2">
                    {row.label}
                    {row.is_hidden ? (
                      <Badge variant="outline" className="ml-2">
                        hidden
                      </Badge>
                    ) : null}
                  </td>
                  {GRANTS.map((grant) => (
                    <td key={grant} className="py-2">
                      <Checkbox
                        aria-label={`${grant} ${row.label}`}
                        checked={row[grant]}
                        disabled={readOnly}
                        onChange={(event) => toggleGrant(row, grant, event.target.checked)}
                      />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          {rows.length === 0 && !matrix.isLoading ? (
            <p className="text-muted-foreground py-6 text-center text-sm">No fields match.</p>
          ) : null}
          <p className="text-muted-foreground mt-3 text-xs">
            Presence is the grant; there is no explicit deny. Export defaults to none — a caller can
            read a phone number on screen without being able to download ten thousand of them.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Access</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {(schema.data?.access ?? []).map((group) => (
            <div key={group.key} className="space-y-1.5 rounded-md border p-3">
              <div className="flex items-center gap-2">
                <h3 className="text-sm font-medium capitalize">{group.key}</h3>
                {group.proposed ? (
                  <Badge
                    variant="outline"
                    title="Contents proposed, not observed in the source system"
                  >
                    proposed
                  </Badge>
                ) : null}
              </div>
              {group.capabilities.map((capability) => (
                <label key={capability} className="flex items-center gap-2 text-sm">
                  <Checkbox
                    checked={
                      accessGroup(template.data?.capabilities, group.key)[capability] === true
                    }
                    disabled={readOnly}
                    onChange={(event) =>
                      void run(
                        updateCapabilities.mutateAsync({
                          templateId: effectiveId,
                          capabilities: {
                            ...(template.data?.capabilities ?? {}),
                            [group.key]: {
                              ...accessGroup(template.data?.capabilities, group.key),
                              [capability]: event.target.checked,
                            },
                          },
                        }),
                      )
                    }
                  />
                  <span className="text-muted-foreground">{capability.replace(/_/g, ' ')}</span>
                </label>
              ))}
            </div>
          ))}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">View</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-4 md:grid-cols-3">
          {(schema.data?.view ?? []).map((group) => (
            <div key={group.key} className="space-y-1.5 rounded-md border p-3">
              <div className="flex items-center gap-2">
                <h3 className="text-sm font-medium capitalize">{group.key.replace(/_/g, ' ')}</h3>
                {group.proposed ? <Badge variant="outline">proposed</Badge> : null}
              </div>
              {group.capabilities.map((capability) => (
                <label key={capability} className="flex items-center gap-2 text-sm">
                  <Checkbox
                    checked={
                      viewGroups(template.data?.capabilities)[group.key]?.[capability] === true
                    }
                    disabled={readOnly}
                    onChange={(event) => {
                      const view = viewGroups(template.data?.capabilities)
                      void run(
                        updateCapabilities.mutateAsync({
                          templateId: effectiveId,
                          capabilities: {
                            ...(template.data?.capabilities ?? {}),
                            view: {
                              ...view,
                              [group.key]: {
                                ...(view[group.key] ?? {}),
                                [capability]: event.target.checked,
                              },
                            },
                          },
                        }),
                      )
                    }}
                  />
                  <span className="text-muted-foreground">{capability.replace(/_/g, ' ')}</span>
                </label>
              ))}
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  )
}
