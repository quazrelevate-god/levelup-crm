/**
 * Composing a dashboard (M9).
 *
 * The editor renders from the **served** widget catalogue, including each
 * widget's config schema — so a widget added backend-side needs no release
 * here. That is the same pattern `/settings/field-types` established in M2, and
 * the reason the catalogue carries schemas at all.
 *
 * The catalogue names *kinds of chart*, never subjects. There is no "leads by
 * source" widget; there is a breakdown widget that asks which field to group
 * by, because which field means "source" is the customer's decision.
 */

import { useState } from 'react'

import { ApiError } from '@/api/client'
import type { DashboardWidget, LeadField, PermissionTemplateSummary } from '@/api/types'
import { api } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label as FieldLabel } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { useAuth } from '@/features/auth/context'
import { useLeadFields } from '@/features/fields/api'
import {
  useArchiveDashboard,
  useCreateDashboard,
  useDashboards,
  useUpdateDashboard,
  useWidgetCatalogue,
} from '@/features/reports/api'
import { useQuery } from '@tanstack/react-query'

function message(cause: unknown): string {
  if (!(cause instanceof ApiError)) return 'That action failed.'
  if (cause.code === 'insufficient_permissions') {
    return 'Your permission template does not allow sharing dashboards.'
  }
  if (cause.code === 'not_your_dashboard') {
    return 'Only the owner can change this dashboard.'
  }
  if (cause.code === 'widget_config_required') return cause.message
  return cause.message
}

export function DashboardSettingsPage() {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string

  const dashboards = useDashboards(workspaceId)
  const catalogue = useWidgetCatalogue(workspaceId)
  const archive = useArchiveDashboard(workspaceId)

  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const templates = useQuery({
    queryKey: ['permission-templates', workspaceId],
    queryFn: () =>
      api.get<PermissionTemplateSummary[]>(
        `/workspaces/${workspaceId}/settings/permission-templates`,
      ),
    enabled: Boolean(workspaceId),
  })
  const templateName = (id: string | null) =>
    (templates.data ?? []).find((template) => template.id === id)?.name

  const current = (dashboards.data ?? []).find((board) => board.id === editing)

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Dashboards</h1>
          <p className="text-muted-foreground text-sm">Compose a screen, then give it to a role.</p>
        </div>
        <Button
          onClick={() => {
            setEditing(null)
            setOpen(true)
          }}
        >
          New dashboard
        </Button>
      </header>

      {error ? (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Yours and shared</CardTitle>
        </CardHeader>
        <CardContent>
          {dashboards.isError ? (
            <p role="alert" className="text-destructive py-6 text-center text-sm">
              {message(dashboards.error)}
            </p>
          ) : (dashboards.data ?? []).length === 0 && !dashboards.isLoading ? (
            <p className="text-muted-foreground py-6 text-center text-sm">
              None yet. The dashboard shows a starter layout until you save one.
            </p>
          ) : (
            <ul className="space-y-2">
              {(dashboards.data ?? []).map((board) => (
                <li
                  key={board.id}
                  data-testid="dashboard-row"
                  className="flex flex-wrap items-center gap-3 rounded-md border p-3"
                >
                  <div className="min-w-48 flex-1">
                    <span className="font-medium">{board.name}</span>
                    <span className="text-muted-foreground block text-xs">
                      {board.layout.length} widget
                      {board.layout.length === 1 ? '' : 's'}
                      {board.owner_id ? ' · yours' : ' · shared'}
                    </span>
                  </div>
                  {board.template_id ? (
                    <Badge variant="outline">
                      {templateName(board.template_id) ?? 'Role-bound'}
                    </Badge>
                  ) : null}
                  {board.is_default ? <Badge variant="outline">Default</Badge> : null}
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      setError(null)
                      setEditing(board.id)
                      setOpen(true)
                    }}
                  >
                    Edit
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      setError(null)
                      archive.mutate(board.id, {
                        onError: (cause) => setError(message(cause)),
                      })
                    }}
                  >
                    Archive
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Available widgets</CardTitle>
        </CardHeader>
        <CardContent>
          <ul className="grid gap-2 sm:grid-cols-2">
            {(catalogue.data ?? []).map((widget) => (
              <li key={widget.key} className="rounded-md border p-3">
                <span className="text-sm font-medium">{widget.label}</span>
                <span className="text-muted-foreground block text-xs">{widget.description}</span>
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>

      <DashboardDialog
        key={editing ?? 'new'}
        open={open}
        existing={current}
        templates={templates.data ?? []}
        onClose={() => setOpen(false)}
        onError={setError}
      />
    </div>
  )
}

function DashboardDialog({
  open,
  existing,
  templates,
  onClose,
  onError,
}: {
  readonly open: boolean
  readonly existing:
    | { id: string; name: string; layout: readonly DashboardWidget[]; template_id: string | null }
    | undefined
  readonly templates: readonly PermissionTemplateSummary[]
  readonly onClose: () => void
  readonly onError: (message: string | null) => void
}) {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string
  const catalogue = useWidgetCatalogue(workspaceId)
  const fields = useLeadFields(workspaceId)
  const create = useCreateDashboard(workspaceId)
  const update = useUpdateDashboard(workspaceId)

  const [name, setName] = useState(existing?.name ?? '')
  const [chosen, setChosen] = useState<DashboardWidget[]>([...(existing?.layout ?? [])])
  const [shared, setShared] = useState(Boolean(existing && !existing.template_id))
  const [templateId, setTemplateId] = useState(existing?.template_id ?? '')

  function toggle(widgetKey: string, on: boolean) {
    setChosen((current) => {
      if (!on) return current.filter((item) => item.widget !== widgetKey)
      const spec = (catalogue.data ?? []).find((entry) => entry.key === widgetKey)
      return [
        ...current,
        {
          widget: widgetKey,
          x: 0,
          y: current.length * 4,
          w: spec?.default_size.w ?? 6,
          h: spec?.default_size.h ?? 4,
          config: {},
        },
      ]
    })
  }

  function configure(widgetKey: string, key: string, value: string) {
    setChosen((current) =>
      current.map((item) =>
        item.widget === widgetKey
          ? { ...item, config: { ...(item.config ?? {}), [key]: value } }
          : item,
      ),
    )
  }

  const incomplete = chosen.some((item) => {
    const spec = (catalogue.data ?? []).find((entry) => entry.key === item.widget)
    return Object.entries(spec?.config ?? {}).some(
      ([key, schema]) => schema.required && !item.config?.[key],
    )
  })

  return (
    <Dialog
      open={open}
      onClose={onClose}
      className="max-w-2xl"
      title={existing ? `Edit ${existing.name}` : 'New dashboard'}
      description="Bind it to a permission template and everyone on that template gets it."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={!name.trim() || incomplete || create.isPending || update.isPending}
            onClick={() => {
              onError(null)
              const handlers = {
                onError: (cause: unknown) => onError(message(cause)),
                onSuccess: () => onClose(),
              }
              if (existing) {
                update.mutate(
                  {
                    dashboardId: existing.id,
                    name: name.trim(),
                    layout: chosen,
                    template_id: templateId || null,
                  },
                  handlers,
                )
              } else {
                create.mutate(
                  {
                    name: name.trim(),
                    layout: chosen,
                    shared: shared || Boolean(templateId),
                    template_id: templateId || null,
                  },
                  handlers,
                )
              }
            }}
          >
            {existing ? 'Save' : 'Create dashboard'}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <div className="space-y-1.5">
          <FieldLabel htmlFor="dashboard-name">Name</FieldLabel>
          <Input
            id="dashboard-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Morning check"
          />
        </div>

        <fieldset className="space-y-1.5">
          <legend className="text-sm font-medium">Widgets</legend>
          <div className="max-h-72 space-y-2 overflow-y-auto rounded-md border p-2">
            {(catalogue.data ?? []).map((widget) => {
              const picked = chosen.find((item) => item.widget === widget.key)
              return (
                <div key={widget.key} className="space-y-1">
                  <label className="flex items-start gap-2 text-sm">
                    <input
                      type="checkbox"
                      className="mt-1"
                      checked={Boolean(picked)}
                      onChange={(event) => toggle(widget.key, event.target.checked)}
                    />
                    <span>
                      {widget.label}
                      <span className="text-muted-foreground block text-xs">
                        {widget.description}
                      </span>
                    </span>
                  </label>

                  {/* The config form is generated from the served schema — this
                      component does not know what a breakdown needs. */}
                  {picked
                    ? Object.entries(widget.config).map(([key, schema]) => (
                        <div key={key} className="ml-6 space-y-1">
                          <FieldLabel htmlFor={`config-${widget.key}-${key}`}>
                            {schema.label}
                          </FieldLabel>
                          {schema.type === 'field' ? (
                            <Select
                              id={`config-${widget.key}-${key}`}
                              value={picked.config?.[key] ?? ''}
                              onChange={(event) => configure(widget.key, key, event.target.value)}
                            >
                              <option value="">Choose a field</option>
                              {(fields.data ?? []).map((field: LeadField) => (
                                <option key={field.key} value={field.key}>
                                  {field.label}
                                </option>
                              ))}
                            </Select>
                          ) : (
                            <Input
                              id={`config-${widget.key}-${key}`}
                              value={picked.config?.[key] ?? ''}
                              onChange={(event) => configure(widget.key, key, event.target.value)}
                            />
                          )}
                          {schema.help ? (
                            <p className="text-muted-foreground text-xs">{schema.help}</p>
                          ) : null}
                        </div>
                      ))
                    : null}
                </div>
              )
            })}
          </div>
        </fieldset>

        <div className="space-y-1.5">
          <FieldLabel htmlFor="dashboard-template">Give it to a role</FieldLabel>
          <Select
            id="dashboard-template"
            value={templateId}
            onChange={(event) => setTemplateId(event.target.value)}
          >
            <option value="">Nobody — keep it to myself</option>
            {templates.map((template) => (
              <option key={template.id} value={template.id}>
                Everyone on {template.name}
              </option>
            ))}
          </Select>
        </div>

        {!existing && !templateId ? (
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={shared}
              onChange={(event) => setShared(event.target.checked)}
            />
            Share with the whole workspace
          </label>
        ) : null}
      </div>
    </Dialog>
  )
}
