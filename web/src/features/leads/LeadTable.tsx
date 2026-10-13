/**
 * The lead grid (M6) — TanStack Table over a server-paginated result.
 *
 * Server-side everything: pagination, sorting and filtering all happen in
 * Postgres, and the table is told the answers rather than computing them. On a
 * 50,000-lead workspace the alternative is not slower, it is impossible — the
 * client never holds more than one page.
 *
 * Columns are built from the *workspace's* fields, not a fixed list. The two
 * built-in columns that always appear are the ones the product owns: the lead's
 * identity value and its stage. Everything else is a field some admin created,
 * rendered through the same display helper the detail view uses.
 */

import { useMemo } from 'react'
import { type ColumnDef, flexRender, getCoreRowModel, useReactTable } from '@tanstack/react-table'

import type { Lead, LeadField, MemberDetail, Stage } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { BUILTIN_COLUMN_IDS, BUILTIN_COLUMNS } from '@/features/leads/columns'
import { toDisplayStringOr } from '@/lib/format'

export interface LeadTableProps {
  readonly leads: readonly Lead[]
  /**
   * Selected row ids. Selection lives with the page rather than the table
   * because it has to survive paging — an operator ticking their way through
   * three pages before hitting "edit" would otherwise lose the first two.
   */
  readonly selected: ReadonlySet<string>
  readonly onSelectedChange: (next: ReadonlySet<string>) => void
  readonly fields: readonly LeadField[]
  readonly stages: readonly Stage[]
  readonly members: readonly MemberDetail[]
  readonly columns: readonly string[]
  readonly sort: string
  readonly indexedKeys: ReadonlySet<string>
  readonly onSortChange: (sort: string) => void
  readonly onSelect: (leadId: string) => void
}

function StageCell({ lead, stages }: { readonly lead: Lead; readonly stages: readonly Stage[] }) {
  const stage = stages.find((candidate) => candidate.id === lead.stage_id)
  if (!stage) return <span className="text-muted-foreground">—</span>
  return (
    <Badge
      variant="outline"
      // The admin picked this colour for this stage, so it has to survive into
      // the chip. A faint wash of the same hue reads as a group marker; the
      // text and border stay at full strength to keep the label legible on any
      // of them.
      style={{
        borderColor: `color-mix(in oklch, ${stage.color} 38%, transparent)`,
        backgroundColor: `color-mix(in oklch, ${stage.color} 12%, transparent)`,
        color: stage.color,
      }}
    >
      {stage.label}
    </Badge>
  )
}

export function LeadTable({
  leads,
  fields,
  stages,
  members,
  columns,
  sort,
  indexedKeys,
  selected,
  onSelectedChange,
  onSortChange,
  onSelect,
}: LeadTableProps) {
  const pageIds = leads.map((lead) => lead.id)
  const allOnPageSelected = pageIds.length > 0 && pageIds.every((id) => selected.has(id))

  function toggle(id: string) {
    const next = new Set(selected)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    onSelectedChange(next)
  }

  function togglePage() {
    const next = new Set(selected)
    // Adds or removes only *this page* — the header box is about what is on
    // screen, and silently clearing selections from other pages would lose
    // work the operator did there.
    for (const id of pageIds) {
      if (allOnPageSelected) next.delete(id)
      else next.add(id)
    }
    onSelectedChange(next)
  }

  const fieldsByKey = useMemo(() => new Map(fields.map((field) => [field.key, field])), [fields])
  const membersById = useMemo(
    () => new Map(members.map((member) => [member.id, member])),
    [members],
  )

  const definitions = useMemo<ColumnDef<Lead>[]>(() => {
    return columns.map((id) => {
      const builtin = BUILTIN_COLUMNS.find((candidate) => candidate.id === id)
      if (builtin) {
        return {
          id,
          header: builtin.label,
          cell: ({ row }) => {
            const lead = row.original
            switch (id) {
              case 'identity_value':
                return <span className="font-medium">{lead.identity_value}</span>
              case 'stage_id':
                return <StageCell lead={lead} stages={stages} />
              case 'assignee_id':
                return (
                  <span>
                    {lead.assignee_id
                      ? (membersById.get(lead.assignee_id)?.user.full_name ?? 'Unknown')
                      : 'Unassigned'}
                  </span>
                )
              case 'last_action_at':
              case 'created_at': {
                const raw = id === 'created_at' ? lead.created_at : lead.last_action_at
                return <span>{raw ? new Date(raw).toLocaleDateString() : '—'}</span>
              }
              default:
                return <span>{toDisplayStringOr(lead[id as keyof Lead], '—')}</span>
            }
          },
        }
      }

      const field = fieldsByKey.get(id)
      return {
        id,
        header: field?.label ?? id,
        cell: ({ row }) => {
          // Absent means "not granted", not "empty" — the server omits a value
          // the caller may not view, so an em dash is the honest rendering.
          const value = row.original.values[id]
          const label = row.original.labels?.[id]
          return <span>{toDisplayStringOr(label ?? value, '—')}</span>
        },
      }
    })
  }, [columns, fieldsByKey, membersById, stages])

  const table = useReactTable({
    data: leads as Lead[],
    columns: definitions,
    getCoreRowModel: getCoreRowModel(),
    // Sorting is the server's job: it is restricted to indexed fields and the
    // client only ever holds one page, so a client-side sorter would reorder
    // twenty rows and call it sorted.
    manualSorting: true,
    manualPagination: true,
  })

  const sortKey = sort.replace(/^-/, '')
  const sortDescending = sort.startsWith('-')

  function sortableColumn(id: string): boolean {
    if (BUILTIN_COLUMN_IDS.has(id)) return true
    // A custom field can only be sorted once an admin declares it indexed —
    // the server would answer 400 otherwise, so the header is inert rather
    // than offering an action that fails.
    return indexedKeys.has(id)
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="bg-muted/45">
          {table.getHeaderGroups().map((headerGroup) => (
            <tr key={headerGroup.id} className="border-border/70 border-b text-left">
              <th className="w-10 px-4 py-2.5">
                <input
                  type="checkbox"
                  aria-label="Select every lead on this page"
                  checked={allOnPageSelected}
                  onChange={togglePage}
                />
              </th>
              {headerGroup.headers.map((header) => {
                const id = header.column.id
                const canSort = sortableColumn(id)
                return (
                  <th
                    key={header.id}
                    className="text-muted-foreground px-4 py-2.5 text-[0.6875rem] font-semibold tracking-wider uppercase"
                  >
                    {canSort ? (
                      <button
                        type="button"
                        className="hover:text-foreground flex items-center gap-1"
                        onClick={() =>
                          onSortChange(sortKey === id && !sortDescending ? `-${id}` : id)
                        }
                      >
                        {flexRender(header.column.columnDef.header, header.getContext())}
                        {sortKey === id && <span aria-hidden>{sortDescending ? '↓' : '↑'}</span>}
                      </button>
                    ) : (
                      <span title="Declare this field indexed in Settings to sort by it">
                        {flexRender(header.column.columnDef.header, header.getContext())}
                      </span>
                    )}
                  </th>
                )
              })}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.map((row) => (
            <tr
              key={row.id}
              data-testid="lead-row"
              className="border-border/45 hover:bg-primary/[0.045] cursor-pointer border-b transition-colors duration-100 last:border-0"
              onClick={() => onSelect(row.original.id)}
            >
              <td className="px-4 py-2.5" onClick={(event) => event.stopPropagation()}>
                <input
                  type="checkbox"
                  aria-label={`Select ${row.original.identity_value}`}
                  checked={selected.has(row.original.id)}
                  onChange={() => toggle(row.original.id)}
                />
              </td>
              {row.getVisibleCells().map((cell) => (
                <td key={cell.id} className="px-4 py-2.5">
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>

      {leads.length === 0 && (
        <p className="text-muted-foreground py-10 text-center text-sm">
          No leads match this filter.
        </p>
      )}
    </div>
  )
}

export interface PaginationProps {
  readonly total: number
  readonly limit: number
  readonly offset: number
  readonly onOffsetChange: (offset: number) => void
}

export function Pagination({ total, limit, offset, onOffsetChange }: PaginationProps) {
  const page = Math.floor(offset / limit) + 1
  const pages = Math.max(1, Math.ceil(total / limit))

  return (
    <div className="flex items-center justify-between gap-4 border-t px-3 py-2 text-sm">
      <span className="text-muted-foreground">
        {total.toLocaleString()} {total === 1 ? 'lead' : 'leads'} · page {page} of{' '}
        {pages.toLocaleString()}
      </span>
      <span className="flex gap-2">
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={offset === 0}
          onClick={() => onOffsetChange(Math.max(0, offset - limit))}
        >
          Previous
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={offset + limit >= total}
          onClick={() => onOffsetChange(offset + limit)}
        >
          Next
        </Button>
      </span>
    </div>
  )
}
