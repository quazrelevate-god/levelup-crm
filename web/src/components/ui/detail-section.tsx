import * as React from 'react'
import type { LucideIcon } from 'lucide-react'

import { cn } from '@/lib/utils'

/**
 * One section of a detail panel — Fields, Log activity, Timeline and so on.
 *
 * The house rule for long detail panels: every section is its own tinted card
 * with a header strip, and neighbouring sections alternate between two blue
 * tones (`tone="a"` / `tone="b"`), the way a spreadsheet bands its rows. A thin
 * rule between sections disappears in a fast scroll; a change of colour does
 * not.
 */
export function DetailSection({
  title,
  icon: Icon,
  tone,
  action,
  className,
  children,
  ...props
}: Omit<React.ComponentProps<'section'>, 'title'> & {
  readonly title: React.ReactNode
  readonly icon?: LucideIcon
  readonly tone: 'a' | 'b'
  /** Optional control on the right of the header, e.g. a Save button. */
  readonly action?: React.ReactNode
}) {
  return (
    <section
      className={cn(
        'border-border/60 mb-4 overflow-hidden rounded-2xl border',
        tone === 'a' ? 'bg-section-a' : 'bg-section-b',
        className,
      )}
      {...props}
    >
      <div
        className={cn(
          'border-section-accent flex min-h-10 items-center justify-between gap-3 border-l-[3px] px-4 py-2',
          tone === 'a' ? 'bg-section-head-a' : 'bg-section-head-b',
        )}
      >
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          {Icon ? <Icon aria-hidden="true" className="text-section-accent size-4" /> : null}
          {title}
        </h3>
        {action}
      </div>
      <div className="space-y-3 p-4">{children}</div>
    </section>
  )
}
