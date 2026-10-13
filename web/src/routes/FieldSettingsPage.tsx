/**
 * Lead field settings (docs/03-configuration-model.md §1.1).
 *
 * Three zones, matching the observed screen: the lead identity field, the H1/H2
 * primary fields, and everything else with search, a type filter and a hidden
 * view.
 *
 * The type filter's options come from the registry rather than a constant here,
 * which is the same rule the drawer follows: this screen knows there is a list
 * of types, not what is in it.
 */

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import type { LeadField, WorkspaceDetail } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { useAuth } from '@/features/auth/context'
import { usePermissions } from '@/features/auth/usePermissions'
import {
  useDeclareIndexed,
  useFieldTypes,
  useIndexedFields,
  useLeadFields,
  useSetFieldHidden,
  useSetIdentityField,
  useSetPrimaryFields,
  useUndeclareIndexed,
} from '@/features/fields/api'
import { FieldDrawer } from '@/features/fields/FieldDrawer'

const ACTION_MESSAGES: Record<string, string> = {
  builtin_field: 'Built-in fields cannot be hidden. Rename it instead.',
  identity_field_in_use: 'This field identifies leads. Choose another identifier first.',
  unsuitable_identity_field: 'That type cannot uniquely identify a lead.',
  indexed_field_limit: 'A workspace can index at most 8 fields. Un-index one first.',
  already_indexed: 'That field is already indexed.',
  insufficient_permissions: 'Your permission template does not allow changing the schema.',
}

function message(cause: unknown): string {
  if (!(cause instanceof ApiError)) return 'That action failed.'
  return ACTION_MESSAGES[cause.code] ?? cause.message
}

export function FieldSettingsPage() {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string
  const { isAdmin } = usePermissions()

  const [search, setSearch] = useState('')
  const [typeFilter, setTypeFilter] = useState('')
  const [showHidden, setShowHidden] = useState(false)
  const [editing, setEditing] = useState<LeadField | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const fieldTypes = useFieldTypes(workspaceId)
  const fields = useLeadFields(workspaceId, { includeHidden: showHidden })
  const indexed = useIndexedFields(workspaceId)
  const workspace = useQuery({
    queryKey: ['workspace', workspaceId],
    queryFn: () => api.get<WorkspaceDetail>(`/workspaces/${workspaceId}`),
  })

  const setHidden = useSetFieldHidden(workspaceId)
  const setIdentity = useSetIdentityField(workspaceId)
  const setPrimary = useSetPrimaryFields(workspaceId)
  const declareIndexed = useDeclareIndexed(workspaceId)
  const undeclareIndexed = useUndeclareIndexed(workspaceId)

  const all = useMemo(() => fields.data ?? [], [fields.data])
  const indexedFieldIds = useMemo(
    () => new Set((indexed.data ?? []).map((entry) => entry.field_id)),
    [indexed.data],
  )

  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase()
    return all.filter((field) => {
      if (typeFilter && field.field_type !== typeFilter) return false
      if (needle && !field.label.toLowerCase().includes(needle)) return false
      return true
    })
  }, [all, search, typeFilter])

  const run = async (work: Promise<unknown>) => {
    setError(null)
    try {
      await work
    } catch (cause) {
      setError(message(cause))
    }
  }

  const openDrawer = (field: LeadField | null) => {
    setEditing(field)
    setDrawerOpen(true)
  }

  // The drawer edits a live field, so re-read it from the query each render —
  // otherwise adding an option would not appear until the drawer reopened.
  const editingLive = editing ? (all.find((f) => f.id === editing.id) ?? editing) : null

  if (fields.isLoading || fieldTypes.isLoading) {
    return <p className="text-muted-foreground text-sm">Loading fields…</p>
  }

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Lead fields</h1>
          <p className="text-muted-foreground text-sm">
            {all.length} field{all.length === 1 ? '' : 's'} · {fieldTypes.data?.length ?? 0} types
            available
          </p>
        </div>
        {isAdmin ? <Button onClick={() => openDrawer(null)}>Add a new field</Button> : null}
      </header>

      {error ? (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Lead identity and headline fields</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-3">
          <div className="space-y-1.5">
            <Label htmlFor="identity-field">Lead Id</Label>
            <Select
              id="identity-field"
              value={workspace.data?.identity_field_id ?? ''}
              disabled={!isAdmin}
              onChange={(event) =>
                void run(setIdentity.mutateAsync({ fieldId: event.target.value }))
              }
            >
              <option value="">Not set</option>
              {all.map((field) => (
                <option key={field.id} value={field.id}>
                  {field.label}
                </option>
              ))}
            </Select>
            <p className="text-muted-foreground text-xs">
              Which field uniquely identifies a lead. Dedup and merge read from it.
            </p>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="h1-field">Primary field H1</Label>
            <Select
              id="h1-field"
              value={workspace.data?.primary_field_1_id ?? ''}
              disabled={!isAdmin}
              onChange={(event) =>
                void run(
                  setPrimary.mutateAsync({
                    h1: event.target.value,
                    h2: workspace.data?.primary_field_2_id ?? null,
                  }),
                )
              }
            >
              {all.map((field) => (
                <option key={field.id} value={field.id}>
                  {field.label}
                </option>
              ))}
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="h2-field">Primary field H2</Label>
            <Select
              id="h2-field"
              value={workspace.data?.primary_field_2_id ?? ''}
              disabled={!isAdmin}
              onChange={(event) =>
                void run(
                  setPrimary.mutateAsync({
                    h1: workspace.data?.primary_field_1_id ?? all[0]?.id ?? '',
                    h2: event.target.value || null,
                  }),
                )
              }
            >
              <option value="">None</option>
              {all.map((field) => (
                <option key={field.id} value={field.id}>
                  {field.label}
                </option>
              ))}
            </Select>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="gap-3">
          <CardTitle className="text-base">Other fields</CardTitle>
          <div className="flex flex-wrap gap-2">
            <Input
              className="max-w-56"
              placeholder="Search fields"
              aria-label="Search fields"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
            <Select
              className="max-w-48"
              aria-label="Filter by type"
              value={typeFilter}
              onChange={(event) => setTypeFilter(event.target.value)}
            >
              <option value="">All types</option>
              {(fieldTypes.data ?? []).map((spec) => (
                <option key={spec.key} value={spec.key}>
                  {spec.label}
                </option>
              ))}
            </Select>
            <Select
              className="max-w-44"
              aria-label="View"
              value={showHidden ? 'all' : 'active'}
              onChange={(event) => setShowHidden(event.target.value === 'all')}
            >
              <option value="active">Active fields</option>
              <option value="all">Include hidden</option>
            </Select>
          </div>
        </CardHeader>
        <CardContent>
          <table className="w-full text-sm">
            <thead className="text-muted-foreground border-b text-left text-xs uppercase">
              <tr>
                <th className="pb-2 font-medium">Field name</th>
                <th className="pb-2 font-medium">Type</th>
                <th className="pb-2 font-medium">Key</th>
                <th className="pb-2 font-medium">Properties</th>
                <th className="pb-2 font-medium">Indexed</th>
                <th className="pb-2 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((field) => {
                const spec = fieldTypes.data?.find((s) => s.key === field.field_type)
                const isIndexed = indexedFieldIds.has(field.id)
                return (
                  <tr key={field.id} className="border-b last:border-0" data-testid="field-row">
                    <td className="py-2">
                      <span className="font-medium">{field.label}</span>
                      {field.is_builtin ? (
                        <Badge variant="secondary" className="ml-2">
                          built-in
                        </Badge>
                      ) : null}
                      {field.is_hidden ? (
                        <Badge variant="outline" className="ml-2">
                          hidden
                        </Badge>
                      ) : null}
                    </td>
                    <td className="text-muted-foreground py-2">
                      {spec?.label ?? field.field_type}
                    </td>
                    <td className="text-muted-foreground py-2">
                      <code className="text-xs">{field.key}</code>
                    </td>
                    <td className="py-2">
                      <div className="flex flex-wrap gap-1">
                        {field.is_required ? <Badge variant="outline">required</Badge> : null}
                        {field.lock_after_create ? <Badge variant="outline">locked</Badge> : null}
                        {field.can_use_variable ? <Badge variant="outline">variables</Badge> : null}
                        {field.show_in_quick_add ? (
                          <Badge variant="outline">quick add</Badge>
                        ) : null}
                      </div>
                    </td>
                    <td className="py-2">
                      {isAdmin ? (
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() =>
                            void run(
                              isIndexed
                                ? undeclareIndexed.mutateAsync({ fieldId: field.id })
                                : declareIndexed.mutateAsync({ fieldId: field.id }),
                            )
                          }
                        >
                          {isIndexed ? 'Indexed' : 'Index'}
                        </Button>
                      ) : (
                        <span className="text-muted-foreground">{isIndexed ? 'Yes' : '—'}</span>
                      )}
                    </td>
                    <td className="py-2 text-right">
                      {isAdmin ? (
                        <>
                          <Button variant="ghost" size="sm" onClick={() => openDrawer(field)}>
                            Edit
                          </Button>
                          {field.is_builtin ? null : (
                            <Button
                              variant="ghost"
                              size="sm"
                              onClick={() =>
                                void run(
                                  setHidden.mutateAsync({
                                    fieldId: field.id,
                                    hidden: !field.is_hidden,
                                  }),
                                )
                              }
                            >
                              {field.is_hidden ? 'Unhide' : 'Hide'}
                            </Button>
                          )}
                        </>
                      ) : null}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          {visible.length === 0 ? (
            <p className="text-muted-foreground py-6 text-center text-sm">
              No fields match that search.
            </p>
          ) : null}
        </CardContent>
      </Card>

      <FieldDrawer
        workspaceId={workspaceId}
        open={drawerOpen}
        onOpenChange={setDrawerOpen}
        field={editingLive}
        fieldTypes={fieldTypes.data ?? []}
        allFields={all}
      />
    </div>
  )
}
