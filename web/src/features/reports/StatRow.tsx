/**
 * Follow-up counts (M9).
 *
 * **Not a chart.** Three headline numbers are a KPI row of stat tiles; a
 * three-bar bar chart of them would be a chart drawn because charts are
 * available. The dataviz form table names this case explicitly.
 *
 * The numbers use proportional figures — `tabular-nums` is for columns that
 * must align vertically, which these do not.
 */

import { AlertTriangle, CalendarClock, PhoneOff, type LucideIcon } from 'lucide-react'

import type { FollowUpCounts } from '@/api/types'
import { Card, CardContent } from '@/components/ui/card'
import { cn } from '@/lib/utils'

export interface StatRowProps {
  readonly counts: FollowUpCounts | undefined
  readonly loading?: boolean
  readonly error?: string | null
}

export function StatRow({ counts, loading = false, error = null }: StatRowProps) {
  if (error) {
    return (
      <p role="alert" className="text-destructive text-sm">
        {error}
      </p>
    )
  }

  const tiles: readonly {
    key: string
    label: string
    value: number
    hint: string
    icon: LucideIcon
    urgent: boolean
  }[] = [
    {
      key: 'late',
      label: 'Late',
      value: counts?.late ?? 0,
      hint: 'Overdue follow-ups, right now',
      icon: AlertTriangle,
      // Status is carried by an icon *and* a label, never by colour alone —
      // and only when there is actually something wrong.
      urgent: (counts?.late ?? 0) > 0,
    },
    {
      key: 'upcoming',
      label: 'Upcoming',
      value: counts?.upcoming ?? 0,
      hint: 'Scheduled and not yet due',
      icon: CalendarClock,
      urgent: false,
    },
    {
      key: 'never',
      label: 'Never contacted',
      value: counts?.never_contacted ?? 0,
      hint: 'No timeline entry at all',
      icon: PhoneOff,
      urgent: false,
    },
  ]

  return (
    <div className="grid gap-3 sm:grid-cols-3">
      {tiles.map((tile) => {
        const Icon = tile.icon
        return (
          <Card key={tile.key} data-testid="stat-tile" className="gap-0 py-5">
            <CardContent className="flex items-start gap-4">
              <span
                aria-hidden="true"
                className={cn(
                  'flex size-10 shrink-0 items-center justify-center rounded-xl',
                  tile.urgent ? 'bg-destructive/12 text-destructive' : 'bg-primary/10 text-primary',
                )}
              >
                <Icon className="size-5" />
              </span>
              <span className="min-w-0 flex-1">
                <span className="text-muted-foreground block text-[0.6875rem] font-semibold tracking-wider uppercase">
                  {tile.label}
                </span>
                <span
                  className={cn(
                    'mt-0.5 block text-3xl leading-none font-semibold tracking-tight',
                    tile.urgent && 'text-destructive',
                  )}
                >
                  {loading ? '—' : tile.value.toLocaleString()}
                </span>
                <span className="text-muted-foreground mt-1.5 block text-xs">{tile.hint}</span>
              </span>
            </CardContent>
          </Card>
        )
      })}
    </div>
  )
}
