import * as React from 'react'

import { cn } from '@/lib/utils'

/**
 * An on/off switch: a native button with `role="switch"`.
 *
 * Native for the same reason the checkbox is — no Radix dependency for one
 * control — and a button rather than a checkbox because a switch acts the
 * moment it is pressed, with no form to submit.
 */
function Switch({
  checked,
  onCheckedChange,
  className,
  ...props
}: Omit<React.ComponentProps<'button'>, 'onChange'> & {
  readonly checked: boolean
  readonly onCheckedChange: (checked: boolean) => void
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      data-slot="switch"
      data-state={checked ? 'checked' : 'unchecked'}
      onClick={() => onCheckedChange(!checked)}
      className={cn(
        'inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border border-transparent transition-colors outline-none',
        'focus-visible:ring-ring/40 focus-visible:ring-[3px]',
        'disabled:cursor-not-allowed disabled:opacity-50',
        checked ? 'bg-primary' : 'bg-muted border-border',
        className,
      )}
      {...props}
    >
      <span
        aria-hidden="true"
        className={cn(
          'pointer-events-none block size-4 rounded-full shadow-sm transition-transform',
          checked ? 'bg-primary-foreground translate-x-4' : 'bg-muted-foreground translate-x-0',
        )}
      />
    </button>
  )
}

export { Switch }
