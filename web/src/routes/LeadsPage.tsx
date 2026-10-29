/**
 * The lead list (M6).
 *
 * Four things share this screen, and the ordering of state between them is the
 * only fiddly part:
 *
 * - a **saved filter** picker, which loads a stored DSL document
 * - the **filter builder**, which edits that document as labelled rules
 * - the **grid**, server-paginated and server-sorted
 * - the **column picker**, whose choice is stored per member *per filter*
 *
 * Selecting a saved filter therefore changes the columns too, because the
 * columns that make sense for "needs a call today" are not the ones that make
 * sense for "closed this quarter". That is why `table_layouts` is keyed on
 * `(member, filter)` rather than on the member alone.
 *
 * Detail still opens as an overlay so filter context survives — going back to a
 * list that had forgotten your filter is the single most irritating thing a CRM
 * can do.
 */

import { useSearchParams } from 'react-router-dom'
import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { ApiError, api, downloadFile } from '@/api/client'
import type {
  GroupNode,
  LeadField,
  MemberDetail,
  Page,
  SavedFilterVisibility,
  WorkspaceDetail,
} from '@/api/types'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { PageHeader, Toolbar } from '@/components/ui/page-header'
import { Dialog } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { cn } from '@/lib/utils'
import { Select } from '@/components/ui/select'
import { useAuth } from '@/features/auth/context'
import { useFieldTypes, useIndexedFields, useLeadFields } from '@/features/fields/api'
import { FieldInput } from '@/features/fields/FieldInput'
import {
  useArchiveFilter,
  useCreateFilter,
  useLeadSearch,
  useSaveLayout,
  useSavedFilters,
  useTableLayout,
} from '@/features/filters/api'
import { FilterBuilder } from '@/features/filters/FilterBuilder'
import { useCreateLead, useLead, useMessageTemplates } from '@/features/leads/api'
import { ColumnPicker } from '@/features/leads/ColumnPicker'
import { LeadDetail } from '@/features/leads/LeadDetail'
import { BUILTIN_COLUMN_IDS, DEFAULT_COLUMNS } from '@/features/leads/columns'
import { LeadTable, Pagination } from '@/features/leads/LeadTable'
import { QuickFilterBar } from '@/features/leads/QuickFilterBar'
import { DistributeDialog } from '@/features/routing/DistributeDialog'
import { useDistribute, useSalesGroups } from '@/features/routing/api'
import { BulkEditDialog } from '@/features/work/BulkEditDialog'
import { useBulkUpdate } from '@/features/work/api'
import { NO_QUICK_FILTERS, type QuickFilters } from '@/features/leads/quickFilters'
import {
  useCustomActions,
  useDispositions,
  useLostReasons,
  useStages,
} from '@/features/pipeline/api'

const PAGE_SIZE = 25

function emptyFilter(): GroupNode {
  return { type: 'group', op: 'AND', children: [] }
}

function message(cause: unknown): string {
  if (!(cause instanceof ApiError)) return 'That action failed.'
  const fields = cause.detail.fields
  if (fields && typeof fields === 'object' && !Array.isArray(fields)) {
    return Object.entries(fields as Record<string, string>)
      .map(([key, problem]) => `${key}: ${problem}`)
      .join(' · ')
  }
  if (cause.code === 'duplicate_identity') return 'A lead with that identifier already exists.'
  if (cause.code === 'identity_required') return 'The workspace identity field needs a value.'
  if (cause.code === 'field_not_indexed') return cause.message
  if (cause.code === 'export_not_permitted') {
    return 'Your permission template does not allow exporting any field.'
  }
  if (cause.code === 'export_too_large') return cause.message
  return cause.message
}

export function LeadsPage() {
  const { activeWorkspaceId, activeMembership } = useAuth()
  const workspaceId = activeWorkspaceId as string
  // `MembershipSummary.id` is this caller's membership in the active
  // workspace, which is what "assigned to me" means on a lead.
  const myMembershipId = activeMembership?.id ?? null

  const [search, setSearch] = useState('')
  const [filterNode, setFilterNode] = useState<GroupNode>(emptyFilter)
  const [activeFilterId, setActiveFilterId] = useState<string | null>(null)
  // Seeded from the URL so a dashboard drill-through lands filtered. Without
  // this the chart's number and the list behind it disagree — and a chart that
  // disagrees with its own drill-through is worse than no chart, because it is
  // believed.
  const [searchParams] = useSearchParams()
  const [quick, setQuick] = useState<QuickFilters>(() => ({
    ...NO_QUICK_FILTERS,
    stageId: searchParams.get('stage_id'),
    unassigned: searchParams.get('unassigned') === 'true',
  }))
  // Selection lives here rather than in the table so it survives paging.
  const [selectedIds, setSelectedIds] = useState<ReadonlySet<string>>(new Set())
  const [bulkOpen, setBulkOpen] = useState(false)
  const [bulkError, setBulkError] = useState<string | null>(null)
  const [exporting, setExporting] = useState(false)
  // Separate from `error`: a completed bulk edit is good news, and showing
  // it in the destructive colour reads as the edit having failed.
  const [notice, setNotice] = useState<string | null>(null)
  const [sort, setSort] = useState('-created_at')
  const [offset, setOffset] = useState(0)
  const [builderOpen, setBuilderOpen] = useState(false)
  const [columnsOpen, setColumnsOpen] = useState(false)
  const [saveOpen, setSaveOpen] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [distributeOpen, setDistributeOpen] = useState(false)
  const [distributeError, setDistributeError] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [draft, setDraft] = useState<Record<string, unknown>>({})
  const [error, setError] = useState<string | null>(null)

  const fields = useLeadFields(workspaceId)
  const fieldTypes = useFieldTypes(workspaceId)
  const indexed = useIndexedFields(workspaceId)
  const stages = useStages(workspaceId)
  const lostReasons = useLostReasons(workspaceId)
  const dispositions = useDispositions(workspaceId)
  const templates = useMessageTemplates(workspaceId)
  const customActions = useCustomActions(workspaceId)
  const savedFilters = useSavedFilters(workspaceId)
  const layout = useTableLayout(workspaceId, activeFilterId)
  const saveLayout = useSaveLayout(workspaceId, activeFilterId)
  const createFilter = useCreateFilter(workspaceId)
  const archiveFilter = useArchiveFilter(workspaceId)
  const createLead = useCreateLead(workspaceId)
  const bulkUpdate = useBulkUpdate(workspaceId)

  const workspace = useQuery({
    queryKey: ['workspace', workspaceId],
    queryFn: () => api.get<WorkspaceDetail>(`/workspaces/${workspaceId}`),
  })
  const members = useQuery({
    queryKey: ['members', workspaceId],
    queryFn: () =>
      api.get<Page<MemberDetail>>(`/workspaces/${workspaceId}/members`, { query: { limit: 100 } }),
  })
  const salesGroups = useSalesGroups(workspaceId)
  const distribute = useDistribute(workspaceId)

  const columns = layout.data?.columns ?? DEFAULT_COLUMNS

  const results = useLeadSearch(workspaceId, {
    filter: filterNode.children.length > 0 ? filterNode : null,
    q: search.trim() || null,
    sort,
    limit: PAGE_SIZE,
    offset,
    // Only the workspace's own fields need hydrating — the built-ins are real
    // columns on `leads` and always come back. Narrowing here is what keeps a
    // fifty-field workspace from shipping fifty values per row to draw four.
    columns: columns.filter((id) => !BUILTIN_COLUMN_IDS.has(id)),
    stage_id: quick.stageId,
    assignee_id: quick.mine ? myMembershipId : null,
    unassigned: quick.unassigned,
  })

  // Any change to what is being asked returns the reader to the first page.
  // Paging is a position within one result set, and silently keeping page 40
  // after a filter change shows an empty table for a filter that matched.
  useEffect(() => {
    setOffset(0)
  }, [search, filterNode, sort, activeFilterId, quick])

  const allStages = useMemo(() => {
    const pipeline = stages.data
    if (!pipeline) return []
    return [pipeline.initial, ...pipeline.active, pipeline.won, pipeline.lost].filter(
      (stage): stage is NonNullable<typeof stage> => stage !== null,
    )
  }, [stages.data])

  const indexedKeys = useMemo(() => {
    const byId = new Map((fields.data ?? []).map((field) => [field.id, field.key]))
    return new Set(
      (indexed.data ?? [])
        .filter((entry) => entry.status === 'READY')
        .map((entry) => byId.get(entry.field_id))
        .filter((key): key is string => key !== undefined),
    )
  }, [indexed.data, fields.data])

  const fieldList = fields.data ?? []
  const quickAddFields = fieldList.filter(
    (field) => !field.is_hidden && (field.show_in_quick_add || field.is_required),
  )
  const rendererFor = (field: LeadField) =>
    fieldTypes.data?.find((spec) => spec.key === field.field_type)?.renderer

  // The row from the list, used only to open the panel without a blank frame.
  const listRow = (results.data?.items ?? []).find((lead) => lead.id === selectedId) ?? null
  // The *whole* lead. The list narrows `values` to the chosen columns so a
  // fifty-field workspace does not ship fifty values per row — which means a
  // field nobody put in the table came back absent, and the detail panel
  // rendered it as "no fields are visible to your permission template". On a
  // fresh workspace, where every column is a built-in, that was every field.
  const detail = useLead(workspaceId, selectedId)
  const selected = detail.data ?? listRow
  const ruleCount = filterNode.children.length

  function applySavedFilter(id: string) {
    if (!id) {
      setActiveFilterId(null)
      setFilterNode(emptyFilter())
      return
    }
    const saved = savedFilters.data?.find((entry) => entry.id === id)
    if (!saved) return
    setActiveFilterId(id)
    // Stored definitions are always groups at the root; anything else is a
    // filter written by an older client and is wrapped rather than rejected.
    setFilterNode(
      saved.definition.type === 'group'
        ? saved.definition
        : { type: 'group', op: 'AND', children: [saved.definition] },
    )
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Leads"
        description={
          <>
            {(results.data?.total ?? 0).toLocaleString()} lead
            {results.data?.total === 1 ? '' : 's'}
            {ruleCount > 0 ? ` matching ${ruleCount} rule${ruleCount === 1 ? '' : 's'}` : ''}
          </>
        }
      />

      {/* Saved views as one-click tabs. A view is a named, saved filter:
          build it under Filters, then "Save as filter". */}
      <nav aria-label="Saved views" className="flex flex-wrap items-center gap-2">
        {[{ id: '', name: 'All leads', visibility: null }, ...(savedFilters.data ?? [])].map(
          (view) => {
            const active = (activeFilterId ?? '') === view.id
            return (
              <button
                key={view.id || 'all'}
                type="button"
                aria-pressed={active}
                onClick={() => applySavedFilter(view.id)}
                className={cn(
                  'h-8 rounded-full border px-3.5 text-sm transition-colors',
                  active
                    ? 'bg-primary text-primary-foreground border-transparent'
                    : 'border-border/70 text-muted-foreground hover:bg-accent hover:text-foreground',
                )}
              >
                {view.name}
                {view.visibility === 'SHARED' ? (
                  <span className="ml-1.5 text-xs opacity-70">shared</span>
                ) : null}
                {view.visibility === 'ROLE' ? (
                  <span className="ml-1.5 text-xs opacity-70">role</span>
                ) : null}
              </button>
            )
          },
        )}
        <button
          type="button"
          onClick={() => setBuilderOpen(true)}
          className="text-muted-foreground hover:text-foreground h-8 rounded-full border border-dashed px-3.5 text-sm transition-colors"
        >
          + Save view
        </button>
      </nav>

      <Toolbar>
        <Input
          className="max-w-56"
          placeholder="Search by identifier"
          aria-label="Search leads"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <Button variant="outline" onClick={() => setBuilderOpen((open) => !open)}>
          Filters{ruleCount > 0 ? ` (${ruleCount})` : ''}
        </Button>
        <Button variant="outline" onClick={() => setColumnsOpen(true)}>
          Columns
        </Button>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            disabled={exporting}
            onClick={() => {
              void (async () => {
                setError(null)
                setNotice(null)
                setExporting(true)
                try {
                  // Exports the same filter the list is showing, so "download
                  // what I am looking at" is one action rather than a second
                  // query to re-describe.
                  const job = await api.post<{ job_id: string; row_count: number }>(
                    `/workspaces/${workspaceId}/leads/export`,
                    { filter: filterNode.children.length > 0 ? filterNode : null },
                  )
                  await downloadFile(
                    `/workspaces/${workspaceId}/leads/export/${job.job_id}`,
                    'leads.csv',
                  )
                } catch (cause) {
                  setError(message(cause))
                } finally {
                  setExporting(false)
                }
              })()
            }}
          >
            Export
          </Button>
          <Button onClick={() => setCreateOpen(true)}>Add lead</Button>
        </div>
      </Toolbar>

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
      {results.error ? (
        <p role="alert" className="text-destructive text-sm">
          {message(results.error)}
        </p>
      ) : null}

      {builderOpen ? (
        <Card>
          <CardContent className="space-y-3 pt-4">
            <FilterBuilder
              node={filterNode}
              fields={fieldList}
              fieldTypes={fieldTypes.data ?? []}
              stages={allStages}
              members={members.data?.items ?? []}
              customActions={customActions.data ?? []}
              onChange={setFilterNode}
            />
            <div className="flex flex-wrap justify-end gap-2 border-t pt-3">
              <Button
                variant="ghost"
                onClick={() => {
                  setFilterNode(emptyFilter())
                  setActiveFilterId(null)
                }}
              >
                Clear
              </Button>
              <Button
                variant="outline"
                disabled={ruleCount === 0}
                onClick={() => setSaveOpen(true)}
              >
                Save as filter
              </Button>
              {activeFilterId ? (
                <Button
                  variant="ghost"
                  onClick={() => {
                    void (async () => {
                      await archiveFilter.mutateAsync(activeFilterId)
                      setActiveFilterId(null)
                      setFilterNode(emptyFilter())
                    })()
                  }}
                >
                  Archive filter
                </Button>
              ) : null}
            </div>
          </CardContent>
        </Card>
      ) : null}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <QuickFilterBar stages={allStages} value={quick} onChange={setQuick} />
        {selectedIds.size > 0 ? (
          <div className="flex items-center gap-2">
            <span className="text-sm">
              {selectedIds.size} selected
              <button
                type="button"
                className="text-muted-foreground hover:text-foreground ml-2 underline"
                onClick={() => setSelectedIds(new Set())}
              >
                clear
              </button>
            </span>
            <Button
              variant="outline"
              onClick={() => {
                setBulkError(null)
                setNotice(null)
                setBulkOpen(true)
              }}
            >
              Edit {selectedIds.size}
            </Button>
            <Button
              variant="outline"
              onClick={() => {
                setDistributeError(null)
                setNotice(null)
                setDistributeOpen(true)
              }}
            >
              Distribute {selectedIds.size}
            </Button>
          </div>
        ) : null}
      </div>

      <Card>
        <CardContent className="p-0">
          <LeadTable
            selected={selectedIds}
            onSelectedChange={setSelectedIds}
            leads={results.data?.items ?? []}
            fields={fieldList}
            stages={allStages}
            members={members.data?.items ?? []}
            columns={columns}
            sort={sort}
            indexedKeys={indexedKeys}
            onSortChange={setSort}
            onSelect={setSelectedId}
          />
          <Pagination
            total={results.data?.total ?? 0}
            limit={PAGE_SIZE}
            offset={offset}
            onOffsetChange={setOffset}
          />
        </CardContent>
      </Card>

      <DistributeDialog
        open={distributeOpen}
        count={selectedIds.size}
        members={members.data?.items ?? []}
        groups={salesGroups.data ?? []}
        pending={distribute.isPending}
        error={distributeError}
        onClose={() => setDistributeOpen(false)}
        onApply={(body) => {
          void (async () => {
            setDistributeError(null)
            try {
              const result = await distribute.mutateAsync({
                lead_ids: [...selectedIds],
                ...body,
              })
              setDistributeOpen(false)
              setSelectedIds(new Set())
              setNotice(
                `Reassigned ${result.assigned} of ${result.total} leads` +
                  (result.skipped > 0 ? `; ${result.skipped} were already there.` : '.') +
                  ' Undo it from the edit report if that was wrong.',
              )
            } catch (cause) {
              setDistributeError(message(cause))
            }
          })()
        }}
      />

      <BulkEditDialog
        open={bulkOpen}
        count={selectedIds.size}
        fields={fieldList}
        fieldTypes={fieldTypes.data ?? []}
        stages={allStages}
        members={members.data?.items ?? []}
        pending={bulkUpdate.isPending}
        error={bulkError}
        onClose={() => setBulkOpen(false)}
        onApply={(body) => {
          void (async () => {
            setBulkError(null)
            try {
              const result = await bulkUpdate.mutateAsync({
                lead_ids: [...selectedIds],
                ...body,
              })
              setBulkOpen(false)
              setSelectedIds(new Set())
              setNotice(`${result.summary}. Undo it from the edit report if that was wrong.`)
            } catch (cause) {
              setBulkError(message(cause))
            }
          })()
        }}
      />

      <ColumnPicker
        open={columnsOpen}
        fields={fieldList}
        selected={columns}
        onClose={() => setColumnsOpen(false)}
        onApply={(next) => {
          void (async () => {
            await saveLayout.mutateAsync({ columns: next })
            setColumnsOpen(false)
          })()
        }}
      />

      <SaveFilterDialog
        open={saveOpen}
        onClose={() => setSaveOpen(false)}
        pending={createFilter.isPending}
        templates={(members.data?.items ?? []).map((member) => ({
          id: member.template_id,
          name: member.template_name,
        }))}
        onSave={(name, visibility, templateId) => {
          void (async () => {
            setError(null)
            try {
              const saved = await createFilter.mutateAsync({
                name,
                definition: filterNode,
                visibility,
                template_id: visibility === 'ROLE' ? templateId : null,
              })
              setActiveFilterId(saved.id)
              setSaveOpen(false)
            } catch (cause) {
              setError(message(cause))
            }
          })()
        }}
      />

      {selected ? (
        <LeadDetail
          workspaceId={workspaceId}
          lead={selected}
          fields={fieldList}
          fieldTypes={fieldTypes.data ?? []}
          stages={allStages}
          lostReasons={lostReasons.data ?? []}
          members={members.data?.items ?? []}
          dispositions={dispositions.data ?? []}
          actionTypes={customActions.data ?? []}
          templates={templates.data ?? []}
          connectedCallMinSeconds={workspace.data?.connected_call_min_seconds ?? 1}
          onClose={() => setSelectedId(null)}
        />
      ) : null}

      <Dialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        title="Add a lead"
        description="Quick-add fields plus anything required, rendered from this workspace's own schema."
        footer={
          <>
            <Button variant="ghost" onClick={() => setCreateOpen(false)}>
              Cancel
            </Button>
            <Button
              disabled={createLead.isPending}
              onClick={() => {
                void (async () => {
                  setError(null)
                  try {
                    await createLead.mutateAsync({ values: draft })
                    setDraft({})
                    setCreateOpen(false)
                  } catch (cause) {
                    setError(message(cause))
                  }
                })()
              }}
            >
              Create lead
            </Button>
          </>
        }
      >
        <div className="space-y-3">
          {quickAddFields.map((field) => {
            const renderer = rendererFor(field)
            if (!renderer) return null
            return (
              <div key={field.id} className="space-y-1.5">
                <Label htmlFor={`new-${field.key}`}>
                  {field.label}
                  {field.is_required ? <span className="text-destructive"> *</span> : null}
                </Label>
                <FieldInput
                  field={field}
                  renderer={renderer}
                  inputId={`new-${field.key}`}
                  value={draft[field.key]}
                  onChange={(next) => setDraft((current) => ({ ...current, [field.key]: next }))}
                />
              </div>
            )
          })}
          {quickAddFields.length === 0 ? (
            <p className="text-muted-foreground text-sm">
              No fields are marked &ldquo;show in quick add&rdquo;. Turn one on in Field settings.
            </p>
          ) : null}
        </div>
      </Dialog>
    </div>
  )
}

function SaveFilterDialog({
  open,
  onClose,
  pending,
  templates,
  onSave,
}: {
  readonly open: boolean
  readonly onClose: () => void
  readonly pending: boolean
  readonly templates: ReadonlyArray<{ id: string; name: string }>
  readonly onSave: (
    name: string,
    visibility: SavedFilterVisibility,
    templateId: string | null,
  ) => void
}) {
  const [name, setName] = useState('')
  const [visibility, setVisibility] = useState<SavedFilterVisibility>('PERSONAL')
  const [templateId, setTemplateId] = useState('')

  // Deduplicated: the member list repeats a template once per member on it.
  const uniqueTemplates = useMemo(() => {
    const seen = new Map<string, string>()
    for (const template of templates) seen.set(template.id, template.name)
    return [...seen.entries()].map(([id, label]) => ({ id, label }))
  }, [templates])

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Save this filter"
      description="A saved filter is a saved question. Everyone who runs it still sees only the leads and columns their own permissions allow."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={pending || !name.trim() || (visibility === 'ROLE' && !templateId)}
            onClick={() => onSave(name.trim(), visibility, templateId || null)}
          >
            Save filter
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <div className="space-y-1.5">
          <Label htmlFor="filter-name">Name</Label>
          <Input
            id="filter-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Needs a call today"
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="filter-visibility">Who can see it</Label>
          <Select
            id="filter-visibility"
            value={visibility}
            onChange={(event) => setVisibility(event.target.value as SavedFilterVisibility)}
          >
            <option value="PERSONAL">Only me</option>
            <option value="SHARED">Everyone in this workspace</option>
            <option value="ROLE">Everyone on one permission template</option>
          </Select>
        </div>
        {visibility === 'ROLE' ? (
          <div className="space-y-1.5">
            <Label htmlFor="filter-template">Permission template</Label>
            <Select
              id="filter-template"
              value={templateId}
              onChange={(event) => setTemplateId(event.target.value)}
            >
              <option value="">Choose a template…</option>
              {uniqueTemplates.map((template) => (
                <option key={template.id} value={template.id}>
                  {template.label}
                </option>
              ))}
            </Select>
          </div>
        ) : null}
      </div>
    </Dialog>
  )
}
