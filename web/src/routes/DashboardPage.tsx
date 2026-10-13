/**
 * The dashboard (M9).
 *
 * Composed from a saved layout, not hardcoded: a dashboard is a list of
 * `{widget, config}` and this renders whatever it is given. The widget
 * catalogue arrives from the server *with each widget's config schema*, so
 * adding a widget backend-side needs no release here — the same pattern
 * `/settings/field-types` established in M2.
 *
 * **Every chart drills through.** Clicking a bar navigates to the lead list
 * filtered to exactly that bucket, and the count on the bar and the total on
 * the list have to agree — a chart that disagrees with the list behind it is
 * worse than no chart, because it is believed.
 */

import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { ApiError } from '@/api/client'
import type { Bucket, DashboardWidget, LeadField } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Select } from '@/components/ui/select'
import { useAuth } from '@/features/auth/context'
import { useLeadFields } from '@/features/fields/api'
import { BarChart } from '@/features/reports/BarChart'
import { Leaderboard } from '@/features/reports/Leaderboard'
import { StatRow } from '@/features/reports/StatRow'
import {
  useActivity,
  useBreakdown,
  useDashboards,
  useFollowUps,
  useFunnel,
  useLeaderboard,
  useLeadsByStage,
  type Range,
} from '@/features/reports/api'

/** Preset rows, per the dataviz filter spec — not a pair of date inputs. */
const PRESETS: ReadonlyArray<{ key: string; label: string; days: number | null }> = [
  { key: '7', label: 'Last 7 days', days: 7 },
  { key: '30', label: 'Last 30 days', days: 30 },
  { key: '90', label: 'Last 90 days', days: 90 },
  { key: 'all', label: 'Last 12 months', days: 365 },
]

function message(cause: unknown): string {
  if (!(cause instanceof ApiError)) return 'That report could not be loaded.'
  if (cause.code === 'insufficient_permissions') {
    return 'Your permission template does not allow reports.'
  }
  return cause.message
}

function rangeFor(days: number | null): Range {
  if (days === null) return {}
  const to = new Date()
  const from = new Date(to.getTime() - days * 86_400_000)
  return { from: from.toISOString().slice(0, 10), to: to.toISOString().slice(0, 10) }
}

/** The default layout for a workspace that has not composed one yet. */
const STARTER_LAYOUT: readonly DashboardWidget[] = [
  { widget: 'follow_ups', x: 0, y: 0, w: 12, h: 2 },
  { widget: 'leads_by_stage', x: 0, y: 2, w: 6, h: 4 },
  { widget: 'funnel', x: 6, y: 2, w: 6, h: 4 },
  { widget: 'activity', x: 0, y: 6, w: 6, h: 4 },
  { widget: 'leaderboard', x: 6, y: 6, w: 6, h: 5 },
]

export function DashboardPage() {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string
  const navigate = useNavigate()

  const [preset, setPreset] = useState('30')
  const range = useMemo(
    () => rangeFor(PRESETS.find((entry) => entry.key === preset)?.days ?? 30),
    [preset],
  )

  const dashboards = useDashboards(workspaceId)
  const [chosen, setChosen] = useState<string | null>(null)

  const boards = dashboards.data ?? []
  const active =
    boards.find((board) => board.id === chosen) ??
    boards.find((board) => board.is_default) ??
    boards[0]
  const layout = active?.layout?.length ? active.layout : STARTER_LAYOUT

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{active?.name ?? 'Dashboard'}</h1>
          <p className="text-muted-foreground text-sm">
            {active
              ? active.template_id
                ? 'Shared with everyone on a permission template.'
                : active.owner_id
                  ? 'Yours alone.'
                  : 'Shared with the workspace.'
              : 'A starter layout — save your own to change it.'}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {boards.length > 0 ? (
            <Select
              aria-label="Dashboard"
              className="w-52"
              value={active?.id ?? ''}
              onChange={(event) => setChosen(event.target.value)}
            >
              {boards.map((board) => (
                <option key={board.id} value={board.id}>
                  {board.name}
                </option>
              ))}
            </Select>
          ) : null}
          {/* Filters in one row above the charts, per the interaction spec. */}
          <Select
            aria-label="Date range"
            title="Applies to activity, the leaderboard and breakdowns. The pipeline is always current."
            className="w-44"
            value={preset}
            onChange={(event) => setPreset(event.target.value)}
          >
            {PRESETS.map((entry) => (
              <option key={entry.key} value={entry.key}>
                {entry.label}
              </option>
            ))}
          </Select>
          <Button variant="outline" onClick={() => void navigate('/settings/dashboards')}>
            Edit
          </Button>
        </div>
      </header>

      {dashboards.isError ? (
        <p role="alert" className="text-destructive text-sm">
          {message(dashboards.error)}
        </p>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-2">
        {layout.map((item, index) => (
          <Widget
            key={`${item.widget}-${index}`}
            item={item}
            range={range}
            workspaceId={workspaceId}
            onDrill={(query) => void navigate(`/leads?${query}`)}
          />
        ))}
      </div>
    </div>
  )
}

function Widget({
  item,
  range,
  workspaceId,
  onDrill,
}: {
  readonly item: DashboardWidget
  readonly range: Range
  readonly workspaceId: string
  readonly onDrill: (query: string) => void
}) {
  const wide = item.w >= 12
  const shell = wide ? 'lg:col-span-2' : ''

  if (item.widget === 'follow_ups') {
    return (
      <div className={shell}>
        <FollowUpsWidget workspaceId={workspaceId} />
      </div>
    )
  }
  if (item.widget === 'leaderboard') {
    return (
      <Card className={shell}>
        <CardHeader>
          <CardTitle className="text-base">Leaderboard</CardTitle>
        </CardHeader>
        <CardContent>
          <LeaderboardWidget workspaceId={workspaceId} range={range} />
        </CardContent>
      </Card>
    )
  }
  return (
    <Card className={shell}>
      <CardContent className="pt-6">
        <ChartWidget item={item} workspaceId={workspaceId} range={range} onDrill={onDrill} />
      </CardContent>
    </Card>
  )
}

function FollowUpsWidget({ workspaceId }: { readonly workspaceId: string }) {
  const counts = useFollowUps(workspaceId)
  return (
    <StatRow
      counts={counts.data}
      loading={counts.isLoading}
      error={counts.isError ? message(counts.error) : null}
    />
  )
}

function LeaderboardWidget({
  workspaceId,
  range,
}: {
  readonly workspaceId: string
  readonly range: Range
}) {
  const board = useLeaderboard(workspaceId, range)
  return (
    <Leaderboard
      rows={board.data ?? []}
      loading={board.isLoading}
      error={board.isError ? message(board.error) : null}
    />
  )
}

function ChartWidget({
  item,
  workspaceId,
  range,
  onDrill,
}: {
  readonly item: DashboardWidget
  readonly workspaceId: string
  readonly range: Range
  readonly onDrill: (query: string) => void
}) {
  const fieldKey = item.config?.['field_key'] ?? null
  // No range: a pipeline is a current-state view, and the endpoints say so.
  const stages = useLeadsByStage(workspaceId)
  const funnel = useFunnel(workspaceId)
  const breakdown = useBreakdown(workspaceId, item.widget === 'breakdown' ? fieldKey : null, range)
  const activity = useActivity(workspaceId, range)
  const fields = useLeadFields(workspaceId)

  const label = (key: string) =>
    (fields.data ?? []).find((field: LeadField) => field.key === key)?.label ?? key

  if (item.widget === 'leads_by_stage') {
    return (
      <BarChart
        title="Leads by stage"
        subtitle="Where the pipeline stands right now."
        buckets={stages.data ?? []}
        loading={stages.isLoading}
        error={stages.isError ? message(stages.error) : null}
        onSelect={(bucket) => onDrill(`stage_id=${encodeURIComponent(bucket.key)}`)}
      />
    )
  }
  if (item.widget === 'funnel') {
    return (
      <BarChart
        title="Funnel"
        subtitle="Right now, in pipeline order — won and lost last."
        buckets={funnel.data ?? []}
        loading={funnel.isLoading}
        error={funnel.isError ? message(funnel.error) : null}
        onSelect={(bucket) => onDrill(`stage_id=${encodeURIComponent(bucket.key)}`)}
      />
    )
  }
  if (item.widget === 'breakdown') {
    return (
      <BarChart
        title={fieldKey ? `Leads by ${label(fieldKey)}` : 'Breakdown'}
        buckets={breakdown.data ?? []}
        loading={breakdown.isLoading}
        error={breakdown.isError ? message(breakdown.error) : null}
        empty="No leads have a value for this field yet."
      />
    )
  }
  if (item.widget === 'activity') {
    const buckets: Bucket[] = Object.entries(activity.data ?? {})
      .map(([kind, count]) => {
        const words = kind.toLowerCase().replaceAll('_', ' ')
        return {
          key: kind,
          // Sentence case: all-lowercase reads as a fragment rather than a
          // label, and SHOUTING_SNAKE_CASE is the wire format, not a word.
          label: words.charAt(0).toUpperCase() + words.slice(1),
          count,
        }
      })
      .sort((a, b) => b.count - a.count)
    return (
      <BarChart
        title="Activity"
        subtitle="Timeline entries in this range."
        buckets={buckets}
        loading={activity.isLoading}
        error={activity.isError ? message(activity.error) : null}
      />
    )
  }
  return (
    <p className="text-muted-foreground py-6 text-center text-sm">
      This dashboard uses a widget this version does not know about.
    </p>
  )
}
