import * as React from 'react'

import { cn } from '@/lib/utils'

/**
 * One title treatment for every screen.
 *
 * Each page used to roll its own heading, so the type size, the spacing and
 * whether a subtitle existed at all varied screen to screen. Actions sit to the
 * right on wide viewports and wrap underneath on narrow ones.
 */
export function PageHeader({
  title,
  description,
  children,
  className,
}: {
  readonly title: React.ReactNode
  readonly description?: React.ReactNode
  readonly children?: React.ReactNode
  readonly className?: string
}) {
  return (
    <header className={cn('flex flex-wrap items-start justify-between gap-x-4 gap-y-3', className)}>
      <div className="min-w-0">
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {description ? <p className="text-muted-foreground mt-1 text-sm">{description}</p> : null}
      </div>
      {children ? <div className="flex flex-wrap items-center gap-2">{children}</div> : null}
    </header>
  )
}

/** The controls that act on what the page is showing: search, filters, views. */
export function Toolbar({ className, ...props }: React.ComponentProps<'div'>) {
  return (
    <div
      className={cn(
        'bg-card/70 border-border/60 shadow-neu-sm flex flex-wrap items-center gap-2 rounded-2xl border p-2.5',
        className,
      )}
      {...props}
    />
  )
}
