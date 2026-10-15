import * as React from 'react'

import { cn } from '@/lib/utils'

/**
 * Fields are the one place the inset half of neumorphism earns its keep: a
 * recessed surface is a genuine affordance for "put something in here".
 * The border stays, because the shadow alone does not survive a projector or
 * a high-contrast mode.
 */
const fieldClasses = [
  'border-input bg-surface/80 text-foreground placeholder:text-muted-foreground/70',
  'shadow-neu-inset w-full rounded-xl border px-3.5 text-sm',
  'transition-[color,box-shadow,border-color] duration-150 outline-none',
  'focus-visible:ring-ring/35 focus-visible:border-ring focus-visible:ring-[3px]',
  'disabled:cursor-not-allowed disabled:opacity-50',
  'aria-invalid:border-destructive aria-invalid:ring-destructive/20',
].join(' ')

function Input({ className, type, ...props }: React.ComponentProps<'input'>) {
  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        fieldClasses,
        'flex h-9.5 min-w-0 py-1',
        'file:text-foreground file:inline-flex file:h-7 file:border-0 file:bg-transparent file:text-sm file:font-medium',
        className,
      )}
      {...props}
    />
  )
}

export { Input, fieldClasses }
